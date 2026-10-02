"""Обработка проекта: эмбеддинги → инкрементальная кластеризация → скоринг NWS."""
import logging
from datetime import timedelta

import numpy as np
from psycopg.types.json import Jsonb

from .. import db, events, jobs
from ..providers import refresh_provider
from ..textutil import utcnow
from .ingest import MIN_RSS_TEXT_CHARS
from . import scoring
from .clustering_core import Cluster, OnlineClusterer, unit_vector

log = logging.getLogger("pulse.process")
DUP_SIM = 0.985  # сходство, выше которого материал считается почти-дубликатом уже принятого
SEARCH_SIM_THRESHOLD = 0.84


class VectorIndex:
    """Растущая матрица единичных векторов для поиска ближайшего точным перебором (окно небольшое — индекс не нужен)."""

    def __init__(self):
        self.ids: list[int] = []
        self._m: np.ndarray | None = None

    def add(self, item_id: int, v: np.ndarray) -> None:
        if self._m is None:
            self._m = np.zeros((256, v.shape[0]), dtype=np.float32)
        if len(self.ids) >= self._m.shape[0]:
            self._m = np.vstack([self._m, np.zeros_like(self._m)])
        self._m[len(self.ids)] = v
        self.ids.append(item_id)

    def best(self, v: np.ndarray) -> tuple[int | None, float]:
        if not self.ids:
            return None, 0.0
        sims = self._m[: len(self.ids)] @ v
        j = int(np.argmax(sims))
        return self.ids[j], float(sims[j])


async def get_project(project_id: int) -> dict | None:
    return await db.fetchone("SELECT * FROM projects WHERE id = %s", (project_id,))


async def ensure_topic_embedding(project: dict, provider) -> None:
    if project["topic"].strip() and project["topic_embedding"] is None:
        vec = (await provider.embed([project["topic"]], project_id=project["id"]))[0]
        await db.execute("UPDATE projects SET topic_embedding = %s WHERE id = %s", (vec, project["id"]))
        project["topic_embedding"] = vec


async def ensure_embedding_space(project: dict, provider) -> bool:
    """Векторы разных моделей несравнимы (и часто разной размерности). Если модель эмбеддингов сменили — например, демо-режим на bge-m3 —
    пересчитываем векторы и пересобираем сюжеты. Материалы, черновики и публикации при этом сохраняются. True — пересчёт был."""
    pid = project["id"]
    sig = f"{provider.name}:{provider.embed_model}"
    key = f"embedding_signature:{pid}"
    row = await db.fetchone("SELECT value FROM kv WHERE key = %s", (key,))
    stored = row["value"].get("sig") if row else None
    changed = stored is not None and stored != sig
    if stored is None:
        existing = await db.fetchone("SELECT vector_dims(embedding) AS d FROM items WHERE project_id = %s AND embedding IS NOT NULL LIMIT 1", (pid,))
        if existing:  # база создана версией без записи о модели: сверяем размерность
            changed = int((await provider.embed(["ping"]))[0].shape[0]) != existing["d"]
    if changed:
        log.warning("project %s: модель эмбеддингов изменилась (%s → %s), пересчитываем векторы и сюжеты", pid, stored, sig)
        async with db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM clusters c WHERE c.project_id = %s AND NOT EXISTS (SELECT 1 FROM drafts d WHERE c.id = ANY(d.cluster_ids))", (pid,))
                await conn.execute(
                    "UPDATE clusters SET centroid = NULL, published_centroid = NULL, score = NULL, score_breakdown = NULL, "
                    "state = CASE WHEN state = 'published' THEN 'published' ELSE 'closed' END WHERE project_id = %s", (pid,))
                await conn.execute("UPDATE items SET status = 'new', embedding = NULL, cluster_id = NULL, dup_of = NULL WHERE project_id = %s", (pid,))
                await conn.execute("UPDATE projects SET topic_embedding = NULL WHERE id = %s", (pid,))
    from psycopg.types.json import Jsonb as _J
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, _J({"sig": sig})),
    )
    return changed


