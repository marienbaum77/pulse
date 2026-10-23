"""Автоподбор источников: языковая модель придумывает поисковые запросы по теме канала, SearXNG находит статьи и видео,
а из выдачи собираются кандидаты — YouTube-каналы, сайты, сабреддиты и Telegram-каналы. Источник, который встречается
в выдаче по нескольким запросам, ценнее случайного попадания. Редактор подтверждает кандидатов вручную."""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from psycopg.types.json import Jsonb

from .. import db, events, jobs
from ..providers import LLMUnavailable, refresh_provider
from ..textutil import utcnow
from . import ingest
from .connectors import reddit, searx, site, telegram, youtube
from .process import get_project, project_topic_aspects

log = logging.getLogger("pulse.discovery")
MAX_QUERIES = 8
MAX_SITE_PROBES = 10
MAX_VIDEO_LOOKUPS = 30
LOOKUP_CONCURRENCY = 4
MAX_SUGGESTIONS = 30
# Каналов YouTube с одним попаданием в выдаче бывает два десятка: без лимита они вытеснили бы сайты и сообщества.
MAX_PER_TYPE = 10
MAX_SAMPLES = 3
SEARCH_CONCURRENCY = 3
# Площадки, которые сами по себе не источник: соцсети, энциклопедии, маркетплейсы, поисковики.
SKIP_DOMAINS = {
    "youtube.com", "youtu.be", "reddit.com", "redd.it", "t.me", "telegram.me", "wikipedia.org", "google.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "linkedin.com", "pinterest.com", "tiktok.com", "amazon.com", "ozon.ru",
    "wildberries.ru", "avito.ru", "yandex.ru", "dzen.ru", "vk.com", "ok.ru", "github.com", "stackoverflow.com", "quora.com",
}
QUERIES_SYSTEM = """Ты помогаешь редактору тематического канала найти источники контента. Тематика канала: {topic}. Язык канала: {language}.
Придумай {n} разных поисковых запросов, по которым находятся полезные статьи, блоги и видео по этой теме. Каждый запрос — конкретная подтема из 2–6 слов, а не общие слова вроде «новости» или «интересное».
Пиши запросы на языке канала; если по теме много англоязычного контента, добавь 2–3 запроса на английском.
Ответь ТОЛЬКО JSON-массивом строк без пояснений."""
RATE_SYSTEM = """Ты главный редактор тематического канала. Тематика: {topic}. Ниже кандидаты в источники контента с примерами найденных материалов.
Оцени каждого по шкале 0–10: насколько регулярно он даёт полезный контент точно по теме канала. Низко — магазины, агрегаторы без своего контента, случайные совпадения, другая тема.
Ответь ТОЛЬКО JSON-объектом вида {{"1": {{"score": 8, "reason": "одно короткое предложение"}}, "2": ...}}."""
KIND_LABELS = {"youtube": "YouTube-канал", "site": "сайт", "reddit": "сабреддит", "telegram": "Telegram-канал"}
_YOUTUBE_SUFFIX = re.compile(r"\s+[-–|]\s*YouTube\s*$")


@dataclass
class Candidate:
    type: str
    value: str
    name: str
    queries: set[str] = field(default_factory=set)
    samples: list[str] = field(default_factory=list)
    reason: str = ""
    rating: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.type, self.value

    @property
    def hits(self) -> int:
        return len(self.queries)

    def add(self, query: str, title: str) -> None:
        self.queries.add(query)
        if title and title not in self.samples and len(self.samples) < MAX_SAMPLES:
            self.samples.append(title)

    @property
    def score(self) -> float:
        # Повторяемость в выдаче по разным запросам и оценка модели; без модели — только повторяемость.
        repeat = min(1.0, self.hits / 3)
        return round(repeat if self.rating is None else 0.4 * repeat + 0.6 * self.rating / 10, 4)


