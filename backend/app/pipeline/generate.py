"""Генерация текста: контекст источников → LLM (или экстрактивный режим без LLM) → проверки → черновик."""
import logging
import re
import unicodedata
from datetime import date

from langdetect import DetectorFactory, LangDetectException, detect_langs
from psycopg.types.json import Jsonb

from .. import db, events, jobs
from ..providers import LLMUnavailable, refresh_provider
from ..textutil import numbers_in, split_sentences, strip_citations, strip_source_footer, truncate, utcnow, words
from . import drafts_service
from .context import Block, build_blocks
from .ingest import MIN_RSS_TEXT_CHARS
from .process import get_project, refresh_clusters, select_clusters

log = logging.getLogger("pulse.generate")
ENRICH_WAIT_SECONDS = 600
# Половина фактических предложений со ссылкой — граница, ниже которой текст явно «плавает»; 80% на практике блокировало почти всё
MIN_CITATION_COVERAGE = 0.5
DetectorFactory.seed = 0

DEFAULT_SYSTEM = """Ты — редактор новостного канала. Язык текста: {language}. Тон: {tone}. Тематика канала: {topic}.
Правила:
1. Используй только факты из блока ИСТОЧНИКИ. Ничего не выдумывай, не добавляй чисел, имён и дат, которых там нет.
2. Если источники противоречат друг другу, прямо укажи на расхождение.
3. После утверждений в тексте поста ставь номера источников в квадратных скобках: [1], [2]. Не ставь ссылки в заголовке.
4. Длина — не более {max_length} символов.
5. Заголовок — одно короткое, ясное и интересное предложение до 100 символов: только главная новость, без деталей, пояснений и ссылок [n]. Все дополнительные факты и пояснения переноси в текст поста. Формат ответа: заголовок первой строкой, затем пустая строка и текст. Без вступлений вроде «Вот пост».
6. Не копируй в пост названия изданий, даты/время публикации и служебные шапки вроде «habr, 26.09 09:00» — они уже есть в метаданных источников.
7. Пиши и заголовок, и текст на языке {language}. Если источник на другом языке, точно переведи факты на {language}; названия продуктов и организаций можно оставить в оригинале.
8. Не добавляй в конце отдельный список источников, названий изданий, дат или ссылок. Указывай подтверждения только маркерами [n] внутри текста.
9. Источники описывают одно и то же событие. Напиши ОДИН связный рассказ, а не пересказ источников по очереди: не начинай абзацы с [n] и не пиши «по данным первого источника».
10. Каждый факт упоминай один раз. Если несколько источников сообщают одно и то же, скажи это одним предложением и поставь несколько ссылок сразу: [1][2]. Повторы недопустимы.
11. Не копируй предложения источников дословно: перескажи своими словами, оставив только новое и важное. Начни с главного, затем детали и контекст, каждая деталь — новая информация.
12. Если источники расходятся в цифрах или деталях, назови оба варианта одним предложением и укажи, кто что сообщает."""


def render_system(project: dict, max_length: int) -> str:
    tpl = project["prompt_template"].strip() or DEFAULT_SYSTEM
    for key, val in {"language": project["language"], "tone": project["tone"], "topic": project["topic"] or "новости", "max_length": str(max_length)}.items():
        tpl = tpl.replace("{" + key + "}", val)
    return tpl


def blocks_prompt(blocks: list[Block], language: str) -> str:
    parts = [f"[{b.n}] {b.source}, {b.published_at:%d.%m %H:%M}. {b.output_title}\n{b.output_text}" for b in blocks]
    return (
        "ИСТОЧНИКИ:\n\n"
        + "\n\n".join(parts)
        + f"\n\nНапиши пост на языке {language}. Заголовок — одно короткое предложение до 100 символов без [n]."
        " Оставшиеся факты и предложения помести в текст после пустой строки. "
        "Если материал написан на другом языке, переведи его. "
        "Маркеры [n] используй только в тексте поста. Объедини источники в один связный текст без повторов, не пересказывай их по очереди."
    )


