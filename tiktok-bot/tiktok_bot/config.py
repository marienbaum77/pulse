"""Настройки бота из окружения (.env рядом с docker-compose или переменные контейнера)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    telegram_token: str
    owner_id: int  # единственный пользователь, которому бот подчиняется и шлёт ролики
    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_redirect_uri: str
    data_dir: str
    channel: str = ""  # @канал для подписи, если пост переслан из приватного канала без @имени
    telegram_api: str = "https://api.telegram.org"
    tiktok_api: str = "https://open.tiktokapis.com"
    tiktok_auth_url: str = "https://www.tiktok.com/v2/auth/authorize/"
    channel_title: str = ""  # подпись на последнем слайде; пусто — имя канала
    min_text_chars: int = 40  # короче — служебный пост («Опрос», «Анонс»), ролик не делается
    max_video_seconds: int = 600  # видео поста идёт целиком, но не дольше: через API TikTok принимает до 10 минут
    hashtags: str = ""  # в конец подписи для TikTok, например «#ии #нейросети #технологии»
    screenshot: bool = True  # ролик — скриншот поста из Telegram; False или приватный канал — слайды
    timezone: str = "Europe/Moscow"  # время поста на скриншоте


def load() -> Config:
    env = os.environ.get
    missing = [k for k in ("TIKTOK_BOT_TELEGRAM_TOKEN", "TIKTOK_BOT_OWNER_ID") if not env(k)]
    if missing:
        raise SystemExit(f"Не заданы переменные окружения: {', '.join(missing)} (см. tiktok-bot/README.md)")
    return Config(
        telegram_token=env("TIKTOK_BOT_TELEGRAM_TOKEN", ""),
        owner_id=int(env("TIKTOK_BOT_OWNER_ID", "0")),
        channel=env("TIKTOK_BOT_CHANNEL", "").strip(),
        tiktok_client_key=env("TIKTOK_CLIENT_KEY", ""),
        tiktok_client_secret=env("TIKTOK_CLIENT_SECRET", ""),
        tiktok_redirect_uri=env("TIKTOK_REDIRECT_URI", ""),
        data_dir=env("TIKTOK_BOT_DATA_DIR", "/data"),
        telegram_api=env("TIKTOK_BOT_TELEGRAM_API", "https://api.telegram.org").rstrip("/"),
        tiktok_api=env("TIKTOK_API", "https://open.tiktokapis.com").rstrip("/"),
        channel_title=env("TIKTOK_BOT_CHANNEL_TITLE", ""),
        min_text_chars=int(env("TIKTOK_BOT_MIN_TEXT_CHARS", "40")),
        max_video_seconds=int(env("TIKTOK_BOT_MAX_VIDEO_SECONDS", "600")),
        hashtags=env("TIKTOK_BOT_HASHTAGS", ""),
        screenshot=env("TIKTOK_BOT_SCREENSHOT", "1").strip().lower() not in ("0", "false", "no", "off"),
        timezone=env("TIKTOK_BOT_TIMEZONE", "Europe/Moscow"),
    )
