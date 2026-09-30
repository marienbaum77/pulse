"""Оценка интересности сюжета языковой моделью: одна короткая оценка 0-10 с обоснованием, кешируется в clusters."""
import json
import logging
import re

from .. import db
from ..providers import LLMUnavailable
from ..textutil import split_sentences

log = logging.getLogger("pulse.interest")
MAX_RATINGS_PER_RUN = 6
RERATE_GROWTH = 1.5

SYSTEM = """Ты — главный редактор новостного канала. Тематика: {topic}. Оцени, насколько сюжет интересен и полезен этой аудитории, по шкале от 0 до 10.
Высокие оценки: важные события, новые факты и цифры, последствия для читателя, громкие релизы и инциденты.
Низкие оценки: мелкие обновления, реклама и анонсы без новизны, слухи, повторы известного, тема вне интересов канала.
Ответь ТОЛЬКО JSON без пояснений вокруг: {{"score": число от 0 до 10, "reason": "одно короткое предложение"}}"""


def parse_rating(raw: str) -> tuple[float, str] | None:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            score = float(data["score"])
            return max(0.0, min(10.0, score)), str(data.get("reason") or "").strip()[:200]
        except (ValueError, KeyError, TypeError):
            pass
    number = re.search(r"(?<![\d.])(10|\d)(?:[.,]\d)?(?![\d])", raw or "")
    return (float(number.group(1)), "") if number else None


async def rate_cluster(project: dict, provider, cluster_id: int) -> float | None:
    """Возвращает оценку 0-10 или None, если модель недоступна; кеш обновляется, только если сюжет вырос."""
    row = await db.fetchone("SELECT title, item_count, interest, interest_items FROM clusters WHERE id = %s", (cluster_id,))
    if not row:
        return None
    if row["interest"] is not None and row["item_count"] < (row["interest_items"] or 0) * RERATE_GROWTH:
        return float(row["interest"])
    if not getattr(provider, "supports_chat", False):
        return None
    items = await db.fetchall(
        "SELECT i.title, i.text, s.name AS source FROM items i LEFT JOIN sources s ON s.id = i.source_id "
        "WHERE i.cluster_id = %s AND i.status = 'clustered' ORDER BY i.published_at DESC LIMIT 4",
        (cluster_id,),
    )
    lines = [f"- {i['source'] or 'Источник'}: {i['title']}. {' '.join(split_sentences(i['text'])[:2])[:300]}" for i in items]
    prompt = f"Сюжет: {row['title']}\nМатериалов: {row['item_count']}\n" + "\n".join(lines)
    system = SYSTEM.format(topic=project["topic"] or "новости")
    try:
        raw = await provider.chat(system, prompt, max_tokens=80, temperature=0.1, project_id=project["id"])
    except LLMUnavailable as e:
        log.info("interest rating skipped for cluster %s: %s", cluster_id, e)
        return None
    parsed = parse_rating(raw)
    if not parsed:
        return None
    score, reason = parsed
    await db.execute(
        "UPDATE clusters SET interest = %s, interest_reason = %s, interest_items = item_count WHERE id = %s",
        (score, reason, cluster_id),
    )
    return score


async def rank_by_interest(project: dict, provider, candidates: list[dict], limit: int) -> list[dict]:
    """Из списка кандидатов (уже отсортированных по теме и весу) берёт `limit` самых интересных.
    Оцениваются только несколько первых без кешированной оценки: каждая оценка — вызов модели."""
    rated: dict[int, float] = {}
    fresh = 0
    for c in candidates:
        cached = await db.fetchone("SELECT interest, interest_items, item_count FROM clusters WHERE id = %s", (c["id"],))
        needs = cached["interest"] is None or cached["item_count"] >= (cached["interest_items"] or 0) * RERATE_GROWTH
        if needs and fresh >= MAX_RATINGS_PER_RUN:
            if cached["interest"] is not None:
                rated[c["id"]] = float(cached["interest"])
            continue
        fresh += 1 if needs else 0
        score = await rate_cluster(project, provider, c["id"])
        if score is not None:
            rated[c["id"]] = score
    order = {c["id"]: i for i, c in enumerate(candidates)}
    # без оценки (модель недоступна, лимит) кандидат идёт с нейтральными 5: порядок по теме и весу сохраняется
    return sorted(candidates, key=lambda c: (-rated.get(c["id"], 5.0), order[c["id"]]))[:limit]
