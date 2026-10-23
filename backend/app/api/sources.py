"""Источники: RSS, поиск Google News, OPML и коннекторы (YouTube, веб-поиск, сайт без RSS, Reddit, Telegram)."""
import xml.etree.ElementTree as ET
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db, jobs
from ..netguard import check_url
from ..pipeline import connectors
from ..pipeline.ingest import fetch_feed, google_news_search_url
from ..security import audit, editor, viewer
from ..textutil import item_hash, utcnow

router = APIRouter(tags=["sources"])

# ---------- sources ----------
class SourceBody(BaseModel):
    project_id: int
    type: Literal["rss"] = "rss"
    name: str = Field(min_length=1, max_length=120)
    url: str = Field("", max_length=1000)
    authority: float = Field(0.5, ge=0, le=1)
    poll_minutes: int = Field(30, ge=5, le=1440)
    enabled: bool = True


class PreviewBody(BaseModel):
    url: str


class SearchSourceBody(BaseModel):
    project_id: int
    query: str = Field(min_length=2, max_length=300)
    authority: float = Field(0.5, ge=0, le=1)
    poll_minutes: int = Field(30, ge=5, le=1440)
    enabled: bool = True


class SearchPreviewBody(BaseModel):
    project_id: int
    query: str = Field(min_length=2, max_length=300)


ConnectorType = Literal["youtube", "web_search", "site", "reddit", "telegram"]


class ConnectorOptions(BaseModel):
    time_range: Literal["day", "week", "month", "year"] | None = None
    category: Literal["general", "news", "videos", "science", "it"] | None = None
    sort: Literal["hot", "top", "new"] | None = None
    period: Literal["day", "week", "month", "year"] | None = None
    mode: Literal["news", "curated"] | None = None  # действует только в смешанном профиле проекта


class ConnectorPreviewBody(BaseModel):
    project_id: int
    type: ConnectorType
    value: str = Field(min_length=2, max_length=500)
    options: ConnectorOptions = Field(default_factory=ConnectorOptions)


class ConnectorBody(ConnectorPreviewBody):
    name: str | None = Field(None, max_length=120)
    authority: float = Field(0.5, ge=0, le=1)
    poll_minutes: int = Field(360, ge=5, le=1440)
    enabled: bool = True


async def _known_hashes(project_id: int, entries: list[dict]) -> tuple[list[str], set[str]]:
    """Хеши записей и те из них, что проект уже видел (в материалах или в памяти curated-контента)."""
    hashes = [item_hash(entry.get("url", ""), entry["title"], entry.get("text", "")) for entry in entries]
    if not hashes:
        return hashes, set()
    rows = await db.fetchall(
        "SELECT url_hash FROM items WHERE project_id = %s AND url_hash = ANY(%s) "
        "UNION SELECT url_hash FROM content_seen WHERE project_id = %s AND url_hash = ANY(%s)",
        (project_id, hashes, project_id, hashes),
    )
    return hashes, {row["url_hash"] for row in rows}


@router.get("/sources")
async def list_sources(project_id: int, user=Depends(viewer)):
    return await db.fetchall(
        "SELECT s.id, s.project_id, s.type, s.name, s.url, s.config, s.authority, s.poll_minutes, s.enabled, s.last_fetched_at, s.last_ok_at, s.last_error, "
        "s.last_entry_count, s.last_new_count, "
        "(SELECT count(*) FROM items i WHERE i.source_id = s.id) AS item_count "
        "FROM sources s WHERE s.project_id = %s ORDER BY s.id",
        (project_id,),
    )


@router.post("/sources/search", status_code=201)
async def create_search_source(body: SearchSourceBody, user=Depends(editor)):
    project = await db.fetchone("SELECT language FROM projects WHERE id = %s", (body.project_id,))
    if not project:
        raise HTTPException(404, "Проект не найден")
    url = google_news_search_url(body.query, project["language"])
    try:
        await check_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = f"Поиск статей: {body.query}".strip()[:120]
    row = await db.fetchone(
        "INSERT INTO sources(project_id, type, name, url, authority, poll_minutes, enabled) "
        "VALUES (%s,'rss',%s,%s,%s,%s,%s) RETURNING id",
        (body.project_id, name, url, body.authority, body.poll_minutes, body.enabled),
    )
    await jobs.enqueue("ingest_source", {"source_id": row["id"]}, dedupe_key=f"ingest:{row['id']}")
    await audit(user, "create", "source", row["id"], {"kind": "google_news_search", "query": body.query})
    return row


