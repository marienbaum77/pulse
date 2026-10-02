"""Переходы состояний черновика. Утверждение идемпотентно: повторный вызов не создаёт вторую публикацию."""
import hashlib
import re

from .. import db, events


class NoChannels(Exception):
    pass


def _publication_fingerprint(title: str, body: str) -> str:
    normalized = re.sub(r"\s+", " ", f"{title}\n{body}").strip().lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _publication_lock_key(channel_id: int, fingerprint: str) -> int:
    value = int(hashlib.sha256(f"{channel_id}:{fingerprint}".encode("ascii")).hexdigest()[:16], 16)
    return value - 2**64 if value >= 2**63 else value


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
            draft = await (await conn.execute("SELECT title, body FROM drafts WHERE id = %s", (draft_id,))).fetchone()
            fingerprint = _publication_fingerprint(draft["title"], draft["body"])
            for ch in channels:
                lock_key = _publication_lock_key(ch["id"], fingerprint)
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                duplicate = await conn.execute(
                    "SELECT p.id FROM publications p JOIN drafts other ON other.id = p.draft_id "
                    "WHERE p.channel_id = %s AND p.status IN ('pending','sending','sent') "
                    "AND p.created_at >= now() - interval '24 hours' "
                    "AND md5(lower(regexp_replace(concat_ws(E'\\n', other.title, other.body), '\\s+', ' ', 'g'))) = %s "
                    "LIMIT 1",
                    (ch["id"], fingerprint),
                )
                duplicate_row = await duplicate.fetchone()
                if duplicate_row:
                    await conn.execute(
                        "INSERT INTO publications(draft_id, channel_id, status, error) "
                        "VALUES (%s, %s, 'cancelled', 'Точный повтор текста уже поставлен в очередь или опубликован за последние 24 часа') "
                        "ON CONFLICT (draft_id, channel_id) DO NOTHING",
                        (draft_id, ch["id"]),
                    )
                else:
                    await conn.execute(
                        "INSERT INTO publications(draft_id, channel_id) VALUES (%s, %s) "
                        "ON CONFLICT (draft_id, channel_id) DO NOTHING",
                        (draft_id, ch["id"]),
                    )
    await events.notify("draft", draft_id=draft_id, status="approved", project_id=d["project_id"])
    return True


async def reject(draft_id: int, *, reopen_clusters: bool = False) -> bool:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE drafts SET status = 'rejected', updated_at = now() WHERE id = %s "
                "AND (status IN ('pending_review','failed') OR (%s AND status = 'rejected')) "
                "RETURNING project_id, cluster_ids",
                (draft_id, reopen_clusters),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await conn.execute(
                "UPDATE clusters SET state = CASE WHEN published_at IS NOT NULL THEN 'published' WHEN %s THEN 'open' ELSE 'excluded' END "
                "WHERE id = ANY(%s) AND state IN ('drafted','excluded')",
                (reopen_clusters, row["cluster_ids"]),
            )
    await events.notify("draft", draft_id=draft_id, status="rejected", project_id=row["project_id"])
    return True
