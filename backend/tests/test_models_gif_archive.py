"""Список моделей с двух серверов (генерация и эмбеддинги), GIF → MP4 перед отправкой и снимок статьи из Wayback Machine,
когда сайт закрыт для ботов."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import providers, publisher
from app.pipeline import ingest, media

from .helpers import MiniServer


@pytest.mark.parametrize(
    ("name", "kind"),
    [("qwen/qwen3.8-27b", "chat"), ("openai/gpt-oss-120b", "chat"), ("bge-m3:latest", "embed"), ("whisper-large-v3", "other"),
     ("canopylabs/orpheus-v1-english", "other"), ("meta-llama/llama-prompt-guard-2-86m", "other"), ("openai/gpt-oss-safeguard-20b", "other")],
)
def test_classify_model_separates_speech_and_guards(name, kind):
    assert providers.classify_model(name) == kind


async def test_models_list_combines_chat_server_and_embedding_server(monkeypatch):
    def groq(method, path, headers, body):
        if path == "/v1/models":
            return 200, {"Content-Type": "application/json"}, '{"data":[{"id":"qwen/qwen3.8-27b"},{"id":"whisper-large-v3"},{"id":"openai/gpt-oss-120b"}]}'
        return 404, {}, b""

    def ollama(method, path, headers, body):
        if path == "/api/tags":
            return 200, {"Content-Type": "application/json"}, (
                '{"models":[{"name":"bge-m3:latest","details":{"family":"bert"}},{"name":"qwen2.5:7b","details":{"family":"qwen2"}}]}'
            )
        return 404, {}, b""

    async with MiniServer(groq) as llm, MiniServer(ollama) as emb:
        settings = SimpleNamespace(llm_provider="openai", llm_base_url=f"{llm.url}/v1", llm_api_key="k", embed_base_url=f"{emb.url}/v1", embed_api_key="")
        monkeypatch.setattr(providers, "get_settings", lambda: settings)
        models = await providers.list_remote_models()
    # чат-модели — только с сервера генерации, без распознавания речи; embedding — с Ollama, без её чат-моделей
    assert [(m["id"], m["kind"]) for m in models] == [("qwen/qwen3.8-27b", "chat"), ("openai/gpt-oss-120b", "chat"), ("bge-m3:latest", "embed")]


async def test_gif_is_converted_to_mp4(tmp_path):
    ffmpeg = media._ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg недоступен")
    gif = tmp_path / "test.gif"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=65x49:rate=5", "-t", "1", str(gif),
    )
    await proc.wait()
    mp4 = await media.gif_to_mp4(gif.read_bytes())
    assert mp4 and mp4[4:8] == b"ftyp"  # нечётные размеры 65×49 не мешают: кадр приводится к чётным
    assert await media.gif_to_mp4(b"not a gif") is None


async def test_animation_goes_out_as_mp4_when_converted(monkeypatch):
    sent = []

    def handler(method, path, headers, body):
        if path == "/a/demo.gif":
            return 200, {"Content-Type": "image/gif"}, b"GIF89a" + b"\0" * 16
        if path.startswith("/bot"):
            sent.append((path.rsplit("/", 1)[-1], body))
        return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":{"message_id":7}}'

    async def fake_convert(data):
        return b"\0\0\0\x18ftypisomMP4DATA"

    monkeypatch.setattr(media, "gif_to_mp4", fake_convert)
    async with MiniServer(handler) as srv:
        monkeypatch.setenv("T_BOT", "OK")
        channel = {"config": {"chat_id": "@c", "token_env": "T_BOT", "api_base": srv.url}}
        draft = {"id": 1, "title": "Демо", "body": "Короткий пост.", "citations": [], "video_url": f"{srv.url}/a/demo.gif", "video_kind": "animation"}
        assert (await publisher.send_telegram(channel, draft, {"id": 1})).kind == "sent"
    method_name, body = sent[-1]
    assert method_name == "sendAnimation" and b'filename="animation.mp4"' in body and b"MP4DATA" in body and b"video/mp4" in body


async def test_blocked_article_falls_back_to_wayback_snapshot(monkeypatch):
    site = "https://openai.com/index/sharing-ai-progress-in-mathematics/"

    async def fake_fetch(url):
        if url == site:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("blocked", request=request, response=httpx.Response(403, request=request))
        assert url == f"https://web.archive.org/web/20261021202844id_/{site}"
        return "<html><body><article><p>Полный текст статьи.</p></article></body></html>", url

    async def fake_get(url, headers):
        assert url.startswith("https://archive.org/wayback/available?")
        return httpx.Response(200, json={"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": "20261021202844"}}})

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch)
    monkeypatch.setattr(ingest, "http_get", fake_get)
    page, url = await ingest.fetch_html_or_archive(site)
    assert "Полный текст" in page and url == site  # адрес исходный: ссылки и картинки разбираются как на сайте

    async def no_snapshot(url, headers):
        return httpx.Response(200, json={"archived_snapshots": {}})

    monkeypatch.setattr(ingest, "http_get", no_snapshot)
    with pytest.raises(httpx.HTTPStatusError):
        await ingest.fetch_html_or_archive(site)  # в архиве нет — ошибка сайта остаётся, дозагрузка повторится позже


def test_image_type_is_sniffed_and_odd_formats_become_jpeg():
    import io

    from PIL import Image

    from app import images

    webp = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(webp, "WEBP")
    assert images.normalize_image(webp.getvalue())[1] == "image/webp"  # сервер мог назвать его application/octet-stream
    bmp = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(bmp, "BMP")
    data, ctype = images.normalize_image(bmp.getvalue())
    assert ctype == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    with pytest.raises(ValueError):
        images.normalize_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")


async def test_octet_stream_webp_is_downloaded():
    import io

    from PIL import Image

    from app import images

    webp = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(webp, "WEBP")

    def handler(method, path, headers, body):
        return 200, {"Content-Type": "application/octet-stream"}, webp.getvalue()

    async with MiniServer(handler) as srv:
        assert (await images.download_image(f"{srv.url}/THUMBNAIL.webp"))[1] == "image/webp"


def test_component_attributes_and_og_video_are_found():
    page = """<html><head><meta property="og:video" content="https://cdn.example/v/teaser.mp4"></head><body><article>
    <uni-inline-image or-mp4-video-url="https://storage.example/original_videos/CREATE.mp4" autoplay="true"></uni-inline-image>
    <div data-video-src="/media/demo.webm"></div><a href="https://youtube.com/watch?v=1">видео</a></article></body></html>"""
    found = media.analyze(page, "https://blog.example/post", "")
    assert [v["url"] for v in found["videos"]] == [
        "https://storage.example/original_videos/CREATE.mp4", "https://blog.example/media/demo.webm", "https://cdn.example/v/teaser.mp4",
    ]
