"""Картинки материалов. В БД хранится только URL (несколько десятков байт на материал), сами файлы не сохраняются:
интерфейс получает их через прокси API (с проверкой SSRF и лимитом размера), Telegram — прямой загрузкой файла."""
import re
from urllib.parse import urljoin, urlsplit

import httpx

from .netguard import check_url

MAX_IMAGE_URL = 1000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"""\b(src|data-src|width|height)\s*=\s*["']?([^"'\s>]+)""", re.I)
# трекинговые пиксели, иконки, аватары и рекламные баннеры не годятся как иллюстрация поста
_JUNK = re.compile(r"(pixel|spacer|blank\.|1x1|/favicon|/logo|/icon|/avatar|/emoji|/smil|gravatar|doubleclick|/ads?/|counter|dummy|placeholder|no-?image|default-?(image|thumb|cover)|stub)", re.I)


def normalize_image_url(url: str | None, base: str = "") -> str | None:
    if not url:
        return None
    url = urljoin(base, url.strip()) if base else url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > MAX_IMAGE_URL:
        return None
    if parts.path.lower().endswith((".svg", ".ico")) or _JUNK.search(url):
        return None
    return url


def _from_html(html: str, base: str) -> str | None:
    for tag in _IMG_TAG.findall(html or ""):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
        try:
            if int(attrs.get("width", 100)) < 100 or int(attrs.get("height", 100)) < 100:
                continue
        except ValueError:
            pass
        url = normalize_image_url(attrs.get("src") or attrs.get("data-src"), base)
        if url:
            return url
    return None


def feed_entry_image(entry, body_html: str) -> str | None:
    """Ищет иллюстрацию записи RSS/Atom: media:content/thumbnail, enclosure, поле image, затем первый <img> в тексте."""
    base = entry.get("link", "") or ""
    for m in list(entry.get("media_content") or []) + list(entry.get("media_thumbnail") or []):
        kind = (m.get("type") or m.get("medium") or "").lower()
        if kind and not kind.startswith("image"):
            continue
        url = normalize_image_url(m.get("url"), base)
        if url:
            return url
    for link in list(entry.get("enclosures") or []) + [ln for ln in entry.get("links") or [] if ln.get("rel") == "enclosure"]:
        if (link.get("type") or "").lower().startswith("image"):
            url = normalize_image_url(link.get("href") or link.get("url"), base)
            if url:
                return url
    image = entry.get("image")
    if isinstance(image, dict):
        url = normalize_image_url(image.get("href") or image.get("url"), base)
        if url:
            return url
    return _from_html(body_html, base)


async def download_image(url: str) -> tuple[bytes, str]:
    """Скачивает картинку с проверкой SSRF на каждом редиректе, лимитом размера и белым списком типов (без SVG)."""
    headers = {"User-Agent": "PulseBot/1.0 (+self-hosted)", "Accept": "image/*"}
    current = url
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, headers=headers) as client:
        for _ in range(4):
            await check_url(current)
            async with client.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = str(httpx.URL(current).join(r.headers["location"]))
                    continue
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in IMAGE_TYPES:
                    raise ValueError("Файл не является поддерживаемой картинкой")
                if int(r.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
                    raise ValueError("Картинка больше 5 МБ")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError("Картинка больше 5 МБ")
                return bytes(data), ctype
    raise ValueError("Слишком много редиректов")
