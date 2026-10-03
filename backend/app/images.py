"""Картинки материалов. В БД хранится только URL (несколько десятков байт на материал); сами файлы при первом запросе
скачиваются с сайта-источника (проверка SSRF, лимит размера) и кладутся в дисковый кеш. Интерфейс получает их через прокси API,
Telegram — загрузкой файла; повторные показы и публикация берут картинку из кеша, а не с чужого сайта."""
import asyncio
import hashlib
import logging
import os
import re
import struct
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .config import get_settings
from .netguard import check_url

log = logging.getLogger("pulse.images")

MAX_IMAGE_URL = 1000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# Минимальный размер обложки: мелкие картинки (логотипы, иконки, старые превью) на посте выглядят размыто.
MIN_COVER_WIDTH = 300
MIN_COVER_HEIGHT = 150
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"""\b(src|data-src|width|height)\s*=\s*["']?([^"'\s>]+)""", re.I)
# трекинговые пиксели, иконки, аватары и рекламные баннеры не годятся как иллюстрация поста;
# отдельно ловим логотипы и карточки для соцсетей — это оформление сайта, а не картинка статьи
_JUNK = re.compile(
    r"(pixel|spacer|blank\.|1x1|/favicon|/logo|/icon|/avatar|/emoji|/smil|gravatar|doubleclick|/ads?/|counter|dummy|placeholder|no-?image|default-?(image|thumb|cover)|stub"
    r"|(?:^|[/_.-])logo(?:[/_.-]|$)|(?:^|[/_.-])social(?:[/_.-]|$)|opengraph|twitter[-_.]?card|sprite)",
    re.I,
)
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


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


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Размеры картинки из её заголовка, без полного декодирования. Поддержаны PNG, JPEG, GIF, WebP."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        return _jpeg_dimensions(data)
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            break
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    fmt = data[12:16]
    if fmt == b"VP8X" and len(data) >= 30:
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    if fmt == b"VP8 " and len(data) >= 30:
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if fmt == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        return ((b0 | ((b1 & 0x3F) << 8)) & 0x3FFF) + 1, (((b3 & 0x0F) << 10) | (b2 << 2) | (b1 >> 6)) + 1
    return None


def cover_quality(data: bytes) -> bool | None:
    """Годится ли картинка на обложку: True — да, False — мелкая, None — размер определить не удалось."""
    size = image_dimensions(data)
    if size is None:
        return None
    return size[0] >= MIN_COVER_WIDTH and size[1] >= MIN_COVER_HEIGHT



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


_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_TYPE = {ext: ctype for ctype, ext in _EXT.items()}
_FAILED_TTL = 600.0
_failed: dict[str, float] = {}  # url -> когда сбой истечёт: не долбим недоступный сайт при каждом показе страницы


class ImageUnavailable(Exception):
    pass


def image_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def cache_dir() -> Path:
    configured = get_settings().image_cache_dir
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "pulse-images"


def _cache_file(url: str) -> Path | None:
    key = image_key(url)
    for ext in _TYPE:
        path = cache_dir() / key[:2] / f"{key}.{ext}"
        if path.is_file():
            return path
    return None


def _read_cached(url: str) -> tuple[bytes, str] | None:
    path = _cache_file(url)
    if path is None:
        return None
    try:
        data = path.read_bytes()
        os.utime(path)  # «использовано недавно»: чистка удаляет прежде всего давно не нужные файлы
    except OSError:
        return None
    return data, _TYPE[path.suffix.lstrip(".")]


def _write_cached(url: str, data: bytes, ctype: str) -> None:
    key = image_key(url)
    folder = cache_dir() / key[:2]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{key}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, folder / f"{key}.{_EXT[ctype]}")
    except OSError:
        log.warning("не удалось записать картинку в кеш %s", cache_dir(), exc_info=True)


async def get_image(url: str) -> tuple[bytes, str]:
    """Картинка из дискового кеша или, если её там нет, с исходного сайта (и тогда она сохраняется в кеш)."""
    cached = await asyncio.to_thread(_read_cached, url)
    if cached:
        return cached
    if _failed.get(url, 0.0) > time.monotonic():
        raise ImageUnavailable(url)
    try:
        data, ctype = await download_image(url)
    except Exception as e:
        _failed[url] = time.monotonic() + _FAILED_TTL
        if len(_failed) > 2000:
            now = time.monotonic()
            for key in [k for k, until in _failed.items() if until < now]:
                del _failed[key]
        raise ImageUnavailable(url) from e
    await asyncio.to_thread(_write_cached, url, data, ctype)
    return data, ctype


def prune_cache(max_age_days: int, max_bytes: int) -> int:
    """Удаляет файлы, к которым не обращались дольше max_age_days, затем самые старые, пока кеш не уложится в max_bytes."""
    root = cache_dir()
    if not root.is_dir():
        return 0
    files = []
    for path in root.rglob("*"):
        try:
            if path.is_file():
                st = path.stat()
                files.append((st.st_mtime, st.st_size, path))
        except OSError:
            continue
    removed, total = 0, sum(size for _, size, _ in files)
    cutoff = time.time() - max_age_days * 86400
    for mtime, size, path in sorted(files):
        if mtime >= cutoff and total <= max_bytes:
            break
        try:
            path.unlink()
            removed += 1
            total -= size
        except OSError:
            continue
    return removed
