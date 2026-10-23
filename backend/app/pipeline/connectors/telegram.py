"""Публичный Telegram-канал через веб-превью t.me/s/<канал>: без бота и без входа в аккаунт."""
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .. import ingest
from ...images import normalize_image_url
from ...textutil import utcnow

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_NAME_IN_URL = re.compile(r"t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#]|$)")
_BG_URL = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)")
TITLE_CHARS = 140


def normalize_channel(value: str) -> str:
    value = (value or "").strip()
    match = _NAME_IN_URL.search(value)
    name = match.group(1) if match else value.removeprefix("@")
    if not _NAME.match(name):
        raise ValueError("Укажите публичный канал: @name или ссылку t.me/name")
    return name


def parse_views(value: str) -> int | None:
    value = (value or "").strip().upper().replace(",", ".")
    match = re.match(r"^([\d.]+)\s*([KM]?)$", value)
    if not match:
        return None
    number = float(match.group(1)) * {"": 1, "K": 1_000, "M": 1_000_000}[match.group(2)]
    return int(number)


def _title(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first) <= TITLE_CHARS:
        return first
    cut = first[:TITLE_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(" ,:;—–-") + "…"


def parse_channel_page(page: str, channel: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        body = msg.select_one(".tgme_widget_message_text")
        if body is None:
            continue  # медиа без подписи: писать пост не из чего
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = "\n".join(line.strip() for line in body.get_text().splitlines()).strip()
        title = _title(text)
        if not title:
            continue
        stamp = msg.select_one("time[datetime]")
        try:
            published = datetime.fromisoformat(stamp["datetime"]) if stamp else utcnow()
        except ValueError:
            published = utcnow()
        image = None
        photo = msg.select_one(".tgme_widget_message_photo_wrap[style]")
        if photo:
            match = _BG_URL.search(photo["style"])
            image = normalize_image_url(match.group(1)) if match else None
        views_tag = msg.select_one(".tgme_widget_message_views")
        views = parse_views(views_tag.get_text()) if views_tag else None
        meta = {"channel": channel}
        if views is not None:
            meta["views"] = views
        out.append({
            "title": title, "url": f"https://t.me/{msg['data-post']}", "text": text[: ingest.MAX_TEXT], "published_at": published,
            "image_url": image, "publisher_name": f"@{channel}", "kind": "post", "meta": meta,
        })
    return out[-ingest.MAX_ENTRIES:]


async def fetch(src: dict) -> tuple[list[dict], dict]:
    channel = (src.get("config") or {})["channel"]
    r = await ingest.http_get(f"https://t.me/s/{channel}", {"User-Agent": ingest.BROWSER_UA})
    r.raise_for_status()
    items = parse_channel_page(r.text, channel)
    if not items and "tgme_channel_info" not in r.text:
        raise ValueError("Канал не найден или у него нет публичного веб-превью")
    return items, {}
