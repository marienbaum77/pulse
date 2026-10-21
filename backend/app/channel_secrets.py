"""Шифрование секретов каналов at-rest (AES-256-GCM, ключ из SECRET_KEY)."""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

PREFIX = "enc:v1:"
# Поля config, которые нельзя хранить и отдавать в API в открытом виде.
SECRET_KEYS = ("secret", "access_token", "webhook_url")


def _aes_key() -> bytes:
    return hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()


def is_encrypted(value: str) -> bool:
    return value.startswith(PREFIX)


def encrypt_value(plain: str) -> str:
    """Шифрует строку; уже зашифрованные значения и пустые строки не трогает."""
    if not plain or is_encrypted(plain):
        return plain
    nonce = os.urandom(12)
    ct = AESGCM(_aes_key()).encrypt(nonce, plain.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_value(value: str) -> str:
    """Расшифровывает enc:v1:…; plaintext (legacy) возвращает как есть."""
    if not value or not is_encrypted(value):
        return value
    raw = base64.urlsafe_b64decode(value[len(PREFIX) :].encode("ascii"))
    if len(raw) < 13:
        raise ValueError("Повреждённый зашифрованный секрет канала")
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_aes_key()).decrypt(nonce, ct, None).decode("utf-8")


def encrypt_channel_config(cfg: dict) -> dict:
    out = dict(cfg)
    for key in SECRET_KEYS:
        val = out.get(key)
        if isinstance(val, str) and val:
            out[key] = encrypt_value(val)
    return out


def decrypt_channel_config(cfg: dict) -> dict:
    out = dict(cfg)
    for key in SECRET_KEYS:
        val = out.get(key)
        if isinstance(val, str) and val:
            out[key] = decrypt_value(val)
    return out


async def encrypt_plaintext_channel_secrets() -> int:
    """Одноразовая миграция: plaintext-секреты в channels.config → enc:v1:…"""
    from psycopg.types.json import Jsonb

    from . import db

    rows = await db.fetchall("SELECT id, config FROM channels")
    updated = 0
    for row in rows:
        cfg = dict(row["config"] or {})
        changed = False
        for key in SECRET_KEYS:
            val = cfg.get(key)
            if isinstance(val, str) and val and not is_encrypted(val):
                cfg[key] = encrypt_value(val)
                changed = True
        if changed:
            await db.execute("UPDATE channels SET config = %s WHERE id = %s", (Jsonb(cfg), row["id"]))
            updated += 1
    return updated

