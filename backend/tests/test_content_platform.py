"""Интеграционные тесты контентной платформы: curated-режим, коннекторы в конвейере и API источников. Нужна PostgreSQL."""
from datetime import timedelta

import httpx
import pytest

from app import db, jobs
from app.main import app
from app.pipeline import connectors, generate, ingest, process  # noqa: F401  (регистрируют обработчики)
from app.pipeline.generate import run_generation
from app.pipeline.process import get_project, process_project, select_clusters
from app.security import hash_password
from app.textutil import utcnow

from .helpers import MiniServer, make_project

pytestmark = pytest.mark.usefixtures("clean")
H = {"X-Requested-With": "pulse"}
TRANSCRIPT = (
    "В этом видео разбираем, как устроены индексы в базах данных. Сначала смотрим на B-деревья и то, почему они хороши для диапазонных запросов. "
    "Потом сравниваем хеш-индексы и объясняем, когда они быстрее. В конце показываем, как читать план запроса и находить медленные места. "
) * 6


async def _profile(pid: int, profile: str, **extra) -> dict:
    sets = ", ".join(["content_profile = %s", *(f"{k} = %s" for k in extra)])
    await db.execute(f"UPDATE projects SET {sets} WHERE id = %s", (profile, *extra.values(), pid))
    return await get_project(pid)


def _entry(title: str, url: str, text: str = "", hours_ago: float = 0, **kw) -> dict:
    return {"title": title, "url": url, "text": text, "published_at": utcnow() - timedelta(hours=hours_ago), **kw}


