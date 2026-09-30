"""Сбор материалов: RSS/Atom (и ручной импорт). Каждый коннектор возвращает список RawItem."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import httpx
import numpy as np
import trafilatura

from .. import db, events, jobs
from ..images import normalize_image_url, feed_entry_image
from ..netguard import check_url
from ..providers import refresh_provider
from ..textutil import item_hash, strip_html, utcnow
from .clustering_core import unit_vector

log = logging.getLogger("pulse.ingest")
MAX_ENTRIES = 100
MAX_TEXT = 20000
MIN_RSS_TEXT_CHARS = 800
MIN_ARTICLE_TEXT_CHARS = 600
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
ARTICLE_BATCH_SIZE = 20
ARTICLE_FETCH_CONCURRENCY = 4
# Материалы сюжетов в этих состояниях можно дообогащать; так же условие читает и генерация, чтобы они не расходились.
ENRICHABLE_CLUSTER_STATES = "('open','drafted','published','closed')"
_READ_MORE = re.compile(r"\b(?:читать\s+(?:далее|дальше|полностью)|read\s+more|continue\s+reading)\b.*$", re.I)


def _strip_read_more_teaser(text: str) -> str:
    return _READ_MORE.sub("", text).strip()


async def _get(url: str, headers: dict) -> httpx.Response:
    """GET с ручной обработкой редиректов: каждый адрес проверяется на SSRF."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, headers=headers) as client:
        for _ in range(4):
            await check_url(url)
            r = await client.get(url)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = str(httpx.URL(url).join(r.headers["location"]))
                continue
            return r
    raise ValueError("Слишком много редиректов")


def _entry_time(e) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(mktime(t), tz=timezone.utc)
    return utcnow()


def parse_feed(content: bytes) -> list[dict]:
    parsed = feedparser.parse(content)
    out = []
    for e in parsed.entries[:MAX_ENTRIES]:
        title = strip_html(e.get("title", ""))
        if not title:
            continue
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        body = body or e.get("summary", "") or e.get("description", "")
        out.append({
            "title": title, "url": e.get("link", ""), "text": _strip_read_more_teaser(strip_html(body))[:MAX_TEXT],
            "published_at": _entry_time(e), "image_url": feed_entry_image(e, body),
        })
    return out


