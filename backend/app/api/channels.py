"""Каналы публикации."""
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db, publisher
from ..security import audit, editor, viewer

router = APIRouter(tags=["channels"])

# ---------- channels ----------
class ChannelBody(BaseModel):
    project_id: int
    type: Literal["telegram", "webhook", "console"]
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = {}
    enabled: bool = True


def _validate_channel(body: ChannelBody) -> dict:
    c = dict(body.config)
    if body.type == "telegram":
        if not str(c.get("chat_id", "")).strip():
            raise HTTPException(422, "Для Telegram укажите chat_id (например, @mychannel или -1001234567890)")
        if c.get("token_env") and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", c["token_env"]):
            raise HTTPException(422, "token_env — имя переменной окружения, например TELEGRAM_BOT_TOKEN")
    if body.type == "webhook" and not str(c.get("url", "")).startswith(("http://", "https://")):
        raise HTTPException(422, "Для webhook укажите url, начинающийся с http:// или https://")
    return c


def _public_channel(row: dict) -> dict:
    cfg = dict(row["config"])
    row["has_secret"] = bool(cfg.pop("secret", None))
    row["config"] = cfg
    return row


@router.get("/channels")
async def list_channels(project_id: int, user=Depends(viewer)):
    rows = await db.fetchall("SELECT * FROM channels WHERE project_id = %s ORDER BY id", (project_id,))
    return [_public_channel(r) for r in rows]


@router.post("/channels", status_code=201)
async def create_channel(body: ChannelBody, user=Depends(editor)):
    cfg = _validate_channel(body)
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
    cfg = _validate_channel(body)
    if not cfg.get("secret") and old["config"].get("secret"):  # пустое поле секрета = «не менять»
        cfg["secret"] = old["config"]["secret"]
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
    return await publisher.check_channel(ch)


