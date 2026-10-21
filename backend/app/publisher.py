"""Публикация. Таблица publications работает как outbox: одна строка на пару (черновик, канал), UNIQUE защищает от дублей.
Идентичный текст также не отправляется повторно в один канал в течение 24 часов.

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
from .channel_secrets import decrypt_channel_config
from .config import get_settings
from .images import download_image
from .netguard import guarded_client
from .textutil import strip_citations, strip_html, strip_source_footer, truncate

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


def render_telegram_caption(title: str, body: str, citations: list[dict], limit: int = CAPTION_LIMIT) -> str:
    """Форматирует HTML-подпись для sendPhoto так, чтобы видимый текст укладывался в limit символов после парсинга HTML."""
    esc = html.escape
    clean_title = strip_citations(title, {c["n"] for c in citations})
    head = f"<b>{esc(clean_title)}</b>\n\n" if clean_title else ""
    footer = sources_footer(citations)
    visible_head = len(clean_title) + (2 if clean_title else 0)
    visible_footer = len(strip_html(footer))
    room = max(limit - visible_head - visible_footer, 100)
    text = strip_citations(strip_source_footer(body), {c["n"] for c in citations})
    while len(text) > room and len(text) > 50:
        text = truncate(text, len(text) - max(20, len(text) // 10))
    result = head + esc(text) + footer
    while len(strip_html(result)) > limit and len(text) > 50:
        text = truncate(text, len(text) - 15)
        result = head + esc(text) + footer
    return result


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


def classify_image_exception(exc: Exception) -> SendResult:
    transient = isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code in (408, 429) or exc.response.status_code >= 500)
    )
    return SendResult(
        "retry" if transient else "failed",
        error=f"Не удалось загрузить изображение: {type(exc).__name__}: {exc!r}",
    )


async def send_telegram(channel: dict, draft: dict, pub: dict, *, client: httpx.AsyncClient | None = None) -> SendResult:
    cfg = channel["config"]
    token = os.environ.get(cfg.get("token_env") or "TELEGRAM_BOT_TOKEN") or get_settings().telegram_bot_token
    if not token or not cfg.get("chat_id"):
        return SendResult("failed", error="Не заданы токен бота (TELEGRAM_BOT_TOKEN) или chat_id канала")
    base = (cfg.get("api_base") or "https://api.telegram.org").rstrip("/")
    text = render_telegram_html(draft["title"], draft["body"], draft["citations"])
    message = {"chat_id": cfg["chat_id"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    image_url = draft.get("image_url")
    # Если клиент передан извне — переиспользуем; иначе создаём локальный.
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        if image_url:
            res = await _send_with_image(client, base, token, cfg["chat_id"], text, message, image_url, draft)
            if res:
                return res
        r = await client.post(f"{base}/bot{token}/sendMessage", json=message)
    except Exception as e:
        return classify_exception(e)
    finally:
        if owns_client:
            await client.aclose()
    return classify_telegram(r.status_code, _json(r))


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


async def _send_with_image(client: httpx.AsyncClient, base: str, token: str, chat_id: str, text: str, message: dict, image_url: str, draft: dict) -> SendResult | None:
    """Короткий пост — фото с подписью; длинный — текст с превью картинки над ним.
    Telegram ограничивает подпись к фото 1024 символами после парсинга HTML-тегов,
    но предпросмотр ссылки позволяет сохранить длинный текст и картинку в одном сообщении.
    Возвращает None, если безопасно отправить текст без картинки."""
    visible_len = len(strip_html(text))
    caption = text
    if visible_len > CAPTION_LIMIT:
        # Если превышение небольшое (до 1200 видимых символов), аккуратно укладываем текст в 1024 символа,
        # чтобы сохранить публикацию полноценным фото вместо ссылки с превью
        if visible_len <= 1200:
            fitted = render_telegram_caption(draft["title"], draft["body"], draft["citations"], limit=CAPTION_LIMIT)
            if len(strip_html(fitted)) <= CAPTION_LIMIT:
                caption = fitted
                visible_len = len(strip_html(caption))

    if visible_len > CAPTION_LIMIT:
        # Используем прямой URL изображения, а не URL статьи-источника:
        # Telegram ненадёжно извлекает og:image при скрейпинге страниц.
        message.pop("disable_web_page_preview", None)
        message["link_preview_options"] = {
            "url": image_url,
            "show_above_text": True,
            "prefer_large_media": True,
        }
        r = await client.post(f"{base}/bot{token}/sendMessage", json=message)
        res = classify_telegram(r.status_code, _json(r))
        if res.kind == "failed" and r.status_code == 400:
            log.info("Telegram rejected image link preview, falling back to text: %s", res.error)
            message.pop("link_preview_options", None)
            message["disable_web_page_preview"] = True
            return None
        return res

    try:
        data, ctype = await download_image(image_url)
    except Exception as e:
        result = classify_image_exception(e)
        log.warning("image download failed (%s): %s", image_url, result.error)
        return result
    r = await client.post(
        f"{base}/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("image", data, ctype)},
    )
    res = classify_telegram(r.status_code, _json(r))
    if res.kind == "failed" and 400 <= r.status_code < 500 and r.status_code != 429:
        log.warning("sendPhoto rejected; publication failed rather than dropping its image: %s", res.error)
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
        async with guarded_client(timeout=15.0) as client:
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


def render_vk_text(title: str, body: str, citations: list[dict]) -> str:
    cleaned = strip_citations(strip_source_footer(body), {c["n"] for c in citations}).strip()
    parts = []
    if title.strip():
        parts.append(title.strip())
    parts.append(cleaned)
    src_lines = [f"• {c['source']}: {c['url']}" for c in citations if c.get("url") and c.get("source")]
    if src_lines:
        parts.append("Источники:\n" + "\n".join(src_lines))
    return "\n\n".join(parts)


async def send_vk(channel: dict, draft: dict, pub: dict, *, client: httpx.AsyncClient | None = None) -> SendResult:
    cfg = channel["config"]
    token = os.environ.get(cfg.get("token_env") or "VK_ACCESS_TOKEN") or cfg.get("access_token")
    if not token:
        return SendResult("failed", error="Токен доступа VK не задан (укажите в настройках канала или в переменной окружения)")
    try:
        owner_id = int(cfg.get("owner_id", 0))
    except (ValueError, TypeError):
        return SendResult("failed", error="Некорректный owner_id для VK")

    text = render_vk_text(draft.get("title", ""), draft["body"], draft["citations"])
    image_url = draft.get("image_url")
    base = (cfg.get("api_base") or "https://api.vk.com").rstrip("/")
    v = "5.199"

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    attachments = []
    try:
        if image_url:
            try:
                img_data, ctype = await download_image(image_url)
                group_id = abs(owner_id)
                srv_res = await client.post(f"{base}/method/photos.getWallUploadServer", data={"access_token": token, "v": v, "group_id": group_id})
                upload_server = srv_res.json().get("response", {})
                upload_url = upload_server.get("upload_url")
                if upload_url:
                    ext = "png" if "png" in ctype else "jpg"
                    files = {"photo": (f"cover.{ext}", img_data, ctype)}
                    up_res = await client.post(upload_url, files=files)
                    up_data = up_res.json()
                    save_res = await client.post(
                        f"{base}/method/photos.saveWallPhoto",
                        data={
                            "access_token": token,
                            "v": v,
                            "group_id": group_id,
                            "photo": up_data.get("photo"),
                            "server": up_data.get("server"),
                            "hash": up_data.get("hash"),
                        },
                    )
                    photos = save_res.json().get("response", [])
                    if photos:
                        p = photos[0]
                        attachments.append(f"photo{p['owner_id']}_{p['id']}")
            except Exception as e:
                log.warning("failed to upload image to VK for draft %s: %s; posting text only", draft["id"], e)

        data = {
            "access_token": token,
            "v": v,
            "owner_id": owner_id,
            "from_group": 1,
            "message": text,
        }
        if attachments:
            data["attachments"] = ",".join(attachments)

        r = await client.post(f"{base}/method/wall.post", data=data)
    except Exception as e:
        return classify_exception(e)
    finally:
        if owns_client:
            await client.aclose()

    try:
        resp = r.json()
    except Exception:
        return SendResult("retry", error=f"HTTP {r.status_code}")

    if "response" in resp:
        post_id = resp["response"].get("post_id")
        return SendResult("sent", external_id=f"vk_{owner_id}_{post_id}")
    err = resp.get("error", {})
    err_msg = err.get("error_msg", "Неизвестная ошибка VK")
    err_code = err.get("error_code", 0)
    if err_code in (6, 9, 10):
        return SendResult("retry", error=f"VK error {err_code}: {err_msg}")
    return SendResult("failed", error=f"VK error {err_code}: {err_msg}")


async def send_discord(channel: dict, draft: dict, pub: dict, *, client: httpx.AsyncClient | None = None) -> SendResult:
    cfg = channel["config"]
    webhook_url = cfg.get("webhook_url")
    if not webhook_url:
        return SendResult("failed", error="Не указан webhook_url для Discord")

    title = draft.get("title", "").strip()
    raw_body = strip_citations(strip_source_footer(draft["body"]), {c["n"] for c in draft["citations"]}).strip()
    image_url = draft.get("image_url")

    embed: dict = {
        "title": title[:256] if title else None,
        "description": raw_body[:4096],
        "color": 0x4B6BFB,
    }
    embed = {k: v for k, v in embed.items() if v is not None}
    if image_url:
        embed["image"] = {"url": image_url}

    citations = draft.get("citations", [])
    src_links = [f"[{c['source']}]({c['url']})" for c in citations if c.get("url") and c.get("source")]
    if src_links:
        embed["fields"] = [{"name": "Источники", "value": ", ".join(src_links)[:1024], "inline": False}]

    payload = {
        "username": "Pulse",
        "embeds": [embed],
    }

    owns_client = client is None
    if owns_client:
        client = guarded_client(timeout=httpx.Timeout(20.0, connect=10.0))
    try:
        r = await client.post(webhook_url, json=payload, params={"wait": "true"})
    except ValueError as e:
        return SendResult("failed", error=str(e))
    except Exception as e:
        return classify_exception(e)
    finally:
        if owns_client:
            await client.aclose()

    if 200 <= r.status_code < 300:
        msg_id = ""
        try:
            msg_id = str(r.json().get("id", ""))
        except Exception:
            pass
        return SendResult("sent", external_id=msg_id or f"discord-{pub['id']}")
    if r.status_code == 429:
        try:
            retry_after = float(r.json().get("retry_after", 5.0))
        except Exception:
            retry_after = 5.0
        return SendResult("retry", error="Rate limited", retry_after=retry_after)
    if r.status_code >= 500:
        return SendResult("retry", error=f"HTTP {r.status_code}")
    return SendResult("failed", error=f"HTTP {r.status_code}: {r.text[:200]}")


async def send_console(channel: dict, draft: dict, pub: dict) -> SendResult:
    log.info("CONSOLE PUBLISH draft=%s: %s", draft["id"], draft["title"])
    return SendResult("sent", external_id=f"console-{pub['id']}")


SENDERS = {
    "telegram": send_telegram,
    "webhook": send_webhook,
    "console": send_console,
    "vk": send_vk,
    "discord": send_discord,
}


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
    # Черновик мог быть удалён пока публикация стояла в очереди;
    # recover_stale использует INNER JOIN drafts и не подхватит такую запись.
    if not draft:
        await db.execute(
            "UPDATE publications SET status = 'cancelled', locked_until = NULL, error = 'Черновик удалён' WHERE id = %s",
            (pub["id"],),
        )
        return True
    channel = await db.fetchone("SELECT * FROM channels WHERE id = %s", (pub["channel_id"],))
    project = await db.fetchone("SELECT auto_retry_unknown FROM projects WHERE id = %s", (draft["project_id"],))
    if not channel or not channel["enabled"]:
        await db.execute("UPDATE publications SET status = 'cancelled', locked_until = NULL, error = 'Канал отключён или удалён' WHERE id = %s", (pub["id"],))
        return True
    channel = {**channel, "config": decrypt_channel_config(dict(channel["config"] or {}))}
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
        if kind == "discord":
            return await _check_discord(cfg)
        if kind == "vk":
            return await _check_vk(cfg)
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
        async with guarded_client(timeout=10.0) as client:
            r = await client.request("OPTIONS", url)  # без тела: получатель не увидит ни поста, ни «проверочного» события
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": f"Адрес отвечает (HTTP {r.status_code}). Данные не отправлялись; принимает ли получатель посты, эта проверка не показывает."}


async def _check_discord(cfg: dict) -> dict:
    url = cfg.get("webhook_url", "")
    try:
        async with guarded_client(timeout=10.0) as client:
            r = await client.get(url)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    if r.status_code == 200:
        data = r.json()
        channel_id = data.get("channel_id", "")
        name = data.get("name", "Webhook")
        return {"ok": True, "message": f"Discord вебхук «{name}» доступен (канал #{channel_id}). Проверка ничего не отправила."}
    return {"ok": False, "message": f"Discord вернул ошибку: HTTP {r.status_code}"}


async def _check_vk(cfg: dict) -> dict:
    token = os.environ.get(cfg.get("token_env") or "VK_ACCESS_TOKEN") or cfg.get("access_token")
    if not token:
        return {"ok": False, "message": "Токен доступа VK не задан. Укажите access_token или token_env."}
    owner_id = cfg.get("owner_id")
    if not owner_id:
        return {"ok": False, "message": "Не указан owner_id сообщества ВКонтакте."}
    base = (cfg.get("api_base") or "https://api.vk.com").rstrip("/")
    v = "5.199"

    async with httpx.AsyncClient(timeout=10.0) as client:
        group_id = abs(int(owner_id))
        r = await client.post(f"{base}/method/groups.getById", data={"access_token": token, "v": v, "group_id": group_id})
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "message": f"VK вернул HTTP {r.status_code}"}
        if "response" in data and data["response"]:
            resp = data["response"]
            groups = resp.get("groups", []) if isinstance(resp, dict) else resp
            group = groups[0] if groups else {}
            name = group.get("name", str(group_id))
            return {"ok": True, "message": f"Сообщество «{name}» найдено, токен действителен. Проверка ничего не опубликовала."}
        err = data.get("error", {})
        return {"ok": False, "message": f"VK ошибка: {err.get('error_msg', 'не удалось получить данные группы')}"}

