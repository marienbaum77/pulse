"""Ссылки Google News из «Поиска статей»: раскрытие в адрес статьи (старый формат id и запрос batchexecute)
и дозагрузка статьи по раскрытому адресу. Без БД; Google заменяет локальный сервер."""
import asyncio
import base64

from app.pipeline import ingest
from app.pipeline.connectors import google_news

from .helpers import MiniServer

NEW_ID = "CBMiYkFVX3lxTE5raGpFUFZYcWtKeHhDQ2RxVHdYN1Viekl3WmJ0TG01SlpJWW5MWUl4MmZXa3l3NXF4U0dxLWhVRmVaR3FycDZ3OXZFZjRTME9nUm5rOGFxcFlTRFU0ZnRXRTdn"
# так Google действительно отвечает на batchexecute (ответ для ссылки на статью Хабра)
BATCH = ")]}'\n\n[[\"wrb.fr\",\"Fbv4je\",\"[\\\"garturlres\\\",\\\"https://habr.com/ru/companies/jugru/articles/503146/\\\",1]\",null,null,null,\"generic\"]," \
        "[\"di\",14],[\"af.httprm\",13,\"-1621618767257303512\",5]]"


def test_article_id_only_for_google_news_article_links():
    assert google_news.article_id(f"https://news.google.com/rss/articles/{NEW_ID}?oc=5") == NEW_ID
    assert google_news.article_id(f"https://news.google.com/articles/{NEW_ID}") == NEW_ID
    assert google_news.article_id("https://news.google.com/rss/search?q=typescript") is None
    assert google_news.article_id(f"https://example.com/rss/articles/{NEW_ID}") is None


def test_old_ids_carry_the_url_and_new_ones_do_not():
    raw = b'\x08\x13"' + bytes([30]) + b"https://lenta.example/news/42/" + b"\xd2\x01\x00"
    old = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert google_news.url_in_id(old) == "https://lenta.example/news/42/"
    assert google_news.url_in_id(NEW_ID) is None


def test_batch_response_is_parsed():
    assert google_news.parse_batch(BATCH) == "https://habr.com/ru/companies/jugru/articles/503146/"
    assert google_news.parse_batch(")]}'\n\n[]") is None
    assert google_news.parse_batch("<html>error</html>") is None


async def test_resolve_and_enrich_follow_google_news_to_the_article(monkeypatch):
    def handler(method, path, headers, body):
        if method == "GET" and path.startswith(f"/rss/articles/{NEW_ID}?hl=en-US"):
            # как Google: страница с другим языком — снова редирект, внутри своего же сайта
            return 302, {"Location": f"/rss/articles/{NEW_ID}?hl=ru&gl=RU&ceid=RU:ru"}, b""
        if method == "GET" and path == f"/rss/articles/{NEW_ID}?hl=ru&gl=RU&ceid=RU:ru":
            return 200, {"Content-Type": "text/html"}, '<html><div data-n-a-sg="SIG1" data-n-a-ts="1792935396"></div></html>'
        if method == "POST" and path == "/_/DotsSplashUi/data/batchexecute":
            assert b"Fbv4je" in body and NEW_ID.encode() in body and b"SIG1" in body and b"1792935396" in body
            return 200, {"Content-Type": "application/json"}, BATCH.replace("https://habr.com/ru/companies/jugru/articles/503146/",
                                                                             f"http://localhost:{srv.port}/article")
        if path == "/article":
            text = " ".join(["Компилятор TypeScript на TypeScript использует LLVM для генерации машинного кода."] * 12)
            return 200, {"Content-Type": "text/html"}, f"<html><body><article><h1>Компилятор</h1><p>{text}</p></article></body></html>"
        return 404, {}, b""

    async with MiniServer(handler) as srv:
        monkeypatch.setattr(google_news, "BASE", srv.url)
        link = f"https://news.google.com/rss/articles/{NEW_ID}?oc=5"
        assert await google_news.resolve(link) == f"http://localhost:{srv.port}/article"
        item = {"id": 1, "title": "Разработка компилятора для TypeScript", "status": "new", "cluster_id": None, "url": link,
                "text": "Разработка компилятора для TypeScript — Хабр", "image_url": None, "kind": "article"}
        result = await ingest._enrich_article(item, asyncio.Semaphore(1))
    assert result["url"] == f"http://localhost:{srv.port}/article" and result["error"] is None
    assert result["article"] and "LLVM" in result["article"]  # текст статьи, а не заглушки Google News
