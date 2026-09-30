"""Переходы состояний черновика. Утверждение идемпотентно: повторный вызов не создаёт вторую публикацию."""
from .. import db, events


class NoChannels(Exception):
    pass


async def clear(project_id: int) -> int:
    """Удаляет незавершённые редакторские черновики, сохраняя утверждённую историю и активную генерацию."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "DELETE FROM drafts WHERE project_id = %s AND status IN ('pending_review','rejected','failed') RETURNING cluster_ids",
                (project_id,),
            )
            rows = await cur.fetchall()
            cluster_ids = list({cluster_id for row in rows for cluster_id in row["cluster_ids"]})
            if cluster_ids:
                await conn.execute(
                    "UPDATE clusters c SET state = CASE WHEN c.published_at IS NOT NULL THEN 'published' ELSE 'open' END "
                    "WHERE c.id = ANY(%s) AND c.state = 'drafted' AND NOT EXISTS ("
                    "SELECT 1 FROM drafts d WHERE c.id = ANY(d.cluster_ids) AND d.status IN ('generating','pending_review','approved'))",
                    (cluster_ids,),
                )
    return len(rows)


async def approve(draft_id: int, user_id: int | None) -> bool:
    """True — черновик утверждён сейчас; False — он уже не в статусе pending_review."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT project_id, status FROM drafts WHERE id = %s FOR UPDATE", (draft_id,))
            d = await cur.fetchone()
            if d is None or d["status"] != "pending_review":
                return False
            cur = await conn.execute("SELECT id FROM channels WHERE project_id = %s AND enabled", (d["project_id"],))
            channels = await cur.fetchall()
            if not channels:
                raise NoChannels()
            await conn.execute("UPDATE drafts SET status = 'approved', approved_by = %s, updated_at = now() WHERE id = %s", (user_id, draft_id))
            for ch in channels:
                await conn.execute("INSERT INTO publications(draft_id, channel_id) VALUES (%s, %s) ON CONFLICT (draft_id, channel_id) DO NOTHING", (draft_id, ch["id"]))
    await events.notify("draft", draft_id=draft_id, status="approved", project_id=d["project_id"])
    return True


async def reject(draft_id: int) -> bool:
    row = await db.fetchone(
        "UPDATE drafts SET status = 'rejected', updated_at = now() WHERE id = %s AND status IN ('pending_review','failed') RETURNING project_id, cluster_ids",
        (draft_id,),
    )
    if not row:
        return False
    # сюжеты возвращаются в работу; опубликованные ранее остаются опубликованными
    await db.execute(
        "UPDATE clusters SET state = CASE WHEN published_at IS NOT NULL THEN 'published' ELSE 'open' END WHERE id = ANY(%s) AND state = 'drafted'",
        (row["cluster_ids"],),
    )
    await events.notify("draft", draft_id=draft_id, status="rejected", project_id=row["project_id"])
    return True