async def fetch_feed(url: str, etag: str | None = None, last_modified: str | None = None) -> tuple[list[dict], dict]:
    headers = {"User-Agent": "PulseBot/1.0 (+self-hosted)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    r = await _get(url, headers)
    if r.status_code == 304:
        return [], {"etag": etag, "last_modified": last_modified}
    r.raise_for_status()
    return parse_feed(r.content), {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")}


def _extract_article(html: str, url: str) -> tuple[str, str | None]:
    extracted = trafilatura.extract(html, url=url, include_comments=False, include_tables=False, favor_precision=True)
    image = None
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        image = normalize_image_url(getattr(meta, "image", None), url)
    except Exception:
        pass
    return " ".join((extracted or "").split())[:MAX_TEXT], image


async def fetch_article(url: str) -> tuple[str, str | None]:
    """Скачивает ограниченный HTML-документ с проверкой SSRF на каждом редиректе; возвращает (текст статьи, og:image)."""
    headers = {"User-Agent": "PulseBot/1.0 (+self-hosted)", "Accept": "text/html,application/xhtml+xml"}
    current_url = url
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(4):
            await check_url(current_url)
            async with client.stream("GET", current_url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return "", None
                    current_url = str(httpx.URL(current_url).join(location))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                    return "", None
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_ARTICLE_BYTES:
                    raise ValueError("Страница статьи превышает лимит 2 МБ")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ARTICLE_BYTES:
                        raise ValueError("Страница статьи превышает лимит 2 МБ")
                    chunks.append(chunk)
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            return await asyncio.to_thread(_extract_article, html, current_url)
    raise ValueError("Слишком много редиректов при загрузке статьи")


async def _claim_short_articles(source_id: int, include_clustered: bool = False) -> list[dict]:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "WITH candidates AS ("
                "SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id "
                "WHERE i.source_id = %s AND s.type = 'rss' AND i.status IN ('new','clustered') AND i.url <> '' "
                f"AND (i.status = 'new' OR (%s AND EXISTS (SELECT 1 FROM clusters c WHERE c.id = i.cluster_id AND c.state IN {ENRICHABLE_CLUSTER_STATES}))) "
                "AND char_length(btrim(i.text)) < %s AND (i.article_content_status = 'pending' OR "
                "(i.article_content_status = 'failed' AND i.article_content_started_at < now() - interval '1 day') OR "
                "(i.article_content_status = 'running' AND i.article_content_started_at < now() - interval '10 minutes')) "
                "ORDER BY i.published_at DESC, i.id LIMIT %s FOR UPDATE OF i SKIP LOCKED) "
                "UPDATE items i SET article_content_status = 'running', article_content_started_at = now() "
                "FROM candidates c WHERE i.id = c.id RETURNING i.id, i.title, i.url, i.text, i.status, i.cluster_id, i.image_url",
                (source_id, include_clustered, MIN_RSS_TEXT_CHARS, ARTICLE_BATCH_SIZE),
            )
            return await cur.fetchall()


async def _enrich_article(item: dict, semaphore: asyncio.Semaphore) -> dict:
    result = {"id": item["id"], "title": item["title"], "status": item["status"], "cluster_id": item["cluster_id"], "article": None, "image": None, "error": None}
    async with semaphore:
        try:
            article, image = await fetch_article(item["url"])
        except Exception as e:
            log.info("article fetch failed for item %s: %s", item["id"], e)
            result["error"] = f"{type(e).__name__}: {e}"
            return result
    result["image"] = None if item.get("image_url") else image
    if len(article) < MIN_ARTICLE_TEXT_CHARS or len(article) <= len(item["text"].strip()):
        return result
    excerpt = _strip_read_more_teaser(item["text"].strip())
    body = article if not excerpt or excerpt.casefold() in article.casefold() else "\n\n".join((excerpt, article))
    result["article"] = body[:MAX_TEXT]
    return result


@jobs.handler("enrich_articles")
async def enrich_articles(payload: dict) -> None:
    source_id = int(payload["source_id"])
    project_id = int(payload["project_id"])
    include_clustered = bool(payload.get("include_clustered"))
    if include_clustered:
        async with db.advisory_lock(1, project_id) as got:
            if not got:
                raise jobs.Retry(5)
            await _run_article_enrichment(source_id, project_id, include_clustered)
    else:
        await _run_article_enrichment(source_id, project_id, include_clustered)


async def _run_article_enrichment(source_id: int, project_id: int, include_clustered: bool) -> None:
    rows = await _claim_short_articles(source_id, include_clustered)
    semaphore = asyncio.Semaphore(ARTICLE_FETCH_CONCURRENCY)
    results = await asyncio.gather(*(_enrich_article(row, semaphore) for row in rows))
    clustered_articles = [(index, r["title"], r["article"]) for index, r in enumerate(results) if r["status"] == "clustered" and r["article"]]
    embeddings: dict[int, np.ndarray] = {}
    if clustered_articles:
        provider = await refresh_provider()
        try:
            vectors = await provider.embed([f"{title}. {article[:1200]}" for _, title, article in clustered_articles], project_id=project_id)
            embeddings = {result_index: vector for (result_index, _, _), vector in zip(clustered_articles, vectors)}
        except Exception as e:
            log.warning("failed to embed fetched article batch for source %s: %s", source_id, e)
    affected_clusters: set[int] = set()
    for result_index, r in enumerate(results):
        item_id, status, cluster_id, article, error = r["id"], r["status"], r["cluster_id"], r["article"], r["error"]
        if r["image"]:
            await db.execute("UPDATE items SET image_url = COALESCE(image_url, %s) WHERE id = %s", (r["image"], item_id))
        if article:
            embedding = embeddings.get(result_index)
            if status == "clustered" and embedding is None:
                await db.execute(
                    "UPDATE items SET article_content_status = 'failed', article_content_started_at = now() WHERE id = %s",
                    (item_id,),
                )
                continue
            await db.execute(
                "UPDATE items SET text = %s, embedding = COALESCE(%s, embedding), article_content_status = 'done', article_content_started_at = now() "
                "WHERE id = %s AND status = %s",
                (article, embedding, item_id, status),
            )
            if status == "clustered" and cluster_id is not None:
                affected_clusters.add(int(cluster_id))
        else:
            await db.execute("UPDATE items SET article_content_status = 'failed', article_content_started_at = now() WHERE id = %s", (item_id,))
        if error:
            log.info("article extraction skipped for item %s: %s", item_id, error)

    for cluster_id in affected_clusters:
        members = await db.fetchall("SELECT embedding FROM items WHERE cluster_id = %s AND status = 'clustered' AND embedding IS NOT NULL", (cluster_id,))
        if members:
            centroid = np.mean([unit_vector(row["embedding"]) for row in members], axis=0)
            await db.execute("UPDATE clusters SET centroid = %s WHERE id = %s", (centroid, cluster_id))

    status_filter = "i.status = 'new'" if not include_clustered else f"i.status IN ('new','clustered') AND (i.status = 'new' OR EXISTS (SELECT 1 FROM clusters c WHERE c.id = i.cluster_id AND c.state IN {ENRICHABLE_CLUSTER_STATES}))"
    pending = await db.fetchone(
        f"SELECT 1 FROM items i WHERE i.source_id = %s AND {status_filter} AND i.url <> '' AND char_length(btrim(i.text)) < %s "
        "AND (i.article_content_status = 'pending' OR "
        "(i.article_content_status = 'failed' AND i.article_content_started_at < now() - interval '1 day') OR "
        "(i.article_content_status = 'running' AND i.article_content_started_at < now() - interval '10 minutes')) LIMIT 1",
        (source_id, MIN_RSS_TEXT_CHARS),
    )
    if pending:
        dedupe = f"article-backfill:{project_id}:{source_id}" if include_clustered else f"enrich:{project_id}:{source_id}"
        await jobs.enqueue(
            "enrich_articles",
            {"source_id": source_id, "project_id": project_id, "include_clustered": include_clustered},
            dedupe_key=dedupe,
        )
    # Пересчёт сюжетов нужен, только если что-то реально обогатилось; пустой проход не должен запускать полный пересчёт.
    if rows or not include_clustered:
        await jobs.enqueue("process_project", {"project_id": project_id}, dedupe_key=f"process:{project_id}")


async def insert_items(project_id: int, source_id: int | None, items: list[dict], window_hours: int | None = None) -> int:
    """Вставляет материалы; дубликаты по URL/хэшу игнорируются. Слишком старые (вне окна) пропускаются."""
    floor = utcnow() - timedelta(hours=window_hours) if window_hours else None
    inserted = 0
    async with db.pool.connection() as conn:
        for it in items:
            if floor and it["published_at"] < floor:
                continue
            h = item_hash(it.get("url", ""), it["title"], it.get("text", ""))
            cur = await conn.execute(
                "INSERT INTO items(project_id, source_id, url, url_hash, title, text, published_at, image_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                # уже известному материалу дописываем картинку, если раньше её не нашли; новым считается только реальная вставка
                "ON CONFLICT (project_id, url_hash) DO UPDATE SET image_url = EXCLUDED.image_url "
                "WHERE items.image_url IS NULL AND EXCLUDED.image_url IS NOT NULL RETURNING (xmax = 0) AS created",
                (project_id, source_id, it.get("url", ""), h, it["title"][:500], it.get("text", "")[:MAX_TEXT], it["published_at"], normalize_image_url(it.get("image_url"))),
            )
            row = await cur.fetchone()
            inserted += 1 if row and row["created"] else 0
    return inserted


@jobs.handler("ingest_source")
async def ingest_source(payload: dict) -> None:
    src = await db.fetchone("SELECT s.*, p.window_hours FROM sources s JOIN projects p ON p.id = s.project_id WHERE s.id = %s", (payload["source_id"],))
    if not src or not src["enabled"] or src["type"] != "rss":
        return
    try:
        entries, meta = await fetch_feed(src["url"], src["etag"], src["last_modified"])
    except Exception as e:
        await db.execute("UPDATE sources SET last_fetched_at = now(), last_error = %s WHERE id = %s", (str(e)[:500], src["id"]))
        await events.notify("source", source_id=src["id"], ok=False)
        raise
    inserted = await insert_items(src["project_id"], src["id"], entries, src["window_hours"])
    await db.execute(
        "UPDATE sources SET last_fetched_at = now(), last_ok_at = now(), last_error = NULL, etag = %s, last_modified = %s WHERE id = %s",
        (meta.get("etag"), meta.get("last_modified"), src["id"]),
    )
    log.info("source %s: %d entries, %d new", src["id"], len(entries), inserted)
    await events.notify("source", source_id=src["id"], ok=True, new_items=inserted)
    if inserted:
        await jobs.enqueue("process_project", {"project_id": src["project_id"]}, dedupe_key=f"process:{src['project_id']}")


async def import_manual(project_id: int, items: list[dict]) -> int:
    """Ручной импорт (API, seed, датасеты): item = {title, text, url, source, authority, published_at | hours_ago}."""
    now = utcnow()
    per_source: dict[str, list[dict]] = {}
    authority: dict[str, float] = {}
    for it in items:
        name = it.get("source") or "Импорт"
        authority.setdefault(name, it.get("authority") if it.get("authority") is not None else 0.5)
        ts = it.get("published_at") or (now - timedelta(hours=it.get("hours_ago") or 0))
        per_source.setdefault(name, []).append({"title": it["title"], "text": it.get("text", ""), "url": it.get("url", ""), "published_at": ts, "image_url": it.get("image_url")})
    inserted = 0
    for name, batch in per_source.items():
        row = await db.fetchone("SELECT id FROM sources WHERE project_id = %s AND type = 'manual' AND name = %s", (project_id, name))
        if not row:
            row = await db.fetchone(
                "INSERT INTO sources(project_id, type, name, authority, enabled) VALUES (%s, 'manual', %s, %s, false) RETURNING id",
                (project_id, name, authority[name]),
            )
        inserted += await insert_items(project_id, row["id"], batch)
    if inserted:
        await jobs.enqueue("process_project", {"project_id": project_id}, dedupe_key=f"process:{project_id}")
    return inserted
