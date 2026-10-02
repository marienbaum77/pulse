import asyncio
import json
import re
from pathlib import Path

from psycopg.types.json import Jsonb

from app import db
from app.pipeline.scoring import DEFAULT_WEIGHTS

DATA = Path(__file__).resolve().parents[2] / "sample_data" / "demo_news.jsonl"


def load_demo() -> list[dict]:
    return [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]


async def make_project(threshold: float = 0.35, channel: bool = True, **over) -> int:
    row = await db.fetchone(
        "INSERT INTO projects(name, topic, sim_threshold, weights, min_items, generation_mode, publish_mode, auto_retry_unknown) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            over.get("name", "Тест"),
            "Технологии: базы данных, облака, искусственный интеллект",
            threshold,
            Jsonb(DEFAULT_WEIGHTS),
            over.get("min_items", 2),
            over.get("generation_mode", "llm"),
            over.get("publish_mode", "review"),
            over.get("auto_retry_unknown", False),
        ),
    )
    if channel:
        await db.execute("INSERT INTO channels(project_id, type, name) VALUES (%s, 'console', 'Консоль')", (row["id"],))
    return row["id"]


class MiniServer:
    """Крошечный HTTP-сервер для тестов: handler(method, path, headers, body) -> (status, headers, body) | None (=оборвать соединение)."""

    def __init__(self, handler):
        self.handler = handler
        self.server = None
        self.port = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _serve(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:] if ": " in l)}
            body = await reader.readexactly(int(headers.get("content-length", 0))) if headers.get("content-length") else b""
            res = self.handler(method, path, headers, body)
            if res is None:
                writer.close()
                return
            status, extra, payload = res
            payload = payload if isinstance(payload, bytes) else payload.encode()
            hdr = "".join(f"{k}: {v}\r\n" for k, v in extra.items())
            writer.write(f"HTTP/1.1 {status} X\r\n{hdr}Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        finally:
            try:
                writer.close()
            except Exception:
                pass
