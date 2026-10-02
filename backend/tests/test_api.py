import httpx
import pytest

from app import db, jobs
from app.main import app
from app.security import hash_password

from .helpers import load_demo, make_project

pytestmark = pytest.mark.usefixtures("clean")
H = {"X-Requested-With": "pulse"}


async def _user(email, role, password="password123"):
    await db.execute("INSERT INTO users(email, password_hash, role) VALUES (%s,%s,%s)", (email, hash_password(password), role))


async def _client(email=None, password="password123"):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H)
    if email:
        r = await c.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
    return c


async def _drain():
    while await jobs.run_one():
        pass


async def test_auth_csrf_and_rbac():
    await _user("admin@t.io", "admin")
    await _user("ed@t.io", "editor")
    await _user("view@t.io", "viewer")

    anon = await _client()
    assert (await anon.get("/api/projects")).status_code == 401
    bad = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # без CSRF-заголовка
    assert (await bad.post("/api/auth/login", json={"email": "admin@t.io", "password": "password123"})).status_code == 403
    assert (await anon.post("/api/auth/login", json={"email": "admin@t.io", "password": "wrong"})).status_code == 401

    viewer, editor, admin = await _client("view@t.io"), await _client("ed@t.io"), await _client("admin@t.io")
    body = {"name": "P", "topic": "t"}
    assert (await viewer.post("/api/projects", json=body)).status_code == 403
    assert (await editor.post("/api/projects", json=body)).status_code == 201
    assert (await viewer.get("/api/projects")).status_code == 200
    assert (await editor.get("/api/users")).status_code == 403
    assert (await admin.get("/api/users")).status_code == 200
    assert (await admin.post("/api/auth/logout")).status_code == 200

    me = await admin.get("/api/auth/me")
    assert me.status_code == 401  # cookie удалена клиентом после logout


