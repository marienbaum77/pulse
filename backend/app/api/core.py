"""Проекты, источники, каналы, расписания."""
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from .. import db, jobs, publisher
from ..netguard import check_url
from ..pipeline.ingest import fetch_feed
from ..pipeline.scoring import COMPONENTS, DEFAULT_WEIGHTS
from ..scheduler import next_fire
from ..security import audit, editor, viewer
from ..textutil import utcnow

router = APIRouter(tags=["core"])

PROJECT_COLS = (
    "id, name, topic, language, tone, max_length, prompt_template, prompt_version, generation_mode, publish_mode, window_hours, "
    "sim_threshold, topic_threshold, weights, context_items, context_sentences, min_items, auto_retry_unknown, show_sources, active, created_at"
)


# ---------- projects ----------
class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    topic: str = Field("", max_length=1000)
    language: str = Field("ru", max_length=10)
    tone: str = Field("нейтральный, информативный", max_length=200)
    max_length: int = Field(900, ge=200, le=3500)
    prompt_template: str = Field("", max_length=6000)
    generation_mode: Literal["llm", "extractive", "auto"] = "extractive"
    publish_mode: Literal["review", "auto", "full_auto"] = "review"
    window_hours: int = Field(48, ge=1, le=720)
    sim_threshold: float = Field(0.72, ge=0.1, le=0.99)
    topic_threshold: float = Field(0.40, ge=0, le=1)
    weights: dict[str, float] | None = None
    context_items: int = Field(6, ge=1, le=15)
    context_sentences: int = Field(3, ge=1, le=8)
    min_items: int = Field(2, ge=1, le=50)
    auto_retry_unknown: bool = False
    show_sources: bool = True
    active: bool = True

    @field_validator("weights")
    @classmethod
    def _weights(cls, v):
        if v is None:
            return v
        bad = set(v) - set(COMPONENTS)
        if bad:
            raise ValueError(f"Неизвестные веса: {', '.join(sorted(bad))}")
        if any(not (0 <= x <= 1) for x in v.values()):
            raise ValueError("Веса должны быть в диапазоне 0..1")
        return v


@router.get("/projects")
async def list_projects(user=Depends(viewer)):
    return await db.fetchall(f"SELECT {PROJECT_COLS} FROM projects ORDER BY id")


@router.get("/projects/{pid}")
async def get_project(pid: int, user=Depends(viewer)):
    row = await db.fetchone(f"SELECT {PROJECT_COLS} FROM projects WHERE id = %s", (pid,))
    if not row:
        raise HTTPException(404, "Проект не найден")
    return row


@router.post("/projects", status_code=201)
async def create_project(body: ProjectBody, user=Depends(editor)):
    d = body.model_dump()
    d["weights"] = Jsonb({**DEFAULT_WEIGHTS, **(d["weights"] or {})})
    cols = list(d)
    row = await db.fetchone(
        f"INSERT INTO projects({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING {PROJECT_COLS}", [d[c] for c in cols]
    )
    await audit(user, "create", "project", row["id"], {"name": body.name})
    return row


@router.put("/projects/{pid}")
async def update_project(pid: int, body: ProjectBody, user=Depends(editor)):
    old = await db.fetchone("SELECT topic, prompt_template, prompt_version FROM projects WHERE id = %s", (pid,))
    if not old:
        raise HTTPException(404, "Проект не найден")
    d = body.model_dump()
    d["weights"] = Jsonb({**DEFAULT_WEIGHTS, **(d["weights"] or {})})
    d["prompt_version"] = old["prompt_version"] + (1 if body.prompt_template != old["prompt_template"] else 0)
    sets = [f"{k} = %s" for k in d]
    if body.topic != old["topic"]:
        sets.append("topic_embedding = NULL")
    row = await db.fetchone(f"UPDATE projects SET {', '.join(sets)} WHERE id = %s RETURNING {PROJECT_COLS}", [*d.values(), pid])
    await audit(user, "update", "project", pid)
    return row


@router.delete("/projects/{pid}", status_code=204)
async def delete_project(pid: int, user=Depends(editor)):
    await db.execute("DELETE FROM projects WHERE id = %s", (pid,))
    await audit(user, "delete", "project", pid)


# ---------- sources ----------
class SourceBody(BaseModel):
    project_id: int
    type: Literal["rss"] = "rss"
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=1000)
    authority: float = Field(0.5, ge=0, le=1)
    poll_minutes: int = Field(30, ge=5, le=1440)
    enabled: bool = True


class PreviewBody(BaseModel):
    url: str


@router.get("/sources")
async def list_sources(project_id: int, user=Depends(viewer)):
    return await db.fetchall(
        "SELECT s.id, s.project_id, s.type, s.name, s.url, s.authority, s.poll_minutes, s.enabled, s.last_fetched_at, s.last_ok_at, s.last_error, "
        "(SELECT count(*) FROM items i WHERE i.source_id = s.id) AS item_count "
        "FROM sources s WHERE s.project_id = %s ORDER BY s.id",
        (project_id,),
    )


@router.post("/sources/preview")
async def preview_source(body: PreviewBody, user=Depends(editor)):
    try:
        entries, _ = await fetch_feed(body.url)
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать ленту: {e}")
    return {"count": len(entries), "sample": [e["title"] for e in entries[:5]]}


@router.post("/sources", status_code=201)
async def create_source(body: SourceBody, user=Depends(editor)):
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
    job = await jobs.enqueue("ingest_source", {"source_id": sid}, dedupe_key=f"ingest:{sid}")
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


