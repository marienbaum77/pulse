"""Бот TikTok: разбор постов и альбомов, сценарий ролика, скриншот поста и сборка MP4, OAuth и загрузка во «Входящие»,
обработка поста, статистика роликов и выгрузка."""
import asyncio
import csv
import io
import json
import re
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest

from PIL import Image, ImageDraw

from tiktok_bot import main as bot_main
from tiktok_bot import render as render_mod
from tiktok_bot import stats as stats_mod
from tiktok_bot import tiktok as tiktok_mod
from tiktok_bot.render import Media, reading_seconds, render, render_shot
from tiktok_bot.screenshot import Shot, capture
from tiktok_bot.store import Store
from tiktok_bot.telegram import AlbumBuffer, Telegram, forwarded_post, merge_album, parse_post
from tiktok_bot.text import build_script
from tiktok_bot.tiktok import TikTok, extract_code

PULSE_POST = """Modal запускает миллион песочниц за секунды

Компания перестроила планировщик: **1 млн** песочниц стартуют за секунды [1].

Что умеет:
• Масштабирование без Kubernetes [1]
• Холодный старт за 2 секунды [1]

Репозиторий 👉 sandbox

@pulse_itnews"""


def cfg(**over):
    base = dict(telegram_token="t", owner_id=7, channel="@pulse_itnews", tiktok_client_key="ck", tiktok_client_secret="cs",
                tiktok_redirect_uri="https://example.com/cb", data_dir="/tmp", telegram_api="https://tg.test",
                tiktok_api="https://tt.test", tiktok_auth_url="https://www.tiktok.com/v2/auth/authorize/", channel_title="",
                min_text_chars=40, max_video_seconds=10, hashtags="#ии", screenshot=False, timezone="UTC")
    return SimpleNamespace(**{**base, **over})


def test_script_splits_post_into_title_slides_and_caption():
    s = build_script(PULSE_POST, "@pulse_itnews", "#ии #технологии")
    assert s.title == "Modal запускает миллион песочниц за секунды"
    assert s.slides[0].startswith("Компания перестроила планировщик") and "[1]" not in s.slides[0]
    assert s.slides[1].startswith("Что умеет:\n• Масштабирование")  # список держится одним слайдом
    assert s.links == ["Репозиторий sandbox"] and not any("@pulse_itnews" in x for x in s.slides)
    assert s.caption.endswith("Подробности и ссылки — в Telegram @pulse_itnews\n\n#ии #технологии")


def test_long_paragraph_is_split_by_sentences():
    para = " ".join(f"Предложение номер {n} с подробностями о релизе модели." for n in range(12))
    s = build_script(f"Заголовок\n\n{para}", "@c")
    assert len(s.slides) >= 2 and all(len(x) <= 240 for x in s.slides)


def test_parse_post_album_and_forward():
    msg = {"message_id": 5, "chat": {"id": -100, "username": "pulse_itnews", "title": "Pulse | IT"}, "caption": "Текст", "media_group_id": "g",
           "photo": [{"file_id": "small", "width": 90, "height": 90}, {"file_id": "big", "width": 1280, "height": 720}]}
    second = {"message_id": 6, "chat": msg["chat"], "media_group_id": "g",
              "video": {"file_id": "v", "file_size": 1000, "thumbnail": {"file_id": "vt"}}}
    buf = AlbumBuffer(quiet=0)
    assert buf.add(parse_post(msg)) is None and buf.add(parse_post(second)) is None
    [post] = buf.ready()
    assert (post.message_id, post.text, post.photos, post.video, post.thumbs) == (5, "Текст", ["big"], "v", ["vt"])
    assert post.link == "https://t.me/pulse_itnews/5"

    big = parse_post({**second, "video": {"file_id": "huge", "file_size": 30 * 1024 * 1024}})
    assert big.video is None  # больше 20 МБ Bot API не отдаст

    fwd = forwarded_post({"message_id": 99, "chat": {"id": 7, "type": "private"}, "text": "Пост",
                          "forward_origin": {"type": "channel", "chat": msg["chat"], "message_id": 42}})
    assert (fwd.chat_username, fwd.message_id) == ("pulse_itnews", 42)
    assert merge_album([parse_post(second), parse_post(msg)]).message_id == 5


