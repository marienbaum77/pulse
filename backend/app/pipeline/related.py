"""Связанный прежний пост: новый пост выходит ответом (reply) на него в Telegram, и читатель видит продолжение истории.
Связь ищется без языковой модели. Надёжные признаки — обновление того же сюжета, тот же репозиторий («👉»)
или тот же первоисточник. Иначе нужны близкие векторы сюжетов и общее имя собственное в заголовках: одно сходство
векторов путает «Microsoft Copilot» с «Claude переписал компилятор» (0.77), а с общим именем — уже нет."""
import re

import numpy as np

from .. import db

# Пороги подобраны по опубликованным постам IT- и игрового каналов: «ARC Raiders» ↔ «Arc Raiders» (0.63, два общих имени),
# «GTA 6» ↔ «GTA 6» (0.66), PHP ↔ PHP (0.77–0.87) — связаны; «Колда» ↔ Total War (0.64, общее только «Steam») — нет.
RELATED_SIM = 0.62  # при двух и более общих именах
SINGLE_NAME_SIM = 0.66  # при одном общем имени
# Имя, которое встречается во многих постах канала (OpenAI, Claude в IT-канале), связь почти не доказывает.
COMMON_SIM = 0.70
COMMON_SHARE = 0.05
COMMON_MIN = 3

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#\-]*")
_CYR_NAME = re.compile(r"[А-ЯЁ][а-яё]{2,}")
_STOP = {
    "the", "and", "for", "with", "from", "new", "how", "why", "what", "you", "your", "now", "its", "are", "has", "can",
    "best", "top", "all", "more", "into", "will", "get", "gets", "out", "this", "that", "who", "big", "first",
    "ai", "ии", "it", "api", "app", "apps", "pc", "vs", "ceo", "via", "news",
    # общие слова английских заголовков в стиле Title Case — не имена
    "critical", "vulnerability", "vulnerabilities", "release", "released", "update", "launch", "launches", "says", "game",
    "games", "model", "models", "tool", "tools", "agent", "agents", "open", "source", "data", "free", "year", "week",
    "guide", "review", "report", "video", "after", "over", "about", "just", "could", "would", "should", "make", "makes",
    # платформы и технические слова игровых и железных новостей: общие почти для любой пары постов
    "steam", "fps", "4k", "8k", "hdr", "dlc", "pve", "pvp", "mmo", "mmorpg", "rpg", "beta", "gpu", "cpu", "ssd", "ram", "usb",
    "ultrawide", "deck", "remaster", "remake", "edition", "complete",
}


def entities(title: str) -> set[str]:
    """Имена в заголовке: латинские слова и названия с цифрами (PHP, Gears, E-Day, RTX5090) целиком,
    русские имена с заглавной буквы не в начале заголовка — по первым четырём буквам («Киеве» ≈ «Киеву»)."""
    title = title or ""
    out = {w.lower().rstrip("-") for w in _LATIN.findall(title) if len(w) >= 3 or w.isupper()}
    # Заглавная в начале заголовка — просто начало предложения, а не имя.
    out |= {m.group(0)[:4].lower() for m in _CYR_NAME.finditer(title) if m.start() > 0 and title[m.start() - 1] in " «\"(-"}
    return {w for w in out if len(w) >= 2 and w not in _STOP}


def _unit(v) -> np.ndarray | None:
    if v is None:
        return None
    a = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(a))
    return a / n if n else None


async def find_related(project: dict, draft_id: int, cluster_ids: list[int], title: str, checks: dict, citations: list[dict]) -> dict | None:
    """{draft_id, title, reason, similarity} прежнего опубликованного поста или None."""
    days = int(project.get("reply_related_days") or 0)
    if days <= 0 or not cluster_ids:
        return None
    cur = await db.fetchone("SELECT title, centroid FROM clusters WHERE id = %s", (cluster_ids[0],))
    if not cur:
        return None
    vec = _unit(cur["centroid"])
    link = (checks.get("primary_link") or {}).get("url")
    origin = next(((c.get("media") or {}).get("origin") for c in citations if (c.get("media") or {}).get("origin")), None)
    names = entities(title) | entities(cur["title"])
    rows = await db.fetchall(
        "SELECT d.id, d.title, d.cluster_ids, c.title AS cluster_title, COALESCE(c.published_centroid, c.centroid) AS v, "
        "d.checks->'primary_link'->>'url' AS link, d.citations->0->'media'->>'origin' AS origin, max(p.sent_at) AS sent_at "
        "FROM drafts d JOIN publications p ON p.draft_id = d.id AND p.status = 'sent' "
        "LEFT JOIN clusters c ON c.id = d.cluster_ids[1] "
        "WHERE d.project_id = %s AND d.id <> %s AND d.kind = 'post' AND p.sent_at > now() - make_interval(days => %s) "
        "GROUP BY d.id, c.id ORDER BY max(p.sent_at) DESC",
        (project["id"], draft_id, days),
    )
    if not rows:
        return None
    candidate_names = [entities(r["title"]) | entities(r["cluster_title"] or "") for r in rows]
    frequency: dict[str, int] = {}
    for found in candidate_names:
        for name in found:
            frequency[name] = frequency.get(name, 0) + 1
    common_at = max(COMMON_MIN, COMMON_SHARE * len(rows))

    best = None
    for r, their_names in zip(rows, candidate_names):
        if set(cluster_ids) & set(r["cluster_ids"] or []):
            return {"draft_id": r["id"], "title": r["title"], "reason": "update", "similarity": 1.0}
        if link and r["link"] == link:
            reason, score = "link", 1.0
        elif origin and r["origin"] == origin:
            reason, score = "origin", 1.0
        else:
            other = _unit(r["v"])
            if vec is None or other is None or other.shape != vec.shape:
                continue
            sim = float(vec @ other)
            shared = names & their_names
            rare = {n for n in shared if frequency.get(n, 0) < common_at}
            linked = (len(rare) >= 2 and sim >= RELATED_SIM) or (rare and sim >= SINGLE_NAME_SIM) or (shared and sim >= COMMON_SIM)
            if not linked:
                continue
            reason, score = "similar", sim
        if best is None or score > best["similarity"]:
            best = {"draft_id": r["id"], "title": r["title"], "reason": reason, "similarity": round(score, 3)}
    return best