async def embed_new(project: dict, provider) -> int:
    total = 0
    while True:
        rows = await db.fetchall(
            "SELECT id, title, text, status FROM items WHERE project_id = %s "
            "AND (status = 'new' OR (embedding IS NOT NULL AND title_embedding IS NULL)) "
            "ORDER BY (status = 'new') DESC, id LIMIT 128",
            (project["id"],),
        )
        if not rows:
            return total
        inputs: list[str] = []
        plan: list[tuple[dict, int, int | None]] = []
        for row in rows:
            title_index = len(inputs)
            inputs.append(row["title"])
            body_index = None
            if row["status"] == "new":
                body_index = len(inputs)
                inputs.append(f"{row['title']}. {row['text'][:1200]}")
            plan.append((row, title_index, body_index))
        vecs = await provider.embed(inputs, project_id=project["id"])
        async with db.pool.connection() as conn:
            for row, title_index, body_index in plan:
                body_embedding = vecs[body_index] if body_index is not None else None
                await conn.execute(
                    "UPDATE items SET title_embedding = %s, embedding = COALESCE(%s, embedding), "
                    "status = CASE WHEN status = 'new' THEN 'embedded' ELSE status END WHERE id = %s",
                    (vecs[title_index], body_embedding, row["id"]),
                )
        total += sum(row["status"] == "new" for row, _, _ in plan)


AGG_SQL = """
UPDATE clusters c SET item_count = s.n, source_count = GREATEST(s.sc, 1), first_seen = s.f, last_seen = s.l
FROM (SELECT i.cluster_id, count(*) AS n,
             count(DISTINCT COALESCE(NULLIF(i.publisher_name, ''), s.name, 'Источник')) AS sc,
             min(i.published_at) AS f, max(i.published_at) AS l
      FROM items i LEFT JOIN sources s ON s.id = i.source_id
      WHERE i.cluster_id = ANY(%s) AND i.status = 'clustered' GROUP BY i.cluster_id) s
WHERE c.id = s.cluster_id
"""
TITLE_SQL = """
UPDATE clusters c SET title = COALESCE(
  (SELECT i.title FROM items i WHERE i.cluster_id = c.id AND i.status = 'clustered' ORDER BY i.embedding <=> c.centroid LIMIT 1), c.title)
WHERE c.id = ANY(%s)
"""


async def cluster_new(project: dict) -> dict:
    pid = project["id"]
    window = timedelta(hours=project["window_hours"])
    items = await db.fetchall(
        "SELECT i.id, i.published_at, i.embedding, "
        "EXISTS(SELECT 1 FROM sources s WHERE s.id = i.source_id AND s.url LIKE 'https://news.google.com/rss/search?%%') AS is_search_result "
        "FROM items i WHERE i.project_id = %s AND i.status = 'embedded' ORDER BY i.published_at, i.id",
        (pid,),
    )
    if not items:
        return {"clustered": 0, "duplicates": 0, "new_clusters": 0}
    since = items[0]["published_at"] - window
    rows = await db.fetchall(
        "SELECT id, centroid, item_count, first_seen, last_seen FROM clusters "
        "WHERE project_id = %s AND state <> 'closed' AND last_seen >= %s AND centroid IS NOT NULL",
        (pid, since),
    )
    clusterer = OnlineClusterer(
        project["sim_threshold"], window, [Cluster.from_db(r["id"], r["centroid"], r["item_count"], r["first_seen"], r["last_seen"]) for r in rows]
    )
    index = VectorIndex()
    for r in await db.fetchall("SELECT id, embedding FROM items WHERE project_id = %s AND status = 'clustered' AND published_at >= %s", (pid, since)):
        index.add(r["id"], unit_vector(r["embedding"]))

    assigned: list[tuple[int, Cluster]] = []
    dups: list[tuple[int, int]] = []
    for r in items:
        v = unit_vector(r["embedding"])
        near_id, near_sim = index.best(v)
        if near_id is not None and near_sim >= DUP_SIM:
            dups.append((r["id"], near_id))
            continue
        threshold = clusterer.threshold
        if r["is_search_result"]:
            clusterer.threshold = max(threshold, SEARCH_SIM_THRESHOLD)
        cluster, _ = clusterer.add(v, r["published_at"])
        clusterer.threshold = threshold
        assigned.append((r["id"], cluster))
        index.add(r["id"], v)

    touched = [c for c in clusterer.clusters if c.touched]
    new_clusters = sum(1 for c in touched if c.is_new)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for c in touched:
                if c.id is None:
                    cur = await conn.execute(
                        "INSERT INTO clusters(project_id, centroid, first_seen, last_seen, item_count) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (pid, c.centroid, c.first_seen, c.last_seen, c.n),
                    )
                    c.id = (await cur.fetchone())["id"]
                else:
                    await conn.execute("UPDATE clusters SET centroid = %s WHERE id = %s", (c.centroid, c.id))
            for item_id, c in assigned:
                await conn.execute("UPDATE items SET status = 'clustered', cluster_id = %s WHERE id = %s", (c.id, item_id))
            for item_id, dup_of in dups:
                await conn.execute("UPDATE items SET status = 'duplicate', dup_of = %s WHERE id = %s", (dup_of, item_id))
            ids = [c.id for c in touched]
            if ids:
                await conn.execute(AGG_SQL, (ids,))
                await conn.execute(TITLE_SQL, (ids,))
    return {"clustered": len(assigned), "duplicates": len(dups), "new_clusters": new_clusters}


