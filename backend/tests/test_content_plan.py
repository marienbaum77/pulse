"""Интеграционные тесты контент-плана: слоты с фильтром вида материала, заблаговременная подготовка,
публикация ко времени слота и кулдаун источников. Нужна PostgreSQL."""
from datetime import timedelta

import httpx
import pytest

from app import db, jobs, publisher, scheduler
from app.main import app
from app.pipeline import drafts_service, generate, ingest, process  # noqa: F401  (регистрируют обработчики)
from app.pipeline.process import get_project, process_project, select_clusters
from app.security import hash_password
from app.textutil import utcnow

from .helpers import make_project

pytestmark = pytest.mark.usefixtures("clean")
async def _curated_project(**extra) -> int:
    pid = await make_project()
    sets = ", ".join(["content_profile = 'curated'", *(f"{k} = %s" for k in extra)])
    await db.execute(f"UPDATE projects SET {sets} WHERE id = %s", (*extra.values(), pid))
    return pid


async def _source(pid: int, name: str) -> int:
    row = await db.fetchone("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'youtube', %s, '') RETURNING id", (pid, name))
    return row["id"]


async def _add(pid: int, source_id: int, title: str, url: str, kind: str) -> None:
    # У каждого материала свой текст: одинаковый текст MinHash справедливо счёл бы перепечаткой.
    entry = {"title": title, "url": url, "text": " ".join([f"{title}: подробный разбор, пример номер {n}." for n in range(20)]),
             "published_at": utcnow(), "kind": kind}
    await ingest.insert_items(pid, source_id, [entry], mode="curated")
    # Текст уже полный: дообогащение (субтитры, статья) в тесте не нужно.
    await db.execute("UPDATE items SET article_content_status = 'done' WHERE project_id = %s", (pid,))


async def _schedule(pid: int, **kw) -> dict:
    cols = {"project_id": pid, "name": "Слот", "cron": "0 9 * * *", "tz": "UTC", "kind": "post", "top_n": 1, **kw}
    return await db.fetchone(
        f"INSERT INTO schedules({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *", list(cols.values())
    )


async def _run_queued_generation() -> dict:
    job = await db.fetchone("SELECT payload FROM jobs WHERE kind = 'generate' AND status = 'queued' ORDER BY id DESC LIMIT 1")
    await generate.generate_job(job["payload"])
    return job["payload"]


async def test_file_import_follows_project_content_profile():
    pid = await _curated_project()
    await ingest.import_manual(pid, [{"title": "Статья о шардировании", "text": "Разбор шардирования. " * 20, "source": "Импорт"}])
    assert (await db.fetchone("SELECT mode FROM items WHERE project_id = %s", (pid,)))["mode"] == "curated"


async def test_slot_filters_by_content_kind():
    pid = await _curated_project()
    src = await _source(pid, "Канал")
    await _add(pid, src, "Видео о репликации баз данных", "https://youtu.be/dQw4w9WgXcQ", "video")
    await _add(pid, src, "Статья о резервном копировании облаков", "https://blog.example.com/backup", "article")
    await process_project(pid)
    project = await get_project(pid)
    assert {c["kind"] for c in await select_clusters(project, 10)} == {"video", "article"}
    assert [c["kind"] for c in await select_clusters(project, 10, ["video"])] == ["video"]
    assert [c["kind"] for c in await select_clusters(project, 10, ["article", "post"])] == ["article"]
    assert await select_clusters(project, 10, ["news"]) == []


async def test_prepared_slot_draft_is_published_at_slot_time_not_on_approval():
    pid = await _curated_project()
    await _add(pid, await _source(pid, "Канал"), "Видео о репликации баз данных", "https://youtu.be/dQw4w9WgXcQ", "video")
    await process_project(pid)
    # каждую минуту, готовить за 30 минут: ближайший слот — примерно через 29 минут
    sched = await _schedule(pid, cron="* * * * *", lead_minutes=30, content_kinds=["video"])
    assert await scheduler.fire_schedules(utcnow()) == 1
    payload = await _run_queued_generation()
    assert payload["content_kinds"] == ["video"]

    draft = await db.fetchone("SELECT id, status, schedule_id, scheduled_for FROM drafts WHERE project_id = %s", (pid,))
    assert draft["status"] == "pending_review" and draft["schedule_id"] == sched["id"]
    assert timedelta(minutes=25) < draft["scheduled_for"] - utcnow() <= timedelta(minutes=30)
    fire = await db.fetchone("SELECT outcome, draft_ids, slot_at FROM schedule_fires WHERE schedule_id = %s", (sched["id"],))
    assert fire == {"outcome": "drafted", "draft_ids": [draft["id"]], "slot_at": draft["scheduled_for"]}

    assert await drafts_service.approve(draft["id"], None)
    pub = await db.fetchone("SELECT due_at FROM publications WHERE draft_id = %s", (draft["id"],))
    assert pub["due_at"] == draft["scheduled_for"]
    assert await publisher.process_one() is False  # время слота ещё не пришло


