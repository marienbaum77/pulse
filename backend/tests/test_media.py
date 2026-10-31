"""Медиа поста из первоисточника и README: поиск оригинала, иллюстрации, видео и GIF, отправка в Telegram (видео,
анимация, альбом) с откатом на фото."""
import json

import pytest

from app import db, publisher
from app.pipeline import generate, media
from app.pipeline.ingest import import_manual

from .helpers import MiniServer, make_project

VIDEO = b"\0\0\0\x18ftypisom" + b"\0" * 64  # заголовок MP4: длительность больше не проверяется
GIF = b"GIF89a\x01\x00\x01\x00" + b"\0" * 32


ARTICLE = """<html><head><meta property="og:image" content="https://news.example/stub.jpg"></head><body>
<header><a href="https://modal.com/">Modal</a></header>
<article><h1>Modal scales sandboxes</h1>
<p>Built on <a href="https://kubernetes.io/docs/concepts/scheduling/">Kubernetes scheduling</a> ideas.</p>
<p>The team explained <a href="https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds">how they rebuilt it</a>.</p>
<img src="/author.jpg" alt="Author avatar"><img src="https://news.example/i/tiny.png" width="40" height="40">
</article></body></html>"""


def test_find_origin_prefers_article_slug_over_technology_docs():
    origin = media.analyze(ARTICLE, "https://news.example/2026/modal", "Beyond Kubernetes at Modal: how to scale 1 million sandboxes")["origin"]
    assert origin == "https://modal.com/blog/scaling-to-1-million-concurrent-sandboxes-in-seconds"
    # без совпадений с заголовком и без подписи «источник» первоисточника нет
    unrelated = '<article><h1>Новости недели</h1><p>См. <a href="https://kubernetes.io/docs/concepts/">документацию</a>.</p></article>'
    assert media.analyze(unrelated, "https://news.example/2026/week", "Новости недели")["origin"] is None
    captioned = '<article><p>Источник: <a href="https://lab.example/notes/7">lab.example</a></p></article>'
    assert media.analyze(captioned, "https://news.example/x", "Новости недели")["origin"] == "https://lab.example/notes/7"


def test_page_media_keeps_illustrations_and_video_sources():
    page = """<html><head><meta property="og:image" content="https://modal.com/docs/social-image.png?title=X"></head><body><article>
    <img src="https://cdn.example/team/colin_avatar.webp"><img src="https://cdn.example/blog/1Msandboxes.webp" alt="Evidence">
    <video poster="https://cdn.example/blog/demo-poster.jpg" autoplay muted loop><source src="/media/demo.mp4" type="video/mp4"></video>
    <img data-src="https://cdn.example/blog/architecture.webp" src="data:image/gif;base64,AAA"></article></body></html>"""
    found = media.analyze(page, "https://modal.com/blog/post", "")
    assert found["images"] == [
        "https://cdn.example/blog/1Msandboxes.webp", "https://cdn.example/blog/demo-poster.jpg", "https://cdn.example/blog/architecture.webp",
    ]  # аватар и карточка для соцсетей отброшены
    assert found["videos"] == [{"url": "https://modal.com/media/demo.mp4", "poster": "https://cdn.example/blog/demo-poster.jpg", "kind": "video"}]


def test_page_media_skips_related_cards_and_ads():
    # у thecode.media слайдер «читайте также» лежит внутри той же <article>, что и статья
    page = """<html><head><meta property="og:image" content="https://news.example/up/hero.jpeg"></head><body><article>
    <div class="article-header"><img src="https://news.example/up/hero-720x405.jpeg"></div>
    <div class="content"><a href="https://news.example/up/chart.png"><img src="https://news.example/up/chart-720x400.png"></a>
    <a href="https://partner.example/course/?utm_source=content"><img src="https://news.example/up/banner-109.png"></a></div>
    <section class="main-cat-slider"><a href="https://news.example/other-story/" class="slider-post__img">
    <img src="https://news.example/up/other-720x404.png"></a></section></article></body></html>"""
    found = media.analyze(page, "https://news.example/this-story/", "")
    assert "https://news.example/up/other-720x404.png" not in found["images"]  # карточка другой статьи сайта
    assert "https://news.example/up/banner-109.png" not in found["images"]  # реклама с utm-метками
    assert "https://news.example/up/chart-720x400.png" in found["images"]  # ссылка на полный размер — иллюстрация
    assert found["og_image"] == "https://news.example/up/hero.jpeg"


