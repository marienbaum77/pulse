"""Картинки материалов. В БД хранится только URL (несколько десятков байт на материал); сами файлы при первом запросе
скачиваются с сайта-источника (проверка SSRF, лимит размера) и кладутся в дисковый кеш. Интерфейс получает их через прокси API,
Telegram — загрузкой файла; повторные показы и публикация берут картинку из кеша, а не с чужого сайта."""
import asyncio
import hashlib
import io
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
from .netguard import guarded_client

log = logging.getLogger("pulse.images")

MAX_IMAGE_URL = 1000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
IMAGE_RETRY_DELAYS = (0.5, 1.5)
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


# Сравнение картинок по содержимому: уменьшенные копии в оттенках серого и нормированная корреляция (1 — совпадают).
SAME_WIDTH = 128  # ширина, к которой приводится картинка при точном сравнении
COARSE_WIDTH = 64  # и при грубом поиске кадрированной части
SAME_MIN = 0.9  # корреляция, начиная с которой это одна картинка
COARSE_MIN = 0.75  # грубый поиск нашёл кандидата — уточняем
# Какую долю ширины большой картинки занимает кадрированная: 0,35–0,95. Пик корреляции узкий (у обложки Unity и её
# кадра у Хабра — 0,92 на доле 0,66 и уже 0,80 на 0,70), поэтому шаг мелкий, а затем уточнение вокруг лучшей доли.
CROP_SCALES = tuple(x / 100 for x in range(35, 96, 2))


def _gray(data: bytes):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("L")
        img.thumbnail((4 * SAME_WIDTH, 16 * SAME_WIDTH), Image.BOX)  # один раз декодируем и уменьшаем, дальше — из этой копии
        return img


def _array(img, width: int):
    import numpy as np
    from PIL import Image

    height = max(1, round(img.height * width / img.width))
    return np.asarray(img.resize((max(1, width), height), Image.BOX), dtype=np.float32)


def _best_match(big, small) -> float:
    """Наибольшая корреляция small по всем положениям внутри big."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    if small.shape[0] > big.shape[0] or small.shape[1] > big.shape[1]:
        return 0.0
    windows = sliding_window_view(big, small.shape)
    s = small - small.mean()
    w = windows - windows.mean(axis=(-1, -2), keepdims=True)
    num = (w * s).sum(axis=(-1, -2))
    den = np.sqrt((w * w).sum(axis=(-1, -2)) * (s * s).sum())
    return float((num / np.maximum(den, 1e-6)).max())


def _best_scale(big, other, width: int, scales) -> tuple[float, float]:
    """(лучшая корреляция, доля ширины), с которой other лучше всего находится внутри big."""
    best = (0.0, 0.0)
    for scale in scales:
        score = _best_match(big, _array(other, round(width * scale)))
        if score > best[0]:
            best = (score, scale)
    return best


def same_picture(a: bytes, b: bytes) -> bool:
    """Одна и та же картинка: в другом размере или формате (у Хабра одна иллюстрация лежит под двумя именами) или
    кадрированная — приближенная часть другой (Хабр обрезает обложку первоисточника). Нечитаемая картинка — не совпадает."""
    try:
        img_a, img_b = _gray(a), _gray(b)
    except Exception:
        return False
    full_a, full_b = _array(img_a, SAME_WIDTH), _array(img_b, SAME_WIDTH)
    if abs(full_a.shape[0] - full_b.shape[0]) <= 2:
        rows = min(full_a.shape[0], full_b.shape[0])
        if _best_match(full_a[:rows], full_b[:rows]) >= SAME_MIN:
            return True
    for big, other, full in ((img_a, img_b, full_a), (img_b, img_a, full_b)):
        score, scale = _best_scale(_array(big, COARSE_WIDTH), other, COARSE_WIDTH, CROP_SCALES)
        if score >= COARSE_MIN:
            near = [scale + d / 100 for d in range(-3, 4) if 0 < scale + d / 100 < 1]
            if _best_scale(full, other, SAME_WIDTH, near)[0] >= SAME_MIN:
                return True
    return False



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
    for attempt in range(len(IMAGE_RETRY_DELAYS) + 1):
        try:
            return await _download_image_once(url)
        except httpx.TransportError:
            if attempt == len(IMAGE_RETRY_DELAYS):
                raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in IMAGE_RETRY_STATUSES or attempt == len(IMAGE_RETRY_DELAYS):
                raise
            retry_after = e.response.headers.get("retry-after", "")
            if retry_after.isdigit():
                await asyncio.sleep(min(float(retry_after), 10.0))
                continue
        await asyncio.sleep(IMAGE_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


async def _download_image_once(url: str) -> tuple[bytes, str]:
    headers = {"User-Agent": "PulseBot/1.0 (+self-hosted)", "Accept": "image/*"}
    current = url
    async with guarded_client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, headers=headers) as client:
        for _ in range(4):
            async with client.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = str(httpx.URL(current).join(r.headers["location"]))
                    continue
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype.startswith(("text/", "application/json", "application/xml", "video/", "audio/")):
                    raise ValueError("Файл не является поддерживаемой картинкой")
                if int(r.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
                    raise ValueError("Картинка больше 5 МБ")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError("Картинка больше 5 МБ")
                if ctype in IMAGE_TYPES:
                    return bytes(data), ctype
                return await asyncio.to_thread(normalize_image, bytes(data))
    raise ValueError("Слишком много редиректов")


def sniff_image_type(data: bytes) -> str | None:
    """Тип картинки по первым байтам. Серверы нередко отдают WebP и PNG как application/octet-stream
    (так делает Google Cloud Storage для картинок блога Google), поэтому заголовку Content-Type не верим."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalize_image(data: bytes) -> tuple[bytes, str]:
    """(данные, MIME) картинки, которую примут Telegram и ВКонтакте. JPEG, PNG, WebP и GIF — как есть, остальное,
    что умеет открыть Pillow (AVIF, BMP, TIFF, ICO в большом размере), перекодируется в JPEG. Не картинка — ValueError."""
    known = sniff_image_type(data)
    if known:
        return data, known
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rgb = img.convert("RGB")
        out = io.BytesIO()
        rgb.save(out, "JPEG", quality=90, optimize=True)
    except Exception as e:
        raise ValueError("Файл не является поддерживаемой картинкой") from e
    return out.getvalue(), "image/jpeg"


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
