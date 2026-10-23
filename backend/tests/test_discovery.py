"""Интеграционные тесты автоподбора источников: поиск подменён, БД настоящая."""
import httpx
import pytest

from app import db, jobs
from app.main import app
from app.pipeline import discovery
from app.pipeline.connectors import searx
from app.security import hash_password

from .helpers import make_project

pytestmark = pytest.mark.usefixtures("clean")
H = {"X-Requested-With": "pulse"}
WEB = [
    {"url": "https://habr.com/ru/articles/1/", "title": "Индексы в PostgreSQL"},
    {"url": "https://shop.example.com/catalog/1", "title": "Купить сервер"},
    {"url": "https://t.me/dbweekly/120", "title": "Пост про индексы"},
    {"url": "https://www.reddit.com/r/PostgreSQL/comments/abc/x/", "title": "Обсуждение индексов"},
]
VIDEOS = [{"url": "https://youtu.be/dQw4w9WgXcQ", "title": "Видео про индексы", "meta": {"author": "DB Channel"}}]


@pytest.fixture
def fake_web(monkeypatch):
    calls = []

    async def search(query, *, language, categories="general", engines=None, time_range=None):
        calls.append((query, categories))
        return VIDEOS + [{"url": "https://rutube.ru/video/1/", "title": "Зеркало"}] if categories == "videos" else WEB

    async def discover(url):
        # у Хабра есть лента, у магазина — ни ленты, ни статей
        return {"feed_url": "https://habr.com/ru/rss/" if "habr" in url else None, "links": []}

    async def video_channel(video_url):
        return "https://www.youtube.com/@dbchannel", "DB Channel"

    monkeypatch.setattr(searx, "search", search)
    monkeypatch.setattr(discovery.site, "discover", discover)
    monkeypatch.setattr(discovery, "video_channel", video_channel)
    return calls


async def _project() -> int:
    pid = await make_project()
    await db.execute("UPDATE projects SET content_profile = 'curated', topic_aspects = %s WHERE id = %s",
                     ('["индексы баз данных", "репликация"]', pid))
    return pid


async def test_discovery_finds_candidates_from_search_without_model(fake_web):
    pid = await _project()
    assert await discovery.run_discovery(pid) == 4
    # без чат-модели запросы — аспекты темы; на каждый — поиск статей и видео
    assert sorted(fake_web) == sorted([(q, c) for q in ("индексы баз данных", "репликация") for c in ("general", "videos")])
    rows = {(r["type"], r["value"]): r for r in await db.fetchall("SELECT * FROM source_suggestions WHERE project_id = %s", (pid,))}
    assert set(rows) == {("site", "https://habr.com/"), ("youtube", "https://www.youtube.com/@dbchannel"), ("telegram", "@dbweekly"), ("reddit", "r/PostgreSQL")}
    assert rows[("site", "https://habr.com/")]["reason"] == "есть RSS-лента"
    assert rows[("youtube", "https://www.youtube.com/@dbchannel")]["name"] == "DB Channel"
    assert all(r["hits"] == 2 and r["samples"] for r in rows.values())
    status = (await db.fetchone("SELECT value FROM kv WHERE key = %s", (f"discovery:{pid}",)))["value"]
    assert status["status"] == "done" and status["found"] == 4


async def test_discovery_skips_connected_and_dismissed_sources(fake_web):
    pid = await _project()
    await db.execute("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Хабр', 'https://habr.com/ru/rss/hubs/postgresql/')", (pid,))
    await db.execute("INSERT INTO sources(project_id, type, name, url, config) VALUES (%s, 'telegram', '@dbweekly', '', '{\"channel\": \"dbweekly\"}')", (pid,))
    await db.execute("INSERT INTO sources(project_id, type, name, url, config) VALUES (%s, 'youtube', 'YouTube: DB Channel', '', '{}')", (pid,))
    await db.execute("INSERT INTO source_suggestions(project_id, type, value, name, status) VALUES (%s, 'reddit', 'r/PostgreSQL', 'r/PostgreSQL', 'dismissed')", (pid,))
    assert await discovery.run_discovery(pid) == 0
    assert await db.fetchone("SELECT 1 FROM source_suggestions WHERE project_id = %s AND status = 'new'", (pid,)) is None


async def test_discovery_without_search_service_reports_failure(monkeypatch):
    pid = await _project()

    async def unavailable(*a, **kw):
        raise searx.SearchUnavailable("Веб-поиск не настроен: задайте SEARXNG_URL")

    monkeypatch.setattr(searx, "search", unavailable)
    await discovery.discover_sources_job({"project_id": pid})  # задача не падает и не повторяется
    status = (await db.fetchone("SELECT value FROM kv WHERE key = %s", (f"discovery:{pid}",)))["value"]
    assert status["status"] == "failed" and "SEARXNG_URL" in status["error"]


async def test_discovery_api_accept_and_dismiss(fake_web):
    pid = await _project()
    await db.execute("INSERT INTO users(email, password_hash, role) VALUES ('ed@t.io', %s, 'editor')", (hash_password("password123"),))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H)
    assert (await client.post("/api/auth/login", json={"email": "ed@t.io", "password": "password123"})).status_code == 200

    assert (await client.post("/api/discovery", json={"project_id": pid})).json() == {"queued": True}
    state = (await client.get("/api/discovery", params={"project_id": pid})).json()
    assert state["run"]["status"] == "queued" and state["suggestions"] == []

    assert await jobs.run_one()  # воркер выполняет поставленный подбор
    state = (await client.get("/api/discovery", params={"project_id": pid})).json()
    by_type = {s["type"]: s for s in state["suggestions"]}
    assert state["run"]["status"] == "done" and set(by_type) == {"site", "youtube", "telegram", "reddit"}

    # Telegram и Reddit добавляются без сети: prepare только нормализует ввод
    r = await client.post(f"/api/discovery/suggestions/{by_type['telegram']['id']}/accept", json={"authority": 0.7})
    assert r.status_code == 201, r.text
    src = await db.fetchone("SELECT type, name, config, authority, poll_minutes FROM sources WHERE id = %s", (r.json()["id"],))
    assert src == {"type": "telegram", "name": "@dbweekly", "config": {"channel": "dbweekly"}, "authority": 0.7, "poll_minutes": 60}
    assert (await client.post(f"/api/discovery/suggestions/{by_type['telegram']['id']}/accept", json={})).status_code == 409

    assert (await client.post(f"/api/discovery/suggestions/{by_type['reddit']['id']}/dismiss")).json() == {"ok": True}
    left = {s["type"] for s in (await client.get("/api/discovery", params={"project_id": pid})).json()["suggestions"]}
    assert left == {"site", "youtube"}

    # повторный подбор не возвращает ни подключённый, ни скрытый источник
    await discovery.run_discovery(pid)
    left = {s["type"] for s in (await client.get("/api/discovery", params={"project_id": pid})).json()["suggestions"]}
    assert left == {"site", "youtube"}

    # уже подключённый источник через предложение второй раз не добавить: предложение просто закрывается
    await db.execute("INSERT INTO source_suggestions(project_id, type, value, name) VALUES (%s, 'telegram', 't.me/dbweekly', 'dup')", (pid,))
    dup = await db.fetchone("SELECT id FROM source_suggestions WHERE value = 't.me/dbweekly'")
    r = await client.post(f"/api/discovery/suggestions/{dup['id']}/accept", json={})
    assert r.status_code == 409 and "уже подключён" in r.json()["detail"]
    assert (await db.fetchone("SELECT status FROM source_suggestions WHERE id = %s", (dup["id"],)))["status"] == "dismissed"
