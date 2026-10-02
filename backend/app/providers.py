"""Поставщики моделей. Всё, что нужно ядру: embed() и chat(). Локальный Ollama/vLLM и облако — один и тот же OpenAI-совместимый API."""
import hashlib
import json
import logging
import re
import time

import asyncio
import httpx
import numpy as np

from . import db
from .config import get_settings
from .textutil import words

log = logging.getLogger("pulse.llm")


class LLMUnavailable(RuntimeError):
    pass


def _provider_error_detail(error: Exception, timeout: float) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        try:
            body = json.dumps(response.json(), ensure_ascii=False)
        except (ValueError, TypeError):
            body = response.text.strip()
        detail = f"HTTP {response.status_code} {response.reason_phrase}"
        return f"{detail}: {body[:500]}" if body else detail
    if isinstance(error, httpx.ReadTimeout):
        return f"ReadTimeout: модель не ответила за {timeout:g} с (LLM_TIMEOUT)"
    if isinstance(error, httpx.ConnectTimeout):
        return f"ConnectTimeout: не удалось подключиться к серверу модели за 10 с"
    message = str(error).strip() or "без подробностей"
    return f"{type(error).__name__}: {message}"


RETRY_STATUSES = {429, 502, 503, 504}
RETRY_DELAYS = (1.0, 3.0)