SCORE_SQL = """
SELECT c.id, c.centroid,
  (SELECT avg(i.title_embedding) FROM items i
   WHERE i.cluster_id = c.id AND i.status = 'clustered' AND i.title_embedding IS NOT NULL) AS title_centroid,
  c.source_count, c.last_seen,
  COALESCE((SELECT avg(a) FROM (SELECT DISTINCT s.id, s.authority AS a FROM items i JOIN sources s ON s.id = i.source_id
            WHERE i.cluster_id = c.id AND i.status = 'clustered') t), 0.5) AS authority,
  (SELECT count(*) FROM items i WHERE i.cluster_id = c.id AND i.status = 'clustered'
     AND i.published_at >= now() - interval '6 hours') AS recent
FROM clusters c
WHERE c.project_id = %s AND c.state IN ('open', 'published') AND c.centroid IS NOT NULL
  AND c.last_seen >= now() - make_interval(hours => %s)
"""


async def refresh_clusters(project: dict) -> int:
    """Закрывает устаревшие кластеры и пересчитывает NWS для активных."""
    pid, hours = project["id"], project["window_hours"]
    await db.execute("UPDATE clusters SET state = 'closed' WHERE project_id = %s AND state = 'open' AND last_seen < now() - make_interval(hours => %s)", (pid, hours))
    rows = sorted(await db.fetchall(SCORE_SQL, (pid, hours)), key=lambda r: r["id"])
    now = utcnow()
    # Без настоящей модели эмбеддингов «близость к теме» не измеряется и остаётся нейтральной.
    provider = await refresh_provider()
    await ensure_topic_embedding(project, provider)
    topic_vec = None if provider.name == "stub" else project["topic_embedding"]
    neutral = frozenset({"topic_fit"}) if topic_vec is None else frozenset()
    async with db.pool.connection() as conn:
        for r in rows:
            feats = scoring.compute_features(
                centroid=r["title_centroid"] if r["title_centroid"] is not None else r["centroid"],
                source_count=r["source_count"],
                authority=float(r["authority"]),
                age_hours=(now - r["last_seen"]).total_seconds() / 3600,
                recent_items=r["recent"],
                window_hours=hours,
                topic_vec=topic_vec,
            )
            if r["title_centroid"] is None and topic_vec is not None:
                feats["topic_fit"] = 0.5
            row_neutral = neutral if r["title_centroid"] is not None else neutral | frozenset({"topic_fit"})
            score, parts = scoring.nws(feats, project["weights"], row_neutral)
            await conn.execute("UPDATE clusters SET score = %s, score_breakdown = %s WHERE id = %s", (score, Jsonb(parts), r["id"]))
    return len(rows)