async def test_validation_errors_and_last_admin_protection():
    await _user("admin@t.io", "admin")
    admin = await _client("admin@t.io")
    r = await admin.post("/api/projects", json={"name": "P", "weights": {"nonsense": 0.5}})
    assert r.status_code == 422
    r = await admin.post("/api/projects", json={"name": "P", "sim_threshold": 5})
    assert r.status_code == 422
    r = await admin.post("/api/schedules", json={"project_id": 1, "name": "x", "cron": "not cron"})
    assert r.status_code == 422
    me = (await admin.get("/api/auth/me")).json()
    assert (await admin.delete(f"/api/users/{me['id']}")).status_code == 409
    assert (await admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"})).status_code == 409


async def test_editorial_flow_through_api():
    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    pid = (await ed.post("/api/projects", json={"name": "Тех", "topic": "технологии", "sim_threshold": 0.35})).json()["id"]

    # нет каналов — утверждать некуда
    imp = await ed.post("/api/items/import", json={"project_id": pid, "items": load_demo()})
    assert imp.json() == {"received": 45, "inserted": 45}
    await _drain()
    clusters = (await ed.get("/api/clusters", params={"project_id": pid})).json()
    assert sum(1 for c in clusters if c["item_count"] >= 2) == 6 and clusters[0]["arrivals"]

    await ed.post("/api/drafts/generate", json={"project_id": pid, "kind": "post", "top_n": 1})
    await _drain()
    drafts = (await ed.get("/api/drafts", params={"project_id": pid})).json()
    assert len(drafts) == 1 and drafts[0]["status"] == "pending_review"
    did = drafts[0]["id"]
    assert (await ed.post(f"/api/drafts/{did}/approve")).status_code == 409  # нет включённых каналов

    ch = await ed.post("/api/channels", json={"project_id": pid, "type": "console", "name": "Консоль"})
    assert ch.status_code == 201
    edit = await ed.patch(f"/api/drafts/{did}", json={"title": "Свой заголовок"})
    assert edit.status_code == 200 and edit.json()["checks"]["edited"] is True

    first = await ed.post(f"/api/drafts/{did}/approve")
    second = await ed.post(f"/api/drafts/{did}/approve")  # двойной клик
    assert first.json()["already"] is False and second.json()["already"] is True
    assert (await ed.patch(f"/api/drafts/{did}", json={"title": "поздно"})).status_code == 409
    assert (await db.fetchone("SELECT count(*) AS n FROM publications"))["n"] == 1

    from app import publisher

    await publisher.process_one()
    pubs = (await ed.get("/api/publications", params={"project_id": pid})).json()
    assert [p["status"] for p in pubs] == ["sent"]

    dash = (await ed.get("/api/dashboard", params={"project_id": pid, "days": 3})).json()
    assert dash["metrics"]["items"] == 45 and dash["metrics"]["published"] == 1
    assert len(dash["daily"]) == 3 and dash["top_clusters"]
    assert all(c["item_count"] >= 2 for c in dash["top_clusters"])  # одиночные заметки в «главных сюжетах» не показываются
    assert dash["metrics"]["open_clusters"] == 5  # 6 событий минус то, что уже в опубликованном посте


async def test_resolve_unknown_publication_via_api():
    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    pid = await make_project()
    ch = await db.fetchone("SELECT id FROM channels")
    d = await db.fetchone("INSERT INTO drafts(project_id, title, body, status) VALUES (%s,'T','B','approved') RETURNING id", (pid,))
    p = await db.fetchone("INSERT INTO publications(draft_id, channel_id, status) VALUES (%s,%s,'unknown') RETURNING id", (d["id"], ch["id"]))
    assert (await ed.post(f"/api/publications/{p['id']}/resolve", json={"action": "retry"})).status_code == 200
    assert (await db.fetchone("SELECT status FROM publications"))["status"] == "pending"
    await db.execute("UPDATE publications SET status = 'sent'")
    assert (await ed.post(f"/api/publications/{p['id']}/resolve", json={"action": "cancel"})).status_code == 409


async def test_ssrf_guard_blocks_private_hosts(monkeypatch):
    from app.config import get_settings

    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    pid = await make_project()
    monkeypatch.setattr(get_settings(), "allow_private_urls", False)
    r = await ed.post("/api/sources", json={"project_id": pid, "name": "x", "url": "http://127.0.0.1:8000/feed"})
    assert r.status_code == 400 and "внутренней сети" in r.text


async def test_article_search_creates_localized_google_news_rss_source(monkeypatch):
    from app.api import sources

    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    project = await ed.post("/api/projects", json={"name": "Research", "topic": "Робототехника", "language": "ru"})
    pid = project.json()["id"]

    async def allow_public_url(_url):
        return None

    monkeypatch.setattr(sources, "check_url", allow_public_url)
    response = await ed.post(
        "/api/sources/search",
        json={"project_id": pid, "query": '"робототехника" site:arxiv.org', "poll_minutes": 60},
    )
    assert response.status_code == 201, response.text
    source = await db.fetchone("SELECT name, type, url, poll_minutes FROM sources WHERE id = %s", (response.json()["id"],))
    assert source["type"] == "rss" and source["poll_minutes"] == 60
    assert source["name"].startswith("Поиск статей:")
    assert "q=%22%D1%80%D0%BE%D0%B1%D0%BE%D1%82%D0%BE%D1%82%D0%B5%D1%85%D0%BD%D0%B8%D0%BA%D0%B0%22" in source["url"]
    assert "ceid=RU%3Aru" in source["url"]
    queued = await db.fetchone("SELECT kind, status FROM jobs WHERE dedupe_key = %s", (f"ingest:{response.json()['id']}",))
    assert queued == {"kind": "ingest_source", "status": "queued"}


async def test_article_search_preview_returns_titles_publishers_and_new_count(monkeypatch):
    from app.api import sources
    from app.pipeline.ingest import insert_items
    from app.textutil import utcnow

    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    project = await ed.post("/api/projects", json={"name": "Research"})
    pid = project.json()["id"]

    async def allow_public_url(_url):
        return None

    async def sample_feed(_url):
        return [
            {"title": "Python разработка", "url": "https://news.google.com/rss/articles/1", "text": "", "published_at": utcnow(), "publisher_name": "Example"},
            {"title": "Питоны в природе", "url": "https://news.google.com/rss/articles/2", "text": "", "published_at": utcnow(), "publisher_name": "Nature"},
        ], {}

    await insert_items(pid, None, [
        {"title": "Python разработка", "url": "https://news.google.com/rss/articles/1", "text": "", "published_at": utcnow()}
    ])
    monkeypatch.setattr(sources, "check_url", allow_public_url)
    monkeypatch.setattr(sources, "fetch_feed", sample_feed)
    response = await ed.post("/api/sources/search/preview", json={"project_id": pid, "query": "python"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["count"] == 2 and data["new_count"] == 1
    assert data["sample"][0]["publisher"] == "Example"


async def test_manual_schedule_run_respects_automatic_publish_mode():
    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    pid = await make_project(publish_mode="auto")
    await ed.post("/api/items/import", json={"project_id": pid, "items": load_demo()})
    await _drain()

    schedule = await ed.post(
        "/api/schedules",
        json={"project_id": pid, "name": "Экономика", "cron": "0 9 * * *", "kind": "post", "top_n": 1},
    )
    assert schedule.status_code == 201, schedule.text
    run = await ed.post(f"/api/schedules/{schedule.json()['id']}/run")
    assert run.status_code == 200, run.text
    await _drain()

    draft = await db.fetchone("SELECT status, checks FROM drafts WHERE project_id = %s", (pid,))
    assert draft["status"] == "approved", draft["checks"]
    assert draft["checks"]["automatic_review"]["passed"] is True
    assert (await db.fetchone("SELECT count(*) AS n FROM publications"))["n"] == 1


async def test_show_sources_setting_and_dashboard_rss_hint():
    await _user("ed@t.io", "editor")
    ed = await _client("ed@t.io")
    p = (await ed.post("/api/projects", json={"name": "P", "show_sources": False})).json()
    assert p["show_sources"] is False
    dash = (await ed.get("/api/dashboard", params={"project_id": p["id"]})).json()
    assert dash["pipeline"]["rss_enabled"] == 0  # без RSS-источников новые материалы не поступают — интерфейс подскажет
    await db.execute("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'r', 'http://x.test/f')", (p["id"],))
    assert (await ed.get("/api/dashboard", params={"project_id": p["id"]})).json()["pipeline"]["rss_enabled"] == 1


async def test_admin_password_reset_command():
    from app import admin
    from app.security import verify_password

    await _user("admin@t.io", "admin", "oldpassword1")
    await admin.reset_password("ADMIN@t.io", "newpassword2")  # регистр почты не важен
    row = await db.fetchone("SELECT password_hash FROM users")
    assert verify_password(row["password_hash"], "newpassword2") and not verify_password(row["password_hash"], "oldpassword1")
    c = await _client()
    assert (await c.post("/api/auth/login", json={"email": "admin@t.io", "password": "newpassword2"})).status_code == 200
    with pytest.raises(SystemExit):
        await admin.reset_password("nobody@t.io", "whatever123")