# ---------- channels ----------
class ChannelBody(BaseModel):
    project_id: int
    type: Literal["telegram", "webhook", "console"]
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = {}
    enabled: bool = True


def _validate_channel(body: ChannelBody) -> dict:
    c = dict(body.config)
    if body.type == "telegram":
        if not str(c.get("chat_id", "")).strip():
            raise HTTPException(422, "Для Telegram укажите chat_id (например, @mychannel или -1001234567890)")
        if c.get("token_env") and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", c["token_env"]):
            raise HTTPException(422, "token_env — имя переменной окружения, например TELEGRAM_BOT_TOKEN")
    if body.type == "webhook" and not str(c.get("url", "")).startswith(("http://", "https://")):
        raise HTTPException(422, "Для webhook укажите url, начинающийся с http:// или https://")
    return c


def _public_channel(row: dict) -> dict:
    cfg = dict(row["config"])
    row["has_secret"] = bool(cfg.pop("secret", None))
    row["config"] = cfg
    return row


@router.get("/channels")
async def list_channels(project_id: int, user=Depends(viewer)):
    rows = await db.fetchall("SELECT * FROM channels WHERE project_id = %s ORDER BY id", (project_id,))
    return [_public_channel(r) for r in rows]


@router.post("/channels", status_code=201)
async def create_channel(body: ChannelBody, user=Depends(editor)):
    cfg = _validate_channel(body)
    row = await db.fetchone(
        "INSERT INTO channels(project_id, type, name, config, enabled) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (body.project_id, body.type, body.name, Jsonb(cfg), body.enabled),
    )
    await audit(user, "create", "channel", row["id"], {"type": body.type})
    return _public_channel(row)


@router.put("/channels/{cid}")
async def update_channel(cid: int, body: ChannelBody, user=Depends(editor)):
    old = await db.fetchone("SELECT config FROM channels WHERE id = %s", (cid,))
    if not old:
        raise HTTPException(404, "Канал не найден")
    cfg = _validate_channel(body)
    if not cfg.get("secret") and old["config"].get("secret"):  # пустое поле секрета = «не менять»
        cfg["secret"] = old["config"]["secret"]
    row = await db.fetchone(
        "UPDATE channels SET name=%s, config=%s, enabled=%s WHERE id=%s RETURNING *", (body.name, Jsonb(cfg), body.enabled, cid)
    )
    await audit(user, "update", "channel", cid)
    return _public_channel(row)


@router.delete("/channels/{cid}", status_code=204)
async def delete_channel(cid: int, user=Depends(editor)):
    await db.execute("DELETE FROM channels WHERE id = %s", (cid,))
    await audit(user, "delete", "channel", cid)


@router.post("/channels/{cid}/test")
async def test_channel(cid: int, user=Depends(editor)):
    """Проверка доступа. В канал НИЧЕГО не публикуется: для Telegram запрашиваются данные бота и его права в чате."""
    ch = await db.fetchone("SELECT * FROM channels WHERE id = %s", (cid,))
    if not ch:
        raise HTTPException(404, "Канал не найден")
    return await publisher.check_channel(ch)


# ---------- schedules ----------
class ScheduleBody(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=120)
    cron: str
    tz: str = "UTC"
    kind: Literal["post", "digest"] = "digest"
    top_n: int = Field(5, ge=1, le=10)
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, v):
        if not croniter.is_valid(v) or len(v.split()) != 5:
            raise ValueError("Cron-выражение из 5 полей, например: 0 9 * * *")
        return v

    @field_validator("tz")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Неизвестный часовой пояс, пример: Europe/Moscow")
        return v


def _with_next(row: dict) -> dict:
    row["next_fire"] = next_fire(row["cron"], row["tz"], utcnow()) if row["enabled"] else None
    return row


@router.get("/schedules")
async def list_schedules(project_id: int, user=Depends(viewer)):
    rows = await db.fetchall("SELECT * FROM schedules WHERE project_id = %s ORDER BY id", (project_id,))
    return [_with_next(r) for r in rows]


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleBody, user=Depends(editor)):
    d = body.model_dump()
    row = await db.fetchone(
        f"INSERT INTO schedules({', '.join(d)}) VALUES ({', '.join(['%s'] * len(d))}) RETURNING *", list(d.values())
    )
    await audit(user, "create", "schedule", row["id"])
    return _with_next(row)


@router.put("/schedules/{sid}")
async def update_schedule(sid: int, body: ScheduleBody, user=Depends(editor)):
    d = body.model_dump()
    d.pop("project_id")
    row = await db.fetchone(f"UPDATE schedules SET {', '.join(f'{k} = %s' for k in d)} WHERE id = %s RETURNING *", [*d.values(), sid])
    if not row:
        raise HTTPException(404, "Расписание не найдено")
    await audit(user, "update", "schedule", sid)
    return _with_next(row)


@router.delete("/schedules/{sid}", status_code=204)
async def delete_schedule(sid: int, user=Depends(editor)):
    await db.execute("DELETE FROM schedules WHERE id = %s", (sid,))
    await audit(user, "delete", "schedule", sid)


@router.post("/schedules/{sid}/run")
async def run_schedule(sid: int, user=Depends(editor)):
    s = await db.fetchone("SELECT * FROM schedules WHERE id = %s", (sid,))
    if not s:
        raise HTTPException(404, "Расписание не найдено")
    # Ручной запуск расписания всегда создаёт черновик на проверку.
    job = await jobs.enqueue(
        "generate",
        {
            "project_id": s["project_id"],
            "kind": s["kind"],
            "top_n": s["top_n"],
            "user_id": user["id"],
            "force_review": True,
        },
    )
    return {"job_id": job}
