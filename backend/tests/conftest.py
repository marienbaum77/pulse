import os
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "postgresql://pulse:pulse@test-db:5432/pulse_test")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ["LLM_PROVIDER"] = "stub"
os.environ["ALLOW_PRIVATE_URLS"] = "true"

database_name = urlsplit(os.environ["DATABASE_URL"]).path.rstrip("/").rsplit("/", 1)[-1]
if not database_name.endswith("_test"):
    raise RuntimeError("Tests truncate their database. Set DATABASE_URL to an isolated database whose name ends with _test.")

from app import db  # noqa: E402
from app.providers import set_provider, StubProvider  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def pool():
    try:
        await db.open_pool()
    except Exception as e:  # БД недоступна — интеграционные тесты пропускаются
        pytest.skip(f"PostgreSQL недоступен: {e}", allow_module_level=False)
    set_provider(StubProvider())
    yield
    await db.close_pool()


@pytest_asyncio.fixture(autouse=False)
async def clean(pool):
    await db.execute("TRUNCATE users, projects, jobs, llm_calls, audit_log, kv RESTART IDENTITY CASCADE")
    yield
