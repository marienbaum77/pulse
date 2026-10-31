"""Аспекты темы, которые составляет языковая модель: редактору достаточно описать тему одной строкой.
Аспекты нужны для близости к теме (заголовок сравнивается с каждым аспектом) и подбора источников."""
import json
import logging
import re

from psycopg.types.json import Jsonb

from .. import db, events, jobs
from ..providers import LLMUnavailable, refresh_provider

log = logging.getLogger("pulse.aspects")
MAX_ASPECTS = 10

SYSTEM = """Ты помогаешь настроить тематический канал. Тематика канала: {topic}. Язык канала: {language}.
Составь от 5 до {n} аспектов темы. Аспект — конкретная подтема из 2–6 слов; по набору аспектов должно быть видно, подходит ли заголовок материала каналу.
Аспекты не повторяют друг друга и вместе покрывают тему. Не используй общие слова вроде «новости», «инциденты», «интересное», «обзоры»: они подходят к любому заголовку.
Пиши на языке канала. Ответь ТОЛЬКО JSON-массивом строк без пояснений."""


def _marker_key(project_id: int) -> str:
    return f"aspects_topic:{project_id}"


def parse_aspects(raw: str, limit: int = MAX_ASPECTS) -> list[str]:
    match = re.search(r"\[.*\]", raw or "", re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except ValueError:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        aspect = " ".join(str(item).split()).strip(" .;,")[:120]
        if len(aspect) >= 3 and aspect.casefold() not in seen:
            seen.add(aspect.casefold())
            out.append(aspect)
    return out[:limit]


async def needs_aspects(project: dict) -> bool:
    """Аспектов нет, тема задана и модель ещё не составляла аспекты для этой формулировки темы."""
    topic = (project.get("topic") or "").strip()
    if project.get("topic_aspects") or not topic:
        return False
    row = await db.fetchone("SELECT value FROM kv WHERE key = %s", (_marker_key(project["id"]),))
    return not row or row["value"].get("topic") != topic


async def schedule(project_id: int, *, reset: bool = False) -> None:
    """reset забывает прошлую попытку: редактор очистил аспекты и ждёт новые для той же темы."""
    if reset:
        await db.execute("DELETE FROM kv WHERE key = %s", (_marker_key(project_id),))
    await jobs.enqueue("generate_aspects", {"project_id": project_id}, dedupe_key=f"aspects:{project_id}")


async def generate_aspects(project_id: int, provider) -> list[str]:
    """Составляет и сохраняет аспекты; пустой список — модель недоступна, ответ не разобран или редактор уже задал аспекты сам."""
    project = await db.fetchone("SELECT id, name, topic, language, topic_aspects FROM projects WHERE id = %s", (project_id,))
    if not project or not await needs_aspects(project) or not getattr(provider, "supports_chat", False):
        return []
    topic = project["topic"].strip()
    try:
        raw = await provider.chat(
            SYSTEM.format(topic=topic, language=project["language"], n=MAX_ASPECTS),
            f"Тема канала «{project['name']}»: {topic}",
            max_tokens=400,
            temperature=0.3,
            project_id=project_id,
        )
    except LLMUnavailable as e:
        log.info("aspects for project %s not generated: %s", project_id, e)
        return []  # попытка не засчитана: следующий проход обработки спросит модель снова
    # Попытку запоминаем и при неразобранном ответе: иначе модель спрашивали бы на каждом проходе обработки.
    await db.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (%s, %s, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (_marker_key(project_id), Jsonb({"topic": topic})),
    )
    aspects = parse_aspects(raw)
    if not aspects:
        log.info("aspects for project %s: model answer not parsed", project_id)
        return []
    # Пока модель думала, редактор мог сменить тему или вписать аспекты сам — тогда его версия важнее.
    saved = await db.fetchone(
        "UPDATE projects SET topic_aspects = %s, topic_aspects_auto = true WHERE id = %s AND topic = %s AND topic_aspects = '[]'::jsonb RETURNING id",
        (Jsonb(aspects), project_id, project["topic"]),
    )
    if not saved:
        return []
    # Тематические оценки материалов считались по разбору строки темы — пересчитываем по новым аспектам.
    await db.execute("UPDATE items SET topic_score = NULL WHERE project_id = %s", (project_id,))
    await jobs.enqueue("process_project", {"project_id": project_id}, dedupe_key=f"process:{project_id}")
    await events.notify("project", project_id=project_id)
    log.info("project %s: %d topic aspects generated", project_id, len(aspects))
    return aspects


@jobs.handler("generate_aspects")
async def generate_aspects_job(payload: dict) -> None:
    await generate_aspects(int(payload["project_id"]), await refresh_provider())