def looks_like_source_dump(body: str, blocks: list[Block]) -> bool:
    """Слабые модели вместо синтеза выдают источники по очереди: абзацы с маркера [n] или предложения, скопированные из источников."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if sum(1 for p in paragraphs if re.match(r"^\[\d+\]", p)) >= 2:
        return True
    source_text = " ".join(" ".join(words(f"{b.output_title}. {b.output_text}")) for b in blocks)
    sentences = [" ".join(words(strip_citations(x))) for x in split_sentences(body)]
    sentences = [x for x in sentences if len(x.split()) >= 5]
    if len(sentences) < 3:
        return False
    return sum(1 for x in sentences if x in source_text) / len(sentences) >= 0.7


def parse_output(raw: str) -> tuple[str, str]:
    raw = re.sub(r"^```\w*\n?|\n?```$", "", raw.strip())
    lines = raw.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return "", ""
    title = re.sub(r"^#+\s*", "", lines[0].strip())
    title = re.sub(r"^\W*заголовок:\s*", "", title, flags=re.I).strip("*_ \"«»")
    return title, strip_source_footer("\n".join(lines[1:]))


def normalize_generated_post(title: str, body: str, max_headline_chars: int = 120) -> tuple[str, str]:
    """Make one concise headline; move title spillover into the post body without dropping facts."""
    original_sentences = split_sentences(title.strip().strip("*_ \"«»"))
    if not original_sentences:
        return strip_citations(title).strip().strip("*_ \"«»"), body.strip()

    headline = strip_citations(original_sentences[0]).strip().strip("*_ \"«»")
    title_details = original_sentences[1:]
    if len(headline) > max_headline_chars:
        cut = headline[:max_headline_chars]
        break_at = max(cut.rfind(", "), cut.rfind(": "), cut.rfind("; "), cut.rfind(" — "), cut.rfind(" – "))
        if break_at >= max_headline_chars // 2:
            headline = cut[:break_at].rstrip(" ,:;—–-") + "…"
        else:
            headline = cut.rsplit(" ", 1)[0].rstrip(" ,:;—–-") + "…"
        title_details.insert(0, original_sentences[0])

    parts = [" ".join(title_details).strip(), body.strip()]
    detail_and_body = "\n\n".join(part for part in parts if part)
    return headline, detail_and_body


def check_text(body: str, citations: list[dict]) -> dict:
    """Эвристические проверки: числа, которых нет в источниках, и корректность ссылок. Это сигнал редактору, а не гарантия фактичности."""
    known: set[str] = set()
    for c in citations:
        known |= numbers_in(f"{c['title']} {c['excerpt']}")
    clean = re.sub(r"\[\d+\]", "", body)
    clean = re.sub(r"(?m)^\s*\d+\.\s", "", clean)
    unsupported = sorted(numbers_in(clean) - known, key=lambda x: (len(x), x))
    used = {int(x) for x in re.findall(r"\[(\d+)\]", body)}
    valid = {c["n"] for c in citations}
    # короткие связки вроде «Это важно.» фактов не несут, поэтому в покрытие не входят
    sentences = [s for s in split_sentences(body) if len(words(strip_citations(s))) >= 4]
    cited = sum(1 for s in sentences if re.search(r"\[\d+\]", s))
    return {
        "unsupported_numbers": unsupported,
        "invalid_citations": sorted(used - valid),
        "citation_coverage": round(cited / len(sentences), 2) if sentences else 1.0,
        "length": len(body),
    }


def _language_mismatch(body: str, language: str) -> bool:
    target = _language_code(language)
    letters = [char for char in body if char.isalpha()]
    if len(letters) >= 40 and len(set(words(body))) >= 4 and target:
        try:
            guesses = detect_langs(body)
        except LangDetectException:
            guesses = []
        if guesses and guesses[0].prob >= 0.6:
            return guesses[0].lang != target

    target = language.casefold().split("-", 1)[0].strip()
    script_by_language = {
        "ru": "CYRILLIC",
        "rus": "CYRILLIC",
        "russian": "CYRILLIC",
        "русский": "CYRILLIC",
        "en": "LATIN",
        "eng": "LATIN",
        "english": "LATIN",
    }
    script = script_by_language.get(target)
    if not script:
        return False
    letters = [char for char in body if char.isalpha()]
    if len(letters) < 20 or len(set(words(body))) < 4:
        return False
    matching = sum(script in unicodedata.name(char, "") for char in letters)
    return matching / len(letters) < 0.4


def _language_code(language: str) -> str | None:
    normalized = language.casefold().split("-", 1)[0].strip()
    aliases = {
        "русский": "ru",
        "russian": "ru",
        "английский": "en",
        "english": "en",
        "украинский": "uk",
        "ukrainian": "uk",
        "испанский": "es",
        "spanish": "es",
        "французский": "fr",
        "french": "fr",
        "немецкий": "de",
        "german": "de",
        "итальянский": "it",
        "italian": "it",
        "португальский": "pt",
        "portuguese": "pt",
    }
    code = aliases.get(normalized, normalized)
    return code if len(code) == 2 else None


def automatic_review(
    title: str,
    body: str,
    citations: list[dict],
    checks: dict,
    max_length: int,
    kind: str = "post",
    language: str = "ru",
    interest: float | None = None,
    interest_reason: str = "",
) -> dict:
    """Conservative publish gate; these heuristics do not establish factual accuracy."""
    reasons = []
    if not title.strip():
        reasons.append("Пустой заголовок")
    if not body.strip():
        reasons.append("Пустой текст")
    elif len(body.strip()) < _MIN_PUBLISHABLE_BODY_CHARS:
        reasons.append(f"Текст слишком короткий (меньше {_MIN_PUBLISHABLE_BODY_CHARS} символов)")
    if not citations:
        reasons.append("Нет источников")
    if len(citations) == 1 and any(
        c.get("article_content_status") == "failed"
        and c.get("content_length", MIN_RSS_TEXT_CHARS) < MIN_RSS_TEXT_CHARS
        for c in citations
    ):
        reasons.append("Полный текст короткой RSS-заметки не удалось получить")
    if body.strip() and _language_mismatch(body, language):
        reasons.append(f"Текст, вероятно, не на заданном языке ({language})")
    if checks.get("unsupported_numbers"):
        reasons.append("Есть числа, не найденные в источниках")
    if checks.get("invalid_citations"):
        reasons.append("Есть ссылки на отсутствующие источники")
    if checks.get("citation_coverage", 0) < MIN_CITATION_COVERAGE:
        reasons.append(f"Покрытие предложений ссылками на источники ниже {round(MIN_CITATION_COVERAGE * 100)}%")
    if kind == "post" and checks.get("length", 0) > max_length:
        reasons.append(f"Превышен лимит длины ({max_length} символов)")
    return {"passed": not reasons, "reasons": reasons}


def extractive_text(blocks: list[Block], limit: int, max_sentences: int = 4) -> str:
    """Режим без LLM: ведущие предложения самых репрезентативных материалов со ссылками.
    Если несколько источников сообщают одно и то же, предложение берётся один раз, а ссылки на все источники ставятся вместе."""
    chosen: list[tuple[str, list[int], set[str]]] = []
    for b in blocks:
        for sentence in b.output_sentences:
            ws = set(words(sentence))
            if not ws:
                continue
            for text, nums, other in chosen:
                if len(ws & other) / len(ws | other) >= 0.6:
                    if b.n not in nums and len(nums) < 3:  # три ссылки достаточно, длинные ряды [1][2][3][4][5] только мешают читать
                        nums.append(b.n)
                    break
            else:
                if len(chosen) < max_sentences:
                    chosen.append((sentence, [b.n], ws))
    text = " ".join(f"{sentence} {''.join(f'[{n}]' for n in nums)}" for sentence, nums, _ in chosen)
    return truncate(text, limit)


def _citation(b: Block) -> dict:
    return {
        "n": b.n,
        "item_id": b.item_id,
        "title": b.title,
        "url": b.url,
        "source": b.source,
        "excerpt": b.text[:600],
        "article_content_status": b.article_content_status,
        "content_length": b.content_length,
        "image_url": b.image_url,
    }


def _pick_image(citations: list[dict]) -> str | None:
    """Иллюстрация поста — картинка первого из использованных в тексте источников, у которого она есть."""
    return next((c["image_url"] for c in citations if c.get("image_url")), None)


_MIN_PUBLISHABLE_BODY_CHARS = 80


def decide_use_llm(mode: str, blocks: list[Block], provider, language: str = "ru") -> tuple[bool, str]:
    """Возвращает (вызывать ли LLM, фактический режим для метаданных)."""
    supports = getattr(provider, "supports_chat", False)
    translation_needed = _language_mismatch(" ".join(f"{block.output_title}. {block.output_text}" for block in blocks), language)
    if not supports:
        return False, "extractive"
    if translation_needed:
        return True, "llm"
    if mode == "extractive":
        return False, "extractive"
    if mode == "llm":
        return True, "llm"
    if len({block.source for block in blocks}) >= 2:
        return True, "llm"
    return False, "extractive"


async def generate_for_clusters(project: dict, cluster_ids: list[int], kind: str, provider) -> dict:
    mode = project["generation_mode"]
    fallback = mode in ("llm", "auto") and not getattr(provider, "supports_chat", False)
    per_limit = project["max_length"] if kind == "post" else max(300, project["max_length"] // max(len(cluster_ids), 1))

    n = 1
    sections: list[tuple[str, str]] = []
    all_blocks: list[Block] = []
    used_llm_any = False
    translation_required = False
    decided_modes: list[str] = []
    title_citations: set[int] = set()
    for cid in cluster_ids:
        blocks = await build_blocks(project, cid, n, provider)
        if not blocks:
            continue
        n += len(blocks)
        all_blocks.extend(blocks)
        source_text = " ".join(f"{block.title}. {block.text}" for block in blocks)
        needs_translation = _language_mismatch(source_text, project["language"])
        translation_required = translation_required or needs_translation
        use_llm, decided = decide_use_llm(mode, blocks, provider, project["language"])
        decided_modes.append(decided)
        if use_llm:
            used_llm_any = True
            raw = await provider.chat(
                render_system(project, per_limit),
                blocks_prompt(blocks, project["language"]),
                max_tokens=700,
                temperature=0.3,
                project_id=project["id"],
            )
            title, body = parse_output(raw)
            if not body:
                title, body = "", title
            if len(blocks) > 1 and looks_like_source_dump(body, blocks):
                log.info("generation for cluster %s repeated sources instead of merging them; retrying once", cid)
                raw = await provider.chat(
                    render_system(project, per_limit),
                    blocks_prompt(blocks, project["language"])
                    + "\n\nПрошлая попытка была неудачной: текст повторял источники по очереди. Напиши один цельный текст на 3–5 предложений, "
                    "в котором каждый факт встречается один раз, а ссылки [n] стоят внутри предложений.",
                    max_tokens=700,
                    temperature=0.2,
                    project_id=project["id"],
                )
                title, body = parse_output(raw)
                if not body:
                    title, body = "", title
                if looks_like_source_dump(body, blocks):
                    body = extractive_text(blocks, per_limit)
            if not title:
                title = (await db.fetchone("SELECT title FROM clusters WHERE id = %s", (cid,)))["title"]
        else:
            title = (await db.fetchone("SELECT title FROM clusters WHERE id = %s", (cid,)))["title"]
            body = extractive_text(blocks, per_limit)
        title_citations.update(int(value) for value in re.findall(r"\[(\d+)\]", title))
        title, body = normalize_generated_post(title, body)
        sections.append((title, truncate(body, per_limit + 200)))
    if not sections:
        raise RuntimeError("В выбранных кластерах нет материалов")

    if kind == "post":
        title, body = sections[0]
    else:
        title = f"Дайджест: {project['name']}, {date.today():%d.%m}"
        body = "\n\n".join(f"{i}. {t}\n{b}" for i, (t, b) in enumerate(sections, 1))

    used = title_citations | {int(x) for x in re.findall(r"\[(\d+)\]", body)}
    citations = [_citation(b) for b in all_blocks if b.n in used] or [_citation(b) for b in all_blocks]
    effective = "llm" if used_llm_any else "extractive"
    checks = {**check_text(body, citations), "mode": "llm" if used_llm_any else "extractive"}
    if mode == "auto":
        checks["auto_decisions"] = decided_modes
    if fallback:
        checks["fallback"] = "Провайдер не поддерживает генерацию — использован экстрактивный режим"
    if translation_required and not getattr(provider, "supports_chat", False):
        checks["translation_required"] = True
        checks["translation_unavailable"] = "Провайдер не поддерживает перевод языковой моделью"
    return {
        "title": title,
        "body": body,
        "citations": citations,
        "checks": checks,
        "model": provider.chat_model if used_llm_any else "extractive",
        "params": {"mode": effective, "requested_mode": mode, "prompt_version": project["prompt_version"], "kind": kind, "generated_at": utcnow().isoformat()},
    }


def gate_auto_publish(review: dict, publish_mode: str) -> bool:
    """Не пропускает небезопасный пустой, короткий или неверно обогащённый текст даже в full_auto."""
    if review["passed"]:
        return True
    if publish_mode != "full_auto":
        return False
    blocking = (
        "Пустой заголовок",
        "Пустой текст",
        f"Текст слишком короткий (меньше {_MIN_PUBLISHABLE_BODY_CHARS} символов)",
        "Нет источников",
        "Полный текст короткой RSS-заметки не удалось получить",
    )
    return not any(
        reason in blocking or reason.startswith(("Текст, вероятно, не на заданном языке", "Модель сочла сюжет малоинтересным"))
        for reason in review.get("reasons", [])
    )


async def _create_and_fill(
    project: dict,
    ids: list[int],
    kind: str,
    provider,
    created_by: int | None,
    prev_states: dict,
    force_review: bool = False,
) -> int:
    initial_title = await db.fetchone(
        "SELECT string_agg(title, ' · ' ORDER BY score DESC NULLS LAST, id) AS title FROM clusters WHERE id = ANY(%s)",
        (ids,),
    )
    row = await db.fetchone(
        "INSERT INTO drafts(project_id, kind, cluster_ids, title, status, created_by) VALUES (%s,%s,%s,%s,'generating',%s) RETURNING id",
        (project["id"], kind, ids, initial_title["title"] or ("Сборка дайджеста" if kind == "digest" else "Подготовка поста"), created_by),
    )
    draft_id = row["id"]
    await events.notify("draft", draft_id=draft_id, status="generating", project_id=project["id"])
    try:
        res = await generate_for_clusters(project, ids, kind, provider)
    except Exception as e:
        log.exception("generation failed for draft %s", draft_id)
        await db.execute("UPDATE drafts SET status = 'failed', error = %s, updated_at = now() WHERE id = %s", (str(e)[:1000], draft_id))
        for cid in ids:
            await db.execute("UPDATE clusters SET state = %s WHERE id = %s AND state = 'drafted'", (prev_states.get(cid, "open"), cid))
        await events.notify("draft", draft_id=draft_id, status="failed", project_id=project["id"])
        if isinstance(e, LLMUnavailable):
            raise
        return draft_id
    review = automatic_review(
        res["title"],
        res["body"],
        res["citations"],
        res["checks"],
        project["max_length"],
        kind,
        project["language"],
    )
    review["auto_publish_enabled"] = (
        not force_review and project["publish_mode"] in ("auto", "full_auto")
    )
    res["checks"]["automatic_review"] = review
    await db.execute(
        "UPDATE drafts SET title = %s, body = %s, citations = %s, checks = %s, model = %s, params = %s, image_url = %s, status = 'pending_review', updated_at = now() WHERE id = %s",
        (res["title"], res["body"], Jsonb(res["citations"]), Jsonb(res["checks"]), res["model"], Jsonb(res["params"]), _pick_image(res["citations"]), draft_id),
    )
    await events.notify("draft", draft_id=draft_id, status="pending_review", project_id=project["id"])
    if not force_review and project["publish_mode"] in ("auto", "full_auto"):
        # «auto»: публикуем только прошедшие проверки, сомнительные остаются редактору.
        # «full_auto»: обычные замечания не блокируют, но критические проблемы требуют проверки.
        should_publish = gate_auto_publish(review, project["publish_mode"])
        if should_publish:
            try:
                await drafts_service.approve(draft_id, None)
            except drafts_service.NoChannels:
                should_publish = False
                review["passed"] = False
                review["reasons"].append("Нет включённых каналов публикации")
            else:
                if not review["passed"]:
                    review["overridden"] = True  # опубликовано, несмотря на замечания — так решил режим «полностью автоматически»
        if not should_publish or review.get("overridden"):
            await db.execute("UPDATE drafts SET checks = %s WHERE id = %s", (Jsonb(res["checks"]), draft_id))
    return draft_id


async def run_generation(payload: dict) -> list[int]:
    project = await get_project(payload["project_id"])
    if not project:
        return []
    provider = await refresh_provider()
    kind = payload.get("kind", "post")
    explicit = payload.get("cluster_ids")
    if explicit:
        ids = [int(i) for i in explicit]
    else:
        # Пересчёт оценок делает только держатель блокировки проекта: параллельные UPDATE clusters из двух задач давали deadlock.
        async with db.advisory_lock(1, project["id"]) as got:
            if got:
                await refresh_clusters(project)
        top_n = int(payload.get("top_n", 1))
        candidates = await select_clusters(project, top_n)
        ids = [c["id"] for c in candidates]
    if not ids:
        await events.notify("pipeline", project_id=project["id"], stage="nothing_to_publish")
        return []

    # Ждём дозагрузки полного текста статей не дольше ENRICH_WAIT_SECONDS: дальше пишем по тому, что есть.
    pending_sources = []
    if jobs.current_age_seconds() < ENRICH_WAIT_SECONDS:
        pending_sources = await db.fetchall(
            "SELECT DISTINCT i.source_id FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE i.cluster_id = ANY(%s) AND i.status IN ('new','clustered') AND s.type = 'rss' AND i.url <> '' "
            "AND char_length(btrim(i.text)) < %s AND (i.article_content_status = 'pending' OR "
            "(i.article_content_status = 'running' AND i.article_content_started_at > now() - interval '10 minutes'))",
            (ids, MIN_RSS_TEXT_CHARS),
        )
    if pending_sources:
        for source in pending_sources:
            await jobs.enqueue(
                "enrich_articles",
                {"source_id": source["source_id"], "project_id": project["id"], "include_clustered": True},
                dedupe_key=f"article-backfill:{project['id']}:{source['source_id']}",
            )
        raise jobs.Retry(15)
    elif jobs.current_age_seconds() >= ENRICH_WAIT_SECONDS:
        log.info("generation for project %s proceeds without waiting for article enrichment", project["id"])

    # Захватываем кластеры атомарно: параллельные запуски не возьмут один и тот же сюжет дважды.
    prev = {r["id"]: r["state"] for r in await db.fetchall("SELECT id, state FROM clusters WHERE id = ANY(%s)", (ids,))}
    claimed = [r["id"] for r in await db.fetchall("UPDATE clusters SET state = 'drafted' WHERE id = ANY(%s) AND state IN ('open','published','closed') RETURNING id", (ids,))]
    if not claimed:
        return []
    groups = [claimed] if kind == "digest" else [[i] for i in claimed]
    out = []
    for g in groups:
        out.append(
            await _create_and_fill(
                project,
                g,
                kind,
                provider,
                payload.get("user_id"),
                prev,
                force_review=bool(payload.get("force_review")),
            )
        )
    return out


@jobs.handler("generate")
async def generate_job(payload: dict) -> None:
    await run_generation(payload)


async def recover_stuck_generations(timeout_minutes: int = 15) -> int:
    """Черновики, застрявшие в 'generating' дольше блокировки задачи (воркер убит на середине
    генерации, например при перезапуске контейнера): помечаем сбоем и возвращаем сюжет в работу,
    чтобы его можно было сгенерировать заново вручную или по расписанию."""
    stuck = await db.fetchall(
        "UPDATE drafts SET status = 'failed', error = 'Генерация прервана перезапуском обработчика — попробуйте ещё раз', updated_at = now() "
        "WHERE status = 'generating' AND created_at < now() - make_interval(mins => %s) "
        "RETURNING id, project_id, cluster_ids",
        (timeout_minutes,),
    )
    for d in stuck:
        await db.execute("UPDATE clusters SET state = 'open' WHERE id = ANY(%s) AND state = 'drafted'", (d["cluster_ids"],))
        await events.notify("draft", draft_id=d["id"], status="failed", project_id=d["project_id"])
    return len(stuck)
