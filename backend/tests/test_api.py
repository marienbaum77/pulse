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
