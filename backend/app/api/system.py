"""Дашборд, события (SSE), пользователи, состояние системы, аудит."""
import asyncio
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, jobs
from ..config import get_settings
from ..events import broker
from ..providers import classify_model, list_remote_models, load_runtime_overrides, pull_remote_model, refresh_provider, save_runtime_overrides
from ..scheduler import next_fire
from ..security import admin, audit, hash_password, viewer
from ..textutil import utcnow

router = APIRouter(tags=["system"])


@router.get("/dashboard")
async def dashboard(project_id: int, days: int = Query(7, ge=1, le=90), user=Depends(viewer)):
    now = utcnow()
    since, prev_since = now - timedelta(days=days), now - timedelta(days=2 * days)
    q = db.fetchone
    cur = await q("SELECT count(*) AS n FROM items WHERE project_id = %s AND published_at >= %s", (project_id, since))
    prev = await q("SELECT count(*) AS n FROM items WHERE project_id = %s AND published_at >= %s AND published_at < %s", (project_id, prev_since, since))
    open_clusters = await q(
        "SELECT count(*) AS n FROM clusters c JOIN projects p ON p.id = c.project_id "
        "WHERE c.project_id = %s AND c.state = 'open' AND c.item_count >= p.min_items", (project_id,)
    )
    pending = await q("SELECT count(*) AS n FROM drafts WHERE project_id = %s AND status = 'pending_review'", (project_id,))
    sent = await q(
        "SELECT count(DISTINCT p.draft_id) AS n FROM publications p JOIN drafts d ON d.id = p.draft_id WHERE d.project_id = %s AND p.status = 'sent' AND p.sent_at >= %s",
        (project_id, since),
    )
    rows = await db.fetchall(
        "SELECT (published_at AT TIME ZONE 'UTC')::date AS d, count(*) AS n FROM items WHERE project_id = %s AND published_at >= %s GROUP BY 1",
        (project_id, since),
    )
    by_day = {r["d"]: r["n"] for r in rows}
    today = now.date()
    daily = [{"date": (today - timedelta(days=i)).isoformat(), "items": by_day.get(today - timedelta(days=i), 0)} for i in range(days - 1, -1, -1)]

    top = await db.fetchall(
        "SELECT c.id, c.title, c.score, c.item_count, c.source_count, c.last_seen FROM clusters c JOIN projects p ON p.id = c.project_id "
        "WHERE c.project_id = %s AND c.state = 'open' AND c.item_count >= p.min_items "
        "AND (p.topic_threshold <= 0 OR COALESCE((c.score_breakdown->'topic_fit'->>'neutral')::boolean, false) "
        "OR COALESCE((c.score_breakdown->'topic_fit'->>'value')::float, 0.5) >= p.topic_threshold) "
        "ORDER BY COALESCE((c.score_breakdown->'topic_fit'->>'value')::float, 0.5) DESC, c.score DESC NULLS LAST LIMIT 6",
        (project_id,),
    )
    queue = await db.fetchall(
        "SELECT p.id, p.status, p.due_at, d.title FROM publications p JOIN drafts d ON d.id = p.draft_id "
        "WHERE d.project_id = %s AND p.status IN ('pending','sending','unknown','failed') ORDER BY p.id DESC LIMIT 5",
        (project_id,),
    )
    review = await db.fetchall(
        "SELECT id, title, created_at, status FROM drafts WHERE project_id = %s AND status = 'pending_review' ORDER BY id DESC LIMIT 5",
        (project_id,),
    )
    generating = await db.fetchall(
        "SELECT d.id, d.kind, COALESCE(NULLIF(d.title, ''), "
        "(SELECT string_agg(c.title, ' · ' ORDER BY c.score DESC NULLS LAST, c.id) FROM clusters c WHERE c.id = ANY(d.cluster_ids)), "
        "CASE WHEN d.kind = 'digest' THEN 'Сборка дайджеста' ELSE 'Подготовка поста' END) AS title, d.created_at "
        "FROM drafts d WHERE d.project_id = %s AND d.status = 'generating' ORDER BY d.id DESC LIMIT 5",
        (project_id,),
    )
    generation_queue = await jobs.queued_generations(project_id)
    schedules = await db.fetchall("SELECT id, name, cron, tz, kind FROM schedules WHERE project_id = %s AND enabled", (project_id,))
    upcoming = sorted(({"id": s["id"], "name": s["name"], "kind": s["kind"], "at": next_fire(s["cron"], s["tz"], now)} for s in schedules), key=lambda x: x["at"])[:3]

    hb = await q("SELECT updated_at FROM kv WHERE key = 'worker_heartbeat'")
    src = await q("SELECT max(last_ok_at) AS last_ok, count(*) FILTER (WHERE last_error IS NOT NULL AND enabled) AS failing, count(*) FILTER (WHERE type = 'rss' AND enabled) AS rss_enabled FROM sources WHERE project_id = %s", (project_id,))
    jobs_row = await q("SELECT count(*) FILTER (WHERE status = 'queued') AS queued, count(*) FILTER (WHERE status = 'failed' AND finished_at > now() - interval '24 hours') AS failed FROM jobs")
    llm = await q(
        "SELECT count(*) AS calls, COALESCE(avg(latency_ms) FILTER (WHERE kind = 'chat'), 0)::int AS avg_chat_ms, "
        "COALESCE(sum(prompt_tokens), 0) AS prompt_tokens, COALESCE(sum(completion_tokens), 0) AS completion_tokens, "
        "count(*) FILTER (WHERE NOT ok) AS errors FROM llm_calls WHERE (project_id = %s OR project_id IS NULL) AND created_at >= %s",
        (project_id, since),
    )
    p = await refresh_provider()
    return {
        "metrics": {"items": cur["n"], "items_prev": prev["n"], "open_clusters": open_clusters["n"], "pending_drafts": pending["n"], "published": sent["n"]},
        "daily": daily,
        "top_clusters": top,
        "review": review,
        "generating": generating,
        "generation_queue": generation_queue,
        "queue": queue,
        "upcoming": upcoming,
        "pipeline": {
            "worker_seen_seconds": int((now - hb["updated_at"]).total_seconds()) if hb else None,
            "last_ingest_ok": src["last_ok"],
            "failing_sources": src["failing"],
            "rss_enabled": src["rss_enabled"],
            "jobs_queued": jobs_row["queued"],
            "jobs_failed_24h": jobs_row["failed"],
        },
        "llm": {**llm, "provider": p.name, "chat_model": p.chat_model, "embed_model": p.embed_model},
    }


