"""Расписания генерации."""
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import db, jobs
from ..scheduler import next_fire
from ..security import audit, editor, viewer

router = APIRouter(tags=["schedules"])

# ---------- schedules ----------
class ScheduleBody(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=120)
    cron: str
    tz: str = "UTC"
    kind: Literal["post", "digest"] = "digest"
    top_n: int = Field(5, ge=1, le=10)
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, v):
        if not croniter.is_valid(v) or len(v.split()) != 5:
            raise ValueError("Cron-выражение из 5 полей, например: 0 9 * * *")
        return v

    @field_validator("tz")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Неизвестный часовой пояс, пример: Europe/Moscow")
        return v


def _with_next(row: dict) -> dict:
    row["next_fire"] = next_fire(row["cron"], row["tz"], utcnow()) if row["enabled"] else None
    return row


@router.get("/schedules")
async def list_schedules(project_id: int, user=Depends(viewer)):
    rows = await db.fetchall("SELECT * FROM schedules WHERE project_id = %s ORDER BY id", (project_id,))
    return [_with_next(r) for r in rows]


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleBody, user=Depends(editor)):
    d = body.model_dump()
    row = await db.fetchone(
        f"INSERT INTO schedules({', '.join(d)}) VALUES ({', '.join(['%s'] * len(d))}) RETURNING *", list(d.values())
    )
    await audit(user, "create", "schedule", row["id"])
    return _with_next(row)


@router.put("/schedules/{sid}")
async def update_schedule(sid: int, body: ScheduleBody, user=Depends(editor)):
    d = body.model_dump()
    d.pop("project_id")
    row = await db.fetchone(f"UPDATE schedules SET {', '.join(f'{k} = %s' for k in d)} WHERE id = %s RETURNING *", [*d.values(), sid])
    if not row:
        raise HTTPException(404, "Расписание не найдено")
    await audit(user, "update", "schedule", sid)
    return _with_next(row)


@router.delete("/schedules/{sid}", status_code=204)
async def delete_schedule(sid: int, user=Depends(editor)):
    await db.execute("DELETE FROM schedules WHERE id = %s", (sid,))
    await audit(user, "delete", "schedule", sid)


@router.post("/schedules/{sid}/run")
async def run_schedule(sid: int, user=Depends(editor)):
    s = await db.fetchone("SELECT * FROM schedules WHERE id = %s", (sid,))
    if not s:
        raise HTTPException(404, "Расписание не найдено")
    job = await jobs.enqueue(
        "generate",
        {
            "project_id": s["project_id"],
            "kind": s["kind"],
            "top_n": s["top_n"],
            "user_id": user["id"],
        },
    )
    return {"job_id": job}
