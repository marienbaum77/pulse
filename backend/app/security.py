import time
from collections import defaultdict

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request

from . import db
from .config import get_settings

COOKIE = "pulse_session"
ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_ph = PasswordHasher()
_fails: dict[str, list[float]] = defaultdict(list)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def make_token(user_id: int) -> str:
    s = get_settings()
    return jwt.encode({"sub": str(user_id), "exp": int(time.time()) + s.session_hours * 3600}, s.secret_key, "HS256")


def check_login_rate(ip: str) -> None:
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < 600]
    if len(_fails[ip]) >= 8:
        raise HTTPException(429, "Слишком много неудачных попыток входа. Повторите через 10 минут.")


def register_login_failure(ip: str) -> None:
    _fails[ip].append(time.time())


def require_csrf_header(request: Request) -> None:
    # Нестандартный заголовок нельзя выставить кросс-доменной формой, а для fetch нужен CORS preflight.
    if request.method not in SAFE_METHODS and request.headers.get("x-requested-with") != "pulse":
        raise HTTPException(403, "Отсутствует заголовок X-Requested-With")


async def current_user(request: Request) -> dict:
    require_csrf_header(request)
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Требуется вход")
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Сессия истекла, войдите снова")
    user = await db.fetchone("SELECT id, email, role FROM users WHERE id = %s", (int(data["sub"]),))
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    return user


def require(role: str):
    async def dep(user: dict = Depends(current_user)) -> dict:
        if ROLE_RANK[user["role"]] < ROLE_RANK[role]:
            raise HTTPException(403, "Недостаточно прав для этого действия")
        return user

    return dep


viewer = require("viewer")
editor = require("editor")
admin = require("admin")


async def audit(user: dict | None, action: str, entity: str, entity_id: int | None = None, detail: dict | None = None):
    from psycopg.types.json import Jsonb

    await db.execute(
        "INSERT INTO audit_log(user_id, user_email, action, entity, entity_id, detail) VALUES (%s,%s,%s,%s,%s,%s)",
        (user["id"] if user else None, user["email"] if user else None, action, entity, entity_id, Jsonb(detail or {})),
    )