async def _post_with_retry(client: httpx.AsyncClient, path: str, payload: dict) -> httpx.Response:
    """POST с повтором при временных сбоях (429/5xx шлюза, обрыв соединения); таймаут чтения не повторяется."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            r = await client.post(path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            if attempt == len(RETRY_DELAYS):
                raise
        else:
            if r.status_code not in RETRY_STATUSES or attempt == len(RETRY_DELAYS):
                return r
            retry_after = r.headers.get("retry-after", "")
            if retry_after.isdigit():
                await asyncio.sleep(min(float(retry_after), 10.0))
                continue
        await asyncio.sleep(RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")

async def _log_call(project_id, kind, model, pt, ct, ms, ok, err=None):
    try:
        await db.execute(
            "INSERT INTO llm_calls(project_id, kind, model, prompt_tokens, completion_tokens, latency_ms, ok, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (project_id, kind, model, pt, ct, ms, ok, (err or None) and str(err)[:500]),
        )
    except Exception:  # телеметрия не должна ломать конвейер
        log.exception("failed to log llm call")


class StubProvider:
    """Детерминированные «эмбеддинги» на хэшировании слов. Нужен для демо и тестов без GPU; качество кластеризации заметно ниже, чем у bge-m3."""

    name = "stub"
    chat_model = "extractive"
    embed_model = "stub-hash-256"
    supports_chat = False
    DIM = 256

    async def embed(self, texts: list[str], project_id: int | None = None) -> np.ndarray:
        out = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in words(t):
                if len(w) < 3:
                    continue
                h = int.from_bytes(hashlib.blake2b(w[:5].encode(), digest_size=8).digest(), "big")
                out[i, h % self.DIM] += 1.0 if (h >> 63) & 1 else -1.0
            n = np.linalg.norm(out[i])
            if n > 0:
                out[i] /= n
        return out

    async def chat(self, system: str, user: str, **kw) -> str:
        raise LLMUnavailable("Провайдер stub не умеет генерировать текст; используйте режим extractive или настройте LLM_PROVIDER=openai")

    async def ping(self) -> dict:
        return {"ok": True, "provider": "stub"}


class OpenAICompatProvider:
    name = "openai"
    supports_chat = True

    def __init__(self):
        s = get_settings()
        self.chat_model = s.llm_model
        self.embed_model = s.embed_model
        self.supports_chat = s.llm_chat_enabled
        timeout = httpx.Timeout(s.llm_timeout, connect=10.0)
        self._client = httpx.AsyncClient(base_url=s.llm_base_url.rstrip("/"), headers={"Authorization": f"Bearer {s.llm_api_key}"}, timeout=timeout)
        if s.embed_base_url:
            self._embed_client = httpx.AsyncClient(
                base_url=s.embed_base_url.rstrip("/"), headers={"Authorization": f"Bearer {s.embed_api_key or s.llm_api_key}"}, timeout=timeout
            )
        else:
            self._embed_client = self._client

    async def embed(self, texts: list[str], project_id: int | None = None) -> np.ndarray:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 8):
            batch = texts[i : i + 8]
            t0 = time.monotonic()
            try:
                r = await _post_with_retry(self._embed_client, "/embeddings", {"model": self.embed_model, "input": batch})
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                vectors.extend(d["embedding"] for d in data)
                usage = r.json().get("usage") or {}
                await _log_call(project_id, "embed", self.embed_model, usage.get("prompt_tokens"), None, int((time.monotonic() - t0) * 1000), True)
            except Exception as e:
                elapsed = time.monotonic() - t0
                detail = _provider_error_detail(e, get_settings().llm_timeout)
                await _log_call(project_id, "embed", self.embed_model, None, None, int(elapsed * 1000), False, detail)
                raise LLMUnavailable(f"Ошибка эмбеддингов ({self.embed_model}, {elapsed:.1f} с): {detail}") from e
        return np.asarray(vectors, dtype=np.float32)

    async def chat(self, system: str, user: str, *, max_tokens: int = 700, temperature: float = 0.3, project_id: int | None = None) -> str:
        if not self.supports_chat:
            raise LLMUnavailable("Генерация языковой моделью выключена; задайте LLM_CHAT_ENABLED=true, чтобы включить её")
        t0 = time.monotonic()
        try:
            r = await _post_with_retry(
                self._client,
                "/chat/completions",
                {
                    "model": self.chat_model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
            )
            r.raise_for_status()
            body = r.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = body.get("usage") or {}
            await _log_call(project_id, "chat", self.chat_model, usage.get("prompt_tokens"), usage.get("completion_tokens"), int((time.monotonic() - t0) * 1000), True)
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        except Exception as e:
            elapsed = time.monotonic() - t0
            detail = _provider_error_detail(e, get_settings().llm_timeout)
            await _log_call(project_id, "chat", self.chat_model, None, None, int(elapsed * 1000), False, detail)
            raise LLMUnavailable(f"Ошибка генерации ({self.chat_model}, {elapsed:.1f} с): {detail}") from e

    async def ping(self) -> dict:
        try:
            v = await self.embed(["ping"])
            return {"ok": True, "provider": "openai", "embed_dim": int(v.shape[1]), "chat_model": self.chat_model, "embed_model": self.embed_model}
        except Exception as e:
            return {"ok": False, "provider": "openai", "error": str(e)}


_provider = None
_runtime_fingerprint: str | None = None
_provider_override = False


def _native_base(url: str) -> str:
    """http://host:11434/v1 → http://host:11434 (нативный API Ollama: /api/tags, /api/pull)."""
    return url.rstrip("/").removesuffix("/v1")


async def load_runtime_overrides() -> dict:
    """Переопределения модели из UI (kv), поверх .env."""
    try:
        row = await db.fetchone("SELECT value FROM kv WHERE key = 'llm_runtime'")
    except Exception:
        return {}
    if not row or not row["value"]:
        return {}
    val = row["value"]
    return val if isinstance(val, dict) else {}


async def save_runtime_overrides(patch: dict) -> dict:
    from psycopg.types.json import Jsonb

    cur = await load_runtime_overrides()
    for k, v in patch.items():
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = v
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES ('llm_runtime', %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (Jsonb(cur),),
    )
    return cur


async def refresh_provider() -> object:
    """Подхватывает llm_model/embed_model из kv (изменения из админки без перезапуска контейнеров)."""
    global _provider, _runtime_fingerprint
    if _provider_override:
        return _provider
    s = get_settings()
    over = await load_runtime_overrides()
    chat = over.get("llm_model") or s.llm_model
    embed = over.get("embed_model") or s.embed_model
    fp = (
        f"{s.llm_provider}|{s.llm_chat_enabled}|{chat}|{embed}|{s.llm_base_url}|"
        f"{s.embed_base_url}|{s.llm_api_key}|{s.embed_api_key}"
    )
    if _provider is not None and _runtime_fingerprint == fp:
        return _provider
    if s.llm_provider == "openai":
        p = OpenAICompatProvider()
        p.chat_model = chat
        p.embed_model = embed
        _provider = p
    else:
        _provider = StubProvider()
    _runtime_fingerprint = fp
    return _provider


