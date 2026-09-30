import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .api import auth, content, core, system
from .config import get_settings
from .events import broker
from .security import hash_password

log = logging.getLogger("pulse.api")


async def bootstrap_admin() -> None:
    s = get_settings()
    if await db.fetchone("SELECT 1 FROM users LIMIT 1"):
        return
    await db.execute(
        "INSERT INTO users(email, password_hash, role) VALUES (%s, %s, 'admin')", (s.admin_email.lower(), hash_password(s.admin_password))
    )
    log.warning("Создан администратор %s. Смените пароль и SECRET_KEY перед использованием в проде.", s.admin_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if s.secret_key == "change-me-in-production" or len(s.secret_key) < 32:
        log.warning("SECRET_KEY по умолчанию или короче 32 символов — небезопасно. Сгенерируйте: openssl rand -hex 32")
    await db.open_pool()
    await bootstrap_admin()
    broker.start()
    yield
    await broker.stop()
    await db.close_pool()


app = FastAPI(title="Pulse API", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
for r in (auth.router, core.router, content.router, system.router):
    app.include_router(r, prefix="/api")