@router.post("/sources/search/preview")
async def preview_search(body: SearchPreviewBody, user=Depends(editor)):
    project = await db.fetchone("SELECT language, window_hours FROM projects WHERE id = %s", (body.project_id,))
    if not project:
        raise HTTPException(404, "Проект не найден")
    url = google_news_search_url(body.query, project["language"])
    try:
        await check_url(url)
        entries, _ = await fetch_feed(url)
    except Exception as e:
        raise HTTPException(400, f"Не удалось выполнить поиск: {e}")
    hashes = [item_hash(entry.get("url", ""), entry["title"], entry.get("text", "")) for entry in entries]
    known_hashes: set[str] = set()
    if hashes:
        rows = await db.fetchall(
            "SELECT url_hash FROM items WHERE project_id = %s AND url_hash = ANY(%s)",
            (body.project_id, hashes),
        )
        known_hashes = {row["url_hash"] for row in rows}
    floor = utcnow() - timedelta(hours=project["window_hours"])
    return {
        "count": len(entries),
        "new_count": sum(
            1 for entry, h in zip(entries, hashes)
            if entry["published_at"] >= floor and h not in known_hashes
        ),
        "sample": [
            {
                "title": entry["title"],
                "publisher": entry.get("publisher_name"),
                "published_at": entry["published_at"],
            }
            for entry in entries[:8]
        ],
    }


@router.post("/sources/connector/preview")
async def preview_connector(body: ConnectorPreviewBody, user=Depends(editor)):
    """Пробный сбор без сохранения: показывает, что будет приходить из источника."""
    project = await db.fetchone("SELECT language, window_hours, content_profile FROM projects WHERE id = %s", (body.project_id,))
    if not project:
        raise HTTPException(404, "Проект не найден")
    try:
        prepared = await connectors.prepare(body.type, body.value, body.options.model_dump())
        entries, _ = await connectors.fetch({"type": body.type, **prepared, "language": project["language"]})
    except Exception as e:
        raise HTTPException(400, f"Не удалось получить материалы: {e}")
    hashes, known = await _known_hashes(body.project_id, entries)
    mode = connectors.item_mode(project["content_profile"], body.type, prepared["config"])
    floor = utcnow() - timedelta(hours=project["window_hours"])
    return {
        "name": prepared["name"],
        "url": prepared["url"],
        "mode": mode,
        "feed_url": prepared["config"].get("feed_url"),
        "count": len(entries),
        "new_count": sum(1 for entry, h in zip(entries, hashes) if h not in known and (mode == "curated" or entry["published_at"] >= floor)),
        "sample": [
            {"title": entry["title"], "publisher": entry.get("publisher_name"), "kind": entry.get("kind") or "article", "url": entry.get("url", "")}
            for entry in entries[:8]
        ],
    }


@router.post("/sources/connector", status_code=201)
async def create_connector_source(body: ConnectorBody, user=Depends(editor)):
    return await add_connector_source(
        user, body.project_id, body.type, body.value, body.options.model_dump(),
        name=body.name, authority=body.authority, poll_minutes=body.poll_minutes, enabled=body.enabled,
    )


async def add_connector_source(
    user, project_id: int, source_type: str, value: str, options: dict, *,
    name: str | None = None, authority: float = 0.5, poll_minutes: int = 360, enabled: bool = True,
) -> dict:
    """Создаёт источник-коннектор и запускает первый сбор; общий путь для формы источника и автоподбора."""
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = %s", (project_id,)):
        raise HTTPException(404, "Проект не найден")
    try:
        prepared = await connectors.prepare(source_type, value, options)
    except Exception as e:
        raise HTTPException(400, str(e))
    duplicate = await db.fetchone(
        "SELECT name FROM sources WHERE project_id = %s AND type = %s AND (config = %s OR (url <> '' AND url = %s))",
        (project_id, source_type, Jsonb(prepared["config"]), prepared["url"]),
    )
    if duplicate:
        raise HTTPException(409, f"Такой источник уже подключён: «{duplicate['name']}»")
    row = await db.fetchone(
        "INSERT INTO sources(project_id, type, name, url, config, authority, poll_minutes, enabled) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (project_id, source_type, (name or "").strip() or prepared["name"], prepared["url"], Jsonb(prepared["config"]),
         authority, poll_minutes, enabled),
    )
    await jobs.enqueue("ingest_source", {"source_id": row["id"]}, dedupe_key=f"ingest:{row['id']}")
    await audit(user, "create", "source", row["id"], {"type": source_type, "value": value})
    return row


@router.post("/sources/preview")
async def preview_source(body: PreviewBody, user=Depends(editor)):
    try:
        entries, _ = await fetch_feed(body.url)
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать ленту: {e}")
    return {"count": len(entries), "sample": [e["title"] for e in entries[:5]]}


@router.post("/sources", status_code=201)
async def create_source(body: SourceBody, user=Depends(editor)):
    if len(body.url) < 8:
        raise HTTPException(400, "Укажите адрес ленты")
    try:
        await check_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    row = await db.fetchone(
        "INSERT INTO sources(project_id, type, name, url, authority, poll_minutes, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (body.project_id, body.type, body.name, body.url, body.authority, body.poll_minutes, body.enabled),
    )
    await jobs.enqueue("ingest_source", {"source_id": row["id"]}, dedupe_key=f"ingest:{row['id']}")
    await audit(user, "create", "source", row["id"], {"url": body.url})
    return row


