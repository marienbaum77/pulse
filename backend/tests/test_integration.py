import json
from datetime import timedelta
from email.utils import formatdate
from unittest.mock import patch

import pytest
import numpy as np
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app import db, jobs, publisher, scheduler
from app.api.content import generate_from_cluster, regenerate_draft
from app.pipeline import drafts_service, generate, ingest, process  # noqa: F401  (регистрируют обработчики)
from app.pipeline.generate import run_generation
from app.pipeline.ingest import import_manual
from app.pipeline.process import get_project, process_project, select_clusters
from app.publisher import SendResult
from app.textutil import utcnow

from .helpers import MiniServer, load_demo, make_project

pytestmark = pytest.mark.usefixtures("clean")


async def _pipeline(threshold=0.35):
    pid = await make_project(threshold)
    await import_manual(pid, load_demo())
    await process_project(pid)
    return pid


async def test_reclaim_expired_jobs_does_not_requeue_duplicate_dedupe_key():
    stale = await db.fetchone(
        "INSERT INTO jobs(kind, payload, dedupe_key, status, locked_until) "
        "VALUES ('process_project', '{}', 'process:expired-duplicate', 'running', now() - interval '15 minutes') RETURNING id"
    )
    queued_id = await jobs.enqueue(
        "process_project", {"project_id": 1}, dedupe_key="process:expired-duplicate"
    )
    standalone = await db.fetchone(
        "INSERT INTO jobs(kind, payload, dedupe_key, status, locked_until) "
        "VALUES ('process_project', '{}', 'process:expired-alone', 'running', now() - interval '15 minutes') RETURNING id"
    )

    assert queued_id is not None
    assert await jobs.reclaim_expired() == 2
    rows = await db.fetchall(
        "SELECT id, status FROM jobs WHERE id = ANY(%s) ORDER BY id",
        ([stale["id"], queued_id, standalone["id"]],),
    )
    assert rows == [
        {"id": stale["id"], "status": "failed"},
        {"id": queued_id, "status": "queued"},
        {"id": standalone["id"], "status": "queued"},
    ]


async def test_expedite_queued_job_instead_of_suppressing_manual_poll():
    job_id = await jobs.enqueue("ingest_source", {"source_id": 42}, dedupe_key="ingest:42")
    assert job_id is not None
    await db.execute("UPDATE jobs SET run_at = now() + interval '1 hour' WHERE id = %s", (job_id,))

    assert await jobs.expedite_queued("ingest:42") == job_id
    row = await db.fetchone("SELECT run_at <= now() AS due FROM jobs WHERE id = %s", (job_id,))
    assert row["due"] is True


async def test_source_fetches_are_claimed_before_expensive_project_processing():
    await jobs.enqueue("process_project", {"project_id": 1}, dedupe_key="process:priority")
    source_job = await jobs.enqueue("ingest_source", {"source_id": 42}, dedupe_key="ingest:priority")

    claimed = await jobs.claim()

    assert source_job is not None
    assert claimed["id"] == source_job
    assert claimed["kind"] == "ingest_source"


# ---------- конвейер ----------
async def test_pipeline_clusters_scores_and_is_incremental():
    pid = await make_project()
    assert await import_manual(pid, load_demo()) == 45
    assert await import_manual(pid, load_demo()) == 0  # повторный импорт не создаёт дублей
    stats = await process_project(pid)
    assert stats["embedded"] == 45 and stats["duplicates"] == 0
    clusters = await db.fetchall("SELECT id, item_count, title, score FROM clusters WHERE project_id = %s", (pid,))
    assert len(clusters) == 12  # 6 событий + 6 несвязанных заметок
    events = [c for c in clusters if c["item_count"] >= 2]
    assert len(events) == 6 and all(c["title"] and c["score"] is not None for c in events)
    assert (await process_project(pid))["clustered"] == 0

    before = next(c for c in events if "ПолярДБ" in c["title"])
    await import_manual(pid, [{"title": "ПолярДБ 5.0 вышла: итоги первого дня", "text": "Версия 5.0 СУБД «ПолярДБ» получила поддержку векторного поиска. Разработчики отметили распределённые транзакции.", "source": "ТехДень", "hours_ago": 0.5}])
    await process_project(pid)
    after = await db.fetchone("SELECT item_count FROM clusters WHERE id = %s", (before["id"],))
    assert after["item_count"] == before["item_count"] + 1
    assert await db.fetchone("SELECT count(*) AS n FROM clusters WHERE project_id = %s", (pid,)) == {"n": 12}


async def test_processing_project_does_not_generate_outside_schedules():
    pid = await make_project(publish_mode="auto")
    await import_manual(pid, load_demo())
    await process_project(pid)
    assert (await db.fetchone("SELECT count(*) AS n FROM jobs WHERE kind = 'generate' AND payload->>'project_id' = %s", (str(pid),)))["n"] == 0


async def test_select_clusters_filters_low_topic_fit_and_zero_disables_filter():
    pid = await _pipeline()
    rows = await db.fetchall(
        "SELECT id, score_breakdown FROM clusters WHERE project_id = %s AND state = 'open' AND item_count >= 2 ORDER BY id",
        (pid,),
    )
    assert len(rows) >= 2
    for index, row in enumerate(rows):
        parts = row["score_breakdown"]
        fit = 0.2 if index == 0 else 0.9 if index == 1 else 0.6
        parts["topic_fit"] = {"value": fit, "weight": 0.6, "contribution": round(fit * 0.6, 4)}
        await db.execute("UPDATE clusters SET score_breakdown = %s WHERE id = %s", (Jsonb(parts), row["id"]))

    project = await get_project(pid)
    selected = await select_clusters(project, 10)
    assert rows[0]["id"] not in {cluster["id"] for cluster in selected}
    assert rows[1]["id"] in {cluster["id"] for cluster in selected}
    assert selected[0]["id"] == rows[1]["id"]

    await db.execute("UPDATE projects SET topic_threshold = 0 WHERE id = %s", (pid,))
    selected = await select_clusters(await get_project(pid), 10)
    assert rows[0]["id"] in {cluster["id"] for cluster in selected}


