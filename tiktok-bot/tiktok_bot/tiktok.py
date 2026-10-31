"""TikTok Content Posting API, режим Upload: ролик попадает во «Входящие» приложения TikTok, владелец открывает
уведомление и публикует. Этот режим работает без аудита приложения (Direct Post без аудита публикует только приватно).
Вход — OAuth: бот даёт ссылку, после входа TikTok переадресует на TIKTOK_REDIRECT_URI с ?code=…, владелец присылает
боту адрес из строки браузера. Страница по этому адресу может и не открыться — нужен только код в нём.
Скоуп video.list (продукт Display API) открывает просмотры и лайки опубликованных роликов; без него бот только загружает."""
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

SCOPES = "user.info.basic,video.upload,video.list"
STATS_SCOPE = "video.list"
SINGLE_CHUNK_LIMIT = 64 * 1024 * 1024  # до 64 МБ TikTok принимает файл одним куском
CHUNK = 32 * 1024 * 1024  # больше — частями: кусок 5–64 МБ, последний вбирает остаток (до 128 МБ)
VIDEO_FIELDS = "id,create_time,title,video_description,duration,share_url,view_count,like_count,comment_count,share_count"


class TikTokError(RuntimeError):
    pass


def extract_code(text: str) -> tuple[str | None, str | None]:
    """(code, state) из присланного адреса переадресации или из «голого» кода."""
    text = text.strip()
    if "code=" in text:
        query = parse_qs(urlsplit(text).query or text.split("?", 1)[-1])
        return (query.get("code") or [None])[0], (query.get("state") or [None])[0]
    return (text, None) if len(text) > 20 and " " not in text else (None, None)


class TikTok:
    def __init__(self, cfg, store, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.store = store
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    @property
    def configured(self) -> bool:
        return bool(self.cfg.tiktok_client_key and self.cfg.tiktok_client_secret and self.cfg.tiktok_redirect_uri)

    @property
    def authorized(self) -> bool:
        tokens = self.store.get("tiktok_tokens") or {}
        return bool(tokens.get("refresh_token")) and tokens.get("refresh_expires_at", 0) > time.time()

    @property
    def can_read_stats(self) -> bool:
        return self.authorized and STATS_SCOPE in (self.store.get("tiktok_tokens") or {}).get("scope", "").split(",")

    def auth_url(self) -> str:
        state = secrets.token_urlsafe(16)
        self.store.set("tiktok_state", state)
        params = {"client_key": self.cfg.tiktok_client_key, "scope": SCOPES, "response_type": "code",
                  "redirect_uri": self.cfg.tiktok_redirect_uri, "state": state}
        return f"{self.cfg.tiktok_auth_url}?{urlencode(params)}"

    async def _token(self, **grant) -> dict:
        r = await self.client.post(
            f"{self.cfg.tiktok_api}/v2/oauth/token/",
            data={"client_key": self.cfg.tiktok_client_key, "client_secret": self.cfg.tiktok_client_secret, **grant},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = r.json()
        if r.status_code != 200 or not data.get("access_token"):
            raise TikTokError(data.get("error_description") or data.get("error") or f"HTTP {r.status_code}")
        now = time.time()
        tokens = {
            "access_token": data["access_token"], "expires_at": now + int(data.get("expires_in", 0)) - 120,
            "refresh_token": data["refresh_token"], "refresh_expires_at": now + int(data.get("refresh_expires_in", 0)),
            "open_id": data.get("open_id"), "scope": data.get("scope", ""),
        }
        self.store.set("tiktok_tokens", tokens)
        return tokens

    async def exchange(self, code: str, state: str | None) -> dict:
        expected = self.store.get("tiktok_state")
        if state is not None and expected and state != expected:
            raise TikTokError("Ссылка входа устарела: запросите новую командой /auth")
        tokens = await self._token(code=code, grant_type="authorization_code", redirect_uri=self.cfg.tiktok_redirect_uri)
        if "video.upload" not in tokens["scope"].split(","):
            raise TikTokError("TikTok не выдал разрешение video.upload: проверьте продукт Content Posting API в приложении")
        return tokens

    async def access_token(self) -> str:
        tokens = self.store.get("tiktok_tokens") or {}
        if not tokens.get("refresh_token"):
            raise TikTokError("TikTok не подключён: отправьте боту /auth")
        if tokens.get("expires_at", 0) > time.time():
            return tokens["access_token"]
        # access_token живёт сутки, refresh_token — год: обновляем без участия владельца
        return (await self._token(grant_type="refresh_token", refresh_token=tokens["refresh_token"]))["access_token"]

    async def _post(self, path: str, body: dict, what: str, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {await self.access_token()}", "Content-Type": "application/json; charset=UTF-8"}
        r = await self.client.post(f"{self.cfg.tiktok_api}{path}", headers=headers, json=body, params=params)
        data = r.json()
        error = data.get("error") or {}
        if r.status_code != 200 or error.get("code") not in (None, "ok"):
            raise TikTokError(f"{what}: {error.get('message') or error.get('code') or r.status_code}")
        return data.get("data") or {}

    async def upload_to_inbox(self, video: bytes) -> str:
        """Загружает ролик во «Входящие» TikTok; возвращает publish_id. Больше 64 МБ — частями по порядку."""
        size = len(video)
        ranges = chunk_ranges(size)
        data = await self._post("/v2/post/publish/inbox/video/init/", {"source_info": {
            "source": "FILE_UPLOAD", "video_size": size, "chunk_size": ranges[0][1] - ranges[0][0], "total_chunk_count": len(ranges)}}, "init")
        for start, end in ranges:
            put = await self.client.put(
                data["upload_url"], content=video[start:end],
                headers={"Content-Type": "video/mp4", "Content-Length": str(end - start), "Content-Range": f"bytes {start}-{end - 1}/{size}"},
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
            if put.status_code not in (200, 201, 206):  # 206 — кусок принят, ждём следующий
                raise TikTokError(f"upload: HTTP {put.status_code}")
        return data["publish_id"]

    async def publish_status(self, publish_id: str) -> dict:
        """Что стало с роликом из «Входящих»: status (SEND_TO_USER_INBOX, PUBLISH_COMPLETE, FAILED…) и post_ids —
        id опубликованного ролика, когда он публичный и прошёл модерацию."""
        data = await self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id}, "status")
        return {"status": data.get("status"), "fail_reason": data.get("fail_reason"),
                "post_ids": [str(x) for x in data.get("publicaly_available_post_id") or []]}  # опечатка — в самом API

    async def list_videos(self, max_count: int = 20) -> list[dict]:
        """Последние публичные ролики аккаунта, новые первыми (скоуп video.list)."""
        data = await self._post("/v2/video/list/", {"max_count": max_count}, "video list", params={"fields": VIDEO_FIELDS})
        return data.get("videos") or []

    async def query_videos(self, ids: list[str]) -> list[dict]:
        """Просмотры, лайки, комментарии и репосты роликов по id (до 20 за запрос)."""
        data = await self._post("/v2/video/query/", {"filters": {"video_ids": ids[:20]}}, "video query", params={"fields": VIDEO_FIELDS})
        return data.get("videos") or []


def chunk_ranges(size: int) -> list[tuple[int, int]]:
    """Куски файла для загрузки [начало, конец): до 64 МБ — одним куском, иначе по CHUNK, последний забирает остаток."""
    if size <= SINGLE_CHUNK_LIMIT:
        return [(0, size)]
    count = size // CHUNK
    return [(i * CHUNK, (i + 1) * CHUNK if i < count - 1 else size) for i in range(count)]
