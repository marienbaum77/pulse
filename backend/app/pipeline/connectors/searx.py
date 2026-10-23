"""Веб-поиск через self-hosted SearXNG (JSON API): статьи с произвольных сайтов и видео YouTube по запросу."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from ...config import get_settings
from ...images import normalize_image_url
from ...textutil import strip_html, utcnow

MAX_RESULTS = 40
TIME_RANGES = ("day", "week", "month", "year")
CATEGORIES = ("general", "news", "videos", "science", "it")


class SearchUnavailable(RuntimeError):
    pass


def _language(code: str) -> str:
    code = (code or "").casefold().split("-", 1)[0]
    return code if len(code) == 2 else "all"


def _published(value) -> datetime:
    if isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return utcnow()


def publisher(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.") or None


def parse_results(data: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for r in data.get("results") or []:
        url, title = (r.get("url") or "").strip(), strip_html(r.get("title") or "")
        if not url.startswith(("http://", "https://")) or not title or url in seen:
            continue
        seen.add(url)
        meta = {key: r[key] for key in ("engine", "author", "length") if r.get(key)}
        if isinstance(r.get("views"), int):
            meta["views"] = r["views"]
        out.append({
            "title": title, "url": url, "text": strip_html(r.get("content") or ""),
            "published_at": _published(r.get("publishedDate")),
            "image_url": normalize_image_url(r.get("img_src") or r.get("thumbnail"), url),
            "publisher_name": publisher(url), "meta": meta,
        })
        if len(out) >= MAX_RESULTS:
            break
    return out


async def search(query: str, *, language: str, categories: str = "general", engines: str | None = None, time_range: str | None = None) -> list[dict]:
    base = get_settings().searxng_url.rstrip("/")
    if not base:
        raise SearchUnavailable("Веб-поиск не настроен: задайте SEARXNG_URL (в docker-compose это сервис searxng)")
    params = {"q": query, "format": "json", "language": _language(language), "categories": categories, "safesearch": 1}
    if engines:
        params["engines"] = engines
    if time_range in TIME_RANGES:
        params["time_range"] = time_range
    # Адрес SearXNG задан администратором и обычно внутренний (http://searxng:8080), поэтому без SSRF-фильтра.
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "PulseBot/1.0 (+self-hosted)"}) as client:
        try:
            r = await client.get(f"{base}/search", params=params)
        except httpx.HTTPError as e:
            raise SearchUnavailable(f"SearXNG недоступен: {e}") from e
    if r.status_code == 403:
        raise SearchUnavailable("SearXNG отклонил запрос: включите формат json в search.formats (settings.yml)")
    r.raise_for_status()
    return parse_results(r.json())


async def fetch(src: dict) -> tuple[list[dict], dict]:
    cfg = src.get("config") or {}
    category = cfg.get("category") if cfg.get("category") in CATEGORIES else "general"
    return await search(cfg["query"], language=src.get("language") or "ru", categories=category, time_range=cfg.get("time_range")), {}
