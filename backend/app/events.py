"""События конвейера: воркер делает NOTIFY, каждый процесс API слушает канал и раздаёт события по SSE."""
import asyncio
import json
import logging

import psycopg
from psycopg.types.json import Jsonb  # noqa: F401

from . import db
from .config import get_settings

log = logging.getLogger("pulse.events")
CHANNEL = "pulse_events"


async def notify(kind: str, **data) -> None:
    payload = json.dumps({"type": kind, **data}, default=str)
    if len(payload) > 7000:  # лимит NOTIFY ~8 КБ: шлём только идентификаторы
        payload = json.dumps({"type": kind, "truncated": True})
    try:
        await db.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))
    except Exception:
        log.exception("notify failed")


class Broker:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    async def _run(self):
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(get_settings().database_url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    async for n in conn.notifies():
                        for q in list(self._subs):
                            if q.full():
                                try:
                                    q.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            q.put_nowait(n.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("event listener reconnecting: %s", e)
                await asyncio.sleep(2)


broker = Broker()
