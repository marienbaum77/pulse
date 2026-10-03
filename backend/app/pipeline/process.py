"""Обработка проекта: эмбеддинги → инкрементальная кластеризация → скоринг NWS."""
import logging
import re
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


def parse_topic_aspects(topic: str) -> list[str]:
    """Разбивает строку темы на аспекты по запятым/точкам с запятой/точкам/двоеточиям.
    Нужно, чтобы «размытая» тема из многих понятий не превращалась в один усреднённый вектор."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;.:]", topic or ""):
        part = part.strip()
        key = part.casefold()
        if part and key not in seen:
            seen.add(key)
            out.append(part[:120])
    return out


def project_topic_aspects(project: dict) -> list[str]:
    """Явные аспекты темы из настроек проекта; если не заданы — авто-разбор строки темы."""
    raw = project.get("topic_aspects") or []
    if isinstance(raw, str):
        raw = [raw]
    aspects = [str(a).strip()[:120] for a in raw if str(a).strip()]
    return aspects or parse_topic_aspects(project.get("topic", ""))


async def load_aspect_vectors(project: dict, provider) -> np.ndarray | None:
    """Векторы аспектов темы; None, если аспектов нет или провайдер без настоящих эмбеддингов."""
    if provider.name == "stub":
        return None
    aspects = project_topic_aspects(project)
    if not aspects:
        return None
    return await provider.embed(aspects, project_id=project["id"])


def max_aspect_similarity(vectors: np.ndarray, aspect_vecs: np.ndarray) -> np.ndarray:
    """Для каждого вектора — максимальный косинус к аспектам темы (0..1)."""
    v = np.asarray(vectors, dtype=np.float32)
    v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
    a = np.asarray(aspect_vecs, dtype=np.float32)
    a = a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
    return np.clip(v @ a.T, 0.0, 1.0).max(axis=1)



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
                await conn.execute("UPDATE items SET status = 'new', embedding = NULL, title_embedding = NULL, topic_score = NULL, cluster_id = NULL, dup_of = NULL WHERE project_id = %s", (pid,))
                await conn.execute("UPDATE projects SET topic_embedding = NULL WHERE id = %s", (pid,))
    from psycopg.types.json import Jsonb as _J
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, _J({"sig": sig})),
    )
    return changed


async def embed_new(project: dict, provider) -> int:
    """Векторы текста и заголовка для новых материалов: без них не работает кластеризация, поэтому обрабатываются все сразу.
    Заодно считается topic_score — максимальная близость заголовка к аспектам темы."""
    total = 0
    aspect_vecs = await load_aspect_vectors(project, provider)
    while True:
        rows = await db.fetchall(
            "SELECT id, title, text FROM items WHERE project_id = %s AND status = 'new' ORDER BY id LIMIT 64",
            (project["id"],),
        )
        if not rows:
            return total
        inputs: list[str] = []
        for row in rows:
            inputs += [row["title"], f"{row['title']}. {row['text'][:1200]}"]
        vecs = await provider.embed(inputs, project_id=project["id"])
        scores = max_aspect_similarity(vecs[0::2], aspect_vecs) if aspect_vecs is not None else None
        async with db.pool.connection() as conn:
            for i, row in enumerate(rows):
                await conn.execute(
                    "UPDATE items SET title_embedding = %s, embedding = %s, status = 'embedded', topic_score = %s "
                    "WHERE id = %s AND status = 'new'",
                    (vecs[2 * i], vecs[2 * i + 1], None if scores is None else float(scores[i]), row["id"]),
                )
        total += len(rows)


# Догонка для материалов, встроенных до появления topic_score: свежие первыми — они сильнее влияют на рейтинг.
# Вектор заголовка уже есть у большинства материалов, поэтому topic_score пересчитывается без обращений к модели.
TOPIC_BACKFILL_BATCH = 128
_BACKFILL_BASE = (
    "i.project_id = %s AND i.status = 'clustered' AND i.embedding IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM clusters c WHERE c.id = i.cluster_id AND c.state IN ('open','published'))"
)
TITLE_BACKFILL_WHERE = f"{_BACKFILL_BASE} AND i.title_embedding IS NULL"
TOPIC_BACKFILL_WHERE = f"{_BACKFILL_BASE} AND (i.title_embedding IS NULL OR i.topic_score IS NULL)"


async def needs_topic_backfill(project_id: int, with_scores: bool = True) -> bool:
    where = TOPIC_BACKFILL_WHERE if with_scores else TITLE_BACKFILL_WHERE
    return await db.fetchone(f"SELECT 1 FROM items i WHERE {where} LIMIT 1", (project_id,)) is not None


async def backfill_topic_scores(project_id: int, provider, limit: int = TOPIC_BACKFILL_BATCH) -> int:
    """Одна порция догонки: досчитывает векторы заголовков и topic_score старым материалам."""
    project = await get_project(project_id)
    if not project:
        return 0
    aspect_vecs = await load_aspect_vectors(project, provider)
    where = TOPIC_BACKFILL_WHERE if aspect_vecs is not None else TITLE_BACKFILL_WHERE
    rows = await db.fetchall(
        f"SELECT i.id, i.title, i.title_embedding FROM items i WHERE {where} ORDER BY i.published_at DESC, i.id LIMIT %s",
        (project_id, limit),
    )
    if not rows:
        return 0
    missing = [row["title"] for row in rows if row["title_embedding"] is None]
    fresh = iter(await provider.embed(missing, project_id=project_id) if missing else [])
    title_vecs = np.asarray(
        [next(fresh) if row["title_embedding"] is None else row["title_embedding"] for row in rows], dtype=np.float32
    )
    scores = max_aspect_similarity(title_vecs, aspect_vecs) if aspect_vecs is not None else None
    async with db.pool.connection() as conn:
        for i, row in enumerate(rows):
            await conn.execute(
                "UPDATE items SET title_embedding = COALESCE(title_embedding, %s), topic_score = %s WHERE id = %s",
                (title_vecs[i], None if scores is None else float(scores[i]), row["id"]),
            )
    return len(rows)


async def schedule_topic_backfill(project_id: int) -> None:
    await jobs.enqueue("backfill_topic_scores", {"project_id": project_id}, dedupe_key=f"topic-backfill:{project_id}")


@jobs.handler("backfill_topic_scores")
async def backfill_topic_scores_job(payload: dict) -> None:
    project_id = int(payload["project_id"])
    project = await get_project(project_id)
    if not project or not project["active"]:
        return
    provider = await refresh_provider()
    if provider.name == "stub":
        return  # без настоящих эмбеддингов тема не измеряется — догонять нечего
    with_scores = bool(project_topic_aspects(project))
    # Отдельный ключ, а не блокировка проекта: бэкфилл не мешает process_project, но две порции не дублируют работу.
    async with db.advisory_lock(3, project_id) as got:
        if not got:
            return  # уже работает другая порция; она сама поставит продолжение
        done = await backfill_topic_scores(project_id, provider)
    if done and await needs_topic_backfill(project_id, with_scores):
        await schedule_topic_backfill(project_id)
    elif done:
        log.info("project %s: topic scores backfill finished", project_id)
        await jobs.enqueue("process_project", {"project_id": project_id}, dedupe_key=f"process:{project_id}")


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
  (SELECT avg(i.topic_score) FROM items i
   WHERE i.cluster_id = c.id AND i.status = 'clustered' AND i.topic_score IS NOT NULL) AS topic_score,
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
            # Приоритет — средний topic_score материалов сюжета (устойчив к размытому центроиду больших сюжетов).
            # Если оценок ещё нет (старые материалы, идёт догонка), остаётся прежний расчёт по центроиду заголовков.
            item_fit = r["topic_score"] is not None
            feats = scoring.compute_features(
                centroid=r["title_centroid"] if r["title_centroid"] is not None else r["centroid"],
                source_count=r["source_count"],
                authority=float(r["authority"]),
                age_hours=(now - r["last_seen"]).total_seconds() / 3600,
                recent_items=r["recent"],
                window_hours=hours,
                topic_vec=topic_vec,
                topic_fit=float(r["topic_score"]) if item_fit else None,
            )
            if item_fit:
                row_neutral = frozenset()  # оценка по материалам измерена — нейтральной подстановки нет
            else:
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
    if provider.name != "stub" and await needs_topic_backfill(project_id, bool(project_topic_aspects(project))):
        await schedule_topic_backfill(project_id)
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
