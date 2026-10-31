"""Статистика своих постов: просмотры и реакции из веб-превью t.me/s/<канал> — без бота, входа и языковой модели.
Из неё считаются вес источников (как их посты заходят по сравнению с медианой канала) и подсказка для оценки
интересности: какие посты канала собрали больше и меньше всего просмотров."""
import logging
import statistics
from datetime import timedelta

from psycopg.types.json import Jsonb

from .. import db, jobs
from ..textutil import utcnow
from . import ingest
from .connectors import telegram

log = logging.getLogger("pulse.stats")
COLLECT_EVERY = timedelta(hours=3)
TRACK_DAYS = 30  # статистику постов старше месяца не обновляем: просмотры давно перестали расти
MAX_PAGES = 6  # страница превью — около 20 сообщений
SETTLED_HOURS = 48  # к этому возрасту пост набирает основную часть просмотров: моложе сравнивать нечестно
MIN_SOURCE_POSTS = 3
MIN_HINT_POSTS = 6
# При единицах просмотров «лучший» и «худший» пост различаются на 1–2 просмотра — это шум, а не вкус аудитории.
MIN_HINT_MEDIAN_VIEWS = 30
PERFORMANCE_RANGE = (0.5, 1.5)


def public_name(chat_id: str) -> str | None:
    """Имя публичного канала для t.me/s; у приватного (-100…) веб-превью нет."""
    try:
        return telegram.normalize_channel(str(chat_id))
    except ValueError:
        return None


async def collect_channel(channel_id: int) -> int:
    """Обновляет просмотры и реакции опубликованных постов канала; возвращает число обновлённых публикаций."""
    ch = await db.fetchone("SELECT id, project_id, type, config FROM channels WHERE id = %s", (channel_id,))
    name = public_name((ch["config"] or {}).get("chat_id", "")) if ch and ch["type"] == "telegram" else None
    if not name:
        return 0
    tracked = await db.fetchall(
        "SELECT id, external_id FROM publications WHERE channel_id = %s AND status = 'sent' AND external_id ~ '^[0-9]+$' "
        "AND sent_at > now() - make_interval(days => %s)",
        (channel_id, TRACK_DAYS),
    )
    wanted = {int(p["external_id"]): p["id"] for p in tracked}
    if not wanted:
        return 0
    found: dict[int, dict] = {}
    url = f"https://t.me/s/{name}"
    for _ in range(MAX_PAGES):
        r = await ingest.http_get(url, {"User-Agent": ingest.BROWSER_UA})
        r.raise_for_status()
        page = telegram.parse_stats(r.text)
        if not page:
            break
        found.update(page)
        oldest = min(page)
        if oldest <= min(wanted):
            break
        url = f"https://t.me/s/{name}?before={oldest}"
    updated = 0
    for message_id, pub_id in wanted.items():
        s = found.get(message_id)
        if s and s["views"] is not None:
            updated += await db.execute(
                "UPDATE publications SET views = %s, reactions = %s, stats_at = now() WHERE id = %s", (s["views"], s["reactions"], pub_id)
            )
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (f"stats_at:{channel_id}", Jsonb({"at": utcnow().isoformat(), "updated": updated})),
    )
    await recompute_project(ch["project_id"])
    return updated


async def settled_posts(project_id: int) -> list[dict]:
    """Посты старше SETTLED_HOURS с просмотрами и их отношение к медиане своего канала (relative)."""
    rows = await db.fetchall(
        "SELECT p.id, p.channel_id, p.views, p.reactions, d.id AS draft_id, d.title, "
        "ARRAY(SELECT DISTINCT i.source_id FROM items i WHERE i.cluster_id = ANY(d.cluster_ids) "
        "AND i.status IN ('clustered', 'duplicate') AND i.source_id IS NOT NULL) AS source_ids "
        "FROM publications p JOIN drafts d ON d.id = p.draft_id "
        "WHERE d.project_id = %s AND p.status = 'sent' AND p.views IS NOT NULL "
        "AND p.sent_at < now() - make_interval(hours => %s) AND p.sent_at > now() - make_interval(days => %s)",
        (project_id, SETTLED_HOURS, TRACK_DAYS),
    )
    medians = {}
    for channel in {r["channel_id"] for r in rows}:
        medians[channel] = statistics.median(r["views"] for r in rows if r["channel_id"] == channel) or 1
    for r in rows:
        r["relative"] = r["views"] / max(medians[r["channel_id"]], 1)
    return rows