async def test_topic_fit_uses_title_embeddings_not_article_body(monkeypatch):
    pid = await _pipeline()
    await db.execute("UPDATE items SET title_embedding = NULL WHERE project_id = %s", (pid,))

    class RealProvider:
        name = "openai"

        async def embed(self, texts, project_id=None):
            return np.tile(np.eye(1, 256, 0, dtype=np.float32), (len(texts), 1))

    provider = RealProvider()
    project = await get_project(pid)
    assert await process.embed_new(project, provider) == 0
    assert await db.fetchone(
        "SELECT count(*) AS n FROM items WHERE project_id = %s AND embedding IS NOT NULL AND title_embedding IS NULL",
        (pid,),
    ) == {"n": 0}

    cluster = await db.fetchone(
        "SELECT id, centroid FROM clusters WHERE project_id = %s AND state = 'open' AND item_count >= 2 ORDER BY id LIMIT 1",
        (pid,),
    )
    topic_vec = np.asarray(cluster["centroid"], dtype=np.float32)
    topic_vec /= np.linalg.norm(topic_vec)
    axis = np.zeros_like(topic_vec)
    axis[int(np.argmin(np.abs(topic_vec)))] = 1
    title_vec = axis - float(axis @ topic_vec) * topic_vec
    title_vec /= np.linalg.norm(title_vec)
    await db.execute("UPDATE projects SET topic_embedding = %s WHERE id = %s", (topic_vec, pid))
    await db.execute("UPDATE items SET title_embedding = %s WHERE cluster_id = %s", (title_vec, cluster["id"]))

    async def get_provider():
        return provider

    monkeypatch.setattr(process, "refresh_provider", get_provider)
    await process.refresh_clusters(await get_project(pid))

    result = await db.fetchone("SELECT score_breakdown FROM clusters WHERE id = %s", (cluster["id"],))
    topic_fit = result["score_breakdown"]["topic_fit"]
    assert not topic_fit.get("neutral")
    assert topic_fit["value"] == pytest.approx(0, abs=0.001)


async def test_manual_generation_rejects_cluster_below_topic_threshold():
    pid = await _pipeline()
    row = await db.fetchone("SELECT id, score_breakdown FROM clusters WHERE project_id = %s AND state = 'open' AND item_count >= 2 ORDER BY id LIMIT 1", (pid,))
    parts = row["score_breakdown"]
    parts["topic_fit"] = {"value": 0.2, "weight": 0.6, "contribution": 0.12}
    await db.execute("UPDATE clusters SET score_breakdown = %s WHERE id = %s", (Jsonb(parts), row["id"]))
    before = await db.fetchone("SELECT count(*) AS n FROM jobs WHERE kind = 'generate'")

    with pytest.raises(HTTPException) as error:
        await generate_from_cluster(row["id"], {"id": 1})

    assert error.value.status_code == 409
    after = await db.fetchone("SELECT count(*) AS n FROM jobs WHERE kind = 'generate'")
    assert after == before


async def test_near_duplicate_is_flagged_not_clustered():
    pid = await make_project()
    item = {"title": "Вышла ПолярДБ 5.0", "text": "Вышла версия 5.0 СУБД «ПолярДБ». Поддержка векторного поиска.", "source": "ТехДень", "hours_ago": 3}
    await import_manual(pid, [item, {**item, "url": "https://example.com/copy"}])
    stats = await process_project(pid)
    assert stats["duplicates"] == 1 and stats["clustered"] == 1


async def test_generate_approve_publish_flow_is_idempotent():
    pid = await _pipeline()
    ids = await run_generation({"project_id": pid, "kind": "digest", "top_n": 3})
    assert len(ids) == 1
    d = await db.fetchone("SELECT * FROM drafts WHERE id = %s", (ids[0],))
    assert d["status"] == "pending_review" and d["kind"] == "digest" and len(d["cluster_ids"]) == 3
    assert d["citations"] and "1." in d["body"] and "[1]" in d["body"]
    assert (await db.fetchone("SELECT count(*) AS n FROM clusters WHERE state = 'drafted'"))["n"] == 3

    assert await drafts_service.approve(d["id"], None) is True
    assert await drafts_service.approve(d["id"], None) is False  # повторное утверждение
    assert (await db.fetchone("SELECT count(*) AS n FROM publications"))["n"] == 1

    assert await publisher.process_one() is True
    pub = await db.fetchone("SELECT * FROM publications")
    assert pub["status"] == "sent" and pub["external_id"].startswith("console-")
    assert await publisher.process_one() is False
    pub_clusters = await db.fetchall("SELECT state, published_centroid FROM clusters WHERE id = ANY(%s)", (d["cluster_ids"],))
    assert all(c["state"] == "published" and c["published_centroid"] is not None for c in pub_clusters)

    second = await run_generation({"project_id": pid, "kind": "digest", "top_n": 3})
    d2 = await db.fetchone("SELECT cluster_ids FROM drafts WHERE id = %s", (second[0],))
    assert not set(d2["cluster_ids"]) & set(d["cluster_ids"])  # опубликованные сюжеты повторно не берутся