def test_readme_media_skips_badges_and_keeps_gifs_and_attachments():
    readme = """<html><body><article class="markdown-body">
    <a href="https://github.com/a/b/actions"><img src="https://camo.githubusercontent.com/1" data-canonical-src="https://img.shields.io/badge/build-passing-green"></a>
    <img src="https://camo.githubusercontent.com/2" data-canonical-src="https://raw.githubusercontent.com/a/b/main/docs/demo.gif">
    <img src="/a/b/raw/main/docs/architecture.png" alt="Architecture">
    <video src="https://github.com/user-attachments/assets/0b5c-77"></video></article></body></html>"""
    found = media.analyze(readme, "https://github.com/a/b", "")
    assert found["images"] == ["https://github.com/a/b/raw/main/docs/architecture.png"]
    assert found["videos"] == [
        {"url": "https://camo.githubusercontent.com/2", "poster": None, "kind": "animation"},
        {"url": "https://github.com/user-attachments/assets/0b5c-77", "poster": None, "kind": "video"},
    ]


async def test_pick_video_takes_any_length_within_telegram_limit(monkeypatch):
    def handler(method, path, headers, body):
        if path == "/page.mp4":
            return 200, {"Content-Type": "text/html"}, b"<html></html>"
        if path == "/long.mp4":
            return 200, {"Content-Type": "video/mp4"}, VIDEO
        if path == "/demo.gif":
            return 200, {"Content-Type": "image/gif"}, GIF
        if path == "/attachment":
            return 302, {"Location": "/long.mp4"}, b""
        return 404, {}, b""

    async with MiniServer(handler) as srv:
        assert await media.pick_video([{"url": f"{srv.url}/page.mp4"}, {"url": f"{srv.url}/long.mp4"}]) == {"url": f"{srv.url}/long.mp4", "kind": "video"}
        assert await media.pick_video([{"url": f"{srv.url}/demo.gif", "kind": "animation"}]) == {"url": f"{srv.url}/demo.gif", "kind": "animation"}
        assert await media.pick_video([{"url": f"{srv.url}/attachment", "kind": "video"}]) == {"url": f"{srv.url}/attachment", "kind": "video"}
        monkeypatch.setattr(media, "MAX_VIDEO_BYTES", 10)
        assert await media.pick_video([{"url": f"{srv.url}/long.mp4"}]) is None  # больше предела Telegram


async def test_cover_prefers_origin_illustrations_and_extras_are_checked(monkeypatch):
    sizes = {"https://modal.com/1M.webp": True, "https://modal.com/arch.webp": True, "https://modal.com/tiny.webp": False}

    async def ok(url):
        return sizes.get(url, True)

    async def no_defaults(urls):
        return set()

    async def no_bytes(url):
        return None  # содержимое недоступно — сравнение только по адресам

    monkeypatch.setattr(generate, "_cover_ok", ok)
    monkeypatch.setattr(generate, "default_image_urls", no_defaults)
    monkeypatch.setattr(generate, "_image_bytes", no_bytes)
    cites = [{"image_url": "https://news.example/stub.jpg"}]
    assert await generate.pick_cover_image(cites, first=["https://modal.com/1M.webp"]) == "https://modal.com/1M.webp"
    assert await generate.pick_cover_image(cites, last=["https://news.example/inline.webp"]) == "https://news.example/stub.jpg"
    extras = await generate.pick_extra_images(["https://modal.com/1M.webp", "https://modal.com/tiny.webp", "https://modal.com/arch.webp"], {"https://modal.com/1M.webp"})
    assert extras == ["https://modal.com/arch.webp"]  # обложка не повторяется, мелкая картинка в альбом не идёт
    # карточка страницы для соцсетей и её уменьшенная копия в шапке статьи — обложки, а не иллюстрации
    extras = await generate.pick_extra_images(["https://news.example/up/hero-720x405.jpeg", "https://news.example/up/chart.png"],
                                              {"https://modal.com/1M.webp", "https://news.example/up/hero.jpeg"})
    assert extras == ["https://news.example/up/chart.png"]