async def test_curated_item_uses_discovery_time_and_is_not_readded_after_retention():
    pid = await make_project()
    await _profile(pid, "curated")
    old = _entry("Как устроены индексы", "https://youtu.be/dQw4w9WgXcQ", TRANSCRIPT, hours_ago=24 * 400)
    stats = await ingest.insert_items(pid, None, [old], window_hours=48, mode="curated")
    assert stats.inserted == 1  # окно новостей на подборку не действует
    item = await db.fetchone("SELECT url, kind, mode, published_at, meta FROM items WHERE project_id = %s", (pid,))
    assert item["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ" and item["kind"] == "video" and item["mode"] == "curated"
    assert utcnow() - item["published_at"] < timedelta(minutes=1)
    assert item["meta"]["published_original"].startswith(str((utcnow() - timedelta(days=400)).year))

    # повторная выдача того же видео (другая форма ссылки) — не новый материал
    again = _entry("Как устроены индексы", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", TRANSCRIPT)
    assert (await ingest.insert_items(pid, None, [again], mode="curated")).inserted == 0
    # материал удалён по сроку хранения — поиск вернул его снова, но кандидатом он больше не станет
    await db.execute("DELETE FROM items WHERE project_id = %s", (pid,))
    assert (await ingest.insert_items(pid, None, [again], mode="curated")).inserted == 0


async def test_curated_materials_are_standalone_candidates_while_news_need_two_sources():
    pid = await make_project(min_items=2)
    await _profile(pid, "mixed")
    await ingest.insert_items(pid, None, [
        _entry("Видео: как устроены индексы в базах данных", "https://youtu.be/dQw4w9WgXcQ", TRANSCRIPT),
        _entry("Гайд по резервному копированию облачных серверов", "https://blog.example.com/backup-guide",
               "Пошаговый разбор резервного копирования: снапшоты, журналы и проверка восстановления. " * 10),
    ], mode="curated")
    await ingest.insert_items(pid, None, [
        _entry("Облачный провайдер объявил о сбое в дата-центре", "https://news.example.com/outage", "Сбой затронул часть клиентов."),
    ], window_hours=48, mode="news")
    await process_project(pid)

    clusters = await db.fetchall("SELECT id, mode, item_count, score_breakdown FROM clusters WHERE project_id = %s ORDER BY id", (pid,))
    assert sorted(c["mode"] for c in clusters) == ["curated", "curated", "news"]
    curated = [c for c in clusters if c["mode"] == "curated"]
    assert set(curated[0]["score_breakdown"]) == {"topic_fit", "engagement", "depth", "authority", "freshness"}
    assert curated[0]["score_breakdown"]["depth"]["value"] > 0.2
    selected = {c["id"] for c in await select_clusters(await get_project(pid), 10)}
    assert selected == {c["id"] for c in curated}  # одиночная новость ждёт второго источника, подборка — нет


async def test_curated_clusters_live_for_the_curated_window():
    pid = await make_project()
    project = await _profile(pid, "curated", window_hours=24, curated_window_hours=336)
    await ingest.insert_items(pid, None, [_entry("Разбор архитектуры распределённых баз данных", "https://blog.example.com/dist-db", TRANSCRIPT)], mode="curated")
    await process_project(pid)
    await db.execute("UPDATE clusters SET last_seen = now() - interval '3 days', first_seen = now() - interval '3 days' WHERE project_id = %s", (pid,))
    await process.refresh_clusters(project)
    assert (await db.fetchone("SELECT state FROM clusters WHERE project_id = %s", (pid,)))["state"] == "open"
    await db.execute("UPDATE clusters SET last_seen = now() - interval '15 days' WHERE project_id = %s", (pid,))
    await process.refresh_clusters(project)
    assert (await db.fetchone("SELECT state FROM clusters WHERE project_id = %s", (pid,)))["state"] == "closed"


async def test_curated_video_becomes_a_video_post():
    pid = await make_project()
    await _profile(pid, "curated")
    await ingest.insert_items(pid, None, [
        _entry("Как устроены индексы в базах данных", "https://youtu.be/dQw4w9WgXcQ", TRANSCRIPT, publisher_name="Tech Channel",
               meta={"views": 50_000}),
    ], mode="curated")
    await process_project(pid)
    [draft_id] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    draft = await db.fetchone("SELECT status, checks, params, citations, body FROM drafts WHERE id = %s", (draft_id,))
    assert draft["status"] == "pending_review"
    assert draft["checks"]["format"] == "video" and draft["params"]["formats"] == ["video"]
    assert draft["citations"][0]["kind"] == "video" and draft["citations"][0]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert "индексы" in draft["body"]
    reasons = draft["checks"]["automatic_review"]["reasons"]
    assert not any("Покрытие" in reason for reason in reasons)


SITE = """<html><head><title>Блог</title></head><body><main>
<a href="/blog/how-we-scaled-postgres">Как мы масштабировали Postgres до миллиона запросов</a>
<a href="/blog/indexes-explained-for-developers">Индексы в базах данных: объяснение для разработчиков</a>
<a href="/about">О компании</a>
</main></body></html>"""


async def test_site_source_without_rss_collects_article_links():
    pid = await make_project()
    await _profile(pid, "curated")

    def handler(method, path, headers, body):
        return 200, {"Content-Type": "text/html; charset=utf-8"}, SITE

    async with MiniServer(handler) as srv:
        src = await db.fetchone(
            "INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'site', 'Блог', %s) RETURNING id", (pid, srv.url + "/blog")
        )
        await ingest.ingest_source({"source_id": src["id"]})
        items = await db.fetchall("SELECT title, url, kind, mode, publisher_name FROM items WHERE project_id = %s ORDER BY id", (pid,))
    assert [i["url"] for i in items] == [srv.url + "/blog/how-we-scaled-postgres", srv.url + "/blog/indexes-explained-for-developers"]
    assert all(i["kind"] == "article" and i["mode"] == "curated" and i["publisher_name"] == "127.0.0.1" for i in items)
    row = await db.fetchone("SELECT last_error, last_new_count FROM sources WHERE id = %s", (src["id"],))
    assert row["last_error"] is None and row["last_new_count"] == 2


async def test_connector_failure_is_recorded_on_source(monkeypatch):
    pid = await make_project()

    async def broken(src):
        raise ValueError("Канал не найден или у него нет публичного веб-превью")

    monkeypatch.setitem(connectors.FETCHERS, "telegram", broken)
    src = await db.fetchone(
        "INSERT INTO sources(project_id, type, name, url, config) VALUES (%s, 'telegram', '@x', 'https://t.me/s/xxxx', '{\"channel\": \"xxxx\"}') RETURNING id",
        (pid,),
    )
    with pytest.raises(ValueError):
        await ingest.ingest_source({"source_id": src["id"]})
    assert "веб-превью" in (await db.fetchone("SELECT last_error FROM sources WHERE id = %s", (src["id"],)))["last_error"]


async def test_scheduler_polls_connector_sources():
    from app import scheduler

    pid = await make_project()
    await db.execute("INSERT INTO sources(project_id, type, name, url, config) VALUES (%s, 'reddit', 'r/python', '', '{\"subreddit\": \"python\"}')", (pid,))
    await db.execute("INSERT INTO sources(project_id, type, name, enabled) VALUES (%s, 'manual', 'Импорт', true)", (pid,))
    assert await scheduler.poll_sources() == 1


async def _editor() -> httpx.AsyncClient:
    await db.execute("INSERT INTO users(email, password_hash, role) VALUES ('ed@t.io', %s, 'editor')", (hash_password("password123"),))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H)
    assert (await client.post("/api/auth/login", json={"email": "ed@t.io", "password": "password123"})).status_code == 200
    return client


async def test_connector_api_previews_and_creates_site_source():
    pid = await make_project()
    await _profile(pid, "mixed")
    client = await _editor()

    def handler(method, path, headers, body):
        return 200, {"Content-Type": "text/html; charset=utf-8"}, SITE

    async with MiniServer(handler) as srv:
        body = {"project_id": pid, "type": "site", "value": srv.url + "/blog"}
        preview = await client.post("/api/sources/connector/preview", json=body)
        assert preview.status_code == 200, preview.text
        p = preview.json()
        assert p["count"] == 2 and p["new_count"] == 2 and p["mode"] == "curated" and p["feed_url"] is None
        assert p["sample"][0]["kind"] == "article"

        created = await client.post("/api/sources/connector", json={**body, "options": {"mode": "news"}, "poll_minutes": 720})
        assert created.status_code == 201, created.text
    src = await db.fetchone("SELECT type, name, config, poll_minutes FROM sources WHERE id = %s", (created.json()["id"],))
    assert src == {"type": "site", "name": "127.0.0.1", "config": {"mode": "news"}, "poll_minutes": 720}
    assert await db.fetchone("SELECT 1 FROM jobs WHERE kind = 'ingest_source' AND status = 'queued'")

    listed = (await client.get("/api/sources", params={"project_id": pid})).json()
    assert listed[0]["config"] == {"mode": "news"}
    renamed = await client.put(f"/api/sources/{created.json()['id']}", json={"project_id": pid, "name": "Блог компании", "url": "", "poll_minutes": 360})
    assert renamed.status_code == 200, renamed.text
    assert (await db.fetchone("SELECT name, url FROM sources WHERE id = %s", (created.json()["id"],)))["name"] == "Блог компании"

    bad = await client.post("/api/sources/connector/preview", json={"project_id": pid, "type": "telegram", "value": "not a channel!"})
    assert bad.status_code == 400 and "публичный канал" in bad.json()["detail"]
    await jobs.run_one()  # не оставляем очередь заполненной для следующего теста


async def test_project_api_exposes_content_profile():
    client = await _editor()
    r = await client.post("/api/projects", json={"name": "Видео про БД", "content_profile": "curated", "curated_window_hours": 720})
    assert r.status_code == 201, r.text
    assert r.json()["content_profile"] == "curated" and r.json()["curated_window_hours"] == 720
    assert (await client.post("/api/projects", json={"name": "x", "content_profile": "videos"})).status_code == 422