@router.get("/events")
async def events(request: Request, user=Depends(viewer)):
    q = broker.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            broker.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/system/health")
async def health(deep: bool = False, user=Depends(viewer)):
    now = utcnow()
    hb = await db.fetchone("SELECT updated_at FROM kv WHERE key = 'worker_heartbeat'")
    p = await refresh_provider()
    out = {
        "db": True,
        "worker_seen_seconds": int((now - hb["updated_at"]).total_seconds()) if hb else None,
        "provider": p.name,
    }
    if deep:
        out["model"] = await p.ping()
    return out


@router.get("/system/config")
async def system_config(user=Depends(admin)):
    s = get_settings()
    over = await load_runtime_overrides()
    p = await refresh_provider()
    return {
        "llm_provider": s.llm_provider,
        "llm_base_url": s.llm_base_url if s.llm_provider == "openai" else None,
        "embed_base_url": (s.embed_base_url or None) if s.llm_provider == "openai" else None,
        "llm_model": p.chat_model,
        "embed_model": p.embed_model,
        "llm_model_env": s.llm_model,
        "embed_model_env": s.embed_model,
        "runtime_override": bool(over.get("llm_model") or over.get("embed_model")),
        "telegram_token_configured": bool(s.telegram_bot_token),
        "allow_private_urls": s.allow_private_urls,
        "retention_days": s.retention_days,
        "worker_concurrency": s.worker_concurrency,
    }


class ModelConfigBody(BaseModel):
    llm_model: str | None = Field(None, min_length=1, max_length=200)
    embed_model: str | None = Field(None, min_length=1, max_length=200)


