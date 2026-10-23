"""YouTube без ключа API: RSS каналов и плейлистов, поиск видео через SearXNG, субтитры как текст материала."""
import asyncio
import html as html_lib
import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import feedparser

from ... import config
from .. import ingest
from ...textutil import strip_html
from . import searx

log = logging.getLogger("pulse.youtube")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtube-nocookie.com", "youtube-nocookie.com"}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,100}$")
_PAGE_CHANNEL_ID = (
    re.compile(r'<meta itemprop="identifier" content="(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"channelId":"(UC[A-Za-z0-9_-]{22})"'),
)
_OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
# Служебные пометки автосубтитров: [Музыка], [Аплодисменты], [Music] — смысла не несут.
_CAPTION_NOISE = re.compile(r"\[[^\]]{1,30}\]")
_SENTENCE_END = re.compile(r"[.!?…]$")
CAPTION_CHUNK_WORDS = 30
TRANSCRIPT_TIMEOUT = 20


def video_id(url: str) -> str | None:
    """ID видео из любой формы ссылки: watch?v=, youtu.be/, /shorts/, /embed/, /live/."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host not in _HOSTS:
        return None
    parts = [part for part in p.path.split("/") if part]
    if host == "youtu.be":
        candidate = parts[0] if parts else ""
    elif parts[:1] == ["watch"]:
        candidate = (parse_qs(p.query).get("v") or [""])[0]
    elif len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
        candidate = parts[1]
    else:
        return None
    return candidate if _VIDEO_ID.match(candidate) else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def thumbnail_url(vid: str) -> str:
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def classify_input(value: str) -> dict:
    """Что ввёл редактор: ссылку на канал/плейлист, @handle, ID канала или поисковый запрос."""
    value = (value or "").strip()
    if _CHANNEL_ID.match(value):
        return {"kind": "channel", "channel_id": value}
    if _HANDLE.match(value):
        return {"kind": "handle", "path": value}
    try:
        p = urlsplit(value if "://" in value else f"https://{value}")
    except ValueError:
        return {"kind": "search", "query": value}
    if (p.hostname or "").lower() not in _HOSTS or " " in value:
        return {"kind": "search", "query": value}
    playlist = (parse_qs(p.query).get("list") or [""])[0]
    if playlist:
        return {"kind": "playlist", "playlist_id": playlist}
    parts = [part for part in p.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and _CHANNEL_ID.match(parts[1]):
        return {"kind": "channel", "channel_id": parts[1]}
    if parts and (parts[0].startswith("@") or (parts[0] in ("c", "user") and len(parts) >= 2)):
        return {"kind": "handle", "path": "/".join(parts[:1] if parts[0].startswith("@") else parts[:2])}
    raise ValueError("Ссылка YouTube не похожа на канал или плейлист: вставьте адрес канала (youtube.com/@name), плейлиста (?list=…) или поисковый запрос")


def channel_id_from_page(page: str) -> str | None:
    for pattern in _PAGE_CHANNEL_ID:
        match = pattern.search(page)
        if match:
            return match.group(1)
    return None


async def resolve(value: str) -> tuple[dict, str]:
    """Нормализует ввод в конфигурацию источника и предлагает название. @handle превращается в ID канала:
    у RSS YouTube нет адреса по handle, поэтому ID достаём со страницы канала."""
    info = classify_input(value)
    if info["kind"] == "search":
        if len(info["query"]) < 2:
            raise ValueError("Запрос слишком короткий")
        return {"kind": "search", "query": info["query"]}, f"YouTube: {info['query']}"[:120]
    if info["kind"] == "playlist":
        return {"kind": "playlist", "playlist_id": info["playlist_id"]}, "Плейлист YouTube"
    title = ""
    if info["kind"] == "handle":
        # Cookie согласия: из ЕС без неё YouTube перенаправляет на consent.youtube.com вместо страницы канала.
        r = await ingest.http_get(f"https://www.youtube.com/{info['path']}", {"User-Agent": ingest.BROWSER_UA, "Cookie": "SOCS=CAI; CONSENT=YES+1"})
        r.raise_for_status()
        page = r.text
        channel = channel_id_from_page(page)
        if not channel:
            raise ValueError("Не удалось определить ID канала по ссылке: вставьте адрес вида youtube.com/channel/UC…")
        match = _OG_TITLE.search(page)
        title = html_lib.unescape(match.group(1)).strip() if match else ""
        info = {"kind": "channel", "channel_id": channel}
    return info, (f"YouTube: {title}" if title else "Канал YouTube")[:120]


def feed_url(cfg: dict) -> str:
    if cfg.get("kind") == "playlist":
        return "https://www.youtube.com/feeds/videos.xml?" + urlencode({"playlist_id": cfg["playlist_id"]})
    return "https://www.youtube.com/feeds/videos.xml?" + urlencode({"channel_id": cfg["channel_id"]})


def _int(value) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_video_feed(content: bytes) -> list[dict]:
    """Atom-лента YouTube: описание, превью, просмотры и число оценок — из media:group."""
    out = []
    for e in feedparser.parse(content).entries[: ingest.MAX_ENTRIES]:
        vid = e.get("yt_videoid") or video_id(e.get("link", ""))
        title = strip_html(e.get("title", ""))
        if not vid or not title:
            continue
        thumbs = e.get("media_thumbnail") or []
        meta = {"video_id": vid, "author": e.get("author") or None}
        views = _int((e.get("media_statistics") or {}).get("views"))
        likes = _int((e.get("media_starrating") or {}).get("count"))
        if views is not None:
            meta["views"] = views
        if likes is not None:
            meta["likes"] = likes
        out.append({
            "title": title, "url": watch_url(vid), "text": strip_html(e.get("summary", ""))[: ingest.MAX_TEXT],
            "published_at": ingest.entry_time(e), "image_url": (thumbs[0].get("url") if thumbs else None) or thumbnail_url(vid),
            "publisher_name": e.get("author") or None, "kind": "video", "meta": meta,
        })
    return out


def search_result_to_video(entry: dict) -> dict | None:
    vid = video_id(entry.get("url", ""))
    if not vid:
        return None
    return {**entry, "url": watch_url(vid), "image_url": entry.get("image_url") or thumbnail_url(vid), "kind": "video",
            "meta": {**entry.get("meta", {}), "video_id": vid}}


async def fetch(src: dict) -> tuple[list[dict], dict]:
    cfg = src.get("config") or {}
    if cfg.get("kind") == "search":
        # Ролики YouTube находят и движок youtube, и bing/duckduckgo; последние выручают, когда YouTube блокирует SearXNG.
        # Параметр engines тут не помогает: SearXNG объединяет его с категорией, поэтому лишнее отсекаем по ссылке.
        results = await searx.search(cfg["query"], language=src.get("language") or "ru", categories="videos",
                                     time_range=cfg.get("time_range") or None)
        return [video for video in map(search_result_to_video, results) if video], {}
    return await ingest.fetch_feed(feed_url(cfg), src.get("etag"), src.get("last_modified"), parser=parse_video_feed)


def captions_to_text(snippets: list[str]) -> str:
    """Склеивает строки субтитров. В автосубтитрах нет пунктуации — тогда режем текст на куски по ~30 слов,
    иначе разбиение на предложения выдаёт один гигантский абзац и выбор опорных фраз для поста не работает."""
    words = " ".join(_CAPTION_NOISE.sub(" ", s) for s in snippets).split()
    if not words:
        return ""
    text = " ".join(words)
    if text.count(".") + text.count("?") + text.count("!") >= len(words) / 40:
        return text
    chunks = [" ".join(words[i : i + CAPTION_CHUNK_WORDS]) for i in range(0, len(words), CAPTION_CHUNK_WORDS)]
    return " ".join(chunk if _SENTENCE_END.search(chunk) else chunk + "." for chunk in chunks)


def _transcript_sync(vid: str, languages: list[str]) -> str:
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig

    class TimeoutSession(requests.Session):
        # Библиотека ходит в сеть без таймаута: зависшее соединение держало бы задачу обогащения бесконечно.
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", TRANSCRIPT_TIMEOUT)
            return super().request(*args, **kwargs)

    proxy = config.get_settings().youtube_proxy_url
    api = YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None, http_client=TimeoutSession()
    )
    transcripts = api.list(vid)
    try:
        transcript = transcripts.find_manually_created_transcript(languages)
    except Exception:
        try:
            transcript = transcripts.find_generated_transcript(languages)
        except Exception:
            # Субтитров на нужных языках нет — берём любые: переводит потом языковая модель.
            transcript = next(iter(transcripts), None)
    if transcript is None:
        return ""
    return captions_to_text([snippet.text for snippet in transcript.fetch()])


async def transcript(vid: str, language: str) -> str:
    code = (language or "ru").casefold().split("-", 1)[0]
    languages = list(dict.fromkeys([code, "en"]))
    return (await asyncio.to_thread(_transcript_sync, vid, languages))[: ingest.MAX_TEXT]
