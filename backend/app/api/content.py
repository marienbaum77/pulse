"""Материалы, сюжеты (кластеры), черновики, публикации."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .. import db, jobs, publisher
from ..images import ImageUnavailable, get_image
from ..pipeline import drafts_service
from ..pipeline.generate import automatic_review, check_text
from ..pipeline.ingest import import_manual
from ..pipeline.process import clear_clusters
from ..pipeline.scoring import topic_passes_threshold
from ..security import audit, editor, viewer
from ..textutil import strip_source_footer

router = APIRouter(tags=["content"])


# ---------- items ----------
class ImportItem(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    text: str = ""
    url: str = ""
    source: str = "Импорт"
    authority: float | None = Field(None, ge=0, le=1)
    published_at: datetime | None = None
    hours_ago: float | None = None


class ImportBody(BaseModel):
    project_id: int
    items: list[ImportItem] = Field(max_length=5000)


@router.get("/items")
async def list_items(project_id: int, cluster_id: int | None = None, status: str | None = None, limit: int = Query(100, le=500), offset: int = 0, user=Depends(viewer)):
    where, params = ["i.project_id = %s"], [project_id]
    if cluster_id:
        where.append("i.cluster_id = %s")
        params.append(cluster_id)
    if status:
        where.append("i.status = %s")
        params.append(status)
    return await db.fetchall(
        "SELECT i.id, i.title, i.url, i.published_at, i.status, i.cluster_id, "
        "COALESCE(NULLIF(i.publisher_name, ''), s.name) AS source_name FROM items i "
        f"LEFT JOIN sources s ON s.id = i.source_id WHERE {' AND '.join(where)} ORDER BY i.published_at DESC LIMIT %s OFFSET %s",
        [*params, limit, offset],
    )


@router.post("/items/import")
async def import_items(body: ImportBody, user=Depends(editor)):
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = %s", (body.project_id,)):
        raise HTTPException(404, "Проект не найден")
    inserted = await import_manual(body.project_id, [it.model_dump() for it in body.items])
    await audit(user, "import", "items", None, {"received": len(body.items), "inserted": inserted})
    return {"received": len(body.items), "inserted": inserted}


# ---------- clusters ----------
@router.get("/clusters")
async def list_clusters(project_id: int, state: str | None = None, limit: int = Query(50, le=200), user=Depends(viewer)):
    where, params = ["c.project_id = %s"], [project_id]
    if state:
        where.append("c.state = %s")
        params.append(state)
    return await db.fetchall(
        "SELECT c.id, c.title, c.state, c.item_count, c.source_count, c.first_seen, c.last_seen, c.score, c.score_breakdown, c.published_at, "
        "(SELECT array_agg(i.published_at ORDER BY i.published_at) FROM items i WHERE i.cluster_id = c.id AND i.status = 'clustered') AS arrivals, "
        "(SELECT i.id FROM items i WHERE i.cluster_id = c.id AND i.status = 'clustered' AND i.image_url IS NOT NULL ORDER BY i.published_at DESC LIMIT 1) AS image_item_id "
        f"FROM clusters c WHERE {' AND '.join(where)} ORDER BY (c.state = 'open') DESC, "
        "COALESCE((c.score_breakdown->'topic_fit'->>'value')::float, 0.5) DESC, c.score DESC NULLS LAST, c.id DESC LIMIT %s",
        [*params, limit],
    )


@router.delete("/clusters")
async def clear_project_clusters(project_id: int, user=Depends(editor)):
    deleted, ignored_items = await clear_clusters(project_id)
    await audit(user, "clear", "clusters", None, {"project_id": project_id, "deleted": deleted, "ignored_items": ignored_items})
    return {"deleted": deleted, "ignored_items": ignored_items}


@router.get("/clusters/{cid}")
async def get_cluster(cid: int, user=Depends(viewer)):
    c = await db.fetchone(
        "SELECT id, project_id, title, state, item_count, source_count, first_seen, last_seen, score, score_breakdown, published_at, interest, interest_reason FROM clusters WHERE id = %s", (cid,)
    )
    if not c:
        raise HTTPException(404, "Сюжет не найден")
    c["items"] = await db.fetchall(
        "SELECT i.id, i.title, i.url, i.published_at, COALESCE(NULLIF(i.publisher_name, ''), s.name) AS source_name, (i.image_url IS NOT NULL) AS has_image FROM items i LEFT JOIN sources s ON s.id = i.source_id "
        "WHERE i.cluster_id = %s AND i.status = 'clustered' ORDER BY i.published_at",
        (cid,),
    )
    c["image_item_id"] = next((i["id"] for i in reversed(c["items"]) if i["has_image"]), None)
    c["drafts"] = await db.fetchall("SELECT id, title, status, created_at, (image_url IS NOT NULL) AS has_image FROM drafts WHERE %s = ANY(cluster_ids) ORDER BY id DESC", (cid,))
    return c


@router.post("/clusters/{cid}/exclude")
async def exclude_cluster(cid: int, user=Depends(editor)):
    n = await db.execute("UPDATE clusters SET state = 'excluded' WHERE id = %s AND state IN ('open','closed','published')", (cid,))
    if not n:
        raise HTTPException(409, "Сюжет нельзя исключить в текущем состоянии")
    await audit(user, "exclude", "cluster", cid)
    return {"ok": True}


@router.post("/clusters/{cid}/reopen")
async def reopen_cluster(cid: int, user=Depends(editor)):
    n = await db.execute("UPDATE clusters SET state = 'open' WHERE id = %s AND state = 'excluded'", (cid,))
    if not n:
        raise HTTPException(409, "Сюжет не был исключён")
    return {"ok": True}


@router.post("/clusters/{cid}/generate")
async def generate_from_cluster(cid: int, user=Depends(editor)):
    c = await db.fetchone("SELECT project_id, state, score_breakdown FROM clusters WHERE id = %s", (cid,))
    if not c:
        raise HTTPException(404, "Сюжет не найден")
    if c["state"] in ("drafted", "excluded"):
        raise HTTPException(409, "Для сюжета уже есть черновик или он исключён")
    project = await db.fetchone("SELECT topic_threshold FROM projects WHERE id = %s", (c["project_id"],))
    topic_fit = (c["score_breakdown"] or {}).get("topic_fit")
    if not topic_passes_threshold(topic_fit, project["topic_threshold"]):
        value = float(topic_fit.get("value", 0.5))
        raise HTTPException(409, f"Близость заголовка сюжета к теме ниже порога ({value:.2f} < {project['topic_threshold']:.2f})")
    job = await jobs.enqueue("generate", {"project_id": c["project_id"], "kind": "post", "cluster_ids": [cid], "user_id": user["id"]})
    return {"job_id": job}


# ---------- drafts ----------
class DraftPatch(BaseModel):
    title: str | None = Field(None, max_length=300)
    body: str | None = Field(None, max_length=8000)
    remove_image: bool = False
    image_item_id: int | None = None  # заменить картинку на картинку одного из источников этого черновика


class GenerateBody(BaseModel):
    project_id: int
    kind: Literal["post", "digest"] = "post"
    top_n: int = Field(1, ge=1, le=10)


@router.get("/drafts")
async def list_drafts(project_id: int, status: str | None = None, limit: int = Query(50, le=200), user=Depends(viewer)):
    where, params = ["d.project_id = %s"], [project_id]
    if status:
        where.append("d.status = %s")
        params.append(status)
    return await db.fetchall(
        "SELECT d.id, d.kind, COALESCE(NULLIF(d.title, ''), "
        "(SELECT string_agg(c.title, ' · ' ORDER BY c.score DESC NULLS LAST, c.id) FROM clusters c WHERE c.id = ANY(d.cluster_ids)), "
        "CASE WHEN d.kind = 'digest' THEN 'Сборка дайджеста' ELSE 'Подготовка поста' END) AS title, "
        "d.status, d.model, d.checks, d.error, d.created_at, d.updated_at, d.cluster_ids, (d.image_url IS NOT NULL) AS has_image FROM drafts d "
        f"WHERE {' AND '.join(where)} ORDER BY (d.status = 'pending_review') DESC, d.id DESC LIMIT %s",
        [*params, limit],
    )


@router.get("/generation-queue")
async def generation_queue(project_id: int, user=Depends(viewer)):
    return await jobs.queued_generations(project_id)


@router.delete("/drafts")
async def clear_drafts(project_id: int, user=Depends(editor)):
    deleted = await drafts_service.clear(project_id)
    await audit(user, "clear", "drafts", None, {"project_id": project_id, "deleted": deleted})
    return {"deleted": deleted}


async def _image_response(url: str | None) -> Response:
    if not url:
        raise HTTPException(404, "Картинки нет")
    try:
        data, ctype = await get_image(url)
    except ImageUnavailable:
        raise HTTPException(404, "Картинка недоступна")
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=604800", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/items/{iid}/image")
async def item_image(iid: int, user=Depends(viewer)):
    row = await db.fetchone("SELECT image_url FROM items WHERE id = %s", (iid,))
    return await _image_response(row["image_url"] if row else None)


@router.get("/drafts/{did}/image")
async def draft_image(did: int, user=Depends(viewer)):
    row = await db.fetchone("SELECT image_url FROM drafts WHERE id = %s", (did,))
    return await _image_response(row["image_url"] if row else None)


@router.get("/drafts/{did}")
async def get_draft(did: int, user=Depends(viewer)):
    d = await db.fetchone("SELECT * FROM drafts WHERE id = %s", (did,))
    if not d:
        raise HTTPException(404, "Черновик не найден")
    d["body"] = strip_source_footer(d["body"])
    d["publications"] = await db.fetchall(
        "SELECT p.id, p.status, p.error, p.sent_at, ch.name AS channel_name FROM publications p JOIN channels ch ON ch.id = p.channel_id WHERE p.draft_id = %s ORDER BY p.id", (did,)
    )
    return d


@router.patch("/drafts/{did}")
async def edit_draft(did: int, body: DraftPatch, user=Depends(editor)):
    d = await db.fetchone("SELECT status, citations, checks, params, body, title, project_id, kind, image_url, cluster_ids FROM drafts WHERE id = %s", (did,))
    if not d:
        raise HTTPException(404, "Черновик не найден")
    if d["status"] not in ("pending_review", "rejected", "failed"):
        raise HTTPException(409, "Утверждённый черновик править нельзя")
    new_body = body.body if body.body is not None else d["body"]
    new_title = body.title if body.title is not None else d["title"]
    checks = {**check_text(new_body, d["citations"]), "mode": d["params"].get("mode", "extractive"), "edited": True}
    project = await db.fetchone("SELECT publish_mode, max_length FROM projects WHERE id = %s", (d["project_id"],))
    if project["publish_mode"] in ("auto", "full_auto"):
        checks["automatic_review"] = automatic_review(
            new_title, new_body, d["citations"], checks, project["max_length"], d["kind"],
        )
    new_image = d["image_url"]
    if body.remove_image:
        new_image = None
    elif body.image_item_id is not None:
        new_image = next((c["image_url"] for c in d["citations"] if c["item_id"] == body.image_item_id and c.get("image_url")), None)
        if new_image is None:
            raise HTTPException(422, "У этого источника нет картинки")
    row = await db.fetchone(
        "UPDATE drafts SET title = COALESCE(%s, title), body = %s, checks = %s, image_url = %s, updated_at = now() WHERE id = %s RETURNING *",
        (body.title, new_body, Jsonb(checks), new_image, did),
    )
    await audit(user, "edit", "draft", did)
    return row


@router.post("/drafts/{did}/approve")
async def approve_draft(did: int, user=Depends(editor)):
    try:
        changed = await drafts_service.approve(did, user["id"])
    except drafts_service.NoChannels:
        raise HTTPException(409, "У проекта нет включённых каналов публикации — добавьте канал")
    if not changed:
        cur = await db.fetchone("SELECT status FROM drafts WHERE id = %s", (did,))
        if cur is None:
            raise HTTPException(404, "Черновик не найден")
        if cur["status"] != "approved":  # повторное нажатие «Утвердить» безопасно и не создаёт второй публикации
            raise HTTPException(409, f"Черновик в статусе «{cur['status']}», утвердить нельзя")
    await audit(user, "approve", "draft", did)
    suppressed = await db.fetchone(
        "SELECT count(*) AS n FROM publications WHERE draft_id = %s AND status = 'cancelled' "
        "AND error LIKE 'Точный повтор текста уже поставлен%%'",
        (did,),
    )
    return {"ok": True, "already": not changed, "suppressed": suppressed["n"]}


@router.post("/drafts/{did}/reject")
async def reject_draft(did: int, user=Depends(editor)):
    if not     await drafts_service.reject(did, reopen_clusters=True):
        raise HTTPException(409, "Черновик нельзя отклонить в текущем статусе")
    await audit(user, "reject", "draft", did)
    return {"ok": True}


@router.post("/drafts/{did}/regenerate")
async def regenerate_draft(did: int, user=Depends(editor)):
    d = await db.fetchone("SELECT project_id, kind, cluster_ids, status FROM drafts WHERE id = %s", (did,))
    if not d:
        raise HTTPException(404, "Черновик не найден")
    if d["status"] not in ("pending_review", "failed", "rejected"):
        raise HTTPException(409, "Этот черновик уже утверждён")
    await drafts_service.reject(did, reopen_clusters=True)
    job = await jobs.enqueue("generate", {"project_id": d["project_id"], "kind": d["kind"], "cluster_ids": d["cluster_ids"], "user_id": user["id"]})
    return {"job_id": job}


@router.post("/drafts/generate")
async def generate_now(body: GenerateBody, user=Depends(editor)):
    job = await jobs.enqueue("generate", {"project_id": body.project_id, "kind": body.kind, "top_n": body.top_n, "user_id": user["id"]})
    return {"job_id": job}


# ---------- publications ----------
class ResolveBody(BaseModel):
    action: Literal["retry", "mark_sent", "cancel"]
    external_id: str | None = None


@router.get("/publications")
async def list_publications(project_id: int, status: str | None = None, limit: int = Query(100, le=300), user=Depends(viewer)):
    where, params = ["d.project_id = %s"], [project_id]
    if status:
        where.append("p.status = %s")
        params.append(status)
    return await db.fetchall(
        "SELECT p.id, p.status, p.due_at, p.attempts, p.external_id, p.error, p.sent_at, p.draft_id, d.title AS draft_title, "
        "ch.name AS channel_name, ch.type AS channel_type FROM publications p JOIN drafts d ON d.id = p.draft_id "
        f"JOIN channels ch ON ch.id = p.channel_id WHERE {' AND '.join(where)} ORDER BY p.id DESC LIMIT %s",
        [*params, limit],
    )


@router.post("/publications/{pid}/resolve")
async def resolve_publication(pid: int, body: ResolveBody, user=Depends(editor)):
    p = await db.fetchone("SELECT p.*, d.cluster_ids, d.project_id FROM publications p JOIN drafts d ON d.id = p.draft_id WHERE p.id = %s", (pid,))
    if not p:
        raise HTTPException(404, "Публикация не найдена")
    if p["status"] in ("sent", "sending"):
        raise HTTPException(409, f"Публикация в статусе «{p['status']}», изменить нельзя")
    if body.action == "retry":
        await db.execute("UPDATE publications SET status = 'pending', attempts = 0, due_at = now(), error = NULL WHERE id = %s", (pid,))
    elif body.action == "mark_sent":
        await db.execute("UPDATE publications SET status = 'sent', sent_at = now(), external_id = %s, error = NULL WHERE id = %s", (body.external_id, pid))
        await publisher._mark_published({"cluster_ids": p["cluster_ids"]})
    else:
        await db.execute("UPDATE publications SET status = 'cancelled' WHERE id = %s", (pid,))
    await audit(user, f"resolve:{body.action}", "publication", pid)
    return {"ok": True}