async def select_clusters(project: dict, limit: int) -> list[dict]:
    """Кандидаты на публикацию: открытые кластеры с достаточным числом материалов
    и уже опубликованные, которые заметно выросли (для поста-обновления)."""
    return await db.fetchall(
        "SELECT id, title, score, item_count, source_count, state FROM clusters "
        "WHERE project_id = %s AND score IS NOT NULL AND last_seen >= now() - make_interval(hours => %s) AND ("
        "  (state = 'open' AND item_count >= %s) OR "
        "  (state = 'published' AND item_count >= COALESCE(item_count_at_publish, 0) * 1.5 AND item_count - COALESCE(item_count_at_publish, 0) >= 3)"
        ") AND (%s <= 0 OR COALESCE((score_breakdown->'topic_fit'->>'neutral')::boolean, false) "
        "OR COALESCE((score_breakdown->'topic_fit'->>'value')::float, 0.5) >= %s) "
        "ORDER BY COALESCE((score_breakdown->'topic_fit'->>'value')::float, 0.5) DESC, score DESC, id LIMIT %s",
        (project["id"], project["window_hours"], project["min_items"], project["topic_threshold"], project["topic_threshold"], limit),
    )


async def clear_clusters(project_id: int) -> tuple[int, int]:
    """Удаляет сюжеты без публикаций и черновиков, оставляя связанные материалы вне повторной обработки."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT c.id FROM clusters c WHERE c.project_id = %s AND c.state <> 'published' AND c.published_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE c.id = ANY(d.cluster_ids)) FOR UPDATE",
                (project_id,),
            )
            cluster_ids = [row["id"] for row in await cur.fetchall()]
            if not cluster_ids:
                return 0, 0
            cur = await conn.execute(
                "UPDATE items SET status = 'ignored', cluster_id = NULL, dup_of = NULL "
                "WHERE cluster_id = ANY(%s) RETURNING id",
                (cluster_ids,),
            )
            ignored_items = len(await cur.fetchall())
            await conn.execute("DELETE FROM clusters WHERE id = ANY(%s)", (cluster_ids,))
    return len(cluster_ids), ignored_items


async def process_project(project_id: int) -> dict:
    project = await get_project(project_id)
    if not project or not project["active"]:
        return {}
    async with db.advisory_lock(1, project_id) as got:
        if not got:
            raise jobs.Retry(5)
        provider = await refresh_provider()
        if await ensure_embedding_space(project, provider):
            project = await get_project(project_id)
        await ensure_topic_embedding(project, provider)
        embedded = await embed_new(project, provider)
        stats = await cluster_new(project)
        stats["embedded"] = embedded
        stats["scored"] = await refresh_clusters(project)
    log.info("project %s processed: %s", project_id, stats)
    await events.notify("pipeline", project_id=project_id, stage="processed", **stats)
    
    return stats


@jobs.handler("process_project")
async def process_project_job(payload: dict) -> None:
    project_id = int(payload["project_id"])
    pending = await db.fetchone(
        "SELECT i.source_id, i.article_content_status, i.article_content_started_at FROM items i "
        "JOIN sources s ON s.id = i.source_id WHERE i.project_id = %s AND s.type = 'rss' "
        "AND i.status = 'new' AND i.url <> '' AND char_length(btrim(i.text)) < %s "
        "AND i.article_content_status IN ('pending','running') ORDER BY i.published_at DESC LIMIT 1",
        (project_id, MIN_RSS_TEXT_CHARS),
    )
    if pending and jobs.current_age_seconds() < 900:
        source_id = int(pending["source_id"])
        if pending["article_content_status"] == "running":
            age = (utcnow() - pending["article_content_started_at"]).total_seconds() if pending["article_content_started_at"] else 0
            if age < 600:
                raise jobs.Retry(5)
            await db.execute(
                "UPDATE items SET article_content_status = 'pending', article_content_started_at = NULL "
                "WHERE source_id = %s AND status = 'new' AND article_content_status = 'running' "
                "AND article_content_started_at < now() - interval '10 minutes'",
                (source_id,),
            )
        active = await db.fetchone(
            "SELECT 1 FROM jobs WHERE kind = 'enrich_articles' AND status IN ('queued','running') "
            "AND payload->>'source_id' = %s LIMIT 1",
            (str(source_id),),
        )
        if not active:
            await jobs.enqueue(
                "enrich_articles",
                {"source_id": source_id, "project_id": project_id},
                dedupe_key=f"enrich:{project_id}:{source_id}",
            )
        raise jobs.Retry(3)
    await process_project(project_id)