async def test_auto_publish_only_approves_posts_that_pass_checks():
    pid = await make_project(publish_mode="auto")
    await import_manual(pid, load_demo())
    await process_project(pid)

    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    draft = await db.fetchone("SELECT status, checks, title, body FROM drafts WHERE id = %s", (did,))
    assert draft["status"] == "approved", (draft["checks"], draft["title"], draft["body"])
    assert draft["checks"]["automatic_review"] == {
        "passed": True,
        "reasons": [],
        "auto_publish_enabled": True,
    }
    assert (await db.fetchone("SELECT count(*) AS n FROM publications WHERE draft_id = %s", (did,)))["n"] == 1


async def test_full_auto_publishes_even_when_checks_fail():
    pid = await make_project(publish_mode="full_auto")
    await import_manual(pid, load_demo())
    await process_project(pid)

    with patch("app.pipeline.generate.automatic_review", return_value={"passed": False, "reasons": ["тест: подставленная причина"]}):
        [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    draft = await db.fetchone("SELECT status, checks FROM drafts WHERE id = %s", (did,))
    assert draft["status"] == "approved"
    review = draft["checks"]["automatic_review"]
    assert review["passed"] is False and review["overridden"] is True
    assert (await db.fetchone("SELECT count(*) AS n FROM publications WHERE draft_id = %s", (did,)))["n"] == 1


async def test_full_auto_still_waits_for_a_channel():
    pid = await make_project(publish_mode="full_auto", channel=False)
    await import_manual(pid, load_demo())
    await process_project(pid)

    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    draft = await db.fetchone("SELECT status, checks FROM drafts WHERE id = %s", (did,))
    assert draft["status"] == "pending_review"
    assert "Нет включённых каналов публикации" in draft["checks"]["automatic_review"]["reasons"]


async def test_force_review_keeps_full_auto_generation_as_draft():
    pid = await make_project(publish_mode="full_auto")
    await import_manual(pid, load_demo())
    await process_project(pid)

    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1, "force_review": True})
    draft = await db.fetchone("SELECT status FROM drafts WHERE id = %s", (did,))
    assert draft["status"] == "pending_review"
    checks = await db.fetchone("SELECT checks FROM drafts WHERE id = %s", (did,))
    assert checks["checks"]["automatic_review"]["auto_publish_enabled"] is False
    assert (await db.fetchone("SELECT count(*) AS n FROM publications WHERE draft_id = %s", (did,)))["n"] == 0


async def test_generation_waits_for_short_selected_rss_article_enrichment():
    pid = await _pipeline()
    cluster = await db.fetchone(
        "SELECT id FROM clusters WHERE project_id = %s AND state = 'open' AND item_count >= 2 ORDER BY id LIMIT 1",
        (pid,),
    )
    source = await db.fetchone(
        "INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Короткая RSS', 'https://example.test/feed') RETURNING id",
        (pid,),
    )
    await db.execute(
        "INSERT INTO items(project_id, source_id, url, url_hash, title, text, published_at, status, cluster_id) "
        "VALUES (%s, %s, 'https://example.test/article', %s, 'Короткая заметка', 'Короткий текст.', now(), 'clustered', %s)",
        (pid, source["id"], f"short-rss-{pid}", cluster["id"]),
    )

    with pytest.raises(jobs.Retry):
        await run_generation({"project_id": pid, "kind": "post", "cluster_ids": [cluster["id"]]})

    enrichment = await db.fetchone(
        "SELECT payload FROM jobs WHERE kind = 'enrich_articles' AND dedupe_key = %s AND status = 'queued'",
        (f"article-backfill:{pid}:{source['id']}",),
    )
    assert enrichment["payload"]["include_clustered"] is True


async def test_recover_stuck_generations_fails_and_reopens_orphaned_drafts():
    pid = await _pipeline()
    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    draft = await db.fetchone("SELECT cluster_ids FROM drafts WHERE id = %s", (did,))
    cluster_id = draft["cluster_ids"][0]
    # эмулируем зависший процесс: воркер убит в момент генерации, черновик навсегда остаётся "generating"
    await db.execute("UPDATE drafts SET status = 'generating', created_at = now() - interval '20 minutes' WHERE id = %s", (did,))

    assert await generate.recover_stuck_generations(timeout_minutes=15) == 1
    updated = await db.fetchone("SELECT status, error FROM drafts WHERE id = %s", (did,))
    assert updated["status"] == "failed" and "перезапуском" in updated["error"]
    assert (await db.fetchone("SELECT state FROM clusters WHERE id = %s", (cluster_id,)))["state"] == "open"
    assert await generate.recover_stuck_generations(timeout_minutes=15) == 0  # уже обработанные повторно не трогаем



async def test_reject_returns_clusters_to_work():
    pid = await _pipeline()
    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    d = await db.fetchone("SELECT cluster_ids FROM drafts WHERE id = %s", (did,))
    assert await drafts_service.reject(did) is True
    assert (await db.fetchone("SELECT state FROM clusters WHERE id = %s", (d["cluster_ids"][0],)))["state"] == "excluded"


async def test_regenerating_a_rejected_draft_explicitly_reopens_its_story():
    pid = await _pipeline()
    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    draft = await db.fetchone("SELECT cluster_ids FROM drafts WHERE id = %s", (did,))
    assert await drafts_service.reject(did) is True

    queued = await regenerate_draft(did, {"id": 1})

    assert "job_id" in queued
    assert (await db.fetchone("SELECT state FROM clusters WHERE id = %s", (draft["cluster_ids"][0],)))["state"] == "open"


