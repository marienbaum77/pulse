"""Интеграционные тесты публикации по интересности: одиночные сюжеты при высокой оценке, немедленная
публикация с дневным лимитом и аспекты темы, которые составляет модель. Нужна PostgreSQL."""
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from psycopg.types.json import Jsonb

from app import db
from app.main import app
from app.pipeline import aspects, generate, interest, process  # noqa: F401  (регистрируют обработчики)
from app.pipeline.generate import run_generation
from app.pipeline.ingest import import_manual
from app.pipeline.process import RATING_POOL, get_project, process_project, queue_instant, select_clusters
from app.providers import LLMUnavailable
from app.security import hash_password

from .helpers import load_demo, make_project

pytestmark = pytest.mark.usefixtures("clean")
H = {"X-Requested-With": "pulse"}


class _Chat:
    supports_chat = True

    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    async def chat(self, system: str, user: str, **kw) -> str:
        self.calls += 1
        return self.answer


async def _pipeline() -> int:
    pid = await make_project()
    await import_manual(pid, load_demo())
    await process_project(pid)
    await db.execute("UPDATE projects SET topic_threshold = 0 WHERE id = %s", (pid,))
    return pid


def _ids(rows: list[dict]) -> set[int]:
    return {r["id"] for r in rows}


async def test_single_story_is_published_only_with_high_interest():
    pid = await _pipeline()
    single = await db.fetchone(
        "SELECT id FROM clusters WHERE project_id = %s AND state = 'open' AND mode = 'news' AND item_count = 1 ORDER BY id LIMIT 1", (pid,)
    )
    assert single, "в демо-данных должен быть сюжет из одного материала"
    sid = single["id"]
    project = await get_project(pid)
    assert sid not in _ids(await select_clusters(project, 100))
    assert sid not in _ids(await select_clusters(project, 100, for_rating=True))  # порог выключен — одиночки даже не оцениваются

    await db.execute("UPDATE projects SET single_min_interest = 7 WHERE id = %s", (pid,))
    project = await get_project(pid)
    assert sid in _ids(await select_clusters(project, 100, for_rating=True))  # без оценки — только в пул для оценки
    assert sid not in _ids(await select_clusters(project, 100))

    await db.execute("UPDATE clusters SET interest = 6.5 WHERE id = %s", (sid,))
    assert sid not in _ids(await select_clusters(project, 100))
    await db.execute("UPDATE clusters SET interest = 8 WHERE id = %s", (sid,))
    assert sid in _ids(await select_clusters(project, 100))


async def test_instant_publication_follows_threshold_and_daily_limit():
    pid = await _pipeline()  # режим публикации — ручное утверждение
    rows = await db.fetchall(
        "SELECT id FROM clusters WHERE project_id = %s AND state = 'open' AND mode = 'news' AND item_count >= 2 ORDER BY id LIMIT 3", (pid,)
    )
    assert len(rows) == 3
    hot, cold, warm = (r["id"] for r in rows)
    for cid, score in ((hot, 9), (cold, 5), (warm, 8.5)):
        await db.execute("UPDATE clusters SET interest = %s, interest_items = item_count WHERE id = %s", (score, cid))

    assert await queue_instant(await get_project(pid)) == []  # порог выключен

    await db.execute("UPDATE projects SET instant_interest = 8, instant_daily_limit = 1 WHERE id = %s", (pid,))
    assert await queue_instant(await get_project(pid)) == [hot]  # самый интересный, в пределах лимита
    job = await db.fetchone("SELECT payload FROM jobs WHERE kind = 'generate' AND status = 'queued' AND dedupe_key = %s", (f"instant:{pid}",))
    assert job["payload"] == {"project_id": pid, "kind": "post", "cluster_ids": [hot], "instant": True}

    with patch("app.pipeline.generate.automatic_review", return_value={"passed": True, "reasons": []}):
        await generate.generate_job(job["payload"])
    draft = await db.fetchone("SELECT id, status, instant, checks, scheduled_for FROM drafts WHERE project_id = %s", (pid,))
    assert draft["instant"] is True and draft["scheduled_for"] is None
    assert draft["status"] == "approved"  # порог интересности публикует и при ручном утверждении
    assert draft["checks"]["automatic_review"]["instant"] is True
    assert (await db.fetchone("SELECT count(*) AS n FROM publications WHERE draft_id = %s", (draft["id"],)))["n"] == 1

    # Лимит за 24 часа исчерпан: новых постов нет, устаревшая задача из очереди тоже ничего не создаёт.
    assert await queue_instant(await get_project(pid)) == []
    assert await run_generation({"project_id": pid, "kind": "post", "cluster_ids": [warm], "instant": True}) == []


