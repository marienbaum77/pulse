"""Контент-план: слоты расписаний на ближайшие дни с их черновиками и итогами прошлых срабатываний."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import db
from ..pipeline.process import get_project, select_clusters
from ..scheduler import GRACE
from ..security import viewer
from ..textutil import utcnow

router = APIRouter(tags=["plan"])
HISTORY = timedelta(hours=24)
MAX_SLOTS_PER_SCHEDULE = 200
CANDIDATE_POOL = 50


def schedule_slots(cron: str, tz: str, start: datetime, end: datetime) -> list[datetime]:
    """Моменты срабатывания cron в интервале (start, end] в UTC."""
    it = croniter(cron, start.astimezone(ZoneInfo(tz)))
    out = []
    for _ in range(MAX_SLOTS_PER_SCHEDULE):
        at = it.get_next(datetime).astimezone(timezone.utc)
        if at > end:
            break
        out.append(at)
    return out


@router.get("/content-plan")
async def content_plan(project_id: int, days: int = Query(7, ge=1, le=31), user=Depends(viewer)):
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    now = utcnow()
    start, end = now - HISTORY, now + timedelta(days=days)
    schedules = await db.fetchall("SELECT * FROM schedules WHERE project_id = %s AND enabled ORDER BY id", (project_id,))
    fires = {
        (r["schedule_id"], r["fire_time"]): r
        for r in await db.fetchall(
            "SELECT f.schedule_id, f.fire_time, f.outcome FROM schedule_fires f JOIN schedules s ON s.id = f.schedule_id "
            "WHERE s.project_id = %s AND f.fire_time > %s",
            (project_id, start),
        )
    }
    drafts: dict[tuple[int, datetime], list[dict]] = {}
    for d in await db.fetchall(
        "SELECT d.id, d.title, d.status, d.kind, d.schedule_id, d.scheduled_for, "
        "(SELECT COALESCE(json_agg(json_build_object('status', p.status, 'sent_at', p.sent_at) ORDER BY p.id), '[]'::json) "
        " FROM publications p WHERE p.draft_id = d.id) AS publications "
        "FROM drafts d WHERE d.project_id = %s AND d.schedule_id IS NOT NULL AND d.scheduled_for > %s ORDER BY d.id",
        (project_id, start),
    ):
        drafts.setdefault((d.pop("schedule_id"), d.pop("scheduled_for")), []).append(d)

    # Сколько кандидатов есть прямо сейчас для каждого фильтра слотов: видно, хватит ли материалов на план.
    candidates: dict[tuple[str, ...], int] = {}
    for s in schedules:
        key = tuple(sorted(s["content_kinds"]))
        if key not in candidates:
            candidates[key] = len(await select_clusters(project, CANDIDATE_POOL, list(key)))

    slots = []
    for s in schedules:
        # История — с момента создания расписания: более ранние слоты не «пропущены», их просто не было.
        # Начинаем на GRACE раньше: планировщик догоняет задним числом слот, пропущенный не более чем на GRACE.
        # Отсчёт именно отсюда, иначе частое расписание исчерпало бы лимит слотов ещё до своего создания.
        for at in schedule_slots(s["cron"], s["tz"], max(start, s["created_at"] - GRACE), end):
            fire = fires.get((s["id"], at))
            if at <= s["created_at"] and not fire and (s["id"], at) not in drafts:
                continue
            slots.append({
                "schedule_id": s["id"], "schedule_name": s["name"], "at": at, "kind": s["kind"], "top_n": s["top_n"],
                "content_kinds": list(s["content_kinds"]), "lead_minutes": s["lead_minutes"],
                "prepare_at": at - timedelta(minutes=s["lead_minutes"]),
                "outcome": fire["outcome"] if fire else None,
                "drafts": drafts.get((s["id"], at), []),
                "candidates": candidates[tuple(sorted(s["content_kinds"]))],
            })
    slots.sort(key=lambda slot: (slot["at"], slot["schedule_id"]))
    backlog = await db.fetchall(
        "SELECT d.id, d.title, d.kind, d.created_at FROM drafts d WHERE d.project_id = %s AND d.status = 'pending_review' "
        "AND d.scheduled_for IS NULL ORDER BY d.created_at DESC LIMIT 20",
        (project_id,),
    )
    return {"now": now, "slots": slots, "backlog": backlog}