async def recompute_project(project_id: int) -> None:
    """Вес источников по просмотрам их постов и подсказка для оценки интересности."""
    posts = await settled_posts(project_id)
    per_source: dict[int, list[float]] = {}
    for p in posts:
        for sid in p["source_ids"]:
            per_source.setdefault(sid, []).append(p["relative"])
    low, high = PERFORMANCE_RANGE
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE sources SET performance = NULL, performance_posts = 0 WHERE project_id = %s", (project_id,))
        for sid, rel in per_source.items():
            value = min(high, max(low, statistics.median(rel))) if len(rel) >= MIN_SOURCE_POSTS else None
            await conn.execute(
                "UPDATE sources SET performance = %s, performance_posts = %s WHERE id = %s AND project_id = %s",
                (value, len(rel), sid, project_id),
            )
    hint = {}
    median_views = statistics.median(p["views"] for p in posts) if posts else 0
    if len(posts) >= MIN_HINT_POSTS and median_views >= MIN_HINT_MEDIAN_VIEWS:
        ranked = sorted(posts, key=lambda p: p["relative"], reverse=True)
        hint = {"top": [p["title"] for p in ranked[:3]], "bottom": [p["title"] for p in ranked[-3:]], "median_views": median_views}
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (f"audience:{project_id}", Jsonb(hint)),
    )


async def audience_hint(project_id: int) -> str:
    """Строка для промпта оценки: что из канала зашло аудитории лучше и хуже всего. Пусто — данных мало."""
    row = await db.fetchone("SELECT value FROM kv WHERE key = %s", (f"audience:{project_id}",))
    hint = row["value"] if row else {}
    # Подсказки, сохранённые до порога по просмотрам, median_views не содержат и не используются до пересчёта.
    if not hint.get("top") or (hint.get("median_views") or 0) < MIN_HINT_MEDIAN_VIEWS:
        return ""
    top = "; ".join(f"«{t}»" for t in hint["top"])
    bottom = "; ".join(f"«{t}»" for t in hint["bottom"])
    return (
        f"По просмотрам канала аудитории больше всего зашли посты: {top}. Меньше всего: {bottom}. "
        "Учитывай это как подсказку о вкусах аудитории, но тема канала и польза материала важнее."
    )


async def schedule_due() -> int:
    """Ставит сбор статистики каналам, у которых он не обновлялся COLLECT_EVERY и есть свежие публикации."""
    rows = await db.fetchall(
        "SELECT ch.id FROM channels ch JOIN projects pr ON pr.id = ch.project_id "
        "LEFT JOIN kv ON kv.key = 'stats_at:' || ch.id "
        "WHERE ch.type = 'telegram' AND ch.enabled AND pr.active AND (kv.updated_at IS NULL OR kv.updated_at < now() - %s) "
        "AND EXISTS (SELECT 1 FROM publications p WHERE p.channel_id = ch.id AND p.status = 'sent' "
        "AND p.sent_at > now() - make_interval(days => %s))",
        (COLLECT_EVERY, TRACK_DAYS),
    )
    for row in rows:
        await jobs.enqueue("collect_channel_stats", {"channel_id": row["id"]}, dedupe_key=f"stats:{row['id']}")
    return len(rows)


@jobs.handler("collect_channel_stats")
async def collect_channel_stats(payload: dict) -> None:
    channel_id = int(payload["channel_id"])
    try:
        await collect_channel(channel_id)
    except Exception as e:
        # Превью недоступно (сеть, блокировка) — попробуем при следующем плановом сборе, а не повтором задачи.
        log.info("stats for channel %s not collected: %s", channel_id, e)
        await db.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (f"stats_at:{channel_id}", Jsonb({"at": utcnow().isoformat(), "error": str(e)[:300]})),
        )
