"""Telegram Bot API: посты каналов, пересланные владельцем, и его команды в личке, скачивание медиа поста. Long polling — без вебхука и публичного адреса."""
import time
from dataclasses import dataclass, field

import httpx

DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API отдаёт боту файлы до 20 МБ; больше — только превью
UPLOAD_LIMIT = 50 * 1024 * 1024  # и принимает от бота файлы до 50 МБ
LONG_POLL_SECONDS = 25  # сколько Telegram держит getUpdates, если новых сообщений нет
ALBUM_QUIET_SECONDS = 3.0  # элементы альбома приходят отдельными обновлениями: ждём, пока поток затихнет


@dataclass
class Post:
    chat_id: int
    message_id: int
    chat_username: str
    chat_title: str
    text: str
    photos: list[str] = field(default_factory=list)  # file_id самого крупного размера каждой фотографии
    video: str | None = None
    animation: str | None = None
    thumbs: list[str] = field(default_factory=list)  # превью видео и GIF — фон, если фото нет
    media_group_id: str | None = None
    clip_kind: str | None = None  # video | gif — что было в посте, даже если файл больше лимита Bot API

    @property
    def link(self) -> str:
        return f"https://t.me/{self.chat_username}/{self.message_id}" if self.chat_username else ""


def parse_post(msg: dict) -> Post:
    chat = msg.get("chat") or {}
    post = Post(
        chat_id=chat.get("id", 0), message_id=msg.get("message_id", 0), chat_username=chat.get("username") or "",
        chat_title=chat.get("title") or "", text=msg.get("text") or msg.get("caption") or "", media_group_id=msg.get("media_group_id"),
    )
    if msg.get("photo"):
        post.photos.append(max(msg["photo"], key=lambda p: p.get("width", 0) * p.get("height", 0))["file_id"])
    for kind in ("video", "animation"):
        media = msg.get(kind)
        if media:
            post.clip_kind = post.clip_kind or ("gif" if kind == "animation" else "video")
            setattr(post, kind, media["file_id"] if media.get("file_size", 0) <= DOWNLOAD_LIMIT else None)
            thumb = media.get("thumbnail") or media.get("thumb")
            if thumb:
                post.thumbs.append(thumb["file_id"])
    return post


def merge_album(parts: list[Post]) -> Post:
    """Альбом — несколько сообщений с одним media_group_id; текст обычно у первого. Номер поста — первого сообщения."""
    parts = sorted(parts, key=lambda p: p.message_id)
    first = parts[0]
    merged = Post(first.chat_id, first.message_id, first.chat_username, first.chat_title,
                  next((p.text for p in parts if p.text), ""), media_group_id=first.media_group_id)
    for p in parts:
        merged.photos += p.photos
        merged.thumbs += p.thumbs
        merged.video = merged.video or p.video
        merged.animation = merged.animation or p.animation
        merged.clip_kind = merged.clip_kind or p.clip_kind
    return merged


class AlbumBuffer:
    """Копит элементы альбомов и отдаёт пост целиком, когда новые элементы перестали приходить."""

    def __init__(self, quiet: float = ALBUM_QUIET_SECONDS):
        self.quiet = quiet
        self.groups: dict[str, tuple[float, list[Post]]] = {}

    def add(self, post: Post) -> Post | None:
        if not post.media_group_id:
            return post
        _, parts = self.groups.get(post.media_group_id, (0.0, []))
        self.groups[post.media_group_id] = (time.monotonic(), [*parts, post])
        return None

    def ready(self, now: float | None = None) -> list[Post]:
        now = time.monotonic() if now is None else now
        done = [gid for gid, (at, _) in self.groups.items() if now - at >= self.quiet]
        return [merge_album(self.groups.pop(gid)[1]) for gid in done]


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, api: str = "https://api.telegram.org", client: httpx.AsyncClient | None = None):
        self.base = f"{api}/bot{token}"
        self.files = f"{api}/file/bot{token}"
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=10.0))

    async def call(self, method: str, *, request_timeout: float | None = None, **params) -> dict | list:
        """Метод Bot API. request_timeout — сколько ждать ответа; не путать с параметром timeout самого getUpdates."""
        timeout = httpx.USE_CLIENT_DEFAULT if request_timeout is None else request_timeout
        r = await self.client.post(f"{self.base}/{method}", json=params, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
        return data["result"]

    async def updates(self, offset: int | None) -> list[dict]:
        # 25 с, а не 50: сети и прокси рвут соединение, молчащее около 50 секунд («Server disconnected»)
        params = {"timeout": LONG_POLL_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", request_timeout=LONG_POLL_SECONDS + 20.0, **params)

    async def send(self, chat_id: int, text: str, **extra) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True, **extra)

    async def send_video(self, chat_id: int, data: bytes, caption: str = "") -> None:
        r = await self.client.post(
            f"{self.base}/sendVideo", data={"chat_id": str(chat_id), "caption": caption[:1024], "supports_streaming": "true"},
            files={"video": ("tiktok.mp4", data, "video/mp4")}, timeout=httpx.Timeout(180.0, connect=10.0),
        )
        if not r.json().get("ok"):
            raise TelegramError(f"sendVideo: {r.json().get('description')}")

    async def send_document(self, chat_id: int, data: bytes, filename: str, caption: str = "") -> None:
        r = await self.client.post(
            f"{self.base}/sendDocument", data={"chat_id": str(chat_id), "caption": caption[:1024]},
            files={"document": (filename, data)}, timeout=httpx.Timeout(120.0, connect=10.0),
        )
        if not r.json().get("ok"):
            raise TelegramError(f"sendDocument: {r.json().get('description')}")

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        if (info.get("file_size") or 0) > DOWNLOAD_LIMIT or not info.get("file_path"):
            raise TelegramError("Файл больше 20 МБ — Bot API его не отдаёт")
        r = await self.client.get(f"{self.files}/{info['file_path']}", timeout=httpx.Timeout(120.0, connect=10.0))
        r.raise_for_status()
        return r.content


def forwarded_post(msg: dict) -> Post | None:
    """Пост канала, пересланный владельцем боту, — единственный источник роликов. Канал и номер — из forward_origin."""
    origin = msg.get("forward_origin") or {}
    if origin.get("type") != "channel":
        return None
    chat = origin.get("chat") or {}
    post = parse_post({**msg, "chat": chat, "message_id": origin.get("message_id", msg.get("message_id", 0))})
    return post
