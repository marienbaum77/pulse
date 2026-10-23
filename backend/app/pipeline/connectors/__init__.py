"""Коннекторы источников. Каждый возвращает (записи, состояние ленты): запись — dict как у ingest.parse_feed,
плюс необязательные kind (article/video/post) и meta (автор, просмотры и т. п.); состояние — etag/last_modified для лент."""
from urllib.parse import urlencode, urlsplit

from .. import ingest
from . import reddit, searx, site, telegram, youtube

SOURCE_TYPES = ("rss", "youtube", "web_search", "site", "reddit", "telegram")
# В смешанном профиле эти источники по умолчанию дают новости (сюжеты), остальные — подборочный контент.
NEWS_DEFAULT_TYPES = frozenset({"rss", "telegram", "manual"})
MODES = ("news", "curated")


async def _rss(src: dict) -> tuple[list[dict], dict]:
    return await ingest.fetch_feed(src["url"], src.get("etag"), src.get("last_modified"))


FETCHERS = {
    "rss": _rss,
    "youtube": youtube.fetch,
    "web_search": searx.fetch,
    "site": site.fetch,
    "reddit": reddit.fetch,
    "telegram": telegram.fetch,
}


async def fetch(src: dict) -> tuple[list[dict], dict]:
    return await FETCHERS[src["type"]](src)


def item_mode(profile: str, source_type: str, cfg: dict | None) -> str:
    """Как отбирать материалы источника: профиль news/curated действует на всё; в mixed решает источник."""
    if profile in MODES:
        return profile
    override = (cfg or {}).get("mode")
    if override in MODES:
        return override
    return "news" if source_type in NEWS_DEFAULT_TYPES else "curated"


async def prepare(source_type: str, value: str, options: dict) -> dict:
    """Превращает ввод редактора (ссылку, @handle, запрос) в name/url/config источника. Может ходить в сеть:
    у YouTube нужно узнать ID канала по handle, у сайта — поискать RSS-ленту."""
    value = (value or "").strip()
    cfg: dict = {}
    if source_type == "youtube":
        cfg, name = await youtube.resolve(value)
        if cfg["kind"] == "search":
            cfg["time_range"] = options.get("time_range") if options.get("time_range") in searx.TIME_RANGES else None
            url = "https://www.youtube.com/results?" + urlencode({"search_query": cfg["query"]})
        else:
            url = youtube.feed_url(cfg)
    elif source_type == "web_search":
        if len(value) < 2:
            raise ValueError("Запрос слишком короткий")
        cfg = {"query": value, "time_range": options.get("time_range") if options.get("time_range") in searx.TIME_RANGES else None,
               "category": options.get("category") if options.get("category") in searx.CATEGORIES else "general"}
        name, url = f"Веб-поиск: {value}"[:120], ""
    elif source_type == "site":
        url = value if "://" in value else f"https://{value}"
        found = await site.discover(url)
        if found["feed_url"]:
            cfg = {"feed_url": found["feed_url"]}
        elif not found["links"]:
            raise ValueError("На странице не нашлось ни RSS-ленты, ни ссылок на статьи: укажите страницу раздела со списком материалов")
        name = (urlsplit(url).hostname or url).removeprefix("www.")[:120]
    elif source_type == "reddit":
        sub = reddit.normalize_subreddit(value)
        cfg = {"subreddit": sub, "sort": options.get("sort") if options.get("sort") in reddit.SORTS else "top",
               "period": options.get("period") if options.get("period") in reddit.PERIODS else "week"}
        name, url = f"r/{sub}", f"https://www.reddit.com/r/{sub}/"
    elif source_type == "telegram":
        channel = telegram.normalize_channel(value)
        cfg = {"channel": channel}
        name, url = f"@{channel}", f"https://t.me/s/{channel}"
    else:
        raise ValueError(f"Неизвестный тип источника: {source_type}")
    if options.get("mode") in MODES:
        cfg["mode"] = options["mode"]
    return {"name": name, "url": url, "config": cfg}
