"""Публикация. Таблица publications работает как outbox: одна строка на пару (черновик, канал), UNIQUE защищает от дублей.

Состояния: pending → sending → sent | failed | unknown.
Главный принцип: если запрос мог дойти до Telegram, но ответа нет (таймаут чтения, 5xx, падение воркера),
результат неизвестен — автоматически не повторяем (иначе возможен дубль), а отдаём решение человеку.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
from dataclasses import dataclass

import httpx

from . import db, events
from .config import get_settings
from .images import download_image
from .netguard import check_url
from .textutil import strip_citations, strip_source_footer, truncate

log = logging.getLogger("pulse.publisher")
MAX_ATTEMPTS = 6
SEND_LEASE = "2 minutes"
CAPTION_LIMIT = 1024  # предел подписи к фото в Telegram


@dataclass
class SendResult:
    kind: str  # sent | retry | failed | unknown
    external_id: str | None = None
    error: str | None = None
    retry_after: float | None = None


def sources_footer(citations: list[dict]) -> str:
    """«Источники: Название1, Название2» — каждое название источника гиперссылкой на материал.
    Без URL источник пропускается (ссылка на несуществующую страницу хуже, чем её отсутствие)."""
    seen: dict[str, str] = {}
    for c in citations:
        url = (c.get("url") or "").strip()
        name = (c.get("source") or "").strip() or url
        if url and url not in seen:
            seen[url] = name
    if not seen:
        return ""
    links = ", ".join(f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>' for url, name in seen.items())
    return f"\n\nИсточники: {links}"


def render_telegram_html(title: str, body: str, citations: list[dict], limit: int = 3900) -> str:
    """Текст для Telegram: без маркеров [n] внутри текста, со списком источников в конце (название — ссылка)."""
    esc = html.escape
    clean_title = strip_citations(title, {c["n"] for c in citations})
    head = f"<b>{esc(clean_title)}</b>\n\n" if clean_title else ""
    footer = sources_footer(citations)
    room = max(limit - len(head) - len(footer), 200)
    text = strip_citations(strip_source_footer(body), {c["n"] for c in citations})
    while len(esc(text)) > room and len(text) > 50:
        text = truncate(text, len(text) - max(50, len(text) // 10))
    return head + esc(text) + footer


def classify_telegram(status: int, data: dict) -> SendResult:
    if status == 200 and data.get("ok"):
        return SendResult("sent", external_id=str((data.get("result") or {}).get("message_id", "")))
    desc = data.get("description") or f"HTTP {status}"
    if status == 429:
        return SendResult("retry", error=desc, retry_after=float((data.get("parameters") or {}).get("retry_after", 5)))
    if 500 <= status < 600:
        return SendResult("unknown", error=desc)  # запрос мог быть обработан
    return SendResult("failed", error=desc)


def classify_exception(exc: Exception) -> SendResult:
    # Соединение не установлено — запрос до Telegram не дошёл, повторять безопасно.
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return SendResult("retry", error=f"{type(exc).__name__}: {exc}")
    # Всё остальное (таймаут чтения, обрыв, неожиданная ошибка) — исход неизвестен.
    return SendResult("unknown", error=f"{type(exc).__name__}: {exc}")


async def send_telegram(channel: dict, draft: dict, pub: dict) -> SendResult:
    cfg = channel["config"]
    token = os.environ.get(cfg.get("token_env") or "TELEGRAM_BOT_TOKEN") or get_settings().telegram_bot_token
    if not token or not cfg.get("chat_id"):
        return SendResult("failed", error="Не заданы токен бота (TELEGRAM_BOT_TOKEN) или chat_id канала")
    base = (cfg.get("api_base") or "https://api.telegram.org").rstrip("/")
    text = render_telegram_html(draft["title"], draft["body"], draft["citations"])
    message = {"chat_id": cfg["chat_id"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    image_url = draft.get("image_url")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            if image_url:
                res = await _send_with_image(client, base, token, cfg["chat_id"], text, message, image_url)
                if res:
                    return res
            r = await client.post(f"{base}/bot{token}/sendMessage", json=message)
    except Exception as e:
        return classify_exception(e)
    return classify_telegram(r.status_code, _json(r))


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


async def _send_with_image(client: httpx.AsyncClient, base: str, token: str, chat_id: str, text: str, message: dict, image_url: str) -> SendResult | None:
    """Короткий пост уходит фото с подписью; длинный — одним сообщением с картинкой над текстом (превью ссылки).
    Возвращает None, если с картинкой не вышло и безопасно отправить обычное текстовое сообщение
    (файл не скачался или Telegram отклонил фото ответом 4xx — значит, ничего не опубликовано)."""
    if len(text) <= CAPTION_LIMIT:
        try:
            data, ctype = await download_image(image_url)
        except Exception as e:
            log.info("image download failed (%s): %s", image_url, e)
            return None
        r = await client.post(
            f"{base}/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
            files={"photo": ("image", data, ctype)},
        )
        res = classify_telegram(r.status_code, _json(r))
        if res.kind == "failed" and 400 <= r.status_code < 500 and r.status_code != 429:
            log.info("sendPhoto rejected, falling back to text: %s", res.error)
            return None
        return res
    message.pop("disable_web_page_preview", None)
    message["link_preview_options"] = {"url": image_url, "show_above_text": True, "prefer_large_media": True}
    r = await client.post(f"{base}/bot{token}/sendMessage", json=message)
    res = classify_telegram(r.status_code, _json(r))
    if res.kind == "failed" and r.status_code == 400:
        message.pop("link_preview_options", None)
        message["disable_web_page_preview"] = True
        return None
    return res


async def send_webhook(channel: dict, draft: dict, pub: dict) -> SendResult:
    """Получатель обязан дедуплицировать по заголовку Idempotency-Key — тогда повторы безопасны (at-least-once)."""
    cfg = channel["config"]
    body = json.dumps(
        {"draft_id": draft["id"], "project_id": draft["project_id"], "kind": draft["kind"], "title": draft["title"], "body": strip_citations(strip_source_footer(draft["body"]), {c["n"] for c in draft["citations"]}), "citations": draft["citations"], "image_url": draft.get("image_url")},
        ensure_ascii=False,
    ).encode()
    headers = {"Content-Type": "application/json", "Idempotency-Key": f"pulse-{draft['id']}-{channel['id']}"}
    if cfg.get("secret"):
        headers["X-Pulse-Signature"] = "sha256=" + hmac.new(cfg["secret"].encode(), body, hashlib.sha256).hexdigest()
    try:
        await check_url(cfg["url"])
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(cfg["url"], content=body, headers=headers)
    except ValueError as e:
        return SendResult("failed", error=str(e))
    except Exception as e:
        return SendResult("retry", error=f"{type(e).__name__}: {e}")
    if 200 <= r.status_code < 300:
        return SendResult("sent", external_id=r.headers.get("x-request-id") or headers["Idempotency-Key"])
    if r.status_code == 429 or r.status_code >= 500:
        return SendResult("retry", error=f"HTTP {r.status_code}")
    return SendResult("failed", error=f"HTTP {r.status_code}")


async def send_console(channel: dict, draft: dict, pub: dict) -> SendResult:
    log.info("CONSOLE PUBLISH draft=%s: %s", draft["id"], draft["title"])
    return SendResult("sent", external_id=f"console-{pub['id']}")


SENDERS = {"telegram": send_telegram, "webhook": send_webhook, "console": send_console}


async def claim() -> dict | None:
    return await db.fetchone(
        f"UPDATE publications SET status = 'sending', attempts = attempts + 1, locked_until = now() + interval '{SEND_LEASE}' "
        "WHERE id = (SELECT id FROM publications WHERE status = 'pending' AND due_at <= now() "
        "ORDER BY due_at, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *"
    )


async def _mark_published(draft: dict) -> None:
    await db.execute(
        "UPDATE clusters SET state = 'published', published_at = now(), item_count_at_publish = item_count, published_centroid = centroid "
        "WHERE id = ANY(%s)",
        (draft["cluster_ids"],),
    )


async def apply_result(pub: dict, res: SendResult, draft: dict, auto_retry_unknown: bool) -> str:
    status = res.kind
    if res.kind == "sent":
        await db.execute(
            "UPDATE publications SET status = 'sent', external_id = %s, sent_at = now(), locked_until = NULL, error = NULL "
            "WHERE id = %s AND status IN ('sending', 'unknown')",
            (res.external_id, pub["id"]),
        )
        await _mark_published(draft)
    elif res.kind == "retry" or (res.kind == "unknown" and auto_retry_unknown):
        if pub["attempts"] >= MAX_ATTEMPTS:
            status = "failed"
            await db.execute("UPDATE publications SET status = 'failed', locked_until = NULL, error = %s WHERE id = %s", (f"Исчерпаны попытки: {res.error}", pub["id"]))
        else:
            delay = res.retry_after if res.retry_after else min(300, 5 * 2 ** pub["attempts"])
            status = "pending"
            await db.execute(
                "UPDATE publications SET status = 'pending', locked_until = NULL, error = %s, due_at = now() + make_interval(secs => %s) WHERE id = %s",
                (res.error, delay, pub["id"]),
            )
    elif res.kind == "unknown":
        await db.execute("UPDATE publications SET status = 'unknown', locked_until = NULL, error = %s WHERE id = %s", (res.error, pub["id"]))
    else:
        await db.execute("UPDATE publications SET status = 'failed', locked_until = NULL, error = %s WHERE id = %s", (res.error, pub["id"]))
    await events.notify("publication", publication_id=pub["id"], draft_id=draft["id"], status=status, project_id=draft["project_id"])
    return status


async def process_one(senders: dict | None = None) -> bool:
    senders = senders or SENDERS
    pub = await claim()
    if not pub:
        return False
    draft = await db.fetchone("SELECT * FROM drafts WHERE id = %s", (pub["draft_id"],))
    channel = await db.fetchone("SELECT * FROM channels WHERE id = %s", (pub["channel_id"],))
    project = await db.fetchone("SELECT auto_retry_unknown FROM projects WHERE id = %s", (draft["project_id"],))
    if not channel or not channel["enabled"]:
        await db.execute("UPDATE publications SET status = 'cancelled', locked_until = NULL, error = 'Канал отключён или удалён' WHERE id = %s", (pub["id"],))
        return True
    try:
        res = await senders[channel["type"]](channel, draft, pub)
    except Exception as e:
        log.exception("sender crashed")
        res = classify_exception(e)
    await apply_result(pub, res, draft, project["auto_retry_unknown"])
    return True


async def recover_stale() -> int:
    """Публикации, зависшие в sending (воркер упал): результат неизвестен."""
    return await db.execute(
        "UPDATE publications p SET status = CASE WHEN pr.auto_retry_unknown THEN 'pending' ELSE 'unknown' END, locked_until = NULL, "
        "error = 'Воркер потерян во время отправки; результат неизвестен' "
        "FROM drafts d JOIN projects pr ON pr.id = d.project_id "
        "WHERE p.draft_id = d.id AND p.status = 'sending' AND p.locked_until < now()"
    )


async def loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            worked = await process_one()
        except Exception:
            log.exception("publisher loop error")
            worked = False
        await asyncio.sleep(0.3 if worked else 1.0)


# ---------- проверка доступа к каналу: ничего не публикует ----------
async def check_channel(channel: dict) -> dict:
    kind, cfg = channel["type"], channel["config"]
    if kind == "console":
        return {"ok": True, "message": "Консольный канал работает: посты записываются в журнал воркера. Никуда наружу ничего не отправляется."}
    try:
        return await (_check_telegram(cfg) if kind == "telegram" else _check_webhook(cfg))
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Нет связи: {type(e).__name__}: {e}"}


async def _check_telegram(cfg: dict) -> dict:
    token = os.environ.get(cfg.get("token_env") or "TELEGRAM_BOT_TOKEN") or get_settings().telegram_bot_token
    if not token:
        return {"ok": False, "message": "Токен бота не задан: добавьте TELEGRAM_BOT_TOKEN в .env и перезапустите api и worker."}
    if not cfg.get("chat_id"):
        return {"ok": False, "message": "Не указан чат или канал."}
    base = (cfg.get("api_base") or "https://api.telegram.org").rstrip("/")

    async with httpx.AsyncClient(timeout=15.0) as client:
        async def call(method: str, **params) -> dict:
            r = await client.post(f"{base}/bot{token}/{method}", json=params)
            try:
                return r.json()
            except ValueError:
                return {"ok": False, "description": f"HTTP {r.status_code}"}

        me = await call("getMe")
        if not me.get("ok"):
            return {"ok": False, "message": f"Telegram не принял токен бота: {me.get('description', 'неизвестная ошибка')}."}
        bot = me["result"]
        chat = await call("getChat", chat_id=cfg["chat_id"])
        if not chat.get("ok"):
            return {"ok": False, "message": f"Бот @{bot.get('username')} не видит чат «{cfg['chat_id']}»: {chat.get('description', 'неизвестная ошибка')}. Проверьте имя и добавьте бота в канал."}
        info = chat["result"]
        title = info.get("title") or info.get("username") or str(info.get("id"))
        member_resp = await call("getChatMember", chat_id=cfg["chat_id"], user_id=bot["id"])
        member = member_resp.get("result", {}) if member_resp.get("ok") else {}
        status = member.get("status")
        if info.get("type") == "channel":
            can = status == "creator" or (status == "administrator" and member.get("can_post_messages", False))
        else:
            can = status in ("creator", "administrator", "member") and member.get("can_send_messages", True) is not False
        if not can:
            return {"ok": False, "message": f"Бот @{bot.get('username')} видит «{title}», но не может там публиковать. Сделайте его администратором с правом «Публикация сообщений»."}
        return {"ok": True, "message": f"Бот @{bot.get('username')} может публиковать в «{title}». Проверка ничего не отправила."}


async def _check_webhook(cfg: dict) -> dict:
    url = cfg.get("url", "")
    try:
        await check_url(url)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.request("OPTIONS", url)  # без тела: получатель не увидит ни поста, ни «проверочного» события
    return {"ok": True, "message": f"Адрес отвечает (HTTP {r.status_code}). Данные не отправлялись; принимает ли получатель посты, эта проверка не показывает."}
