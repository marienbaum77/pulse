from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest

from app import db
from app.main import app
from app.pipeline import editorial_model, interest
from app.providers import StubProvider, set_provider
from app.security import hash_password

from .helpers import make_project


def test_local_mode_does_not_require_chat(monkeypatch):
    settings = interest.get_settings()
    monkeypatch.setattr(settings, "interest_mode", "local")
    assert interest.local_enabled()
    assert interest.enabled(StubProvider())
    monkeypatch.setattr(settings, "interest_mode", "llm")
    assert not interest.enabled(StubProvider())


async def _make_cluster(pid: int) -> int:
    row = await db.fetchone(
        "INSERT INTO clusters(project_id, title, first_seen, last_seen, source_count, item_count) "
        "VALUES (%s, 'Новый рекорд: 50%% роста', now(), now(), 2, 1) RETURNING id", (pid,),
    )
    vector = np.zeros(256, dtype=np.float32)
    vector[0] = 1
    await db.execute(
        "INSERT INTO items(project_id, url_hash, title, published_at, status, cluster_id, title_embedding, topic_score) "
        "VALUES (%s, 'local-story', 'Новый рекорд', now(), 'clustered', %s, %s, 0.8)",
        (pid, row["id"], vector),
    )
    return row["id"]


@pytest.mark.usefixtures("clean")
async def test_local_rank_uses_stub_without_chat_and_replaces_foreign_cache(monkeypatch):
    monkeypatch.setattr(interest.get_settings(), "interest_mode", "local")
    provider = StubProvider()
    chat = AsyncMock()
    monkeypatch.setattr(provider, "chat", chat)
    set_provider(provider)
    pid = await make_project(channel=False)
    cid = await _make_cluster(pid)
    project = await db.fetchone("SELECT * FROM projects WHERE id = %s", (pid,))
    await db.execute("UPDATE clusters SET interest = 9.9, interest_items = 1, interest_model = 'llm' WHERE id = %s", (cid,))

    ranked = await interest.rank_by_interest(project, provider, [{"id": cid}], 1)
    assert ranked[0]["interest"] <= 6.5
    assert chat.await_count == 0
    cached = await db.fetchone("SELECT interest, interest_model FROM clusters WHERE id = %s", (cid,))
    assert cached["interest"] <= 6.5 and cached["interest_model"] == "local:cold"

    evaluate = AsyncMock(side_effect=AssertionError("актуальный кеш не должен пересчитываться"))
    monkeypatch.setattr(editorial_model, "evaluate_interest_local", evaluate)
    await interest.rank_by_interest(project, provider, [{"id": cid}], 1)
    assert evaluate.await_count == 0


@pytest.mark.usefixtures("clean")
async def test_editorial_model_api_validates_channels_and_queues_retrain():
    await db.execute("INSERT INTO users(email, password_hash, role) VALUES ('editor@t.io', %s, 'editor')", (hash_password("password123"),))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-Requested-With": "pulse"})
    await client.post("/api/auth/login", json={"email": "editor@t.io", "password": "password123"})
    assert (await client.post("/api/projects", json={"name": "P", "reference_tg_channels": ["not a channel"]})).status_code == 422
    assert (await client.post("/api/projects", json={"name": "P", "reference_tg_channels": [f"@channel{i}" for i in range(6)]})).status_code == 422
    response = await client.post("/api/projects", json={"name": "P", "reference_tg_channels": ["@Valid_Name", "https://t.me/second"]})
    assert response.status_code == 201, response.text
    pid = response.json()["id"]
    assert response.json()["reference_tg_channels"] == ["Valid_Name", "second"]
    assert (await client.get(f"/api/projects/{pid}/editorial-model")).json()["stage"] == "untrained"
    assert (await client.post(f"/api/projects/{pid}/editorial-model/retrain")).status_code == 202
    job = await db.fetchone("SELECT kind, dedupe_key FROM jobs WHERE kind = 'train_editorial_model' AND status = 'queued'")
    assert job["dedupe_key"] == f"editorial:{pid}"
    assert await db.fetchone("SELECT 1 FROM audit_log WHERE action = 'retrain' AND entity_id = %s", (pid,))
    await client.aclose()