async def test_draft_approved_after_its_slot_is_published_immediately():
    pid = await _curated_project()
    await _add(pid, await _source(pid, "Канал"), "Видео о репликации баз данных", "https://youtu.be/dQw4w9WgXcQ", "video")
    await process_project(pid)
    [draft_id] = await generate.run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    await db.execute("UPDATE drafts SET scheduled_for = now() - interval '1 hour' WHERE id = %s", (draft_id,))
    assert await drafts_service.approve(draft_id, None)
    assert await publisher.process_one() is True


async def test_empty_slot_is_recorded_for_the_plan_history():
    pid = await _curated_project()
    await _add(pid, await _source(pid, "Канал"), "Статья о резервном копировании облаков", "https://blog.example.com/backup", "article")
    await process_project(pid)
    sched = await _schedule(pid, cron="* * * * *", content_kinds=["video"])
    assert await scheduler.fire_schedules(utcnow()) == 1
    await _run_queued_generation()
    assert (await db.fetchone("SELECT outcome FROM schedule_fires WHERE schedule_id = %s", (sched["id"],)))["outcome"] == "empty"
    assert await db.fetchone("SELECT 1 FROM drafts WHERE project_id = %s", (pid,)) is None


async def test_source_cooldown_spreads_posts_across_sources():
    pid = await _curated_project(source_cooldown_hours=24)
    busy, other = await _source(pid, "Плодовитый канал"), await _source(pid, "Другой канал")
    await _add(pid, busy, "Видео о репликации баз данных", "https://youtu.be/dQw4w9WgXcQ", "video")
    await _add(pid, busy, "Видео о шардировании таблиц", "https://youtu.be/aircAruvnKk", "video")
    await _add(pid, other, "Видео о резервном копировании облаков", "https://youtu.be/fNk_zzaMoSs", "video")
    await process_project(pid)

    # за один запуск — не больше одного поста из источника
    ids = await generate.run_generation({"project_id": pid, "kind": "post", "top_n": 3})
    assert len(ids) == 2
    used = await db.fetchall(
        "SELECT DISTINCT i.source_id FROM drafts d JOIN items i ON i.cluster_id = ANY(d.cluster_ids) WHERE d.project_id = %s", (pid,)
    )
    assert {r["source_id"] for r in used} == {busy, other}
    # и второе видео плодовитого канала ждёт окончания кулдауна
    assert await select_clusters(await get_project(pid), 10) == []
    await db.execute("UPDATE drafts SET created_at = now() - interval '25 hours' WHERE project_id = %s", (pid,))
    assert len(await select_clusters(await get_project(pid), 10)) == 1


async def test_content_plan_api_lists_slots_with_drafts_and_candidates():
    pid = await _curated_project()
    await _add(pid, await _source(pid, "Канал"), "Видео о репликации баз данных", "https://youtu.be/dQw4w9WgXcQ", "video")
    await process_project(pid)
    videos = await _schedule(pid, name="Видео дня", cron="0 10 * * *", content_kinds=["video"], lead_minutes=120)
    await _schedule(pid, name="Новости", cron="0 18 * * *", content_kinds=["news"])
    await _schedule(pid, name="Выключено", cron="0 12 * * *", enabled=False)

    slot_at = utcnow().replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
    [draft_id] = await generate.run_generation(
        {"project_id": pid, "kind": "post", "top_n": 1, "schedule_id": videos["id"], "slot_at": slot_at.isoformat()}
    )

    await db.execute("INSERT INTO users(email, password_hash, role) VALUES ('v@t.io', %s, 'viewer')", (hash_password("password123"),))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Requested-With": "pulse"})
    assert (await client.post("/api/auth/login", json={"email": "v@t.io", "password": "password123"})).status_code == 200
    r = await client.get("/api/content-plan", params={"project_id": pid, "days": 3})
    assert r.status_code == 200, r.text
    plan = r.json()
    names = {s["schedule_name"] for s in plan["slots"]}
    assert names == {"Видео дня", "Новости"}
    assert sum(s["schedule_name"] == "Видео дня" for s in plan["slots"]) in (3, 4)  # сутки истории + 3 дня вперёд
    tomorrow = next(s for s in plan["slots"] if s["schedule_name"] == "Видео дня" and s["drafts"])
    assert tomorrow["drafts"][0]["id"] == draft_id and tomorrow["drafts"][0]["status"] == "pending_review"
    assert tomorrow["prepare_at"] < tomorrow["at"]
    news_slot = next(s for s in plan["slots"] if s["schedule_name"] == "Новости")
    # видео уже в черновике — свободных кандидатов на слот видео нет, новостей нет вовсе
    assert news_slot["candidates"] == 0 and tomorrow["candidates"] == 0
    assert plan["backlog"] == []

    # частое расписание: лимит слотов не должен уходить на время до создания расписания
    await _schedule(pid, name="Часто", cron="*/2 * * * *")
    frequent = [s for s in (await client.get("/api/content-plan", params={"project_id": pid, "days": 1})).json()["slots"] if s["schedule_name"] == "Часто"]
    assert len(frequent) >= 100
    assert all(s["outcome"] is None and not s["drafts"] for s in frequent)
    assert min(s["at"] for s in frequent) > (utcnow() - timedelta(minutes=3)).isoformat()
    await jobs.run_one()