@router.put("/sources/{sid}")
async def update_source(sid: int, body: SourceBody, user=Depends(editor)):
    current = await db.fetchone("SELECT type FROM sources WHERE id = %s", (sid,))
    if not current:
        raise HTTPException(404, "Источник не найден")
    if current["type"] != "rss":
        # Адрес коннектора вычислен из его настроек (ID канала, запрос), поэтому меняются только общие параметры.
        await db.execute(
            "UPDATE sources SET name=%s, authority=%s, poll_minutes=%s, enabled=%s WHERE id = %s",
            (body.name, body.authority, body.poll_minutes, body.enabled, sid),
        )
        await audit(user, "update", "source", sid)
        return {"id": sid}
    try:
        await check_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    row = await db.fetchone(
        "UPDATE sources SET name=%s, url=%s, authority=%s, poll_minutes=%s, enabled=%s, "
        "etag = CASE WHEN url = %s THEN etag END, last_modified = CASE WHEN url = %s THEN last_modified END WHERE id = %s RETURNING id",
        (body.name, body.url, body.authority, body.poll_minutes, body.enabled, body.url, body.url, sid),
    )
    if not row:
        raise HTTPException(404, "Источник не найден")
    await audit(user, "update", "source", sid)
    return row


@router.delete("/sources/{sid}", status_code=204)
async def delete_source(sid: int, user=Depends(editor)):
    await db.execute("DELETE FROM sources WHERE id = %s", (sid,))
    await audit(user, "delete", "source", sid)


@router.post("/sources/{sid}/fetch")
async def fetch_source(sid: int, user=Depends(editor)):
    if not await db.fetchone("SELECT 1 FROM sources WHERE id = %s", (sid,)):
        raise HTTPException(404, "Источник не найден")
    key = f"ingest:{sid}"
    job = await jobs.expedite_queued(key)
    if job is None:
        job = await jobs.enqueue("ingest_source", {"source_id": sid}, dedupe_key=key)
    if job is None:
        job = await jobs.expedite_queued(key)
    return {"queued": job is not None}


def parse_opml(content: str) -> list[dict]:
    """Достаёт из OPML пары name/url для каждого outline с xmlUrl."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise ValueError(f"Некорректный OPML/XML: {e}") from e
    feeds: list[dict] = []
    seen: set[str] = set()
    for outline in root.iter():
        tag = outline.tag.split("}")[-1] if "}" in outline.tag else outline.tag
        if tag.lower() != "outline":
            continue
        attrs = {k.lower(): v for k, v in outline.attrib.items()}
        url = (attrs.get("xmlurl") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        name = (attrs.get("title") or attrs.get("text") or url).strip()[:120] or url[:120]
        feeds.append({"name": name, "url": url})
    return feeds


@router.post("/sources/opml")
async def import_opml(
    project_id: int = Form(...),
    authority: float = Form(0.5),
    poll_minutes: int = Form(30),
    enabled: bool = Form(True),
    file: UploadFile = File(...),
    user=Depends(editor),
):
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = %s", (project_id,)):
        raise HTTPException(404, "Проект не найден")
    raw = (await file.read()).decode("utf-8", errors="replace")
    try:
        feeds = parse_opml(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not feeds:
        raise HTTPException(400, "В файле нет лент с атрибутом xmlUrl")
    authority = max(0.0, min(1.0, authority))
    poll_minutes = max(5, min(1440, poll_minutes))
    created, skipped, errors = [], [], []
    for feed in feeds:
        existing = await db.fetchone(
            "SELECT id FROM sources WHERE project_id = %s AND type = 'rss' AND url = %s",
            (project_id, feed["url"]),
        )
        if existing:
            skipped.append(feed["url"])
            continue
        try:
            await check_url(feed["url"])
        except ValueError as e:
            errors.append({"url": feed["url"], "error": str(e)})
            continue
        row = await db.fetchone(
            "INSERT INTO sources(project_id, type, name, url, authority, poll_minutes, enabled) VALUES (%s,'rss',%s,%s,%s,%s,%s) RETURNING id",
            (project_id, feed["name"], feed["url"], authority, poll_minutes, enabled),
        )
        await jobs.enqueue("ingest_source", {"source_id": row["id"]}, dedupe_key=f"ingest:{row['id']}")
        created.append({"id": row["id"], "name": feed["name"], "url": feed["url"]})
    await audit(user, "import", "sources", None, {"opml": True, "created": len(created), "skipped": len(skipped), "errors": len(errors)})
    return {"created": created, "skipped": skipped, "errors": errors}