async def test_instant_post_with_review_remarks_waits_for_editor():
    pid = await _pipeline()
    row = await db.fetchone("SELECT id FROM clusters WHERE project_id = %s AND state = 'open' AND mode = 'news' AND item_count >= 2 ORDER BY id LIMIT 1", (pid,))
    await db.execute("UPDATE clusters SET interest = 9, interest_items = item_count WHERE id = %s", (row["id"],))
    await db.execute("UPDATE projects SET instant_interest = 8 WHERE id = %s", (pid,))
    with patch("app.pipeline.generate.automatic_review", return_value={"passed": False, "reasons": ["тест: подставленная причина"]}):
        [did] = await run_generation({"project_id": pid, "kind": "post", "cluster_ids": [row["id"]], "instant": True})
    draft = await db.fetchone("SELECT status FROM drafts WHERE id = %s", (did,))
    assert draft["status"] == "pending_review"


async def test_slot_stays_empty_when_best_story_is_below_interest_floor():
    pid = await _pipeline()
    await db.execute("UPDATE clusters SET interest = 5, interest_items = item_count WHERE project_id = %s", (pid,))
    pool = await select_clusters(await get_project(pid), RATING_POOL)
    assert 2 <= len(pool) < RATING_POOL  # слот видит весь пул
    best = pool[-1]["id"]  # последний по близости к теме: отсечка идёт по оценке, а не по порядку пула
    chat = _Chat('{"score": 9, "reason": "тест"}')
    fill = AsyncMock(return_value=1)
    slot = {"project_id": pid, "kind": "post"}
    with patch("app.pipeline.generate.refresh_provider", AsyncMock(return_value=chat)), patch("app.pipeline.generate._create_and_fill", fill):
        await db.execute("UPDATE projects SET slot_min_interest = 6 WHERE id = %s", (pid,))
        assert await run_generation(slot) == []  # все кандидаты на 5 из 10 — слот пустой
        assert fill.await_count == 0 and chat.calls == 0  # кешированные оценки модель не переспрашивает
        assert not await db.fetchone("SELECT 1 FROM clusters WHERE project_id = %s AND state = 'drafted'", (pid,))

        # Сюжет без оценки (модель недоступна) в слот без редактора тоже не идёт.
        await db.execute("UPDATE clusters SET interest = NULL WHERE id = %s", (best,))
        with patch.object(chat, "chat", AsyncMock(side_effect=LLMUnavailable("нет сети"))):
            assert await run_generation(slot) == []

        await db.execute("UPDATE clusters SET interest = 7.5, interest_items = item_count WHERE id = %s", (best,))
        assert await run_generation(slot) == [1]
        assert fill.await_args.args[1] == [best]

        # Порог выключен — слот, как раньше, берёт лучшего из слабых.
        await db.execute("UPDATE projects SET slot_min_interest = NULL WHERE id = %s", (pid,))
        assert await run_generation(slot) == [1]
        assert fill.await_count == 2


def test_rating_prompt_penalizes_self_promotion_and_ads_outside_video():
    project = {"topic": "ИИ-инструменты", "interest_criteria": "Высоко: инструменты, которые можно попробовать уже сейчас."}
    article = {"title": "ai-bro", "text": "Я сделал набор скиллов для агентов.", "kind": "article", "source": "Хабр"}
    video = {**article, "kind": "video", "source": "YouTube"}
    row = {"title": "Разработчик представил ai-bro", "item_count": 1, "mode": "curated"}

    system, prompt = interest.rating_prompt(project, row, [article])
    # Свои критерии канала не отменяют правило о самопродвижении.
    assert interest.SELF_PROMO in system and "попробовать уже сейчас" in system
    assert "Рекламные вставки" not in system  # в статье реклама оценку снижает
    assert prompt.startswith("Материал: Разработчик представил ai-bro")

    system, _ = interest.rating_prompt(project, row, [video])
    assert "Рекламные вставки" in system and interest.SELF_PROMO in system

    system, prompt = interest.rating_prompt({"topic": "новости"}, {**row, "mode": "news", "item_count": 3}, [article])
    assert interest.SELF_PROMO in system and "самопродвижение" in system  # и в стандартных критериях
    assert "Материалов: 3" in prompt