@router.patch("/system/config")
async def patch_system_config(body: ModelConfigBody, user=Depends(admin)):
    """Смена моделей без правки .env и перезапуска. Значения пишутся в kv и подхватываются api/worker."""
    s = get_settings()
    if s.llm_provider != "openai":
        raise HTTPException(400, "Смена модели в интерфейсе доступна при LLM_PROVIDER=openai. Сейчас включён stub.")
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(400, "Укажите llm_model и/или embed_model")
    await save_runtime_overrides(patch)
    p = await refresh_provider()
    await audit(user, "update", "system", None, patch)
    return {"ok": True, "llm_model": p.chat_model, "embed_model": p.embed_model}


@router.get("/system/models")
async def system_models(user=Depends(admin)):
    s = get_settings()
    if s.llm_provider != "openai":
        return {"models": [], "hint": "Список моделей доступен при LLM_PROVIDER=openai (Ollama или другой OpenAI-совместимый сервер)."}
    try:
        models = await list_remote_models()
    except Exception as e:
        raise HTTPException(502, f"Не удалось получить список моделей: {e}")
    return {"models": models}


class PullBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.post("/system/models/pull")
async def system_pull_model(body: PullBody, user=Depends(admin)):
    try:
        result = await pull_remote_model(body.name.strip())
    except Exception as e:
        raise HTTPException(502, f"Не удалось скачать модель: {e}")
    await audit(user, "pull", "model", None, {"name": body.name})
    return {"ok": True, "result": result}


@router.get("/audit")
async def audit_log(limit: int = Query(100, le=500), user=Depends(admin)):
    return await db.fetchall("SELECT id, user_email, action, entity, entity_id, detail, created_at FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))


# ---------- users ----------
class UserBody(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    role: Literal["admin", "editor", "viewer"] = "viewer"


class UserPatch(BaseModel):
    role: Literal["admin", "editor", "viewer"] | None = None
    password: str | None = Field(None, min_length=8, max_length=200)


@router.get("/users")
async def list_users(user=Depends(admin)):
    return await db.fetchall("SELECT id, email, role, created_at FROM users ORDER BY id")


@router.post("/users", status_code=201)
async def create_user(body: UserBody, user=Depends(admin)):
    if await db.fetchone("SELECT 1 FROM users WHERE lower(email) = lower(%s)", (body.email,)):
        raise HTTPException(409, "Пользователь с такой почтой уже есть")
    row = await db.fetchone(
        "INSERT INTO users(email, password_hash, role) VALUES (%s,%s,%s) RETURNING id, email, role, created_at", (body.email.lower(), hash_password(body.password), body.role)
    )
    await audit(user, "create", "user", row["id"], {"role": body.role})
    return row


async def _would_remove_last_admin(uid: int) -> bool:
    row = await db.fetchone("SELECT (SELECT role FROM users WHERE id = %s) AS role, (SELECT count(*) FROM users WHERE role = 'admin') AS admins", (uid,))
    return bool(row and row["role"] == "admin" and row["admins"] <= 1)


@router.patch("/users/{uid}")
async def update_user(uid: int, body: UserPatch, user=Depends(admin)):
    if body.role and body.role != "admin" and await _would_remove_last_admin(uid):
        raise HTTPException(409, "Нельзя понизить последнего администратора")
    if body.role:
        await db.execute("UPDATE users SET role = %s WHERE id = %s", (body.role, uid))
    if body.password:
        await db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(body.password), uid))
    await audit(user, "update", "user", uid, {"role": body.role, "password_changed": bool(body.password)})
    return {"ok": True}


@router.delete("/users/{uid}", status_code=204)
async def delete_user(uid: int, user=Depends(admin)):
    if uid == user["id"]:
        raise HTTPException(409, "Нельзя удалить самого себя")
    if await _would_remove_last_admin(uid):
        raise HTTPException(409, "Нельзя удалить последнего администратора")
    await db.execute("DELETE FROM users WHERE id = %s", (uid,))
    await audit(user, "delete", "user", uid)
