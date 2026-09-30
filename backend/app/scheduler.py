"""Планировщик: опрос источников, cron-расписания публикаций, обслуживание."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from psycopg.types.json import Jsonb

from . import db, jobs, publisher
from .config import get_settings
from .pipeline import generate
from .textutil import utcnow

log = logging.getLogger("pulse.scheduler")
GRACE = timedelta(minutes=10)  # пропущенный запуск старше этого срока не «догоняем»
MAINTENANCE_EVERY = timedelta(minutes=10)
_last_maintenance: datetime | None = None


def last_fire(cron: str, tz: str, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return croniter(cron, local).get_prev(datetime).astimezone(timezone.utc)


def next_fire(cron: str, tz: str, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return croniter(cron, local).get_next(datetime).astimezone(timezone.utc)


async def fire_schedules(now: datetime) -> int:
    fired = 0
    rows = await db.fetchall("SELECT s.* FROM schedules s JOIN projects p ON p.id = s.project_id WHERE s.enabled AND p.active")
    for s in rows:
        try:
            prev = last_fire(s["cron"], s["tz"], now)
        except Exception:
            log.warning("bad schedule %s", s["id"])
            continue
        if now - prev > GRACE:
            continue
        # UNIQUE (schedule_id, fire_time): даже при двух воркерах запуск произойдёт один раз
        ins = await db.fetchone("INSERT INTO schedule_fires(schedule_id, fire_time) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING fire_time", (s["id"], prev))
        if ins:
            await jobs.enqueue("generate", {"project_id": s["project_id"], "kind": s["kind"], "top_n": s["top_n"], "scheduled": True, "schedule_id": s["id"]}, dedupe_key=f"schedule:{s['id']}")
            await db.execute("UPDATE schedules SET last_fired_at = now() WHERE id = %s", (s["id"],))
            fired += 1
    return fired


async def poll_sources() -> int:
    due = await db.fetchall(
        "UPDATE sources SET next_poll_at = now() + make_interval(mins => poll_minutes) "
        "WHERE enabled AND type = 'rss' AND next_poll_at <= now() AND project_id IN (SELECT id FROM projects WHERE active) RETURNING id"
    )
    for s in due:
        await jobs.enqueue("ingest_source", {"source_id": s["id"]}, dedupe_key=f"ingest:{s['id']}")
    return len(due)


async def maintenance() -> None:
    days = get_settings().retention_days
    await db.execute("DELETE FROM items WHERE published_at < now() - make_interval(days => %s)", (days,))
    await db.execute(
        "DELETE FROM clusters c WHERE c.state IN ('closed', 'excluded') AND c.last_seen < now() - make_interval(days => %s) "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE c.id = ANY(d.cluster_ids))",
        (days,),
    )
    await db.execute("DELETE FROM jobs WHERE status IN ('done', 'failed') AND finished_at < now() - interval '7 days'")
    await db.execute("DELETE FROM llm_calls WHERE created_at < now() - interval '30 days'")
    # пересчёт свежести и закрытие устаревших сюжетов, даже если новых материалов нет
    for p in await db.fetchall("SELECT id FROM projects WHERE active"):
        await jobs.enqueue("process_project", {"project_id": p["id"]}, dedupe_key=f"process:{p['id']}")


async def tick() -> None:
    global _last_maintenance
    now = utcnow()
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES ('worker_heartbeat', %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (Jsonb({"pid": os.getpid()}),),
    )
    await jobs.reclaim_expired()
    await publisher.recover_stale()
    await generate.recover_stuck_generations()
    await poll_sources()
    await fire_schedules(now)
    if _last_maintenance is None or now - _last_maintenance > MAINTENANCE_EVERY:
        _last_maintenance = now
        await maintenance()


async def loop(stop: asyncio.Event) -> None:
    interval = get_settings().tick_seconds
    while not stop.is_set():
        try:
            await tick()
        except Exception:
            log.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
