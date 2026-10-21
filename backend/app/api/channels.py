"""Каналы публикации."""
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db, publisher
from ..channel_secrets import SECRET_KEYS, decrypt_channel_config, encrypt_channel_config
from ..security import audit, editor, viewer

router = APIRouter(tags=["channels"])

# ---------- channels ----------
class ChannelBody(BaseModel):
    project_id: int
    type: Literal["telegram", "webhook", "console", "vk", "discord"]
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = {}
    enabled: bool = True


def _validate_channel(body: ChannelBody, *, existing: dict | None = None) -> dict:
    c = dict(body.config)
    existing = existing or {}
    if body.type == "telegram":
        if not str(c.get("chat_id", "")).strip():
            raise HTTPException(422, "Для Telegram укажите chat_id (например, @mychannel или -1001234567890)")
        if c.get("token_env") and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", c["token_env"]):
            raise HTTPException(422, "token_env — имя переменной окружения, например TELEGRAM_BOT_TOKEN")
    elif body.type == "webhook":
        if not str(c.get("url", "")).startswith(("http://", "https://")):
            raise HTTPException(422, "Для webhook укажите url, начинающийся с http:// или https://")
    elif body.type == "vk":
        if not c.get("owner_id"):
            raise HTTPException(422, "Для ВКонтакте укажите owner_id (ID сообщества, например -123456789)")
        try:
            int(c["owner_id"])
        except (ValueError, TypeError):
            raise HTTPException(422, "owner_id должен быть целым числом (для сообществ обычно с минусом)")
        if c.get("token_env") and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(c["token_env"])):
            raise HTTPException(422, "token_env — имя переменной окружения, например VK_ACCESS_TOKEN")
    elif body.type == "discord":
        url = str(c.get("webhook_url", "")).strip()
        if not url and existing.get("webhook_url"):
            pass  # пустое поле при правке = оставить прежний URL
        elif not (
            url.startswith("https://discord.com/api/webhooks/")
            or url.startswith("https://discordapp.com/api/webhooks/")
        ):
            raise HTTPException(422, "Для Discord укажите корректный Webhook URL (https://discord.com/api/webhooks/...)")
    return c


def _merge_secrets(cfg: dict, existing: dict | None) -> dict:
    """Пустое секретное поле при обновлении = не менять; новые значения шифруются."""
    out = dict(cfg)
    existing = existing or {}
    for key in SECRET_KEYS:
        val = out.get(key)
        if not (isinstance(val, str) and val.strip()):
            if existing.get(key):
                out[key] = existing[key]
            else:
                out.pop(key, None)
    return encrypt_channel_config(out)


def _public_channel(row: dict) -> dict:
    cfg = dict(row["config"])
    has_secret = False
    for key in SECRET_KEYS:
        if cfg.pop(key, None):
            has_secret = True
    row["has_secret"] = has_secret
    row["config"] = cfg
    return row


def _channel_for_use(row: dict) -> dict:
    """Канал с расшифрованным config для publisher/check."""
    return {**row, "config": decrypt_channel_config(dict(row["config"]))}


@router.get("/channels")
async def list_channels(project_id: int, user=Depends(viewer)):
    rows = await db.fetchall("SELECT * FROM channels WHERE project_id = %s ORDER BY id", (project_id,))
    return [_public_channel(r) for r in rows]


@router.post("/channels", status_code=201)
async def create_channel(body: ChannelBody, user=Depends(editor)):
    cfg = _merge_secrets(_validate_channel(body), None)
    row = await db.fetchone(
        "INSERT INTO channels(project_id, type, name, config, enabled) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (body.project_id, body.type, body.name, Jsonb(cfg), body.enabled),
    )
    await audit(user, "create", "channel", row["id"], {"type": body.type})
    return _public_channel(row)


@router.put("/channels/{cid}")
async def update_channel(cid: int, body: ChannelBody, user=Depends(editor)):
    old = await db.fetchone("SELECT config FROM channels WHERE id = %s", (cid,))
    if not old:
        raise HTTPException(404, "Канал не найден")
    old_cfg = dict(old["config"] or {})
    cfg = _merge_secrets(_validate_channel(body, existing=old_cfg), old_cfg)
    row = await db.fetchone(
        "UPDATE channels SET name=%s, config=%s, enabled=%s WHERE id=%s RETURNING *", (body.name, Jsonb(cfg), body.enabled, cid)
    )
    await audit(user, "update", "channel", cid)
    return _public_channel(row)


@router.delete("/channels/{cid}", status_code=204)
async def delete_channel(cid: int, user=Depends(editor)):
    await db.execute("DELETE FROM channels WHERE id = %s", (cid,))
    await audit(user, "delete", "channel", cid)


@router.post("/channels/{cid}/test")
async def test_channel(cid: int, user=Depends(editor)):
    """Проверка доступа. В канал НИЧЕГО не публикуется: для Telegram запрашиваются данные бота и его права в чате."""
    ch = await db.fetchone("SELECT * FROM channels WHERE id = %s", (cid,))
    if not ch:
        raise HTTPException(404, "Канал не найден")
    return await publisher.check_channel(_channel_for_use(ch))
