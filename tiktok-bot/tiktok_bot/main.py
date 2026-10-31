"""Бот: пост канала, пересланный владельцем, → вертикальный ролик → «Входящие» TikTok и копия владельцу в личку.
Сам канал бот не читает: ролик делается только из того, что ему переслали.
Запуск: python -m tiktok_bot.main"""
import asyncio
import logging
import time
from pathlib import Path

import httpx

from . import config as config_mod
from . import stats as stats_mod
from .render import Media, render, render_shot
from .screenshot import capture, embed_url
from .store import Store
from .telegram import UPLOAD_LIMIT, AlbumBuffer, Post, Telegram, TelegramError, forwarded_post
from .text import build_script
from .tiktok import TikTok, TikTokError, extract_code

log = logging.getLogger("tiktok_bot")
MAX_IMAGES = 4
MAX_CLIP_BYTES = 400 * 1024 * 1024  # видео поста из CDN Telegram: больше — ролик со скриншотом без видео
SOUND_OWN = "Звук: у видео свой звук. Если добавите трек в TikTok, убавьте громкость оригинала."
SOUND_NONE = "Звук: ролик без звука — перед публикацией нажмите «Добавить звук» и выберите трек в TikTok."
STATS_HINT = ("Статистику роликов бот не видит: добавьте в приложение TikTok продукт Display API (скоуп video.list) "
              "и подключите TikTok заново командой /auth.")

HELP = """Я делаю из постов Telegram-каналов вертикальные ролики и отправляю их во «Входящие» TikTok.

Как это работает: перешлите мне пост канала → ролик со скриншотом поста (видео поста играет в нём целиком, со своим звуком) → черновик во «Входящих» TikTok (откройте уведомление в приложении, добавьте звук и нажмите «Опубликовать») и копия ролика сюда. После публикации я слежу за просмотрами и лайками ролика.

Команды:
/auth — подключить аккаунт TikTok
/status — что с подключением и последними постами
/export — таблица CSV: посты, ролики и их статистика

Посты, которые вы не пересылали, я не трогаю."""


