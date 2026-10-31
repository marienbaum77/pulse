"""Оценка интересности сюжета языковой моделью: одна короткая оценка 0-10 с обоснованием, кешируется в clusters."""
import json
import logging
import re

from .. import db
from ..providers import LLMUnavailable
from ..textutil import split_sentences

log = logging.getLogger("pulse.interest")
MAX_RATINGS_PER_RUN = 12  # новых оценок за проход: каждая — вызов модели, остальные ждут следующего прохода
RERATE_GROWTH = 1.5

SYSTEM = """Ты — главный редактор новостного канала. Тематика: {topic}. Оцени, насколько сюжет интересен и полезен этой аудитории, по шкале от 0 до 10.
{criteria}
{self_promo}
Ответь ТОЛЬКО JSON без пояснений вокруг: {{"score": число от 0 до 10, "reason": "одно короткое предложение"}}"""
CRITERIA = """Высокие оценки: важные события, новые факты и цифры, последствия для читателя, громкие релизы и инциденты.
Низкие оценки: мелкие обновления, реклама и анонсы без новизны, самопродвижение, слухи, повторы известного, тема вне интересов канала."""
CURATED_SYSTEM = """Ты — главный редактор тематического канала. Тематика: {topic}. Оцени, насколько этот материал (статья, видео или пост) ценен для аудитории канала, по шкале от 0 до 10.
{criteria}
{self_promo}{video_ads}
Ответь ТОЛЬКО JSON без пояснений вокруг: {{"score": число от 0 до 10, "reason": "одно короткое предложение"}}"""
CURATED_CRITERIA = """Высокие оценки: полезные разборы и руководства, глубокие обзоры, оригинальные идеи и опыт, качественные видео точно по теме канала.
Низкие оценки: реклама и продающие тексты, самопродвижение, кликбейт, поверхностные заметки без содержания, тема вне интересов канала."""
# Действует и при своих критериях канала: «инструмент, который можно попробовать» иначе пропускает анонс автором своего продукта.
SELF_PROMO = """Самопродвижение — автор или компания представляет собственный продукт, репозиторий, сервис, курс или канал. Такой материал оценивай не выше 4, \
если в нём нет независимых фактов: цифр, сравнения с аналогами, отзывов пользователей. Слова «ИИ», «агент», «инструмент» в заголовке сами по себе не делают материал интересным."""
# Только для видео: в расшифровках вставки рекламы — норма. В статье и посте реклама оценку снижает.
VIDEO_ADS = """
Рекламные вставки, промокоды и призывы подписаться в расшифровке видео — обычное дело: не снижай за них оценку, оценивай основное содержание. \
Видео, которое целиком реклама или самопродвижение, оценивай низко."""


def criteria_for(project: dict, curated: bool) -> str:
    """Критерии канала из настроек проекта заменяют стандартные; материал вне темы оценивается низко в любом случае."""
    own = (project.get("interest_criteria") or "").strip()
    if not own:
        return CURATED_CRITERIA if curated else CRITERIA
    return f"Критерии этого канала (важнее любых общих представлений о «важных новостях»):\n{own}\nМатериал вне темы канала — низкая оценка."
KIND_LABELS = {"video": "видео", "post": "пост", "article": "статья"}


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


def rating_prompt(project: dict, row: dict, items: list[dict]) -> tuple[str, str]:
    """Системный промпт и текст запроса оценки: row — сюжет (title, item_count, mode), items — его материалы."""
    if row["mode"] == "curated":
        # У одиночного материала охвата нет — модель судит по содержанию, поэтому текста даём больше.
        lines = [
            f"- {KIND_LABELS.get(i['kind'], 'материал')}, {i['source'] or 'Источник'}: {i['title']}. {' '.join(split_sentences(i['text'])[:6])[:900]}"
            for i in items
        ]
        system = CURATED_SYSTEM.format(
            topic=project["topic"] or "без ограничений", criteria=criteria_for(project, True), self_promo=SELF_PROMO,
            video_ads=VIDEO_ADS if any(i["kind"] == "video" for i in items) else "",
        )
        return system, f"Материал: {row['title']}\n" + "\n".join(lines)
    lines = [f"- {i['source'] or 'Источник'}: {i['title']}. {' '.join(split_sentences(i['text'])[:2])[:300]}" for i in items]
    system = SYSTEM.format(topic=project["topic"] or "новости", criteria=criteria_for(project, False), self_promo=SELF_PROMO)
    return system, f"Сюжет: {row['title']}\nМатериалов: {row['item_count']}\n" + "\n".join(lines)


async def rate_cluster(project: dict, provider, cluster_id: int) -> float | None:
    """Возвращает оценку 0-10 или None, если модель недоступна; кеш обновляется, только если сюжет вырос."""
    row = await db.fetchone("SELECT title, item_count, interest, interest_items, mode FROM clusters WHERE id = %s", (cluster_id,))
    if not row:
        return None
    if row["interest"] is not None and row["item_count"] < (row["interest_items"] or 0) * RERATE_GROWTH:
        return float(row["interest"])
    if not getattr(provider, "supports_chat", False):
        return None
    items = await db.fetchall(
        "SELECT i.title, i.text, i.kind, s.name AS source FROM items i LEFT JOIN sources s ON s.id = i.source_id "
        "WHERE i.cluster_id = %s AND i.status = 'clustered' ORDER BY i.published_at DESC LIMIT 4",
        (cluster_id,),
    )
    system, prompt = rating_prompt(project, row, items)
    from .stats import audience_hint  # stats тянет коннекторы и ingest: импорт здесь не создаёт цикла при загрузке

    # Какие посты канала зашли аудитории: те же вызовы модели, только на пару строк длиннее.
    hint = await audience_hint(project["id"])
    if hint:
        system = f"{system}\n{hint}"
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
    Оцениваются только несколько первых без кешированной оценки: каждая оценка — вызов модели.
    В c["interest"] записывается актуальная оценка; None — оценки нет (модель недоступна или исчерпан лимит прохода)."""
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
    for c in candidates:
        c["interest"] = rated.get(c["id"])
    order = {c["id"]: i for i, c in enumerate(candidates)}
    # без оценки (модель недоступна, лимит) кандидат идёт с нейтральными 5: порядок по теме и весу сохраняется
    return sorted(candidates, key=lambda c: (-rated.get(c["id"], 5.0), order[c["id"]]))[:limit]