async def test_approving_exact_duplicate_text_cancels_second_publication():
    pid = await make_project()
    first = await db.fetchone(
        "INSERT INTO drafts(project_id, title, body, status) VALUES (%s, 'Повтор', 'Одинаковый текст поста', 'pending_review') RETURNING id",
        (pid,),
    )
    second = await db.fetchone(
        "INSERT INTO drafts(project_id, title, body, status) VALUES (%s, 'Повтор', 'Одинаковый текст поста', 'pending_review') RETURNING id",
        (pid,),
    )

    assert await drafts_service.approve(first["id"], None) is True
    assert await drafts_service.approve(second["id"], None) is True

    rows = await db.fetchall(
        "SELECT p.draft_id, p.status, p.error FROM publications p WHERE p.draft_id = ANY(%s) ORDER BY p.draft_id",
        ([first["id"], second["id"]],),
    )
    assert [row["status"] for row in rows] == ["pending", "cancelled"]
    assert "Точный повтор текста" in rows[1]["error"]


async def test_clear_drafts_preserves_approved_and_generating_entries():
    pid = await _pipeline()
    [pending_id] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    pending = await db.fetchone("SELECT cluster_ids FROM drafts WHERE id = %s", (pending_id,))
    for status in ("rejected", "failed", "approved", "generating"):
        await db.execute("INSERT INTO drafts(project_id, status) VALUES (%s, %s)", (pid, status))

    assert await drafts_service.clear(pid) == 3
    remaining = await db.fetchall("SELECT status FROM drafts WHERE project_id = %s ORDER BY status", (pid,))
    assert remaining == [{"status": "approved"}, {"status": "generating"}]
    assert (await db.fetchone("SELECT state FROM clusters WHERE id = %s", (pending["cluster_ids"][0],)))["state"] == "open"


async def test_clear_clusters_preserves_published_and_drafted_stories():
    pid = await _pipeline()
    clusters = await db.fetchall("SELECT id FROM clusters WHERE project_id = %s ORDER BY id", (pid,))
    keep_draft, keep_published = clusters[:2]
    await db.execute("INSERT INTO drafts(project_id, cluster_ids, status) VALUES (%s, %s, 'rejected')", (pid, [keep_draft["id"]]))
    await db.execute("UPDATE clusters SET state = 'published', published_at = now() WHERE id = %s", (keep_published["id"],))

    deleted, ignored = await process.clear_clusters(pid)
    assert deleted == len(clusters) - 2
    assert ignored > 0
    remaining = await db.fetchall("SELECT id FROM clusters WHERE project_id = %s", (pid,))
    assert {row["id"] for row in remaining} == {keep_draft["id"], keep_published["id"]}
    assert (await db.fetchone("SELECT count(*) AS n FROM items WHERE project_id = %s AND status = 'ignored'", (pid,)))["n"] == ignored


async def test_approve_without_channels_is_refused():
    pid = await make_project(channel=False)
    await import_manual(pid, load_demo())
    await process_project(pid)
    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})
    with pytest.raises(drafts_service.NoChannels):
        await drafts_service.approve(did, None)


# ---------- публикатор ----------
async def _approved_publication(auto_retry=False, attempts=0):
    pid = await make_project(auto_retry_unknown=auto_retry)
    ch = await db.fetchone("SELECT id FROM channels WHERE project_id = %s", (pid,))
    d = await db.fetchone("INSERT INTO drafts(project_id, title, body, status) VALUES (%s, 'T', 'B', 'approved') RETURNING id", (pid,))
    p = await db.fetchone("INSERT INTO publications(draft_id, channel_id, attempts) VALUES (%s, %s, %s) RETURNING id", (d["id"], ch["id"], attempts))
    return pid, p["id"]


def _sender(result):
    async def send(channel, draft, pub):
        if isinstance(result, Exception):
            raise result
        return result

    return {"console": send}


async def test_unknown_outcome_is_not_retried_automatically():
    _, pub_id = await _approved_publication()
    assert await publisher.process_one(_sender(SendResult("unknown", error="504"))) is True
    assert (await db.fetchone("SELECT status FROM publications"))["status"] == "unknown"
    assert await publisher.process_one(_sender(SendResult("sent", external_id="1"))) is False  # больше не берётся в работу


async def test_late_success_overrides_unknown():
    _, pub_id = await _approved_publication()
    await publisher.process_one(_sender(SendResult("unknown")))
    pub = await db.fetchone("SELECT * FROM publications")
    draft = await db.fetchone("SELECT * FROM drafts")
    await publisher.apply_result(pub, SendResult("sent", external_id="777"), draft, False)
    row = await db.fetchone("SELECT status, external_id FROM publications")
    assert row == {"status": "sent", "external_id": "777"}


async def test_retry_backoff_and_attempt_limit():
    _, pub_id = await _approved_publication()
    await publisher.process_one(_sender(SendResult("retry", error="429", retry_after=60)))
    row = await db.fetchone("SELECT status, due_at FROM publications")
    assert row["status"] == "pending" and row["due_at"] > utcnow() + timedelta(seconds=30)
    assert await publisher.process_one(_sender(SendResult("sent"))) is False  # ещё не пора

    await db.execute("TRUNCATE users, projects RESTART IDENTITY CASCADE")
    _, pub_id = await _approved_publication(attempts=publisher.MAX_ATTEMPTS)
    await publisher.process_one(_sender(SendResult("retry", error="net")))
    assert (await db.fetchone("SELECT status FROM publications"))["status"] == "failed"