def get_provider():
    global _provider
    if _provider is None:
        _provider = OpenAICompatProvider() if get_settings().llm_provider == "openai" else StubProvider()
    return _provider


def set_provider(p) -> None:  # для тестов
    global _provider, _runtime_fingerprint, _provider_override
    _provider = p
    _runtime_fingerprint = None
    _provider_override = True


_EMBED_NAME = re.compile(
    r"(?:^|[:/\-_.])(?:bge|e5|gte|jina[-_]?embed|nomic[-_]?embed|mxbai[-_]?embed|snowflake[-_]?arctic[-_]?embed|"
    r"all[-_]?minilm|multilingual[-_]?e5|text[-_]?embedding|embed(?:ding)?s?|qwen\d*(?:\.\d+)?[-_]?embed)",
    re.I,
)
_EMBED_FAMILIES = frozenset({
    "bert", "nomic-bert", "nomic-bert-moe", "jina-bert-v2", "gemma-embedding",
})


def classify_model(model_id: str, *, family: str | None = None, families: list[str] | None = None) -> str:
    """'embed' | 'chat' — по имени и семейству Ollama. Эмбеддинг-модели не годятся для чата и наоборот."""
    name = (model_id or "").strip()
    base = name.split(":")[0]
    fams = {*(families or []), *( [family] if family else [] )}
    fams = {f.lower() for f in fams if f}
    if fams & _EMBED_FAMILIES or _EMBED_NAME.search(name) or _EMBED_NAME.search(base):
        return "embed"
    return "chat"


async def list_remote_models() -> list[dict]:
    """Список моделей с пометкой kind=embed|chat. Предпочитает нативный Ollama /api/tags."""
    s = get_settings()
    if s.llm_provider != "openai":
        return []
    timeout = httpx.Timeout(30.0, connect=10.0)
    native = _native_base(s.llm_base_url)
    # Ollama: /api/tags даёт family — надёжнее, чем /v1/models
    try:
        async with httpx.AsyncClient(base_url=native, timeout=timeout) as client:
            r = await client.get("/api/tags")
            if r.status_code == 200:
                out = []
                for m in r.json().get("models") or []:
                    name = m.get("name") or m.get("model")
                    if not name:
                        continue
                    details = m.get("details") or {}
                    kind = classify_model(name, family=details.get("family"), families=details.get("families"))
                    out.append({"id": name, "kind": kind, "family": details.get("family")})
                if out:
                    return out
    except Exception:
        log.debug("ollama /api/tags unavailable, falling back to /v1/models", exc_info=True)

    async with httpx.AsyncClient(base_url=s.llm_base_url.rstrip("/"), headers={"Authorization": f"Bearer {s.llm_api_key}"}, timeout=timeout) as client:
        r = await client.get("/models")
        r.raise_for_status()
        data = r.json().get("data") or []
        return [{"id": m["id"], "kind": classify_model(m["id"]), "owned_by": m.get("owned_by")} for m in data if m.get("id")]


async def pull_remote_model(name: str) -> dict:
    """Скачать модель через нативный Ollama API. Для облачных провайдеров недоступно."""
    s = get_settings()
    if s.llm_provider != "openai":
        raise LLMUnavailable("Скачивание моделей доступно только при LLM_PROVIDER=openai (Ollama)")
    base = _native_base(s.llm_base_url)
    timeout = httpx.Timeout(600.0, connect=10.0)
    async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
        r = await client.post("/api/pull", json={"name": name, "stream": False})
        if r.status_code == 404:
            raise LLMUnavailable("Сервер моделей не поддерживает /api/pull (нужен Ollama)")
        r.raise_for_status()
        return r.json() if r.content else {"status": "success"}