def _probe(path) -> str:
    proc = __import__("subprocess").run([render_mod.ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return proc.stderr


async def test_render_builds_vertical_mp4_with_post_video(tmp_path):
    ff = render_mod.ffmpeg_exe()
    img, clip = tmp_path / "img.png", tmp_path / "clip.mp4"
    for args, out in ((["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=1", "-frames:v", "1"], img),
                      (["-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "2", "-pix_fmt", "yuv420p"], clip)):
        proc = await asyncio.create_subprocess_exec(ff, "-hide_banner", "-loglevel", "error", "-y", *args, str(out))
        await proc.wait()
    script = build_script(PULSE_POST, "@pulse_itnews")
    data = await render(script, Media(images=[img.read_bytes()], video=clip.read_bytes()), "@pulse_itnews", "Pulse | IT", max_video_seconds=5)
    out = tmp_path / "out.mp4"
    out.write_bytes(data)
    info = _probe(out)
    assert "1080x1920" in info and "h264" in info and "aac" in info
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    seconds = int(duration[2]) * 60 + float(duration[3])
    # заголовок 3.5 + видео 2 + три текстовых слайда + финал 3
    assert 12 < seconds < 40


async def test_render_without_media_uses_gradient(tmp_path):
    data = await render(build_script("Заголовок без картинок\n\nКороткий текст поста.", "@c"), Media(), "@c")
    assert data[4:8] == b"ftyp"


def test_extract_code_from_redirect_address():
    assert extract_code("https://example.com/cb?code=abc123&scopes=video.upload&state=s1") == ("abc123", "s1")
    assert extract_code("просто текст") == (None, None)


async def test_oauth_and_inbox_upload(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("content-range")))
        if request.url.path == "/v2/oauth/token/":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            return httpx.Response(200, json={"access_token": f"at-{form['grant_type']}", "expires_in": 86400, "refresh_token": "rt",
                                             "refresh_expires_in": 31536000, "open_id": "u1", "scope": "user.info.basic,video.upload"})
        if request.url.path == "/v2/post/publish/inbox/video/init/":
            body = json.loads(request.content)
            assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 9, "chunk_size": 9, "total_chunk_count": 1}
            assert request.headers["authorization"] == "Bearer at-authorization_code"
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://upload.test/u"}, "error": {"code": "ok"}})
        if request.url.host == "upload.test":
            return httpx.Response(201)
        return httpx.Response(404)

    store = Store(tmp_path / "s.db")
    tt = TikTok(cfg(), store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    url = tt.auth_url()
    assert "scope=user.info.basic%2Cvideo.upload%2Cvideo.list" in url and "redirect_uri=https%3A%2F%2Fexample.com%2Fcb" in url
    state = store.get("tiktok_state")
    await tt.exchange("code1", state)
    assert tt.authorized
    assert await tt.upload_to_inbox(b"123456789") == "p1"
    assert calls[-1] == ("PUT", "https://upload.test/u", "bytes 0-8/9")

    store.set("tiktok_tokens", {**store.get("tiktok_tokens"), "expires_at": 0})  # сутки прошли — токен обновляется сам
    assert await tt.access_token() == "at-refresh_token"


class FakeTelegram:
    def __init__(self):
        self.sent, self.videos, self.documents = [], [], []

    async def send_document(self, chat_id, data, filename, caption=""):
        self.documents.append((chat_id, data, filename))

    async def send(self, chat_id, text, **extra):
        self.sent.append((chat_id, text))

    async def send_video(self, chat_id, data, caption=""):
        self.videos.append((chat_id, data, caption))

    async def download(self, file_id):
        return b"img"


class FakeTikTok:
    configured = True
    can_read_stats = False

    def __init__(self, authorized=True):
        self.authorized = authorized
        self.uploads = []

    async def upload_to_inbox(self, video):
        self.uploads.append(video)
        return "p1"


@pytest.fixture
def no_render(monkeypatch):
    async def fake_render(script, media, handle, channel_title="", max_video_seconds=60):
        return b"MP4"

    monkeypatch.setattr(bot_main, "render", fake_render)


def _forward(update_id: int, post: dict, sender: int = 7) -> dict:
    """Пост канала, пересланный боту в личку: у пересылки свой номер, номер в канале — в forward_origin."""
    msg = {k: v for k, v in post.items() if k not in ("chat", "message_id")}
    return {"update_id": update_id, "message": {**msg, "from": {"id": sender}, "chat": {"id": sender, "type": "private"},
                                                "message_id": 500 + update_id,
                                                "forward_origin": {"type": "channel", "chat": post["chat"], "message_id": post["message_id"]}}}


async def test_only_forwarded_posts_become_videos(tmp_path, no_render):
    tg, tt, store = FakeTelegram(), FakeTikTok(), Store(tmp_path / "s.db")
    bot = bot_main.Bot(cfg(), tg, tt, store)
    post = {"message_id": 10, "chat": {"id": -100, "username": "pulse_itnews", "title": "Pulse"}, "text": PULSE_POST}
    await bot.handle({"update_id": 1, "channel_post": post})
    await bot.flush_albums()
    assert tt.uploads == [] and tg.videos == [] and store.recent() == []  # пост канала сам по себе не трогается

    await bot.handle(_forward(2, post))
    assert tt.uploads == [b"MP4"]
    [(chat, data, caption)] = tg.videos
    assert chat == 7 and "во «Входящих» TikTok" in caption and "Подпись:\nModal запускает" in caption
    assert store.recent(1)[0]["message_id"] == 10  # номер поста в канале, а не пересылки

    await bot.handle(_forward(3, post))  # переслали ещё раз — ещё один ролик
    other = {**post, "message_id": 11, "chat": {"id": -200, "username": "someone_else"}}
    await bot.handle(_forward(4, other))  # пересылка из любого канала
    assert len(tt.uploads) == 3
    await bot.handle(_forward(5, post, sender=999))  # пересылка от чужого
    assert len(tt.uploads) == 3


async def test_owner_commands_and_strangers(tmp_path, no_render):
    tg, tt = FakeTelegram(), FakeTikTok(authorized=False)
    bot = bot_main.Bot(cfg(), tg, tt, Store(tmp_path / "s.db"))
    await bot.handle({"update_id": 1, "message": {"from": {"id": 999}, "chat": {"id": 999, "type": "private"}, "text": "/start"}})
    assert tg.sent[-1][0] == 999 and "999" in tg.sent[-1][1]  # чужому — только его id
    owner = {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}}
    await bot.handle({"update_id": 2, "message": {**owner, "text": "/start"}})
    assert "перешлите мне пост канала" in tg.sent[-1][1] and "/pause" not in tg.sent[-1][1]
    await bot.handle({"update_id": 3, "message": {**owner, "text": "Привет", "forward_origin": {"type": "user", "sender_user": {"id": 5}}}})
    assert "не из канала" in tg.sent[-1][1]
    short = {"message_id": 13, "chat": {"id": -100, "username": "pulse_itnews"}, "text": "Опрос"}
    await bot.handle(_forward(4, short))
    assert tg.videos == [] and "меньше 40 символов" in tg.sent[-1][1]  # пропуск короткого поста — с объяснением
    # TikTok не подключён — ролик только в личку
    await bot.handle(_forward(5, {"message_id": 12, "chat": {"id": -100, "username": "pulse_itnews"}, "text": PULSE_POST}))
    assert tg.videos and "TikTok не подключён" in tg.videos[-1][2]
    await bot.handle({"update_id": 5, "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/status"}})
    assert "TikTok: не подключён" in tg.sent[-1][1]


# ---------- скриншот поста и ролик из него ----------

def _card(width: int, height: int) -> bytes:
    """Карточка как со страницы t.me: тёмный прямоугольник со скруглёнными углами на прозрачном фоне."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, width - 1, height - 1], radius=40, fill=(33, 33, 33, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


async def _clip(tmp_path, seconds: float, audio: bool) -> bytes:
    path = tmp_path / f"clip{seconds}{audio}.mp4"
    args = ["-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    proc = await asyncio.create_subprocess_exec(render_mod.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args,
                                                "-pix_fmt", "yuv420p", str(path))
    await proc.wait()
    return path.read_bytes()


async def _info(tmp_path, data: bytes) -> tuple[str, float]:
    out = tmp_path / "out.mp4"
    out.write_bytes(data)
    seconds, _, _ = await render_mod.probe(out)
    return _probe(out), seconds


async def test_render_shot_plays_post_video_whole_with_its_sound(tmp_path):
    shot = Shot(_card(924, 1000), video_box=(30, 110, 863, 487), video_url="https://cdn.test/v.mp4")
    data, seconds, has_audio = await render_shot(shot, await _clip(tmp_path, 3, audio=True), "Короткий пост", max_video_seconds=600)
    info, real = await _info(tmp_path, data)
    assert "1080x1920" in info and "h264" in info and "aac" in info
    assert has_audio and seconds == pytest.approx(3, abs=0.1) and real == pytest.approx(3, abs=0.2)

    # обрезка по лимиту длительности
    data, seconds, _ = await render_shot(shot, await _clip(tmp_path, 3, audio=True), "Пост", max_video_seconds=2)
    assert seconds == 2 and (await _info(tmp_path, data))[1] == pytest.approx(2, abs=0.2)


async def test_render_shot_loops_gif_and_scrolls_tall_text(tmp_path):
    gif = Shot(_card(924, 1000), video_box=(30, 110, 863, 487), gif=True)
    data, seconds, has_audio = await render_shot(gif, await _clip(tmp_path, 1, audio=False), "Пост с GIF")
    # GIF без звука крутится, пока читается пост (не меньше 8 с), а не одну секунду
    assert not has_audio and seconds == reading_seconds("Пост с GIF") and (await _info(tmp_path, data))[1] == pytest.approx(8, abs=0.3)

    text = "Длинный текст поста. " * 60
    data, seconds, has_audio = await render_shot(Shot(_card(924, 3200)), None, text)
    info, real = await _info(tmp_path, data)
    assert "1080x1920" in info and "aac" in info and not has_audio  # тишина, звук владелец добавит в TikTok
    assert seconds > 8 and real == pytest.approx(seconds, abs=0.3)  # стоит, ровно едет до конца текста, стоит


async def test_capture_finds_card_player_and_hides_views(tmp_path):
    page = tmp_path / "post.html"
    page.write_text("""<html><body style="margin:0;background:#fff">
<div class="tgme_widget_message" style="margin:10px;width:400px">
  <div class="tgme_widget_message_bubble" style="background:#222;border-radius:12px;padding:30px 0 20px 20px">
    <a class="tgme_widget_message_video_player" style="display:block;width:300px;height:150px">
      <video class="tgme_widget_message_video" src="https://cdn.test/gif.mp4" loop muted></video></a>
    <div class="tgme_widget_message_text" style="color:#fff">Текст поста</div>
    <span class="tgme_widget_message_views">2</span> <time datetime="2026-10-25T08:01:09+00:00">Oct 25 at 08:01</time>
  </div></div></body></html>""", encoding="utf-8")
    shot = await capture(page.as_uri(), timezone="Europe/Moscow", attempts=1)
    img = Image.open(io.BytesIO(shot.png))
    assert img.mode == "RGBA" and img.getpixel((0, 0))[3] == 0  # фон страницы прозрачный — карточка ляжет на свой фон
    assert shot.gif and shot.video_url == "https://cdn.test/gif.mp4"
    x, y, w, h = shot.video_box
    assert (x, y, w, h) == pytest.approx((20 * 2.2, 30 * 2.2, 300 * 2.2, 150 * 2.2), abs=2)
    assert img.size[0] == pytest.approx(400 * 2.2, abs=2)

    missing = tmp_path / "missing.html"
    missing.write_text("<html><body>Post not found</body></html>", encoding="utf-8")
    assert await capture(missing.as_uri(), attempts=1) is None


async def test_post_becomes_screenshot_video_and_falls_back_to_slides(tmp_path, monkeypatch, no_render):
    calls = []

    async def fake_capture(url, timezone="Europe/Moscow", **kw):
        calls.append(url)
        return Shot(b"png", video_box=(1, 2, 3, 4), video_url="https://cdn.test/v.mp4") if "/10?" in url else None

    async def fake_render_shot(shot, clip, text, max_video_seconds=600):
        assert clip == b"clip"
        return b"SHOT", 12.0, True

    async def fake_clip(self, url, post):
        return b"clip"

    monkeypatch.setattr(bot_main, "capture", fake_capture)
    monkeypatch.setattr(bot_main, "render_shot", fake_render_shot)
    monkeypatch.setattr(bot_main.Bot, "clip", fake_clip)
    tg, tt, store = FakeTelegram(), FakeTikTok(), Store(tmp_path / "s.db")
    bot = bot_main.Bot(cfg(screenshot=True), tg, tt, store)
    post = {"message_id": 10, "chat": {"id": -100, "username": "pulse_itnews", "title": "Pulse"}, "caption": PULSE_POST,
            "video": {"file_id": "v", "file_size": 30 * 1024 * 1024}}
    await bot.handle(_forward(1, post))
    assert calls == ["https://t.me/pulse_itnews/10?embed=1&mode=tme&dark=1"] and tt.uploads == [b"SHOT"]
    row = store.recent(1)[0]
    assert (row["format"], row["kind"], row["seconds"], row["channel"]) == ("shot", "video", 12.0, "pulse_itnews")
    assert row["uploaded_at"] and bot_main.SOUND_OWN in tg.videos[-1][2]

    # поста нет на t.me (например, удалён) — ролик из слайдов, звук добавляется в TikTok
    await bot.handle(_forward(2, {**post, "message_id": 11, "video": None, "caption": None, "text": PULSE_POST}))
    row = store.recent(1)[0]
    assert tt.uploads[-1] == b"MP4" and (row["format"], row["kind"]) == ("slides", "text")
    assert bot_main.SOUND_NONE in tg.videos[-1][2]


# ---------- загрузка частями ----------

async def test_big_video_is_uploaded_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok_mod, "SINGLE_CHUNK_LIMIT", 10)
    monkeypatch.setattr(tiktok_mod, "CHUNK", 4)
    assert tiktok_mod.chunk_ranges(9) == [(0, 9)]
    assert tiktok_mod.chunk_ranges(11) == [(0, 4), (4, 11)]  # последний кусок забирает остаток
    puts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init/"):
            assert json.loads(request.content)["source_info"] == {"source": "FILE_UPLOAD", "video_size": 11, "chunk_size": 4, "total_chunk_count": 2}
            return httpx.Response(200, json={"data": {"publish_id": "p9", "upload_url": "https://upload.test/u"}, "error": {"code": "ok"}})
        puts.append((request.headers["content-range"], request.content))
        return httpx.Response(206 if len(puts) == 1 else 201)

    store = Store(tmp_path / "s.db")
    store.set("tiktok_tokens", {"access_token": "at", "expires_at": time.time() + 600, "refresh_token": "rt",
                                "refresh_expires_at": time.time() + 600, "scope": "video.upload"})
    tt = TikTok(cfg(), store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await tt.upload_to_inbox(b"0123456789A") == "p9"
    assert puts == [("bytes 0-3/11", b"0123"), ("bytes 4-10/11", b"456789A")]


# ---------- статистика роликов ----------

def test_due_takes_one_snapshot_per_mark():
    assert not stats_mod.due(0.5, [])
    assert stats_mod.due(1.2, []) and not stats_mod.due(1.4, [1.2])
    assert stats_mod.due(7, [1.2]) and not stats_mod.due(20, [1.2, 7])
    assert stats_mod.due(100, [1.2, 7])  # бот был выключен: отметки 24 и 72 часа — одним замером


async def test_stats_sync_finds_published_videos_and_exports_csv(tmp_path):
    now = 1_800_000_000.0
    store = Store(tmp_path / "s.db")
    store.set("tiktok_tokens", {"access_token": "at", "expires_at": now + 10**9, "refresh_token": "rt",
                                "refresh_expires_at": now + 10**9, "scope": "user.info.basic,video.upload,video.list"})
    store.mark(-100, 10, "inbox", title="Modal запускает миллион песочниц за секунды", publish_id="p1", uploaded_at=now - 3 * 3600,
               channel="pulse_itnews", kind="video", format="shot", text_chars=300, seconds=12.0)
    store.mark(-100, 11, "inbox", title="CrewAI: фреймворк для оркестрации агентов", publish_id="p2", uploaded_at=now - 3 * 3600,
               channel="pulse_itnews", kind="text", format="shot", text_chars=700, seconds=40.0)
    counts = {"111": 50, "222": 7}
    created = {"111": now - 1.2 * 3600, "222": now - 1.3 * 3600}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/status/fetch/"):
            ids = [111] if body["publish_id"] == "p1" else []  # второй ролик TikTok не вернул — ищем по подписи
            return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ids}, "error": {"code": "ok"}})
        if request.url.path == "/v2/video/list/":
            return httpx.Response(200, json={"data": {"videos": [
                {"id": "333", "create_time": now - 30 * 86400, "title": "CrewAI: фреймворк для оркестрации агентов"},  # старый, до загрузки
                {"id": "222", "create_time": created["222"], "title": "CrewAI: фреймворк для оркестрации агентов\n\nCrewAI — open-source"},
            ], "has_more": False}, "error": {"code": "ok"}})
        assert request.url.path == "/v2/video/query/" and "view_count" in request.url.params["fields"]
        return httpx.Response(200, json={"data": {"videos": [
            {"id": vid, "create_time": created[vid], "view_count": counts[vid], "like_count": counts[vid] // 10, "comment_count": 1,
             "share_count": 0, "share_url": f"https://www.tiktok.com/@me/video/{vid}"} for vid in body["filters"]["video_ids"]]}, "error": {"code": "ok"}})

    tt = TikTok(cfg(), store, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert tt.can_read_stats
    assert await stats_mod.sync(store, tt, now=now) == 2
    assert await stats_mod.sync(store, tt, now=now + 600) == 0  # следующая отметка — 6 часов
    counts["111"] = 400
    assert await stats_mod.sync(store, tt, now=now + 5 * 3600) == 2

    rows = {r["message_id"]: r for r in store.all_posts()}
    assert (rows[10]["video_id"], rows[11]["video_id"]) == ("111", "222")
    table = list(csv.DictReader(io.StringIO(stats_mod.export_csv(store).decode("utf-8-sig"))))
    first = next(r for r in table if r["message_id"] == "10")
    assert first["telegram_url"] == "https://t.me/pulse_itnews/10" and first["kind"] == "video" and first["format"] == "shot"
    assert (first["views_1h"], first["views_6h"], first["views_24h"], first["last_views"]) == ("50", "400", "", "400")
    assert first["publish_delay_hours"] == "1.8" and first["tiktok_url"].endswith("/111")


async def test_export_command_and_status_with_stats(tmp_path, no_render):
    tg, store = FakeTelegram(), Store(tmp_path / "s.db")
    store.mark(-100, 10, "inbox", title="Пост", publish_id="p1", uploaded_at=time.time(), channel="pulse_itnews")
    store.update(-100, 10, video_id="111")
    store.add_stats("111", 1.1, 50, 5, 1, 0)
    bot = bot_main.Bot(cfg(), tg, FakeTikTok(), store)
    owner = {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}}
    await bot.handle({"update_id": 1, "message": {**owner, "text": "/export"}})
    [(chat, data, name)] = tg.documents
    assert chat == 7 and name == "tiktok_stats.csv" and data.startswith("﻿".encode()) and b"views_1h" in data
    await bot.handle({"update_id": 2, "message": {**owner, "text": "/status"}})
    assert "#10 опубликован — 👁 50 ♥ 5 💬 1" in tg.sent[-1][1]
    assert "Display API" in tg.sent[-1][1]  # подключён без video.list — подсказка, как включить статистику


def test_store_upgrades_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE posts (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, status TEXT NOT NULL, publish_id TEXT, "
                     "error TEXT, title TEXT, updated_at REAL NOT NULL, PRIMARY KEY (chat_id, message_id));"
                     "INSERT INTO posts VALUES (-100, 5, 'inbox', 'p0', NULL, 'Старый пост', 1700000000);")
    db.commit()
    db.close()
    store = Store(path)
    [row] = store.awaiting_video(0)
    assert row["uploaded_at"] == 1700000000 and row["video_id"] is None
    with pytest.raises(ValueError):
        store.mark(-100, 5, "inbox", bogus=1)


async def test_get_updates_sends_long_polling_timeout_to_telegram():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content), timeout=request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})

    tg = Telegram("t", "https://tg.test", httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=33.0))
    assert await tg.updates(5) == [{"update_id": 1}]
    assert (seen["timeout"], seen["offset"]) == (45.0, 5)  # ждём дольше, чем Telegram держит long polling
    await tg.call("getMe")
    assert seen["timeout"] == 33.0  # остальные запросы — с таймаутом клиента, а не бесконечно


def test_video_keeps_its_frame_rate_and_scrolling_is_smooth():
    assert render_mod.clip_fps(23.976) == 23.976 and render_mod.clip_fps(59.94) == 59.94
    assert render_mod.clip_fps(12) == 30 and render_mod.clip_fps(None) == 30  # медленные GIF — к 30 кадрам
    assert render_mod.SCROLL_FPS == 60
    # прокрутка — целое число пикселей за кадр или за два: шаги одинаковые, рывков нет
    assert render_mod.scroll_step(1500, 25) == (1, 1) and render_mod.scroll_step(1500, 50) == (1, 2)
    assert render_mod.scroll_step(1500, 6) == (3, 1)
    expr, seconds = render_mod.scroll_y(170, -830, 2.0, (1, 2))
    assert seconds == 1000 * 2 / 60 and "floor(clip(n-120,0,2000)*1/2)" in expr


async def test_tall_post_with_video_scrolls_to_the_end_after_the_video(tmp_path):
    text = "Текст поста про новый инструмент. " * 25
    shot = Shot(_card(924, 3000), video_box=(30, 110, 863, 487))
    data, seconds, has_audio = await render_shot(shot, await _clip(tmp_path, 3, audio=True), text)
    info, real = await _info(tmp_path, data)
    assert has_audio and "aac" in info and "1080x1920" in info
    assert seconds >= 3 + 4 + render_mod.HOLD_END and real == pytest.approx(seconds, abs=0.3)  # видео, потом прокрутка
    assert " 60 fps" in info  # 30-кадровое видео рядом с прокруткой — 60 кадров, без рывков


async def test_post_slightly_taller_than_safe_zone_is_shrunk_not_scrolled(tmp_path):
    text = "Пост чуть выше безопасной зоны кадра. " * 10
    data, seconds, _ = await render_shot(Shot(_card(924, 1700)), None, text)
    info, real = await _info(tmp_path, data)
    assert seconds == reading_seconds(text) and " 30 fps" in info  # статичный кадр: прокрутки нет
