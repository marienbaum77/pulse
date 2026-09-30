"""Команды администратора.

  docker compose exec api python -m app.admin reset-password [--email admin@example.com]
  docker compose exec api python -m app.admin list-users
"""
import argparse
import asyncio
import getpass

from . import db
from .config import get_settings
from .security import hash_password

MIN_LEN = 8


async def reset_password(email: str, password: str) -> None:
    owns_pool = db.pool is None  # пул закрываем только если открыли сами (из командной строки)
    if owns_pool:
        await db.open_pool()
    try:
        updated = await db.execute(
            "UPDATE users SET password_hash = %s WHERE lower(email) = lower(%s)",
            (hash_password(password), email.strip()),
        )
        if updated != 1:
            raise SystemExit(f"Пользователь не найден: {email}. Список: python -m app.admin list-users")
        print(f"Пароль обновлён для {email}")
    finally:
        if owns_pool:
            await db.close_pool()


async def list_users() -> None:
    await db.open_pool()
    try:
        for u in await db.fetchall("SELECT email, role FROM users ORDER BY id"):
            print(f"{u['email']}\t{u['role']}")
    finally:
        await db.close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("reset-password", help="задать пользователю новый пароль")
    rp.add_argument("--email", help="по умолчанию — ADMIN_EMAIL из окружения")
    rp.add_argument("--password", help="лучше не указывать: пароль попадёт в историю команд; без флага он запрашивается скрыто")
    sub.add_parser("list-users", help="показать пользователей и роли")
    args = ap.parse_args()

    if args.cmd == "list-users":
        asyncio.run(list_users())
        return
    email = args.email or get_settings().admin_email
    password = args.password
    if not password:
        password = getpass.getpass(f"Новый пароль для {email}: ")
        if password != getpass.getpass("Повторите пароль: "):
            raise SystemExit("Пароли не совпали")
    if len(password) < MIN_LEN:
        raise SystemExit(f"Пароль должен быть не короче {MIN_LEN} символов")
    asyncio.run(reset_password(email, password))


if __name__ == "__main__":
    main()
