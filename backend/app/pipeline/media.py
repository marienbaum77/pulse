"""Иллюстрации, видео и GIF для поста. Пересказ (InfoQ, Хабр, пост Reddit) часто несёт заглушку сайта,
а настоящие картинки — в первоисточнике, на который он ссылается, и в README репозитория из строки «👉».
Ссылку на первоисточник и медиа статьи находим при дозагрузке её текста, а страницы первоисточника и репозитория
скачиваем один раз при подготовке поста. Языковая модель здесь не участвует: только загрузка страниц и разбор HTML."""
import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from psycopg.types.json import Jsonb

from .. import db
from ..images import normalize_image_url
from ..netguard import guarded_client
from .primary_links import _NOT_OFFICIAL, content_root

log = logging.getLogger("pulse.media")
MAX_IMAGES = 6
MAX_VIDEOS = 3
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # предел Telegram для файла, который бот загружает сам; длительность не ограничена
VIDEO_TYPES = {"video/mp4", "video/quicktime"}  # sendVideo в Telegram — MPEG-4
ANIMATION_TYPES = {"image/gif"}  # GIF уходит через sendAnimation и проигрывается в ленте, как в оригинале
_VIDEO_EXT = (".mp4", ".m4v", ".mov")
MIN_IMAGE_SIDE = 300  # как у обложки: мельче — иконка, аватар или кнопка
# Аватар автора, логотип, значок — не иллюстрация статьи, даже если стоят в её тексте.
_NOT_ILLUSTRATION = re.compile(r"avatar|author|profile|headshot|userpic|logo|icon|badge|emoji|sprite|gravatar", re.I)
# Бейджи README (сборка, версия, звёзды) — картинки-строчки, а не иллюстрации; на GitHub они идут через camo-прокси.
_BADGE = re.compile(r"shields\.io|badgen\.net|badge\.fury\.io|codecov\.io|circleci\.com|travis-ci|/badge|/workflows/|star-history\.com|deepwiki\.com", re.I)
# Первоисточник без совпадения имени сайта с заголовком узнаём по подписи ссылки: «Источник: …», «via …».
_ORIGIN_WORDS = re.compile(r"(?<![\w-])(?:оригинал\w*|первоисточник\w*|источник\w*|original|source|via)(?![\w-])", re.I)
# Код, веса и статьи — это «👉»-ссылка поста, а не первоисточник с иллюстрациями.
_NOT_ORIGIN = _NOT_OFFICIAL | {"github.com", "gitlab.com", "huggingface.co", "arxiv.org", "web.archive.org", "archive.org", "medium.com"}
_PREFIXES = ("www.", "blog.", "news.", "docs.", "m.", "en.")
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}
_MEDIA_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".mp3")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _site(host: str) -> str:
    """Регистрируемый домен: blog.modal.com → modal.com, bbc.co.uk → bbc.co.uk."""
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def brand(host: str) -> str:
    """Имя сайта, которое пишут в заголовках: modal.com → modal, deepmind.google → deepmind."""
    return _site(host).split(".")[0]


_WORD = re.compile(r"[^\W\d_]{4,}", re.U)
_STOP = {"with", "from", "that", "this", "your", "have", "what", "about", "into", "more", "than", "будет", "этот", "como", "para"}
_BLOG_PATH = re.compile(r"/(?:blog|news|post|posts|article|articles|announc\w*|press|research|stories|updates?|engineering)(?:/|$)", re.I)
ORIGIN_MIN_SCORE = 3


def _stems(text: str) -> set[str]:
    """Основы слов для сравнения адреса и заголовка: первые 4 буквы (scale/scaling, sandbox/sandboxes)."""
    return {w[:4].lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}


def find_origin(root, page_url: str, title: str) -> str | None:
    """Первоисточник пересказа среди внешних ссылок текста. Баллы: слова адреса страницы совпадают со словами
    заголовка (…/scaling-to-1-million-concurrent-sandboxes ↔ «scale 1 million sandboxes») — по баллу за слово;
    имя сайта есть в заголовке («…at Modal» → modal.com) — 2; путь блога или новости — 1; подпись «Источник:»,
    «original», «via» в тексте ссылки или прямо перед ней — 3, этого хватает и без других признаков.
    Ссылка на документацию упомянутой технологии набирает мало: её адрес не про эту новость."""
    own = _site(_host(page_url))
    title_stems = _stems(title)
    best, best_score = None, 0
    for a in root.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        host = _host(url)
        if not host or not url.startswith(("http://", "https://")) or _site(host) == own:
            continue
        path = urlsplit(url).path
        if any(host == d or host.endswith("." + d) for d in _NOT_ORIGIN) or path.lower().endswith(_MEDIA_EXT):
            continue
        score = len(_stems(path.replace("-", " ").replace("_", " ")) & title_stems)
        name = brand(host)
        if len(name) >= 3 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", title, re.I):
            score += 2
        if _BLOG_PATH.search(path):
            score += 1
        before = a.previous_sibling if isinstance(a.previous_sibling, str) else ""
        if _ORIGIN_WORDS.search(f"{before[-40:]} {a.get_text(' ', strip=True)}"):
            score += 3
        if score > best_score:
            best, best_score = url, score
    return best if best_score >= ORIGIN_MIN_SCORE else None


