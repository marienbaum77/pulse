"""Статистика роликов в TikTok — данные для будущего отбора постов. После публикации из «Входящих» бот узнаёт id
ролика (из статуса загрузки, а если TikTok его не вернул — по подписи среди последних роликов) и снимает просмотры,
лайки, комментарии и репосты, когда ролику исполняется 1 час, 6 часов, сутки, 3 дня, неделя и месяц.
/export отдаёт всё одной таблицей CSV: строка — пост канала, его признаки и замеры."""
import csv
import io
import logging
import re
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

HORIZONS = (1, 6, 24, 72, 168, 720)  # часы жизни ролика, на которые делается замер
TOLERANCE = 1.5  # замер считается за отметку h, если сделан не позже 1.5·h (бот мог быть выключен)
WAIT_PUBLISH_DAYS = 14  # дольше черновик во «Входящих» не ждём
POLL_MINUTES = 15
METRICS = (("views", "view_count"), ("likes", "like_count"), ("comments", "comment_count"), ("shares", "share_count"))


def due(age_hours: float, ages_done: list[float]) -> bool:
    """Нужен ли замер: ролик перешагнул отметку, после которой замеров ещё не было."""
    return any(h <= age_hours and not any(a >= h for a in ages_done) for h in HORIZONS)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def match_videos(posts: list, videos: list[dict], taken: frozenset[str]) -> dict[tuple[int, int], dict]:
    """Ролик к посту по подписи: бот предлагает подпись, которая начинается с заголовка поста. Ролик должен появиться
    не раньше загрузки во «Входящие», и один ролик достаётся одному посту."""
    found: dict[tuple[int, int], dict] = {}
    taken = set(taken)
    for post in sorted(posts, key=lambda p: p["uploaded_at"] or 0):
        key = _norm(post["title"])[:40]
        if len(key) < 10:
            continue
        for video in sorted(videos, key=lambda v: v.get("create_time") or 0):
            vid = str(video.get("id"))
            text = _norm(f"{video.get('title') or ''} {video.get('video_description') or ''}")
            if vid not in taken and (video.get("create_time") or 0) >= (post["uploaded_at"] or 0) - 60 and key in text:
                found[(post["chat_id"], post["message_id"])] = video
                taken.add(vid)
                break
    return found


async def sync(store, tiktok, now: float | None = None) -> int:
    """Один проход: находит id опубликованных роликов и делает назревшие замеры. Возвращает число замеров."""
    now = now or time.time()
    pending = store.awaiting_video(now - WAIT_PUBLISH_DAYS * 86400)
    left = []
    for post in pending:
        try:
            status = await tiktok.publish_status(post["publish_id"])
        except Exception as e:
            log.info("publish status %s: %s", post["publish_id"], e)
            status = {"post_ids": []}
        if status["post_ids"]:
            store.update(post["chat_id"], post["message_id"], video_id=status["post_ids"][0])
        else:
            left.append(post)
    if not tiktok.can_read_stats:
        return 0
    if left:
        for (chat_id, message_id), video in match_videos(left, await tiktok.list_videos(), store.taken_videos()).items():
            store.update(chat_id, message_id, video_id=str(video["id"]))

    tracked = store.tracked(now - (max(HORIZONS) * TOLERANCE + 24) * 3600)
    wanted = []
    for post in tracked:
        created = post["tiktok_created_at"]
        ages = [s["age_hours"] for s in store.snapshots(post["video_id"])]
        if created is None or due((now - created) / 3600, ages):
            wanted.append(post)
    taken = 0
    for i in range(0, len(wanted), 20):
        batch = {p["video_id"]: p for p in wanted[i:i + 20]}
        for video in await tiktok.query_videos(list(batch)):
            post = batch.get(str(video.get("id")))
            if not post or not video.get("create_time"):
                continue
            created = float(video["create_time"])
            store.update(post["chat_id"], post["message_id"], tiktok_created_at=created, tiktok_url=video.get("share_url"))
            age = (now - created) / 3600
            if due(age, [s["age_hours"] for s in store.snapshots(post["video_id"])]):
                store.add_stats(post["video_id"], age, *(video.get(field) for _, field in METRICS), fetched_at=now)
                taken += 1
    return taken


def latest(store, video_id: str | None):
    snaps = store.snapshots(video_id) if video_id else []
    return snaps[-1] if snaps else None


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def export_csv(store) -> bytes:
    """Таблица для анализа (pandas, Excel): признаки поста и ролика, замеры на каждой отметке и последний замер.
    Номер поста совпадает с номером сообщения канала — по нему строки сводятся с публикациями Pulse."""
    head = ["channel", "message_id", "telegram_url", "title", "kind", "format", "text_chars", "seconds", "status",
            "uploaded_at_utc", "tiktok_video_id", "tiktok_url", "published_at_utc", "publish_delay_hours"]
    head += [f"{m}_{h}h" for h in HORIZONS for m, _ in METRICS]
    head += ["last_age_hours", *(f"last_{m}" for m, _ in METRICS)]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(head)
    for post in store.all_posts():
        snaps = store.snapshots(post["video_id"]) if post["video_id"] else []
        row = [post["channel"] or "", post["message_id"],
               f"https://t.me/{post['channel']}/{post['message_id']}" if post["channel"] else "",
               post["title"] or "", post["kind"] or "", post["format"] or "", post["text_chars"] or "",
               round(post["seconds"], 1) if post["seconds"] else "", post["status"], _iso(post["uploaded_at"]),
               post["video_id"] or "", post["tiktok_url"] or "", _iso(post["tiktok_created_at"]),
               round((post["tiktok_created_at"] - post["uploaded_at"]) / 3600, 1) if post["tiktok_created_at"] and post["uploaded_at"] else ""]
        for h in HORIZONS:
            snap = next((s for s in snaps if h <= s["age_hours"] <= h * TOLERANCE), None)
            row += [snap[m] if snap and snap[m] is not None else "" for m, _ in METRICS]
        last = snaps[-1] if snaps else None
        row += [round(last["age_hours"], 1) if last else "", *((last[m] if last and last[m] is not None else "") for m, _ in METRICS)]
        w.writerow(row)
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу
