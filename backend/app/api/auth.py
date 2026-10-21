import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import db
from ..config import get_settings
from ..security import COOKIE, check_login_rate, current_user, make_token, register_login_failure, require_csrf_header, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: str
    password: str


def _parse_trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Парсинг TRUSTED_PROXIES из настроек в список сетей (кешируется на уровне модуля)."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in get_settings().trusted_proxies.split(","):
        entry = entry.strip()
        if entry:
            nets.append(ipaddress.ip_network(entry, strict=False))
    return nets


def _is_trusted_proxy(host: str) -> bool:
    """Проверяет, что прямой клиент входит в список доверенных прокси."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _parse_trusted_networks())


def _ip(request: Request) -> str:
    # Доверяем X-Forwarded-For только если прямой клиент — доверенный прокси
    direct = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for")
    if fwd and direct != "?" and _is_trusted_proxy(direct):
        return fwd.split(",")[0].strip()
    return direct


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    require_csrf_header(request)
    ip = _ip(request)
    check_login_rate(ip)
    user = await db.fetchone("SELECT id, email, role, password_hash FROM users WHERE lower(email) = lower(%s)", (body.email.strip(),))
    if not user or not verify_password(user["password_hash"], body.password):
        register_login_failure(ip)
        raise HTTPException(401, "Неверная почта или пароль")
    s = get_settings()
    response.set_cookie(COOKIE, make_token(user["id"]), httponly=True, samesite="lax", secure=s.cookie_secure, max_age=s.session_hours * 3600, path="/")
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


@router.post("/logout")
async def logout(response: Response, request: Request):
    require_csrf_header(request)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user