def _too_small(tag) -> bool:
    for attr in ("width", "height"):
        try:
            if int(str(tag.get(attr, "")).rstrip("px") or MIN_IMAGE_SIDE) < MIN_IMAGE_SIDE:
                return True
        except ValueError:
            pass
    return False


def _img_src(tag) -> str | None:
    src = tag.get("data-src") or tag.get("src") or ""
    if (not src or src.startswith("data:")) and tag.get("srcset"):
        src = tag["srcset"].split(",")[0].strip().split(" ")[0]
    return None if src.startswith("data:") else src


def _video_src(tag, base: str) -> str | None:
    """Адрес ролика из <video>. Расширение не обязательно: у вложений GitHub (/user-attachments/assets/…) его нет,
    тип файла проверяется при загрузке."""
    candidates = [tag.get("src")] + [s.get("src") for s in tag.find_all("source") if (s.get("type") or "video/mp4") in VIDEO_TYPES]
    for src in candidates:
        if not src or src.startswith(("data:", "blob:")):
            continue
        url = urljoin(base, src.strip())
        path = urlsplit(url).path.lower()
        if url.startswith(("http://", "https://")) and (path.endswith(_VIDEO_EXT) or "." not in path.rsplit("/", 1)[-1]):
            return url
    return None


def _links_away(tag, base: str) -> bool:
    """Картинка-ссылка на другую страницу того же сайта — карточка «читайте также» (у thecode.media такой слайдер
    лежит внутри самой <article>), а ссылка с utm-метками — реклама. Ни то, ни другое не иллюстрирует статью.
    Ссылка на полноразмерную версию картинки или на внешний сайт без меток — обычная иллюстрация."""
    a = tag.find_parent("a", href=True)
    if a is None:
        return False
    href = urljoin(base, a["href"].strip())
    parts = urlsplit(href)
    if "utm_" in parts.query:
        return True
    if _host(href) != _host(base) or parts.path.lower().endswith(_MEDIA_EXT):
        return False
    return parts.path.rstrip("/") != urlsplit(base).path.rstrip("/")


def og_image(head, base: str) -> str | None:
    """Карточка страницы для соцсетей: годится на обложку, но не в альбом — это не иллюстрация из текста."""
    if head is None:
        return None
    meta = head.find("meta", attrs={"property": "og:image"}) or head.find("meta", attrs={"name": "twitter:image"})
    return normalize_image_url(meta.get("content") if meta else None, base)


def page_media(root, base: str, head=None) -> tuple[list[str], list[dict]]:
    """Иллюстрации и ролики из текста страницы в порядке появления; в конце — og:image страницы.
    Ролик — {url, poster, kind}: kind «video» для <video>, «animation» для GIF. Кадр-обложка видео (poster) —
    тоже иллюстрация: если ролик не подойдёт, пост выйдет с ним."""
    images: list[str] = []
    videos: list[dict] = []
    for tag in root.find_all(True):
        if tag.name not in ("img", "video"):
            # Ролик в атрибуте своего компонента: у блога Google <uni-inline-image or-mp4-video-url="….mp4" autoplay>,
            # у других сайтов data-video-src, data-mp4 и т. п. Браузер проигрывает такие ролики как GIF.
            for value in tag.attrs.values():
                if isinstance(value, str) and urlsplit(value.strip()).path.lower().endswith((*_VIDEO_EXT, ".webm")):
                    url = urljoin(base, value.strip())
                    if url.startswith(("http://", "https://")) and url not in {v["url"] for v in videos}:
                        videos.append({"url": url, "poster": None, "kind": "video"})
            continue
        if tag.name == "video":
            poster = normalize_image_url(tag.get("poster"), base)
            url = _video_src(tag, base)
            if url and url not in {v["url"] for v in videos}:
                videos.append({"url": url, "poster": poster, "kind": "video"})
            if poster and poster not in images:
                images.append(poster)
            continue
        # GitHub отдаёт картинки README через camo-прокси, а исходный адрес кладёт в data-canonical-src.
        canonical = str(tag.get("data-canonical-src") or _img_src(tag) or "")
        hint = " ".join([str(tag.get("alt", "")), " ".join(tag.get("class") or []), str(tag.get("src", "")), canonical])
        if _too_small(tag) or _NOT_ILLUSTRATION.search(hint) or _BADGE.search(hint) or _links_away(tag, base):
            continue
        url = normalize_image_url(_img_src(tag), base)
        if not url:
            continue
        if urlsplit(canonical).path.lower().endswith(".gif"):
            if url not in {v["url"] for v in videos}:
                videos.append({"url": url, "poster": None, "kind": "animation"})
        elif url not in images:
            images.append(url)
    if head is not None:
        og_video = head.find("meta", attrs={"property": ["og:video", "og:video:url", "og:video:secure_url"]})
        video_url = urljoin(base, str(og_video.get("content") or "").strip()) if og_video else ""
        if urlsplit(video_url).path.lower().endswith(_VIDEO_EXT) and video_url not in {v["url"] for v in videos}:
            videos.append({"url": video_url, "poster": None, "kind": "video"})  # файл, а не плеер YouTube
        og = og_image(head, base)
        if og and og not in images:
            images.append(og)
    return images[:MAX_IMAGES + 1], videos[:MAX_VIDEOS]