class Bot:
    def __init__(self, cfg, tg: Telegram, tiktok: TikTok, store: Store, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.tg = tg
        self.tiktok = tiktok
        self.store = store
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True)
        self.forwards = AlbumBuffer()

    async def notify(self, text: str) -> None:
        try:
            await self.tg.send(self.cfg.owner_id, text)
        except TelegramError as e:
            log.warning("owner notification failed: %s", e)

    async def handle(self, update: dict) -> None:
        msg = update.get("message") or {}  # посты самого канала не обрабатываются: ролик только из пересланного владельцем
        if (msg.get("from") or {}).get("id") != self.cfg.owner_id:
            if msg.get("chat", {}).get("type") == "private":
                await self.tg.send(msg["chat"]["id"], f"Бот личный. Ваш id: {(msg.get('from') or {}).get('id')} — "
                                                      "его владелец указывает в TIKTOK_BOT_OWNER_ID.")
            return
        forwarded = forwarded_post(msg)
        if forwarded:
            post = self.forwards.add(forwarded)
            if post:
                await self.process(post)
            return
        if msg.get("forward_origin"):
            await self.notify("Это пересылка не из канала. Перешлите пост канала — ролик делается только из него.")
            return
        await self.command((msg.get("text") or "").strip())

    async def command(self, text: str) -> None:
        if text.startswith(("/start", "/help")):
            await self.notify(HELP)
        elif text.startswith("/auth"):
            if not self.tiktok.configured:
                await self.notify("Не заданы TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET и TIKTOK_REDIRECT_URI (см. README бота).")
                return
            await self.notify("1. Откройте ссылку и войдите в TikTok:\n" + self.tiktok.auth_url() +
                              "\n\n2. После входа браузер перейдёт на адрес переадресации (страница может не открыться — это нормально). "
                              "Скопируйте адрес из строки браузера целиком и пришлите мне.")
        elif "code=" in text or text.startswith("/code"):
            code, state = extract_code(text.removeprefix("/code"))
            if not code:
                await self.notify("Не нашёл код в сообщении: пришлите адрес из строки браузера целиком.")
                return
            try:
                await self.tiktok.exchange(code, state)
            except TikTokError as e:
                await self.notify(f"TikTok не подключился: {e}")
                return
            await self.notify("TikTok подключён. Пересылайте мне посты канала — ролики из них придут во «Входящие» TikTok."
                              + ("" if self.tiktok.can_read_stats else "\n\n" + STATS_HINT))
        elif text.startswith("/status"):
            await self.notify(self.status_text())
        elif text.startswith("/export"):
            try:
                await self.tg.send_document(self.cfg.owner_id, stats_mod.export_csv(self.store), "tiktok_stats.csv",
                                            caption="Посты, ролики и замеры на 1 ч, 6 ч, сутки, 3 дня, неделю и месяц.")
            except TelegramError as e:
                await self.notify(f"Не смог отправить таблицу: {e}")
        elif text:
            await self.notify("Не понял. /help — что я умею.")

    def status_text(self) -> str:
        lines = [f"TikTok: {'подключён' if self.tiktok.authorized else 'не подключён — /auth'}",
                 "Режим: ролики только из пересланных постов"]
        if self.tiktok.authorized and not self.tiktok.can_read_stats:
            lines.append(STATS_HINT)
        names = {"inbox": "во входящих TikTok", "uploaded": "загружен в TikTok", "rendered": "только ролик (TikTok не подключён)",
                 "failed": "ошибка", "skipped": "пропущен", "paused": "пропущен (пауза)"}
        for row in self.store.recent():
            state = "опубликован" if row["video_id"] else names.get(row["status"], row["status"])
            last = stats_mod.latest(self.store, row["video_id"])
            numbers = f" — 👁 {last['views'] or 0} ♥ {last['likes'] or 0} 💬 {last['comments'] or 0}" if last else ""
            lines.append(f"• #{row['message_id']} {state}{numbers}: {(row['title'] or '')[:60]}"
                         + (f" — {row['error'][:120]}" if row["error"] else ""))
        return "\n".join(lines)

    async def media(self, post: Post) -> Media:
        out = Media()
        for file_id in (post.photos or post.thumbs)[:MAX_IMAGES]:
            try:
                out.images.append(await self.tg.download(file_id))
            except Exception as e:
                log.info("image %s skipped: %s", file_id, e)
        clip = post.video or post.animation
        if clip:
            try:
                out.video = await self.tg.download(clip)
                out.animation = clip == post.animation
            except Exception as e:
                log.info("video skipped: %s", e)
        return out

    async def clip(self, url: str | None, post: Post) -> bytes | None:
        """Видео поста: из CDN Telegram по ссылке со страницы поста (там нет лимита Bot API в 20 МБ), иначе через Bot API."""
        if url:
            try:
                async with self.http.stream("GET", url) as r:
                    r.raise_for_status()
                    data = bytearray()
                    async for part in r.aiter_bytes():
                        data += part
                        if len(data) > MAX_CLIP_BYTES:
                            raise ValueError(f"видео больше {MAX_CLIP_BYTES // 1024 // 1024} МБ")
                    return bytes(data)
            except Exception as e:
                log.info("post video from CDN skipped: %s", e)
        file_id = post.video or post.animation
        if file_id:
            try:
                return await self.tg.download(file_id)
            except Exception as e:
                log.info("post video from Bot API skipped: %s", e)
        return None

    async def build(self, post: Post, script, handle: str) -> tuple[bytes, str, float | None, bool]:
        """Ролик, его вид (shot — скриншот поста, slides — слайды), длительность и есть ли в нём звук.
        Скриншот нужен публичный пост; если страница поста недоступна, ролик собирается из слайдов."""
        if self.cfg.screenshot and post.chat_username:
            try:
                shot = await capture(embed_url(post.chat_username, post.message_id), self.cfg.timezone)
                if shot:
                    clip = await self.clip(shot.video_url, post) if shot.video_box else None
                    shot.gif = shot.gif or (not shot.video_url and post.clip_kind == "gif")  # без <video> на странице
                    data, seconds, has_audio = await render_shot(shot, clip, post.text, self.cfg.max_video_seconds)
                    return data, "shot", seconds, has_audio
                log.info("post %s not found on t.me, using slides", post.message_id)
            except Exception as e:
                log.warning("screenshot video failed for %s, using slides: %s", post.message_id, e)
        data = await render(script, await self.media(post), handle, self.cfg.channel_title or post.chat_title, self.cfg.max_video_seconds)
        return data, "slides", None, False

    async def process(self, post: Post) -> None:
        """Пересланный пост — всегда ролик, даже повторно: переслать ещё раз — осознанная просьба владельца."""
        handle = f"@{post.chat_username}" if post.chat_username else self.cfg.channel
        script = build_script(post.text, handle, self.cfg.hashtags)
        if len(post.text.strip()) < self.cfg.min_text_chars or not script.title:
            self.store.mark(post.chat_id, post.message_id, "skipped", title=script.title, error="мало текста")
            await self.notify(f"В посте меньше {self.cfg.min_text_chars} символов текста — ролик не делаю "
                              "(порог — TIKTOK_BOT_MIN_TEXT_CHARS).")
            return
        try:
            video, fmt, seconds, has_audio = await self.build(post, script, handle)
        except Exception as e:
            log.exception("render failed for %s", post.message_id)
            self.store.mark(post.chat_id, post.message_id, "failed", title=script.title, error=f"сборка ролика: {e}")
            await self.notify(f"Не смог собрать ролик для поста {post.link or post.message_id}: {e}")
            return
        self.store.mark(post.chat_id, post.message_id, "rendered", title=script.title, channel=post.chat_username or None,
                        kind=kind_of(post), format=fmt, text_chars=len(post.text), seconds=seconds)
        note = "TikTok не подключён (/auth) — ролик можно загрузить вручную."
        if self.tiktok.authorized:
            try:
                publish_id = await self.tiktok.upload_to_inbox(video)
                self.store.mark(post.chat_id, post.message_id, "inbox", publish_id=publish_id, uploaded_at=time.time())
                note = "Ролик во «Входящих» TikTok: откройте уведомление в приложении, вставьте подпись и опубликуйте."
            except Exception as e:
                log.warning("tiktok upload failed for %s: %s", post.message_id, e)
                self.store.mark(post.chat_id, post.message_id, "failed", error=f"TikTok: {e}")
                note = f"TikTok не принял ролик: {e}. Его можно загрузить вручную."
        note = f"{note}\n{SOUND_OWN if has_audio else SOUND_NONE}"
        if len(video) > UPLOAD_LIMIT:
            await self.notify(f"{note}\n(Ролик {len(video) // 1024 // 1024} МБ — больше лимита Telegram для ботов, копию сюда не прислать.)"
                              f"\n\nПодпись:\n{script.caption}")
            return
        try:
            await self.tg.send_video(self.cfg.owner_id, video, caption=f"{note}\n\nПодпись:\n{script.caption}")
        except TelegramError as e:
            await self.notify(f"{note}\n(Копию ролика отправить не удалось: {e})\n\nПодпись:\n{script.caption}")

    async def flush_albums(self) -> None:
        for post in self.forwards.ready():
            await self.process(post)

    async def stats_loop(self, stop: asyncio.Event) -> None:
        """Раз в POLL_MINUTES: какие ролики владелец опубликовал и сколько они набрали."""
        while not stop.is_set():
            if self.tiktok.authorized:
                try:
                    taken = await stats_mod.sync(self.store, self.tiktok)
                    if taken:
                        log.info("tiktok stats: %s snapshots", taken)
                except Exception as e:
                    log.warning("tiktok stats failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=stats_mod.POLL_MINUTES * 60)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        stats_task = asyncio.create_task(self.stats_loop(stop))
        try:
            await self.poll(stop)
        finally:
            stats_task.cancel()

    async def poll(self, stop: asyncio.Event) -> None:
        offset = self.store.get("offset")
        while not stop.is_set():
            try:
                updates = await self.tg.updates(offset)
            except Exception as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.store.set("offset", offset)
                try:
                    await self.handle(update)
                except Exception:
                    log.exception("update %s failed", update.get("update_id"))
            await self.flush_albums()


def kind_of(post: Post) -> str:
    return post.clip_kind or ("photo" if post.photos else "text")


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load()
    store = Store(Path(cfg.data_dir) / "bot.sqlite3")
    tg = Telegram(cfg.telegram_token, cfg.telegram_api)
    bot = Bot(cfg, tg, TikTok(cfg, store), store)
    me = await tg.call("getMe")
    log.info("bot @%s started, owner %s: videos from forwarded posts only", me.get("username"), cfg.owner_id)
    await bot.run(asyncio.Event())


if __name__ == "__main__":
    asyncio.run(amain())