def parse_queries(raw: str, limit: int = MAX_QUERIES) -> list[str]:
    match = re.search(r"\[.*\]", raw or "", re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except ValueError:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        query = " ".join(str(item).split())[:120]
        if len(query) >= 3 and query.casefold() not in seen:
            seen.add(query.casefold())
            out.append(query)
    return out[:limit]


def fallback_queries(project: dict) -> list[str]:
    """Без языковой модели запросы — это аспекты темы проекта."""
    return project_topic_aspects(project)[:MAX_QUERIES]


async def generate_queries(project: dict, provider) -> list[str]:
    if getattr(provider, "supports_chat", False):
        system = QUERIES_SYSTEM.format(topic=project["topic"] or project["name"], language=project["language"], n=MAX_QUERIES)
        aspects = project_topic_aspects(project)
        prompt = "Аспекты темы: " + "; ".join(aspects) if aspects else f"Тема: {project['topic'] or project['name']}"
        try:
            queries = parse_queries(await provider.chat(system, prompt, max_tokens=400, temperature=0.4, project_id=project["id"]))
            if queries:
                return queries
        except LLMUnavailable as e:
            log.info("discovery queries fall back to topic aspects: %s", e)
    return fallback_queries(project)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")


def _skipped(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)


def collect(results: dict[str, list[dict]]) -> tuple[dict[tuple[str, str], Candidate], list[tuple[str, str, str]]]:
    """Группирует выдачу по запросам в кандидатов-источников. results: запрос → найденные материалы (статьи и видео).
    Видео YouTube возвращаются отдельно как (запрос, ссылка, заголовок): канал видео поиск не сообщает надёжно,
    его узнаём позже через oEmbed."""
    found: dict[tuple[str, str], Candidate] = {}
    videos: list[tuple[str, str, str]] = []

    def note(cand: Candidate, query: str, title: str) -> None:
        found.setdefault(cand.key, cand).add(query, title)

    for query, entries in results.items():
        for e in entries:
            url, title = e.get("url", ""), e.get("title", "")
            host = _host(url)
            vid = youtube.video_id(url)
            if vid:
                # bing и duckduckgo дописывают к заголовку « - YouTube»
                videos.append((query, youtube.watch_url(vid), _YOUTUBE_SUFFIX.sub("", title)))
                continue
            if host in ("t.me", "telegram.me"):
                try:
                    channel = telegram.normalize_channel(url)
                except ValueError:
                    continue
                note(Candidate("telegram", f"@{channel}", f"@{channel}"), query, title)
                continue
            if host.endswith("reddit.com"):
                try:
                    sub = reddit.normalize_subreddit(url)
                except ValueError:
                    continue
                note(Candidate("reddit", f"r/{sub}", f"r/{sub}"), query, title)
                continue
            if host and not _skipped(host):
                note(Candidate("site", f"https://{host}/", host), query, title)
    return found, videos


async def video_channel(video_url: str) -> tuple[str, str] | None:
    """(ссылка на канал, название канала) по видео через oEmbed YouTube. Выдача SearXNG канал не сообщает,
    если видео нашёл не движок youtube, а bing или duckduckgo, — а движок youtube YouTube часто блокирует."""
    r = await ingest.http_get(
        "https://www.youtube.com/oembed?" + urlencode({"url": video_url, "format": "json"}), {"User-Agent": ingest.BROWSER_UA}
    )
    if r.status_code != 200:
        return None
    data = r.json() or {}
    author_url, author = data.get("author_url") or "", (data.get("author_name") or "").strip()
    return (author_url, author) if author_url.startswith("https://www.youtube.com/") and author else None


async def _youtube_channels(videos: list[tuple[str, str, str]]) -> list[Candidate]:
    """Группирует найденные видео по каналам: канал, чьи видео нашлись по нескольким запросам, — сильный кандидат."""
    counts: dict[str, int] = {}
    for _, url, _ in videos:
        counts[url] = counts.get(url, 0) + 1
    urls = sorted(counts, key=lambda u: -counts[u])[:MAX_VIDEO_LOOKUPS]
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def lookup(url: str) -> tuple[str, str] | None:
        async with semaphore:
            # Один повтор: кратковременный сбой DNS или сети иначе выбросил бы канал из подборки.
            for attempt in range(2):
                try:
                    return await video_channel(url)
                except Exception as e:
                    log.info("oembed failed for %s (attempt %d): %s", url, attempt + 1, e)
            return None

    owners = dict(zip(urls, await asyncio.gather(*(lookup(u) for u in urls))))
    channels: dict[str, Candidate] = {}
    for query, url, title in videos:
        owner = owners.get(url)
        if owner:
            channels.setdefault(owner[0], Candidate("youtube", owner[0], owner[1])).add(query, title)
    return list(channels.values())


async def _probe_sites(cands: list[Candidate]) -> list[Candidate]:
    """Оставляет сайты, которые реально можно читать: с RSS-лентой или со списком статей на главной."""
    out = []
    for cand in sorted(cands, key=lambda c: -c.hits)[:MAX_SITE_PROBES]:
        try:
            found = await site.discover(cand.value)
        except Exception as e:
            log.info("site probe failed for %s: %s", cand.value, e)
            continue
        if found["feed_url"]:
            cand.reason = "есть RSS-лента"
        elif found["links"]:
            cand.reason = "статьи со страницы, без RSS"
        else:
            continue
        out.append(cand)
    return out


def cap_per_type(cands: list[Candidate]) -> list[Candidate]:
    """Лучшие по повторяемости кандидаты каждого типа, не больше MAX_PER_TYPE на тип и MAX_SUGGESTIONS всего."""
    out: list[Candidate] = []
    per_type: dict[str, int] = {}
    for c in sorted(cands, key=lambda c: -c.hits):
        if per_type.get(c.type, 0) < MAX_PER_TYPE:
            per_type[c.type] = per_type.get(c.type, 0) + 1
            out.append(c)
    return out[:MAX_SUGGESTIONS]


def parse_ratings(raw: str) -> dict[int, tuple[float, str]]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    out = {}
    for key, value in data.items() if isinstance(data, dict) else []:
        try:
            score = float(value["score"] if isinstance(value, dict) else value)
        except (KeyError, TypeError, ValueError):
            continue
        reason = str(value.get("reason") or "").strip()[:200] if isinstance(value, dict) else ""
        if str(key).isdigit():
            out[int(key)] = (max(0.0, min(10.0, score)), reason)
    return out


async def _rate(project: dict, provider, cands: list[Candidate]) -> None:
    if not cands or not getattr(provider, "supports_chat", False):
        return
    lines = [f"{i}. {KIND_LABELS[c.type]} {c.name}: " + " | ".join(c.samples) for i, c in enumerate(cands, 1)]
    try:
        raw = await provider.chat(RATE_SYSTEM.format(topic=project["topic"] or project["name"]), "\n".join(lines),
                                  max_tokens=60 * len(cands) + 100, temperature=0.1, project_id=project["id"])
    except LLMUnavailable as e:
        log.info("discovery rating skipped: %s", e)
        return
    for i, (score, reason) in parse_ratings(raw).items():
        if 1 <= i <= len(cands):
            cands[i - 1].rating = score
            cands[i - 1].reason = "; ".join(part for part in (reason, cands[i - 1].reason) if part)


async def _existing_keys(project_id: int) -> set[tuple[str, str]]:
    """Уже подключённые источники и скрытые редактором кандидаты: их не предлагаем."""
    keys = {(r["type"], r["value"]) for r in await db.fetchall(
        "SELECT type, value FROM source_suggestions WHERE project_id = %s AND status <> 'new'", (project_id,))}
    for s in await db.fetchall("SELECT type, url, name, config FROM sources WHERE project_id = %s", (project_id,)):
        cfg = s["config"] or {}
        if s["type"] == "telegram" and cfg.get("channel"):
            keys.add(("telegram", f"@{cfg['channel']}"))
        elif s["type"] == "reddit" and cfg.get("subreddit"):
            keys.add(("reddit", f"r/{cfg['subreddit']}"))
        for url in (s["url"], cfg.get("feed_url") or ""):
            if url and s["type"] in ("rss", "site"):
                keys.add(("site", f"https://{_host(url)}/"))
        if s["type"] == "youtube":
            keys.add(("youtube-name", s["name"].removeprefix("YouTube: ").casefold()))
    return keys


async def _search_all(queries: list[str], language: str) -> dict[str, list[dict]]:
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def one(query: str) -> list[dict]:
        out: list[dict] = []
        async with semaphore:
            for category in ("general", "videos"):
                try:
                    found = await searx.search(query, language=language, categories=category)
                    # Из видеопоиска нужны только ролики YouTube (их находят и bing, и duckduckgo):
                    # зеркала и прочие видеохостинги текстовым источником не станут.
                    out += [e for e in found if youtube.video_id(e.get("url", ""))] if category == "videos" else found
                except searx.SearchUnavailable:
                    raise
                except Exception as e:
                    log.info("discovery search failed for %r: %s", query, e)
        return out

    return dict(zip(queries, await asyncio.gather(*(one(q) for q in queries))))


async def _set_status(project_id: int, **status) -> None:
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (f"discovery:{project_id}", Jsonb(status)),
    )
    await events.notify("discovery", project_id=project_id, status=status.get("status"))