async def test_auto_retry_unknown_setting_and_crash_recovery():
    _, _ = await _approved_publication(auto_retry=True)
    await publisher.process_one(_sender(RuntimeError("boom")))
    assert (await db.fetchone("SELECT status FROM publications"))["status"] == "pending"

    await db.execute("UPDATE publications SET status = 'sending', locked_until = now() - interval '1 minute'")
    await db.execute("UPDATE projects SET auto_retry_unknown = false")
    assert await publisher.recover_stale() == 1
    row = await db.fetchone("SELECT status, error FROM publications")
    assert row["status"] == "unknown" and "результат неизвестен" in row["error"]


async def test_telegram_sender_against_mock_bot_api(monkeypatch):
    def handler(method, path, headers, body):
        token = path.split("/")[1][3:]
        if token == "DROP":
            return None  # соединение оборвано без ответа — как таймаут после отправки
        if token == "OK":
            payload = json.loads(body)
            assert payload["parse_mode"] == "HTML" and "<b>Заголовок</b>" in payload["text"]
            return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":{"message_id":42}}'
        if token == "LIMIT":
            return 429, {"Content-Type": "application/json"}, '{"ok":false,"description":"Too Many Requests","parameters":{"retry_after":3}}'
        return 400, {"Content-Type": "application/json"}, '{"ok":false,"description":"chat not found"}'

    draft = {"id": 1, "title": "Заголовок", "body": "Текст", "citations": []}
    async with MiniServer(handler) as srv:
        for token, kind in (("OK", "sent"), ("LIMIT", "retry"), ("BAD", "failed"), ("DROP", "unknown")):
            monkeypatch.setenv("TEST_BOT_TOKEN", token)
            channel = {"config": {"chat_id": "@c", "token_env": "TEST_BOT_TOKEN", "api_base": srv.url}}
            res = await publisher.send_telegram(channel, draft, {"id": 1})
            assert res.kind == kind, (token, res)
            if kind == "sent":
                assert res.external_id == "42"
            if kind == "retry":
                assert res.retry_after == 3


async def test_webhook_sender_signs_and_sets_idempotency_key():
    seen = {}

    def handler(method, path, headers, body):
        seen.update(headers)
        return 200, {}, "ok"

    async with MiniServer(handler) as srv:
        ch = {"id": 5, "config": {"url": srv.url + "/hook", "secret": "s3cret"}}
        res = await publisher.send_webhook(ch, {"id": 9, "project_id": 1, "kind": "post", "title": "t", "body": "b", "citations": []}, {"id": 1})
    assert res.kind == "sent" and seen["idempotency-key"] == "pulse-9-5" and seen["x-pulse-signature"].startswith("sha256=")


# ---------- очередь и планировщик ----------
async def test_job_queue_dedupe_retry_and_reclaim():
    calls = []

    @jobs.handler("t_fail")
    async def _f(payload):
        calls.append(1)
        raise RuntimeError("нет")

    @jobs.handler("t_busy")
    async def _b(payload):
        raise jobs.Retry(30)

    assert await jobs.enqueue("t_fail", {}, dedupe_key="k") is not None
    assert await jobs.enqueue("t_fail", {}, dedupe_key="k") is None  # уже в очереди
    assert await jobs.run_one() is True
    row = await db.fetchone("SELECT status, attempts, last_error, run_at FROM jobs")
    assert row["status"] == "queued" and row["attempts"] == 1 and "нет" in row["last_error"] and row["run_at"] > utcnow()
    assert await jobs.run_one() is False  # бэкофф ещё не прошёл

    await db.execute("TRUNCATE jobs")
    await jobs.enqueue("t_busy", {})
    await jobs.run_one()
    row = await db.fetchone("SELECT status, attempts FROM jobs")
    assert row == {"status": "queued", "attempts": 0}

    await db.execute("UPDATE jobs SET status = 'running', locked_until = now() - interval '1 minute'")
    assert await jobs.reclaim_expired() == 1


async def test_job_queue_can_reserve_generate_lane():
    seen = []

    @jobs.handler("t_generate")
    async def _g(payload):
        seen.append(("generate", payload["id"]))

    @jobs.handler("t_process")
    async def _p(payload):
        seen.append(("process", payload["id"]))

    await jobs.enqueue("t_process", {"id": 1})
    await jobs.enqueue("t_generate", {"id": 2})

    assert await jobs.run_one(include_kinds=("t_generate",)) is True
    assert seen == [("generate", 2)]
    assert await jobs.run_one(exclude_kinds=("t_generate",)) is True
    assert seen == [("generate", 2), ("process", 1)]


async def test_schedule_fires_exactly_once_per_slot_and_polls_sources():
    pid = await make_project()
    await db.execute("INSERT INTO schedules(project_id, name, cron, tz, kind, top_n) VALUES (%s, 's', '* * * * *', 'UTC', 'digest', 3)", (pid,))
    now = utcnow()
    assert await scheduler.fire_schedules(now) == 1
    assert await scheduler.fire_schedules(now) == 0  # тот же слот — повторного запуска нет
    assert await scheduler.fire_schedules(now + timedelta(seconds=61)) == 1
    # второй слот наступил, пока задача первого ещё ждёт в очереди: дубль не создаётся, очередь не копит одинаковые генерации
    assert (await db.fetchone("SELECT count(*) AS n FROM jobs WHERE kind = 'generate'"))["n"] == 1
    schedule_payload = (await db.fetchone("SELECT payload FROM jobs WHERE kind = 'generate' ORDER BY id LIMIT 1"))["payload"]
    assert schedule_payload["scheduled"] is True
    assert schedule_payload["top_n"] == 3 and schedule_payload["kind"] == "digest"
    queued = await jobs.queued_generations(pid)
    assert len(queued) == 1 and all(j["title"] == "Плановая генерация" for j in queued)

    await db.execute("INSERT INTO sources(project_id, type, name, url, poll_minutes) VALUES (%s, 'rss', 'r', 'http://x.test/feed', 30)", (pid,))
    assert await scheduler.poll_sources() == 1
    assert await scheduler.poll_sources() == 0  # next_poll_at сдвинут