def analyze(page: str, page_url: str, title: str = "") -> dict:
    """{origin, images, videos, og_image} страницы; origin — адрес первоисточника или None."""
    if not page:
        return {"origin": None, "images": [], "videos": [], "og_image": None}
    soup = BeautifulSoup(page, "html.parser")
    head = soup.head
    heading = " ".join(x for x in (title, soup.title.get_text(" ", strip=True) if soup.title else "") if x)
    root = content_root(soup)
    h1 = root.find("h1")
    images, videos = page_media(root, page_url, head)
    return {"origin": find_origin(root, page_url, f"{heading} {h1.get_text(' ', strip=True) if h1 else ''}"), "images": images,
            "videos": videos, "og_image": og_image(head, page_url)}


def _accepted_type(ctype: str, url: str, kind: str) -> str | None:
    """MIME для отправки или None. Сервер без Content-Type (application/octet-stream) — по расширению адреса."""
    allowed = ANIMATION_TYPES if kind == "animation" else VIDEO_TYPES
    if ctype in allowed:
        return ctype
    path = urlsplit(url).path.lower()
    if ctype in ("", "application/octet-stream", "binary/octet-stream"):
        if kind == "animation" and path.endswith(".gif"):
            return "image/gif"
        if kind == "video" and path.endswith(_VIDEO_EXT):
            return "video/mp4"
    return None


async def download_video(url: str, kind: str = "video", *, body: bool = True) -> tuple[bytes, str]:
    """Скачивает ролик (MPEG-4 или GIF) с проверкой SSRF на каждом редиректе и пределом Telegram в 50 МБ.
    body=False — только проверка: тип и размер из заголовков, тело читается, лишь если размер не указан."""
    accept = "image/gif" if kind == "animation" else "video/mp4,video/*;q=0.8"
    headers = {"User-Agent": "PulseBot/1.0 (+self-hosted)", "Accept": accept}
    current = url
    async with guarded_client(timeout=httpx.Timeout(60.0, connect=5.0), follow_redirects=False, headers=headers) as client:
        for _ in range(5):
            async with client.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = str(httpx.URL(current).join(r.headers["location"]))
                    continue
                r.raise_for_status()
                ctype = _accepted_type(r.headers.get("content-type", "").split(";")[0].strip().lower(), current, kind)
                if ctype is None:
                    raise ValueError("Файл не является видео MPEG-4" if kind == "video" else "Файл не является GIF")
                length = int(r.headers.get("content-length") or 0)
                if length > MAX_VIDEO_BYTES:
                    raise ValueError("Файл больше 50 МБ — Telegram не примет его от бота")
                if not body and length:
                    return b"", ctype
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_VIDEO_BYTES:
                        raise ValueError("Файл больше 50 МБ — Telegram не примет его от бота")
                return bytes(data), ctype
    raise ValueError("Слишком много редиректов")


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


