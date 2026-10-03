"""Процесс-воркер: планировщик, обработчики задач, публикатор. Запуск: python -m app.worker"""
import asyncio
import logging
import signal

from . import db, jobs, publisher, scheduler
from .config import get_settings
from .pipeline import generate, ingest, process  # noqa: F401  (регистрируют обработчики задач)

log = logging.getLogger("pulse.worker")

# Сбор лент дешёвый и не должен ждать долгую обработку проектов; генерация — плановая и тоже получает свою полосу.
INGEST_KINDS = ("ingest_source", "enrich_articles")
GENERATE_KINDS = ("generate",)
Lane = tuple[tuple[str, ...] | None, tuple[str, ...] | None]  # (include_kinds, exclude_kinds)


def plan_lanes(concurrency: int) -> list[Lane]:
    """Делит воркеры на полосы: 1 → общая; 2 → сбор + общая; 3+ → сбор + генерация + общие (без generate).
    Общие полосы тоже берут ingest_source первыми (см. приоритет в jobs.claim)."""
    n = max(1, concurrency)
    if n == 1:
        return [(None, None)]
    lanes: list[Lane] = [(INGEST_KINDS, None)]
    if n == 2:
        return lanes + [(None, None)]
    lanes.append((GENERATE_KINDS, None))
    lanes += [(None, GENERATE_KINDS)] * (n - 2)
    return lanes


async def job_loop(stop: asyncio.Event, idx: int, *, include_kinds: tuple[str, ...] | None = None, exclude_kinds: tuple[str, ...] | None = None) -> None:
    while not stop.is_set():
        try:
            worked = await jobs.run_one(include_kinds=include_kinds, exclude_kinds=exclude_kinds)
        except Exception:
            log.exception("job loop error")
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass


async def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.open_pool()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    lanes = plan_lanes(s.worker_concurrency)
    log.info(
        "worker started (provider=%s, concurrency=%d, lanes=%s)",
        s.llm_provider,
        s.worker_concurrency,
        ", ".join("all" if not inc and not exc else f"only {'/'.join(inc)}" if inc else f"not {'/'.join(exc)}" for inc, exc in lanes),
    )
    tasks = [asyncio.create_task(scheduler.loop(stop)), asyncio.create_task(publisher.loop(stop))]
    tasks += [
        asyncio.create_task(job_loop(stop, i, include_kinds=inc, exclude_kinds=exc))
        for i, (inc, exc) in enumerate(lanes)
    ]
    await stop.wait()
    log.info("shutting down")
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