def test_parse_aspects_drops_duplicates_and_short_phrases():
    raw = 'Вот аспекты: ["базы данных и СУБД", "облачная инфраструктура.", "Базы данных и  СУБД", "ии", 42]'
    assert aspects.parse_aspects(raw) == ["базы данных и СУБД", "облачная инфраструктура"]
    assert aspects.parse_aspects("не знаю") == []


async def test_model_writes_aspects_once_per_topic():
    pid = await make_project()
    chat = _Chat('["базы данных и СУБД", "облачная инфраструктура", "машинное обучение"]')
    assert await aspects.generate_aspects(pid, chat) == ["базы данных и СУБД", "облачная инфраструктура", "машинное обучение"]
    project = await get_project(pid)
    assert project["topic_aspects"] == ["базы данных и СУБД", "облачная инфраструктура", "машинное обучение"]
    assert project["topic_aspects_auto"] is True
    assert await db.fetchone("SELECT 1 FROM jobs WHERE kind = 'process_project' AND status = 'queued'")

    # Аспекты есть — модель больше не спрашивают.
    assert await aspects.generate_aspects(pid, chat) == [] and chat.calls == 1

    # Неразобранный ответ тоже запоминается: модель не спрашивают на каждом проходе обработки.
    other = await make_project(name="Другой")
    garbage = _Chat("не знаю")
    assert await aspects.generate_aspects(other, garbage) == []
    assert not await aspects.needs_aspects(await get_project(other))
    await aspects.generate_aspects(other, garbage)
    assert garbage.calls == 1


async def test_manual_aspects_written_while_model_thinks_win():
    pid = await make_project()

    class Slow(_Chat):
        async def chat(self, system, user, **kw):
            await db.execute("UPDATE projects SET topic_aspects = %s WHERE id = %s", (Jsonb(["свой аспект"]), pid))
            return '["аспект модели", "ещё аспект"]'

    assert await aspects.generate_aspects(pid, Slow("")) == []
    project = await get_project(pid)
    assert project["topic_aspects"] == ["свой аспект"] and project["topic_aspects_auto"] is False


async def test_api_regenerates_model_aspects_only_until_edited():
    await db.execute("INSERT INTO users(email, password_hash, role) VALUES ('ed@t.io', %s, 'editor')", (hash_password("password123"),))
    ed = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H)
    assert (await ed.post("/api/auth/login", json={"email": "ed@t.io", "password": "password123"})).status_code == 200

    r = await ed.post("/api/projects", json={"name": "Канал", "topic": "Базы данных"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    queued = "SELECT count(*) AS n FROM jobs WHERE kind = 'generate_aspects' AND status = 'queued'"
    assert (await db.fetchone(queued))["n"] == 1
    await db.execute("DELETE FROM jobs")
    await db.execute("UPDATE projects SET topic_aspects = %s, topic_aspects_auto = true WHERE id = %s", (Jsonb(["СУБД", "SQL-запросы"]), pid))

    def body(**over):
        return {"name": "Канал", "topic": "Базы данных", "topic_aspects": ["СУБД", "SQL-запросы"], **over}

    # Тема та же — аспекты модели остаются.
    r = await ed.put(f"/api/projects/{pid}", json=body())
    assert r.json()["topic_aspects"] == ["СУБД", "SQL-запросы"] and r.json()["topic_aspects_auto"] is True
    assert (await db.fetchone(queued))["n"] == 0

    # Тема сменилась — аспекты прежней темы убраны, модель составит новые.
    r = await ed.put(f"/api/projects/{pid}", json=body(topic="Облачная инфраструктура"))
    assert r.json()["topic_aspects"] == [] and r.json()["topic_aspects_auto"] is False
    assert (await db.fetchone(queued))["n"] == 1

    # Правка вручную отключает автообновление: при следующей смене темы аспекты остаются.
    await db.execute("UPDATE projects SET topic_aspects = %s, topic_aspects_auto = true WHERE id = %s", (Jsonb(["СУБД", "SQL-запросы"]), pid))
    r = await ed.put(f"/api/projects/{pid}", json=body(topic="Облачная инфраструктура", topic_aspects=["СУБД", "индексы"]))
    assert r.json()["topic_aspects_auto"] is False
    r = await ed.put(f"/api/projects/{pid}", json=body(topic="Базы данных", topic_aspects=["СУБД", "индексы"]))
    assert r.json()["topic_aspects"] == ["СУБД", "индексы"]
    await ed.aclose()
