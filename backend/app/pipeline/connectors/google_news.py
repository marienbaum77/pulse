"""Ссылки Google News (news.google.com/rss/articles/<id>) — их отдаёт «Поиск статей». Страница по такой ссылке —
заглушка, настоящий адрес статьи она подставляет скриптом через несколько секунд, поэтому дозагрузка получала
заглушку вместо статьи. В старых ссылках адрес закодирован прямо в id; с 2024 года его выдаёт сама Google по запросу
batchexecute с подписью и временем со страницы-заглушки."""
import base64
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from ...netguard import guarded_client

HOST = "news.google.com"
BASE = f"https://{HOST}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Согласие с cookie: без него из Европы Google отвечает страницей consent.google.com вместо заглушки.
COOKIES = {"CONSENT": "YES+", "SOCS": "CAI"}
LOCALE = "hl=en-US&gl=US&ceid=US:en"
_ID = re.compile(r"^/(?:rss/)?articles/([\w-]+)$")
_SIGNATURE = re.compile(r'data-n-a-sg="([^"]+)"')
_TIMESTAMP = re.compile(r'data-n-a-ts="(\d+)"')
_URL_IN_ID = re.compile(rb"https?://[\x21-\x7e]+")


def article_id(url: str) -> str | None:
    """id статьи из ссылки Google News; None — это не ссылка на статью Google News."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    m = _ID.match(parts.path) if parts.hostname == HOST else None
    return m[1] if m else None


def url_in_id(gid: str) -> str | None:
    """Адрес из id старого формата: protobuf в base64, адрес лежит в нём открытым текстом."""
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except ValueError:
        return None
    m = _URL_IN_ID.search(raw)
    return m[0].decode() if m else None


def batch_request(gid: str, timestamp: int, signature: str) -> str:
    """Тело запроса batchexecute (RPC Fbv4je), каким его шлёт сама страница Google News."""
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, timestamp, signature]
    return "f.req=" + quote(json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]]))


def parse_batch(text: str) -> str | None:
    """Адрес статьи из ответа batchexecute: «)]}'», пустая строка, затем JSON, где адрес — второй элемент вложенного JSON."""
    try:
        payload = json.loads(json.loads(text.split("\n\n", 1)[1])[0][2])
    except (IndexError, TypeError, ValueError):
        return None
    url = payload[1] if isinstance(payload, list) and len(payload) > 1 else None
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


async def resolve(url: str) -> str | None:
    """Настоящий адрес статьи по ссылке Google News; None — ссылка не из Google News или адрес узнать не удалось."""
    gid = article_id(url)
    if not gid:
        return None
    found = url_in_id(gid)
    if found:
        return found
    async with guarded_client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, headers={"User-Agent": UA},
                              cookies=COOKIES) as client:
        # Без языка и региона Google перенаправляет на ту же страницу с ними; переходы — только внутри Google News.
        page = await client.get(f"{BASE}/rss/articles/{gid}?{LOCALE}")
        for _ in range(3):
            target = page.next_request.url if page.is_redirect and page.next_request else None
            if target is None or target.host != httpx.URL(BASE).host:
                break
            page = await client.get(target)
        page.raise_for_status()
        signature, timestamp = _SIGNATURE.search(page.text), _TIMESTAMP.search(page.text)
        if not signature or not timestamp:
            return None
        r = await client.post(f"{BASE}/_/DotsSplashUi/data/batchexecute", content=batch_request(gid, int(timestamp[1]), signature[1]),
                              headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        r.raise_for_status()
        return parse_batch(r.text)
