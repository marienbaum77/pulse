"""Reddit: Atom-лента сабреддита (hot/top/new). Анонимный JSON API Reddit закрыт (403), а RSS открыт, но без счётчика апвоутов.
Ссылочный пост ведёт на внешнюю статью или видео — их текст потом дозагружается."""
import re
from urllib.parse import urlencode, urlsplit

import feedparser
from bs4 import BeautifulSoup

from .. import ingest
from ...images import normalize_image_url
from ...textutil import strip_html
from . import youtube

SORTS = ("hot", "top", "new")
PERIODS = ("day", "week", "month", "year")
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_SUB_IN_URL = re.compile(r"(?:^|/)r/([A-Za-z0-9_]{2,21})(?:/|$)")


def normalize_subreddit(value: str) -> str:
    value = (value or "").strip()
    match = _SUB_IN_URL.search(value)
    name = match.group(1) if match else value.removeprefix("r/").strip("/")
    if not _SUBREDDIT.match(name):
        raise ValueError("Укажите сабреддит: название, r/name или ссылку reddit.com/r/name")
    return name


def listing_url(cfg: dict) -> str:
    sort = cfg.get("sort") if cfg.get("sort") in SORTS else "top"
    url = f"https://www.reddit.com/r/{cfg['subreddit']}/{sort}/.rss"
    if sort == "top":
        url += "?" + urlencode({"t": cfg.get("period") if cfg.get("period") in PERIODS else "week"})
    return url


def _is_reddit(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith("reddit.com") or host.endswith("redd.it")


def parse_feed(content: bytes, subreddit: str) -> list[dict]:
    out = []
    for e in feedparser.parse(content).entries[: ingest.MAX_ENTRIES]:
        title, permalink = strip_html(e.get("title", "")), e.get("link", "")
        if not title or not permalink:
            continue
        soup = BeautifulSoup((e.get("content") or [{}])[0].get("value", ""), "html.parser")
        link = next((a["href"] for a in soup.find_all("a", href=True) if a.get_text(strip=True) == "[link]"), "")
        # [link] у текстового поста указывает на сам пост, у ссылочного — на внешнюю статью или видео
        is_self = not link or _is_reddit(link)
        body = soup.select_one("div.md")
        vid = youtube.video_id(link) if not is_self else None
        kind = "video" if vid else "post" if is_self else "article"
        thumbs = e.get("media_thumbnail") or []
        out.append({
            "title": title,
            "url": youtube.watch_url(vid) if vid else permalink if is_self else link,
            "text": " ".join(body.get_text(" ", strip=True).split())[: ingest.MAX_TEXT] if body else "",
            "published_at": ingest.entry_time(e),
            "image_url": normalize_image_url(thumbs[0].get("url")) if thumbs else None,
            "publisher_name": f"r/{subreddit}",
            "kind": kind,
            "meta": {"permalink": permalink, "author": e.get("author") or None},
        })
    return out


async def fetch(src: dict) -> tuple[list[dict], dict]:
    cfg = src.get("config") or {}
    # С «ботовым» User-Agent Reddit отвечает 200 с пустым телом, поэтому представляемся браузером.
    r = await ingest.http_get(listing_url(cfg), {"User-Agent": ingest.BROWSER_UA, "Accept": "application/atom+xml"})
    if r.status_code in (403, 429):
        raise ValueError(f"Reddit ограничил доступ ({r.status_code}): уменьшите частоту опроса или повторите позже")
    r.raise_for_status()
    if not r.content.strip():
        raise ValueError("Reddit вернул пустую ленту: вероятно, временное ограничение по IP")
    return parse_feed(r.content, cfg["subreddit"]), {}
