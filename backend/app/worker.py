"""Процесс-воркер: планировщик, обработчики задач, публикатор. Запуск: python -m app.worker"""
import asyncio
import logging
import signal

from . import db, jobs, publisher, scheduler
from .config import get_settings
from .pipeline import generate, ingest, process  # noqa: F401  (регистрируют обработчики задач)

log = logging.getLogger("pulse.worker")


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
    priority_workers = 1 if s.worker_concurrency > 1 else 0
    general_workers = max(1, s.worker_concurrency - priority_workers)
    log.info(
        "worker started (provider=%s, concurrency=%d, generate_workers=%d, general_workers=%d)",
        s.llm_provider,
        s.worker_concurrency,
        priority_workers,
        general_workers,
    )
    tasks = [asyncio.create_task(scheduler.loop(stop)), asyncio.create_task(publisher.loop(stop))]
    tasks += [asyncio.create_task(job_loop(stop, i, include_kinds=("generate",))) for i in range(priority_workers)]
    if priority_workers:
        tasks += [
            asyncio.create_task(job_loop(stop, priority_workers + i, exclude_kinds=("generate",)))
            for i in range(general_workers)
        ]
    else:
        tasks += [asyncio.create_task(job_loop(stop, i)) for i in range(general_workers)]
    await stop.wait()
    log.info("shutting down")
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