@pytest.mark.usefixtures("clean")
async def test_post_media_follows_article_to_origin_and_repo_once():
    hits = []

    def handler(method, path, headers, body):
        hits.append(path)
        if path == "/news/modal":
            return 200, {"Content-Type": "text/html"}, (
                f'<html><body><article><p>Read <a href="http://localhost:{srv.port}/blog/scaling-million-sandboxes">the post</a>.</p>'
                "</article></body></html>"
            )
        if path == "/blog/scaling-million-sandboxes":
            return 200, {"Content-Type": "text/html"}, (
                '<html><body><article><img src="/img/1M.webp" alt="chart"><video><source src="/v/demo.mp4"></video></article></body></html>'
            )
        if path == "/modal-labs/sandbox":
            return 200, {"Content-Type": "text/html"}, '<html><body><article><img src="/img/readme.png" alt="diagram"></article></body></html>'
        return 404, {}, b""

    async with MiniServer(handler) as srv:
        pid = await make_project()
        await import_manual(pid, [{"title": "Scaling to 1 million sandboxes", "url": f"{srv.url}/news/modal", "text": "t", "source": "News"}])
        item = await db.fetchone("SELECT id, url, title FROM items WHERE project_id = %s", (pid,))
        cites = [{"item_id": item["id"], "url": item["url"], "title": item["title"], "kind": "article", "media": None}]
        repo = {"url": f"{srv.url}/modal-labs/sandbox", "kind": "repo"}
        found = await media.post_media(cites, repo)
        assert found["origin_images"] == [f"http://localhost:{srv.port}/img/1M.webp", f"{srv.url}/img/readme.png"]
        assert found["videos"] == [{"url": f"http://localhost:{srv.port}/v/demo.mp4", "poster": None, "kind": "video"}]
        assert hits == ["/news/modal", "/blog/scaling-million-sandboxes", "/modal-labs/sandbox"]

        # результат сохранён в материале: повторная подготовка поста страниц не скачивает
        meta = (await db.fetchone("SELECT meta FROM items WHERE id = %s", (item["id"],)))["meta"]
        cites[0]["media"] = meta["media"]
        assert (await media.post_media(cites, repo))["origin_images"] == found["origin_images"]
        assert len(hits) == 3


def test_classify_album_response_uses_first_message():
    res = publisher.classify_telegram(200, {"ok": True, "result": [{"message_id": 11}, {"message_id": 12}]})
    assert res.kind == "sent" and res.external_id == "11"


