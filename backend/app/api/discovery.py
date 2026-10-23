"""Автоподбор источников: запуск поиска кандидатов по теме канала и решения редактора по ним."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, jobs
from ..pipeline import discovery  # noqa: F401  (регистрирует обработчик задачи)
from ..security import audit, editor, viewer
from .sources import add_connector_source

router = APIRouter(tags=["discovery"])
# Сколько опрашивать предложенный источник по умолчанию: подборочный контент не требует частого опроса.
POLL_MINUTES = {"youtube": 180, "site": 360, "reddit": 180, "telegram": 60}


class DiscoveryBody(BaseModel):
    project_id: int


class AcceptBody(BaseModel):
    authority: float = Field(0.5, ge=0, le=1)


@router.get("/discovery")
async def get_discovery(project_id: int, user=Depends(viewer)):
    status = await db.fetchone("SELECT value FROM kv WHERE key = %s", (f"discovery:{project_id}",))
    queued = await db.fetchone(
        "SELECT 1 FROM jobs WHERE kind = 'discover_sources' AND status IN ('queued','running') AND payload->>'project_id' = %s LIMIT 1",
        (str(project_id),),
    )
    suggestions = await db.fetchall(
        "SELECT id, type, value, name, reason, score, hits, samples, created_at FROM source_suggestions "
        "WHERE project_id = %s AND status = 'new' ORDER BY score DESC, hits DESC, id",
        (project_id,),
    )
    run = status["value"] if status else None
    if queued and (not run or run.get("status") != "running"):
        run = {**(run or {}), "status": "queued"}
    return {"run": run, "suggestions": suggestions}


@router.post("/discovery", status_code=202)
async def start_discovery(body: DiscoveryBody, user=Depends(editor)):
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = %s", (body.project_id,)):
        raise HTTPException(404, "Проект не найден")
    job = await jobs.enqueue("discover_sources", {"project_id": body.project_id}, dedupe_key=f"discover:{body.project_id}")
    await audit(user, "discover", "sources", None, {"project_id": body.project_id})
    return {"queued": job is not None}


async def _suggestion(sid: int) -> dict:
    row = await db.fetchone("SELECT * FROM source_suggestions WHERE id = %s", (sid,))
    if not row:
        raise HTTPException(404, "Предложение не найдено")
    if row["status"] != "new":
        raise HTTPException(409, "По этому предложению уже принято решение")
    return row


@router.post("/discovery/suggestions/{sid}/accept", status_code=201)
async def accept_suggestion(sid: int, body: AcceptBody, user=Depends(editor)):
    s = await _suggestion(sid)
    try:
        source = await add_connector_source(
            user, s["project_id"], s["type"], s["value"], {}, authority=body.authority, poll_minutes=POLL_MINUTES[s["type"]],
        )
    except HTTPException as e:
        if e.status_code == 409:  # источник уже есть — предложение больше не нужно
            await db.execute("UPDATE source_suggestions SET status = 'dismissed', updated_at = now() WHERE id = %s", (sid,))
        raise
    await db.execute("UPDATE source_suggestions SET status = 'accepted', source_id = %s, updated_at = now() WHERE id = %s", (source["id"], sid))
    return source


@router.post("/discovery/suggestions/{sid}/dismiss")
async def dismiss_suggestion(sid: int, user=Depends(editor)):
    await _suggestion(sid)
    await db.execute("UPDATE source_suggestions SET status = 'dismissed', updated_at = now() WHERE id = %s", (sid,))
    return {"ok": True}