async def run_discovery(project_id: int) -> int:
    """Находит кандидатов и сохраняет их; возвращает число новых или обновлённых предложений."""
    project = await get_project(project_id)
    if not project:
        return 0
    started = utcnow().isoformat()
    await _set_status(project_id, status="running", started_at=started)
    try:
        provider = await refresh_provider()
        queries = await generate_queries(project, provider)
        if not queries:
            raise ValueError("Не из чего составить запросы: задайте тему или аспекты темы в настройках проекта")
        found, videos = collect(await _search_all(queries, project["language"]))
        known = await _existing_keys(project_id)
        by_type: dict[str, list[Candidate]] = {}
        for cand in found.values():
            if cand.key not in known:
                by_type.setdefault(cand.type, []).append(cand)
        cands = [
            *[c for c in await _youtube_channels(videos) if ("youtube-name", c.name.casefold()) not in known and c.key not in known],
            *await _probe_sites(by_type.get("site", [])),
            *by_type.get("reddit", []),
            *by_type.get("telegram", []),
        ]
        cands = cap_per_type(cands)
        await _rate(project, provider, cands)
        saved = 0
        for c in cands:
            saved += await db.execute(
                "INSERT INTO source_suggestions(project_id, type, value, name, reason, score, hits, samples) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (project_id, type, value) DO UPDATE SET name = EXCLUDED.name, reason = EXCLUDED.reason, score = EXCLUDED.score, "
                "hits = EXCLUDED.hits, samples = EXCLUDED.samples, updated_at = now() WHERE source_suggestions.status = 'new'",
                (project_id, c.type, c.value, c.name[:120], c.reason, c.score, c.hits, Jsonb(c.samples)),
            )
    except Exception as e:
        await _set_status(project_id, status="failed", started_at=started, finished_at=utcnow().isoformat(), error=str(e)[:500])
        raise
    await _set_status(project_id, status="done", started_at=started, finished_at=utcnow().isoformat(), queries=queries, found=saved)
    log.info("discovery for project %s: %d queries, %d suggestions", project_id, len(queries), saved)
    return saved


@jobs.handler("discover_sources")
async def discover_sources_job(payload: dict) -> None:
    try:
        await run_discovery(int(payload["project_id"]))
    except (searx.SearchUnavailable, ValueError):
        return  # ошибка уже записана в статус подбора; повтор не поможет, пока не исправят настройки