async def gif_to_mp4(data: bytes) -> bytes | None:
    """GIF → MP4 (H.264, CRF 18) для sendAnimation. Присланный GIF Telegram перекодирует сам, с низким битрейтом,
    и мелкий текст и схемы расплываются; готовый MP4 без звука он показывает анимацией почти без пережатия.
    None — ffmpeg недоступен или файл не перекодировался: тогда уходит исходный GIF."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.gif", Path(tmp) / "out.mp4"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            # чётные размеры — требование yuv420p; yuv420p и faststart — чтобы ролик играл во всех клиентах сразу
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-movflags", "+faststart", "-an", str(dst),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            log.info("gif conversion timed out")
            return None
        if proc.returncode != 0 or not dst.exists():
            log.info("gif conversion failed: %s", (err or b"").decode(errors="replace")[:300])
            return None
        out = dst.read_bytes()
        return out if 0 < len(out) <= MAX_VIDEO_BYTES else None


async def pick_video(videos: list[dict]) -> dict | None:
    """Первый ролик, который отвечает как MPEG-4 или GIF и укладывается в 50 МБ: {url, kind}.
    Длительность не ограничена; файл при проверке не скачивается целиком, если сервер сообщает его размер."""
    for video in videos[:MAX_VIDEOS]:
        kind = video.get("kind") or "video"
        try:
            await download_video(video["url"], kind, body=False)
        except Exception as e:
            log.info("%s %s skipped: %s", kind, video["url"], e)
            continue
        return {"url": video["url"], "kind": kind}
    return None


# Пост Telegram-канала, видео YouTube и обсуждение Reddit — не статьи: своих иллюстраций в тексте у них нет.
_NOT_ARTICLE = ("t.me", "telegram.me", "youtube.com", "youtu.be", "reddit.com")


async def _analyzed(item_id: int, url: str, title: str, media: dict | None) -> dict:
    """Медиа материала из meta; если статью не дозагружали, страница скачивается один раз и результат сохраняется."""
    if media is not None:
        return media
    from .ingest import fetch_html_or_archive as fetch_html  # ingest сам использует analyze при дозагрузке

    try:
        page = await fetch_html(url)
        media = analyze(page[0], page[1], title) if page else {}
    except Exception as e:
        log.info("article page for media of item %s not loaded: %s", item_id, e)
        media = {}
    await db.execute("UPDATE items SET meta = meta || jsonb_build_object('media', %s::jsonb) WHERE id = %s", (Jsonb(media), item_id))
    return media


async def _page_media(url: str) -> dict:
    """{images, videos} страницы первоисточника или репозитория; {} — страницу загрузить не удалось."""
    from .ingest import fetch_html_or_archive as fetch_html  # ingest сам использует analyze при дозагрузке

    try:
        page = await fetch_html(url)
        found = analyze(page[0], page[1]) if page else {}
        return {"images": found.get("images", []), "videos": found.get("videos", []), "og_image": found.get("og_image")}
    except Exception as e:
        log.info("media page %s not loaded: %s", url, e)
        return {}


async def post_media(citations: list[dict], primary: dict | None = None) -> dict:
    """Картинки и ролики для поста по главному материалу: {origin_images, images, videos, covers}.
    origin_images — иллюстрации первоисточника и README репозитория из строки «👉» (primary), images — из текста
    самой статьи. Порядок для обложки: первоисточник и репозиторий, затем картинка материала, затем текст пересказа.
    covers — карточки этих страниц для соцсетей (og:image): на обложку годятся, в альбом — нет.
    Каждая страница скачивается один раз: результат сохраняется в meta главного материала."""
    empty = {"origin_images": [], "images": [], "videos": [], "covers": []}
    main = next(
        (c for c in citations if c.get("kind", "article") == "article" and c.get("url", "").startswith(("http://", "https://"))
         and not any(_host(c["url"]) == d or _host(c["url"]).endswith("." + d) for d in _NOT_ARTICLE)),
        None,
    )
    if main is None:
        return empty  # статьи среди источников нет: обложка выбирается как раньше
    media = await _analyzed(main["item_id"], main["url"], main.get("title", ""), main.get("media"))
    changed = False
    origin = media.get("origin")
    if origin and "origin_media" not in media:
        media["origin_media"] = await _page_media(origin)
        changed = True
    # README репозитория — демо-GIF, видео и схемы проекта. Если сам материал и есть репозиторий (GitHub Trending),
    # его страница уже разобрана выше.
    repo = primary.get("url") if primary and primary.get("kind") == "repo" else None
    if repo and repo != main["url"] and (media.get("repo_media") or {}).get("url") != repo:
        media["repo_media"] = {"url": repo, **await _page_media(repo)}
        changed = True
    if changed:
        await db.execute("UPDATE items SET meta = meta || jsonb_build_object('media', %s::jsonb) WHERE id = %s", (Jsonb(media), main["item_id"]))
    from_origin = media.get("origin_media") or {}
    from_repo = media.get("repo_media") or {} if repo else {}
    return {
        "origin_images": list(from_origin.get("images") or []) + list(from_repo.get("images") or []),
        "images": list(media.get("images") or []),
        "videos": list(from_origin.get("videos") or []) + list(from_repo.get("videos") or []) + list(media.get("videos") or []),
        "covers": [x for x in (media.get("og_image"), from_origin.get("og_image"), from_repo.get("og_image")) if x],
    }
