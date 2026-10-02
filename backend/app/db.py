"""Доступ к PostgreSQL: пул соединений, миграции, мелкие хелперы."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async
from pgvector.psycopg.vector import VectorBinaryLoader, VectorLoader
from psycopg.rows import dict_row
from psycopg.types import TypeInfo
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

log = logging.getLogger("pulse.db")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
pool: AsyncConnectionPool | None = None


async def migrate() -> None:
    """Применяет SQL-миграции по порядку имён; безопасно при параллельном запуске api и worker."""
    dsn = get_settings().database_url
    last_err: Exception | None = None
    for _ in range(40):
        try:
            conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
            break
        except psycopg.OperationalError as e:  # БД ещё стартует
            last_err = e
            await asyncio.sleep(1.5)
    else:
        raise RuntimeError(f"База данных недоступна: {last_err}")
    async with conn:
        await conn.execute("SELECT pg_advisory_lock(747001)")
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in done:
                    continue
                log.info("applying migration %s", f.name)
                async with conn.transaction():
                    await conn.execute(f.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (f.name,))
        finally:
            await conn.execute("SELECT pg_advisory_unlock(747001)")


class _NumpyLoader(VectorLoader):
    def load(self, data):
        v = super().load(data)
        return None if v is None else v.to_numpy()


class _NumpyBinaryLoader(VectorBinaryLoader):
    def load(self, data):
        v = super().load(data)
        return None if v is None else v.to_numpy()


async def _configure(conn: psycopg.AsyncConnection) -> None:
    """Регистрирует тип vector: в Python значения приходят как numpy.ndarray."""
    await register_vector_async(conn)
    info = await TypeInfo.fetch(conn, "vector")
    conn.adapters.register_loader(info.oid, _NumpyLoader)
    conn.adapters.register_loader(info.oid, _NumpyBinaryLoader)
    await conn.commit()


async def open_pool() -> None:
    global pool
    await migrate()
    pool = AsyncConnectionPool(
        get_settings().database_url,
        min_size=1,
        max_size=12,
        kwargs={"row_factory": dict_row},
        configure=_configure,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open(wait=True, timeout=30)


async def close_pool() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def fetchall(sql: str, params=None) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetchone(sql: str, params=None) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params=None) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return cur.rowcount


@asynccontextmanager
async def advisory_lock(k1: int, k2: int):
    """Сессионная advisory-блокировка; отпускается при выходе или при обрыве соединения."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s) AS got", (k1, k2))
        got = (await cur.fetchone())["got"]
        try:
            yield got
        finally:
            if got:
                await conn.execute("SELECT pg_advisory_unlock(%s, %s)", (k1, k2))
