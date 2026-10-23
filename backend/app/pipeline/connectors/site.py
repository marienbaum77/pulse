"""Сайт без RSS: ищем ленту в <link rel="alternate">, иначе собираем ссылки на статьи со страницы-раздела.
Полный текст каждой статьи потом дозагружает общий механизм обогащения (trafilatura)."""
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .. import ingest
from ...textutil import utcnow

MAX_LINKS = 40
MIN_ANCHOR_CHARS = 20
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
# Разделы-служебки: навигация, авторы, теги, авторизация, пагинация.
_JUNK_PATH = re.compile(
    r"/(?:tags?|category|categories|rubric|topics?|authors?|users?|profile|login|signin|signup|register|search|page/\d+|"
    r"privacy|terms|about|contacts?|subscribe|cart|account|feed|rss)(?:/|$)", re.I,
)
_FILE_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|pdf|zip|mp3|mp4|xml|css|js)$", re.I)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def feed_links(page: str, base: str) -> list[str]:
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "alternate" in rel and (link.get("type") or "").lower() in _FEED_TYPES:
            out.append(urljoin(base, link["href"]))
    return list(dict.fromkeys(out))


def _looks_like_article(path: str) -> bool:
    segments = [s for s in path.split("/") if s]
    if not segments or _JUNK_PATH.search(path) or _FILE_EXT.search(path):
        return False
    last = segments[-1]
    # Статья обычно — длинный слаг с дефисами, числовой ID или вложенный путь; короткое /news — это раздел.
    return len(segments) >= 2 or "-" in last or "_" in last or any(ch.isdigit() for ch in last) or len(last) >= 20


def article_links(page: str, base: str, limit: int = MAX_LINKS) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup(["nav", "footer", "header", "aside", "script", "style", "form"]):
        tag.decompose()
    host = _host(base)
    base_path = urlsplit(base).path.rstrip("/")
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"].strip())
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or _host(url) != host:
            continue
        clean = urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
        if clean in seen or p.path.rstrip("/") == base_path or not _looks_like_article(p.path):
            continue
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) < MIN_ANCHOR_CHARS or len(title.split()) < 3:
            continue
        seen.add(clean)
        out.append({"title": title[:300], "url": clean})
        if len(out) >= limit:
            break
    return out


async def discover(url: str) -> dict:
    """Предпросмотр сайта: адрес найденной ленты и ссылки на статьи, если ленты нет."""
    page = await ingest.fetch_html(url)
    if page is None:
        raise ValueError("По адресу не HTML-страница")
    html, final_url = page
    feeds = feed_links(html, final_url)
    return {"feed_url": feeds[0] if feeds else None, "links": article_links(html, final_url)}


async def fetch(src: dict) -> tuple[list[dict], dict]:
    cfg = src.get("config") or {}
    if cfg.get("feed_url"):
        return await ingest.fetch_feed(cfg["feed_url"], src.get("etag"), src.get("last_modified"))
    page = await ingest.fetch_html(src["url"])
    if page is None:
        raise ValueError("По адресу не HTML-страница")
    html, final_url = page
    now = utcnow()
    return [{**link, "text": "", "published_at": now, "publisher_name": _host(final_url) or None} for link in article_links(html, final_url)], {}
