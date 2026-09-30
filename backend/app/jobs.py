"""Очередь задач на PostgreSQL (SELECT ... FOR UPDATE SKIP LOCKED). Без внешнего брокера."""
import logging
from contextvars import ContextVar
from typing import Awaitable, Callable

from psycopg.types.json import Jsonb

from . import db, events
from .textutil import utcnow

log = logging.getLogger("pulse.jobs")
Handler = Callable[[dict], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


_current: ContextVar[dict | None] = ContextVar("current_job", default=None)


def current_age_seconds() -> float:
    """Сколько секунд выполняемая задача живёт в очереди (для ограничения ожиданий внутри обработчика)."""
    job = _current.get()
    if not job or not job.get("created_at"):
        return 0.0
    return (utcnow() - job["created_at"]).total_seconds()


class Retry(Exception):
    """Отложить задачу без списания попытки (например, занят проект)."""

    def __init__(self, delay: float = 5.0):
        self.delay = delay


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


async def enqueue(kind: str, payload: dict, dedupe_key: str | None = None, delay_seconds: float = 0) -> int | None:
    row = await db.fetchone(
        "INSERT INTO jobs(kind, payload, dedupe_key, run_at) VALUES (%s, %s, %s, now() + make_interval(secs => %s)) "
        "ON CONFLICT (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL DO NOTHING RETURNING id",
        (kind, Jsonb(payload), dedupe_key, delay_seconds),
    )
    if row and kind == "generate":
        await events.notify("generation", project_id=payload.get("project_id"))
    return row["id"] if row else None


async def queued_generations(project_id: int) -> list[dict]:
    return await db.fetchall(
        "SELECT j.id, j.status, j.created_at, COALESCE(j.payload->>'kind', 'post') AS kind, "
        "COALESCE(topics.title, CASE WHEN j.payload ? 'schedule_id' THEN 'Плановая генерация' ELSE 'Подбор сюжета' END) AS title "
        "FROM jobs j LEFT JOIN LATERAL ("
        "SELECT string_agg(c.title, ' · ' ORDER BY c.score DESC NULLS LAST, c.id) AS title "
        "FROM jsonb_array_elements_text(COALESCE(j.payload->'cluster_ids', '[]'::jsonb)) AS cid(value) "
        "JOIN clusters c ON c.id = cid.value::bigint"
        ") topics ON true "
        "WHERE j.kind = 'generate' AND j.status = 'queued' AND j.payload->>'project_id' = %s "
        "ORDER BY j.created_at, j.id",
        (str(project_id),),
    )


def _kind_filter(include_kinds: tuple[str, ...] | None, exclude_kinds: tuple[str, ...] | None) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if include_kinds:
        clauses.append(f"kind = ANY(%s)")
        params.append(list(include_kinds))
    if exclude_kinds:
        clauses.append(f"kind <> ALL(%s)")
        params.append(list(exclude_kinds))
    return (" AND ".join(clauses), params)


async def claim(include_kinds: tuple[str, ...] | None = None, exclude_kinds: tuple[str, ...] | None = None) -> dict | None:
    kind_where, kind_params = _kind_filter(include_kinds, exclude_kinds)
    where = "status = 'queued' AND run_at <= now()"
    if kind_where:
        where = f"{where} AND {kind_where}"
    return await db.fetchone(
        "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_until = now() + interval '10 minutes' "
        f"WHERE id = (SELECT id FROM jobs WHERE {where} "
        "ORDER BY CASE kind WHEN 'enrich_articles' THEN 0 WHEN 'generate' THEN 1 "
        "WHEN 'process_project' THEN 2 WHEN 'ingest_source' THEN 3 ELSE 4 END, run_at, id "
        "FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
        kind_params,
    )


async def complete(job_id: int) -> None:
    await db.execute("UPDATE jobs SET status = 'done', finished_at = now(), locked_until = NULL WHERE id = %s", (job_id,))


async def fail(job: dict, error: str) -> None:
    if job["attempts"] >= job["max_attempts"]:
        await db.execute(
            "UPDATE jobs SET status = 'failed', finished_at = now(), locked_until = NULL, last_error = %s WHERE id = %s",
            (error[:1000], job["id"]),
        )
    else:
        delay = min(300, 5 * 2 ** job["attempts"])
        await db.execute(
            "UPDATE jobs SET status = 'queued', locked_until = NULL, last_error = %s, run_at = now() + make_interval(secs => %s) WHERE id = %s",
            (error[:1000], delay, job["id"]),
        )


async def defer(job: dict, delay: float) -> None:
    await db.execute(
        "UPDATE jobs SET status = 'queued', locked_until = NULL, attempts = GREATEST(attempts - 1, 0), "
        "run_at = now() + make_interval(secs => %s) WHERE id = %s",
        (delay, job["id"]),
    )


async def reclaim_expired() -> int:
    return await db.execute("UPDATE jobs SET status = 'queued', locked_until = NULL WHERE status = 'running' AND locked_until < now()")


async def run_one(include_kinds: tuple[str, ...] | None = None, exclude_kinds: tuple[str, ...] | None = None) -> bool:
    """Берёт одну задачу и выполняет её. Возвращает False, если очередь пуста."""
    job = await claim(include_kinds=include_kinds, exclude_kinds=exclude_kinds)
    if not job:
        return False
    fn = HANDLERS.get(job["kind"])
    token = _current.set(job)
    try:
        if fn is None:
            raise RuntimeError(f"нет обработчика для {job['kind']}")
        await fn(job["payload"])
        await complete(job["id"])
    except Retry as r:
        await defer(job, r.delay)
    except Exception as e:
        log.exception("job %s (%s) failed", job["id"], job["kind"])
        await fail(job, f"{type(e).__name__}: {e}")
    finally:
        _current.reset(token)
    return True