async def test_rss_ingest_from_local_feed():
    fresh, old = formatdate(usegmt=True), formatdate(utcnow().timestamp() - 30 * 86400, usegmt=True)
    xml = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Свежая новость</title><link>http://n.test/1?utm_source=x</link><description>&lt;p&gt;Текст &lt;b&gt;новости&lt;/b&gt;.&lt;/p&gt; &lt;a&gt;Читать далее&lt;/a&gt;</description><pubDate>{fresh}</pubDate></item>
    <item><title>Старая новость</title><link>http://n.test/2</link><description>Давно</description><pubDate>{old}</pubDate></item>
    </channel></rss>"""
    hits = []

    def handler(method, path, headers, body):
        hits.append(headers.get("if-none-match"))
        if headers.get("if-none-match") == '"v1"':
            return 304, {}, b""
        return 200, {"Content-Type": "application/rss+xml", "ETag": '"v1"'}, xml

    pid = await make_project()
    async with MiniServer(handler) as srv:
        src = await db.fetchone("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Лента', %s) RETURNING id", (pid, srv.url + "/feed"))
        await ingest.ingest_source({"source_id": src["id"]})
        items = await db.fetchall("SELECT title, text, url FROM items")
        assert [i["title"] for i in items] == ["Свежая новость"]  # старая вне окна
        assert items[0]["text"] == "Текст новости."
        assert "Читать далее" not in items[0]["text"]
        await ingest.ingest_source({"source_id": src["id"]})  # ETag → 304
        assert hits == [None, '"v1"']
    row = await db.fetchone("SELECT last_error, last_ok_at FROM sources")
    assert row["last_error"] is None and row["last_ok_at"] is not None


async def test_short_rss_item_is_enriched_from_article_before_project_processing():
    lead = "Инфраструктурная платформа расширила поддержку баз данных и облачных сред."
    article_text = f"{lead} " + "Инженеры добавили новые инструменты мониторинга и резервного копирования. " * 25
    html = f"<html><body><nav>Навигация и ссылки</nav><article><h1>Техническое обновление</h1><p>{article_text}</p></article></body></html>"

    def handler(method, path, headers, body):
        assert path == "/article"
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html

    pid = await make_project()
    async with MiniServer(handler) as srv:
        source = await db.fetchone(
            "INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Лента', %s) RETURNING id",
            (pid, srv.url + "/feed"),
        )
        item = await db.fetchone(
            "INSERT INTO items(project_id, source_id, url, url_hash, title, text, published_at) "
            "VALUES (%s,%s,%s,'article-hash','Техническое обновление',%s,now()) RETURNING id",
            (pid, source["id"], srv.url + "/article", f"{lead} Читать далее"),
        )
        with pytest.raises(jobs.Retry):
            await process.process_project_job({"project_id": pid})
        queued = await db.fetchone(
            "SELECT payload FROM jobs WHERE kind = 'enrich_articles' AND status = 'queued' ORDER BY id DESC LIMIT 1"
        )
        assert queued["payload"]["source_id"] == source["id"]
        await ingest.enrich_articles(queued["payload"])

    enriched = await db.fetchone("SELECT text, article_content_status FROM items WHERE id = %s", (item["id"],))
    assert enriched["article_content_status"] == "done"
    assert "Читать далее" not in enriched["text"]
    assert enriched["text"].count(lead) == 1
    assert "Инфраструктурная платформа" in enriched["text"]
    assert "инструменты мониторинга" in enriched["text"].casefold()
    assert "Навигация и ссылки" not in enriched["text"]


async def test_legacy_clustered_rss_item_is_enriched_without_rewriting_draft():
    from app.providers import StubProvider

    article_text = "Облачная инфраструктура и базы данных получили важное техническое обновление. " * 30
    html = f"<html><body><article><h1>Обновление</h1><p>{article_text}</p></article></body></html>"

    def handler(method, path, headers, body):
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html

    pid = await make_project()
    provider = StubProvider()
    old_embedding = (await provider.embed(["Короткая новость"]))[0]
    async with MiniServer(handler) as srv:
        source = await db.fetchone(
            "INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Legacy RSS', %s) RETURNING id",
            (pid, srv.url + "/feed"),
        )
        cluster = await db.fetchone(
            "INSERT INTO clusters(project_id,title,first_seen,last_seen,item_count,state,centroid) "
            "VALUES (%s,'Старый сюжет',now(),now(),1,'drafted',%s) RETURNING id",
            (pid, old_embedding),
        )
        item = await db.fetchone(
            "INSERT INTO items(project_id,source_id,url,url_hash,title,text,published_at,status,embedding,cluster_id) "
            "VALUES (%s,%s,%s,'legacy-hash','Обновление','Короткая новость',now(),'clustered',%s,%s) RETURNING id",
            (pid, source["id"], srv.url + "/article", old_embedding, cluster["id"]),
        )
        draft = await db.fetchone(
            "INSERT INTO drafts(project_id,cluster_ids,title,body,status) VALUES (%s,%s,'Старый заголовок','Старый текст','pending_review') RETURNING id",
            (pid, [cluster["id"]]),
        )
        await ingest.enrich_articles({"source_id": source["id"], "project_id": pid, "include_clustered": True})

    enriched = await db.fetchone("SELECT text,embedding,article_content_status FROM items WHERE id=%s", (item["id"],))
    updated_cluster = await db.fetchone("SELECT centroid FROM clusters WHERE id=%s", (cluster["id"],))
    existing_draft = await db.fetchone("SELECT body FROM drafts WHERE id=%s", (draft["id"],))
    assert enriched["article_content_status"] == "done"
    assert "Облачная инфраструктура" in enriched["text"]
    assert not np.allclose(enriched["embedding"], old_embedding)
    assert not np.allclose(updated_cluster["centroid"], old_embedding)
    assert existing_draft["body"] == "Старый текст"


# ---------- проверка канала не публикует ----------
async def test_channel_check_never_posts_to_telegram(monkeypatch):
    calls = []

    def handler(method, path, headers, body):
        api = path.split("/")[2]
        token = path.split("/")[1][3:]
        calls.append(api)
        js = {"Content-Type": "application/json"}
        if token == "BADTOKEN":
            return 401, js, '{"ok":false,"description":"Unauthorized"}'
        if api == "getMe":
            return 200, js, '{"ok":true,"result":{"id":777,"username":"pulse_bot"}}'
        if api == "getChat":
            return (200, js, '{"ok":true,"result":{"id":-100,"type":"channel","title":"Мой канал"}}') if json.loads(body)["chat_id"] != "@nope" else (400, js, '{"ok":false,"description":"Bad Request: chat not found"}')
        if api == "getChatMember":
            admin = token == "ADMIN"
            return 200, js, json.dumps({"ok": True, "result": {"status": "administrator", "can_post_messages": True} if admin else {"status": "member"}})
        return 200, js, '{"ok":true,"result":{"message_id":1}}'  # sendMessage — не должен вызываться

    async with MiniServer(handler) as srv:
        def ch(chat="@c"):
            return {"type": "telegram", "config": {"chat_id": chat, "token_env": "T_BOT", "api_base": srv.url}}

        monkeypatch.setenv("T_BOT", "ADMIN")
        ok = await publisher.check_channel(ch())
        assert ok["ok"] and "Мой канал" in ok["message"] and "@pulse_bot" in ok["message"]
        monkeypatch.setenv("T_BOT", "MEMBER")
        assert not (await publisher.check_channel(ch()))["ok"]  # не администратор — публиковать в канал нельзя
        monkeypatch.setenv("T_BOT", "ADMIN")
        no_chat = await publisher.check_channel(ch("@nope"))
        assert not no_chat["ok"] and "не видит чат" in no_chat["message"]
        monkeypatch.setenv("T_BOT", "BADTOKEN")
        assert "не принял токен" in (await publisher.check_channel(ch()))["message"]
    assert "sendMessage" not in calls  # главное: в канал ничего не отправлено


async def test_webhook_check_sends_no_payload():
    seen = []

    def handler(method, path, headers, body):
        seen.append((method, body))
        return 405, {}, ""

    async with MiniServer(handler) as srv:
        res = await publisher.check_channel({"type": "webhook", "config": {"url": srv.url + "/hook"}})
    assert res["ok"] and seen == [("OPTIONS", b"")]


async def test_telegram_message_has_no_markers_and_named_sources(monkeypatch):
    sent = {}

    def handler(method, path, headers, body):
        sent.update(json.loads(body))
        return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":{"message_id":5}}'

    draft = {"id": 1, "title": "З", "body": "Факт. [1][2] Ещё факт [1].\n\nИсточники:\nТехДень, 27.09 17:57", 
             "citations": [{"n": 1, "source": "ТехДень", "url": "https://t.test/1"}, {"n": 2, "source": "Дозор", "url": ""}]}
    async with MiniServer(handler) as srv:
        monkeypatch.setenv("T_BOT", "OK")
        res = await publisher.send_telegram({"config": {"chat_id": "@c", "token_env": "T_BOT", "api_base": srv.url}}, draft, {"id": 1})
    assert res.kind == "sent"
    assert "[" not in sent["text"] and "Факт. Ещё факт." in sent["text"]
    assert 'Источники: <a href="https://t.test/1">ТехДень</a>' in sent["text"] and "Дозор" not in sent["text"]


# ---------- смена модели эмбеддингов ----------
async def test_switching_embedding_model_rebuilds_vectors_instead_of_crashing():
    from app.providers import StubProvider, set_provider

    class Other(StubProvider):
        name = "other"
        embed_model = "other-128"
        DIM = 128

    pid = await _pipeline()
    [did] = await run_generation({"project_id": pid, "kind": "post", "top_n": 1})  # у старого сюжета есть черновик — он должен пережить смену
    old_cluster = (await db.fetchone("SELECT cluster_ids FROM drafts WHERE id = %s", (did,)))["cluster_ids"][0]
    try:
        set_provider(Other())
        stats = await process_project(pid)
    finally:
        set_provider(StubProvider())
    assert stats["embedded"] == 45  # всё пересчитано новой моделью
    dims = await db.fetchall("SELECT DISTINCT vector_dims(embedding) AS d FROM items WHERE embedding IS NOT NULL")
    assert dims == [{"d": 128}]
    assert (await db.fetchone("SELECT count(*) AS n FROM drafts"))["n"] == 1  # черновики не тронуты
    assert await db.fetchone("SELECT id FROM clusters WHERE id = %s", (old_cluster,))  # сюжет с черновиком сохранён как история
    assert (await db.fetchone("SELECT count(*) AS n FROM clusters WHERE item_count >= 2 AND state = 'open'"))["n"] >= 1  # и новые сюжеты собраны


async def test_stub_mode_marks_topic_fit_as_not_measured():
    pid = await _pipeline()
    row = await db.fetchone("SELECT score_breakdown FROM clusters WHERE project_id = %s AND item_count >= 2 LIMIT 1", (pid,))
    tf = row["score_breakdown"]["topic_fit"]
    assert tf["neutral"] is True and tf["value"] == 0.5


async def test_embeddings_and_chat_can_use_different_servers(monkeypatch):
    from app.config import get_settings
    from app.providers import OpenAICompatProvider

    hits = {"chat": [], "embed": []}

    def chat_srv(method, path, headers, body):
        hits["chat"].append((path, headers.get("authorization")))
        return 200, {"Content-Type": "application/json"}, json.dumps({"choices": [{"message": {"content": "Заголовок\n\nТекст [1]."}}], "usage": {}})

    def embed_srv(method, path, headers, body):
        hits["embed"].append((path, headers.get("authorization")))
        n = len(json.loads(body)["input"])
        return 200, {"Content-Type": "application/json"}, json.dumps({"data": [{"index": i, "embedding": [1.0, 0.0, 0.5]} for i in range(n)], "usage": {}})

    async with MiniServer(chat_srv) as c, MiniServer(embed_srv) as e:
        s = get_settings()
        monkeypatch.setattr(s, "llm_base_url", c.url + "/v1")
        monkeypatch.setattr(s, "llm_api_key", "chat-key")
        monkeypatch.setattr(s, "llm_chat_enabled", True)
        monkeypatch.setattr(s, "embed_base_url", e.url + "/v1")
        monkeypatch.setattr(s, "embed_api_key", "embed-key")
        prov = OpenAICompatProvider()
        vecs = await prov.embed(["a", "b"])
        text = await prov.chat("sys", "user")
    assert vecs.shape == (2, 3) and text.startswith("Заголовок")
    assert hits["embed"] == [("/v1/embeddings", "Bearer embed-key")]
    assert hits["chat"] == [("/v1/chat/completions", "Bearer chat-key")]


async def test_telegram_sends_photo_for_short_post_and_link_preview_for_long(monkeypatch):
    calls = []

    def handler(method, path, headers, body):
        if path.startswith("/img/"):
            return 200, {"Content-Type": "image/png"}, b"\x89PNG-bytes"
        calls.append((path.rsplit("/", 1)[-1], headers.get("content-type", ""), body))
        return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":{"message_id":7}}'

    async with MiniServer(handler) as srv:
        monkeypatch.setenv("T_BOT", "OK")
        channel = {"config": {"chat_id": "@c", "token_env": "T_BOT", "api_base": srv.url}}
        short = {"id": 1, "title": "З", "body": "Короткий пост", "citations": [], "image_url": f"{srv.url}/img/1.png"}
        assert (await publisher.send_telegram(channel, short, {"id": 1})).kind == "sent"
        assert calls[-1][0] == "sendPhoto" and calls[-1][1].startswith("multipart/form-data") and b"PNG-bytes" in calls[-1][2]

        long_draft = {**short, "id": 2, "body": "Длинный пост. " * 100}
        assert (await publisher.send_telegram(channel, long_draft, {"id": 1})).kind == "sent"
        method, _, body = calls[-1]
        assert method == "sendMessage" and json.loads(body)["link_preview_options"]["show_above_text"] is True

        broken = {**short, "id": 3, "image_url": f"{srv.url}/missing.txt"}
        assert (await publisher.send_telegram(channel, broken, {"id": 1})).kind == "sent"
        assert calls[-1][0] == "sendMessage"  # картинка не скачалась — пост уходит текстом, а не теряется


async def test_scheduled_generation_jobs_are_deduplicated():
    from app import jobs

    first = await jobs.enqueue("generate", {"project_id": 1}, dedupe_key="schedule:1")
    second = await jobs.enqueue("generate", {"project_id": 1}, dedupe_key="schedule:1")
    assert first and second is None


async def test_repeated_feed_fetch_backfills_image_without_counting_as_new_item():
    pid = await make_project()
    src = await db.fetchone("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'S', 'https://s.test/rss') RETURNING id", (pid,))
    entry = {"title": "Новость", "url": "https://s.test/1", "text": "текст", "published_at": utcnow()}
    assert (await ingest.insert_items(pid, src["id"], [entry])).inserted == 1
    assert (await db.fetchone("SELECT image_url FROM items WHERE project_id = %s", (pid,)))["image_url"] is None
    assert (await ingest.insert_items(pid, src["id"], [{**entry, "image_url": "https://cdn.test/a.jpg"}])).inserted == 0
    assert (await db.fetchone("SELECT image_url FROM items WHERE project_id = %s", (pid,)))["image_url"] == "https://cdn.test/a.jpg"
    assert (await ingest.insert_items(pid, src["id"], [{**entry, "image_url": "https://cdn.test/other.jpg"}])).inserted == 0
    assert (await db.fetchone("SELECT image_url FROM items WHERE project_id = %s", (pid,)))["image_url"] == "https://cdn.test/a.jpg"
    await db.execute("DELETE FROM sources WHERE id = %s", (src["id"],))
    replacement = await db.fetchone("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Search', 'https://search.test/rss') RETURNING id", (pid,))
    result = await ingest.insert_items(pid, replacement["id"], [{**entry, "publisher_name": "The Example"}])
    assert result.inserted == 0 and result.updated == 1
    attached = await db.fetchone("SELECT source_id, publisher_name FROM items WHERE project_id = %s", (pid,))
    assert attached == {"source_id": replacement["id"], "publisher_name": "The Example"}