async def test_telegram_sends_video_animation_and_album_with_fallbacks(monkeypatch):
    calls = []
    reject = set()

    def handler(method, path, headers, body):
        if path == "/v/demo.mp4":
            return 200, {"Content-Type": "video/mp4"}, VIDEO
        if path == "/a/demo.gif":
            return 200, {"Content-Type": "image/gif"}, GIF
        if path.startswith("/img/"):
            return 200, {"Content-Type": "image/png"}, b"\x89PNG-bytes"
        if path.startswith("/bot"):
            method_name = path.rsplit("/", 1)[-1]
            calls.append((method_name, body))
            if method_name in reject:
                return 400, {"Content-Type": "application/json"}, '{"ok":false,"description":"Bad Request: wrong file"}'
            if method_name == "sendMediaGroup":
                return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":[{"message_id":21},{"message_id":22}]}'
        return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":{"message_id":7}}'

    async with MiniServer(handler) as srv:
        monkeypatch.setenv("T_BOT", "OK")
        channel = {"config": {"chat_id": "@c", "token_env": "T_BOT", "api_base": srv.url}}
        draft = {"id": 1, "title": "Modal", "body": "Миллион песочниц за секунды.", "citations": [],
                 "image_url": f"{srv.url}/img/1.png", "video_url": f"{srv.url}/v/demo.mp4", "video_kind": "video"}
        assert (await publisher.send_telegram(channel, draft, {"id": 1})).kind == "sent"
        assert calls[-1][0] == "sendVideo" and b"supports_streaming" in calls[-1][1] and b"<b>Modal</b>" in calls[-1][1]

        reject.add("sendVideo")
        assert (await publisher.send_telegram(channel, draft, {"id": 2})).kind == "sent"
        assert [c[0] for c in calls[-2:]] == ["sendVideo", "sendPhoto"]  # Telegram отклонил видео — пост ушёл с картинкой
        reject.clear()

        long_post = {**draft, "body": "Длинный пост. " * 120}
        assert (await publisher.send_telegram(channel, long_post, {"id": 3})).kind == "sent"
        assert calls[-1][0] == "sendMessage"  # подпись к видео не вмещает текст: полный текст с картинкой над ним

        gif = {**draft, "video_url": f"{srv.url}/a/demo.gif", "video_kind": "animation", "extra_images": [f"{srv.url}/img/2.png"]}
        assert (await publisher.send_telegram(channel, gif, {"id": 4})).kind == "sent"
        assert calls[-1][0] == "sendAnimation" and b"GIF89a" in calls[-1][1]  # GIF в альбом не берётся

        album = {**draft, "extra_images": [f"{srv.url}/img/2.png", f"{srv.url}/img/3.png"], "reply_to_message_id": 5}
        res = await publisher.send_telegram(channel, album, {"id": 5})
        assert res.kind == "sent" and res.external_id == "21"
        method_name, body = calls[-1]
        assert method_name == "sendMediaGroup" and b'"message_id": 5' in body
        items = json.loads(body.split(b'name="media"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0])
        assert [i["type"] for i in items] == ["video", "photo", "photo", "photo"] and items[0]["caption"].startswith("<b>Modal</b>")

        reject.add("sendMediaGroup")
        assert (await publisher.send_telegram(channel, album, {"id": 6})).kind == "sent"
        assert [c[0] for c in calls[-2:]] == ["sendMediaGroup", "sendVideo"]  # альбом отклонён — видео с подписью


def test_image_key_matches_sizes_of_one_picture():
    key = generate.image_key
    assert key("https://g.example/images/THUMBNAIL_BLOG.width-1300.png") == key("https://g.example/images/THUMBNAIL_BLOG.max-600x600.format-webp.webp")
    assert key("https://wp.example/2026/10/photo-1024x768.jpg") == key("https://wp.example/2026/10/photo.jpg")
    assert key("https://modal.com/1M.webp") != key("https://modal.com/arch.webp")


def _picture(seed: int, size=(1280, 720)):
    """Иллюстрация с крупными деталями: прямоугольники и полосы разной яркости."""
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        draw.rectangle([x, y, x + rnd.randrange(40, 300), y + rnd.randrange(20, 160)], fill=tuple(rnd.randrange(256) for _ in range(3)))
    return img


def _encode(img, fmt="PNG") -> bytes:
    import io

    out = io.BytesIO()
    img.save(out, fmt, quality=80) if fmt == "JPEG" else img.save(out, fmt)
    return out.getvalue()


def test_same_picture_finds_copies_and_crops_but_not_different_pictures():
    pytest.importorskip("PIL")
    from app.images import same_picture

    original = _picture(1)
    copy = _encode(original.resize((609, 343)), "JPEG")  # та же картинка под другим именем, меньше и в JPEG
    crop = _encode(original.crop((300, 160, 1150, 638)).resize((1280, 720)))  # приближенная часть, как у Хабра
    assert same_picture(_encode(original), copy)
    assert same_picture(_encode(original), crop) and same_picture(crop, _encode(original))
    assert not same_picture(_encode(original), _encode(_picture(2)))
    assert not same_picture(_encode(original), b"not an image")


async def test_album_skips_content_duplicates_of_cover_and_extras(monkeypatch):
    pytest.importorskip("PIL")
    pictures = {"https://src.example/cover.png": _encode(_picture(1)),
                "https://habr.example/a.png": _encode(_picture(2)),
                "https://habr.example/b.png": _encode(_picture(2).resize((640, 360)), "JPEG"),  # та же, что a.png
                "https://habr.example/c.jpg": _encode(_picture(1).crop((300, 160, 1150, 638)).resize((1280, 720))),  # кадр обложки
                "https://habr.example/d.png": _encode(_picture(3))}

    async def image_bytes(url):
        return pictures.get(url)

    async def ok(url):
        return True

    async def no_defaults(urls):
        return set()

    monkeypatch.setattr(generate, "_image_bytes", image_bytes)
    monkeypatch.setattr(generate, "_cover_ok", ok)
    monkeypatch.setattr(generate, "default_image_urls", no_defaults)
    extras = await generate.pick_extra_images(list(pictures)[1:], {"https://src.example/cover.png"}, cover="https://src.example/cover.png")
    assert extras == ["https://habr.example/a.png", "https://habr.example/d.png"]
