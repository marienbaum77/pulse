"""Статистика своих постов и вес источников по ней."""
from fastapi import APIRouter, Depends, Query

from .. import db, jobs
from ..pipeline import stats
from ..security import audit, editor, viewer

router = APIRouter(tags=["stats"])


@router.get("/stats")
async def project_stats(project_id: int, days: int = Query(30, ge=1, le=stats.TRACK_DAYS), user=Depends(viewer)):
    posts = await db.fetchall(
        "SELECT p.id, p.draft_id, d.title, ch.name AS channel_name, ch.config->>'chat_id' AS chat_id, p.external_id, p.sent_at, "
        "p.views, p.reactions, p.stats_at, d.reply_to_draft_id, "
        "ARRAY(SELECT DISTINCT s.name FROM items i JOIN sources s ON s.id = i.source_id WHERE i.cluster_id = ANY(d.cluster_ids) "
        "AND i.status IN ('clustered', 'duplicate') ORDER BY s.name) AS sources "
        "FROM publications p JOIN drafts d ON d.id = p.draft_id JOIN channels ch ON ch.id = p.channel_id "
        "WHERE d.project_id = %s AND p.status = 'sent' AND p.sent_at > now() - make_interval(days => %s) ORDER BY p.sent_at DESC",
        (project_id, days),
    )
    relative = {p["id"]: round(p["relative"], 2) for p in await stats.settled_posts(project_id)}
    for p in posts:
        p["relative"] = relative.get(p["id"])  # None — пост моложе 48 часов или просмотров ещё нет
        name = stats.public_name(p.pop("chat_id") or "")
        p["url"] = f"https://t.me/{name}/{p['external_id']}" if name and (p["external_id"] or "").isdigit() else None
    sources = await db.fetchall(
        "SELECT id, name, type, authority, performance, performance_posts, LEAST(1, authority * COALESCE(performance, 1)) AS effective_authority "
        "FROM sources WHERE project_id = %s AND performance_posts > 0 ORDER BY performance DESC NULLS LAST, performance_posts DESC",
        (project_id,),
    )
    collected = await db.fetchone(
        "SELECT max(kv.updated_at) AS at FROM kv JOIN channels ch ON kv.key = 'stats_at:' || ch.id WHERE ch.project_id = %s", (project_id,)
    )
    return {"posts": posts, "sources": sources, "collected_at": collected["at"], "settled_hours": stats.SETTLED_HOURS}


@router.post("/stats/refresh")
async def refresh_stats(project_id: int, user=Depends(editor)):
    """Собрать просмотры сейчас, не дожидаясь планового сбора раз в три часа."""
    channels = await db.fetchall("SELECT id FROM channels WHERE project_id = %s AND type = 'telegram' AND enabled", (project_id,))
    for ch in channels:
        await jobs.enqueue("collect_channel_stats", {"channel_id": ch["id"]}, dedupe_key=f"stats:{ch['id']}")
    await audit(user, "refresh", "stats", None, {"project_id": project_id})
    return {"queued": len(channels)}
