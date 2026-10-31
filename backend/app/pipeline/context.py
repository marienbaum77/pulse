"""Подготовка контекста для генерации: из кластера берём несколько репрезентативных материалов и в каждом —
предложения, ближайшие к центроиду. Так модель получает 2–5 тыс. токенов вместо полных текстов, а у каждого предложения известен источник."""
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .. import db
from ..providers import LLMUnavailable
from ..textutil import promised_count, split_sentences, truncate
from .clustering_core import unit_vector

log = logging.getLogger("pulse.context")
MAX_CONTEXT_CHARS = 7000
# Пост о видео или статье пишется по одному материалу — ему нужен не «ядро сюжета» из трёх фраз, а заметная часть текста.
CURATED_CONTEXT_SENTENCES = 24
CURATED_SCAN_SENTENCES = 120
# Новость из одного источника: трёх фраз «ядра сюжета» модели мало, и она добирает объём общими словами.
SINGLE_CONTEXT_SENTENCES = 12
SINGLE_SCAN_SENTENCES = 40
# Длинная статья из одного источника (аналитика, разбор, лонгрид): главное в ней не в первых абзацах, а «ядро сюжета»
# считается по заголовку и началу. Из 12 фраз первых 40 модель писала пост по вступлению — например, по новости, с
# которой автор начал рассуждение. Такая статья идёт модели целиком или, если длиннее, началом и выборкой по всему тексту.
LONGREAD_CHARS = 6000
LONGREAD_CONTEXT_CHARS = 12000
LONGREAD_HEAD_SHARE = 0.25  # такую долю объёма занимает начало статьи подряд


def spread_sentences(sentences: list[str], cap: int = LONGREAD_CONTEXT_CHARS) -> list[str]:
    """Предложения статьи в пределах cap знаков: все, если влезают; иначе начало подряд (четверть объёма) и
    равномерная выборка по остальному тексту в исходном порядке. Перед предложением после пропуска стоит «…»."""
    size = lambda s: len(s) + 1  # noqa: E731
    if sum(map(size, sentences)) <= cap:
        return list(sentences)
    head: list[str] = []
    used = 0
    for s in sentences:
        if used + size(s) > cap * LONGREAD_HEAD_SHARE:
            break
        head.append(s)
        used += size(s)
    rest = sentences[len(head):]
    count = max(1, int((cap - used) / (sum(map(size, rest)) / len(rest) + 2)))  # +2 — на «… » перед предложением после пропуска
    picked = sorted({int(i * len(rest) / count) for i in range(count)})
    out, prev = list(head), len(head) - 1
    for j in picked:
        idx = len(head) + j
        sentence = sentences[idx] if idx == prev + 1 else f"… {sentences[idx]}"
        if used + size(sentence) > cap:
            break
        out.append(sentence)
        used += size(sentence)
        prev = idx
    return out


@dataclass
class Block:
    n: int
    item_id: int
    source: str
    title: str
    url: str
    sentences: list[str]
    published_at: datetime
    article_content_status: str = "pending"
    content_length: int = 0
    image_url: str | None = None
    kind: str = "article"
    links: list[dict] | None = None  # первичные ссылки материала (primary_links)
    media: dict | None = None  # первоисточник, картинки и видео статьи (media.analyze); None — страницу не разбирали
    longread: bool = False  # длинная статья из одного источника: в sentences — она целиком или с выборкой по всему тексту

    @property
    def text(self) -> str:
        return " ".join(self.sentences)

    @property
    def output_title(self) -> str:
        return self.title

    @property
    def output_sentences(self) -> list[str]:
        return self.sentences

    @property
    def output_text(self) -> str:
        return " ".join(self.output_sentences)


async def build_blocks(project: dict, cluster_id: int, start_n: int, provider) -> list[Block]:
    cluster = await db.fetchone("SELECT centroid, mode FROM clusters WHERE id = %s", (cluster_id,))
    rows = await db.fetchall(
        "SELECT i.id, i.source_id, i.title, i.url, i.text, i.published_at, i.embedding, i.kind, i.meta->'links' AS links, i.meta->'media' AS media, "
        "i.article_content_status, char_length(btrim(i.text)) AS content_length, i.image_url, "
        "COALESCE(NULLIF(i.publisher_name, ''), s.name, 'Источник') AS source_name, COALESCE(s.authority, 0.5) AS authority "
        "FROM items i LEFT JOIN sources s ON s.id = i.source_id WHERE i.cluster_id = %s AND i.status = 'clustered'",
        (cluster_id,),
    )
    if not rows or cluster is None:
        return []
    centroid = unit_vector(cluster["centroid"])
    rows.sort(key=lambda r: -(float(unit_vector(r["embedding"]) @ centroid) + 0.15 * float(r["authority"])))

    chosen, per_source = [], Counter()
    for r in rows:
        if len(chosen) >= project["context_items"]:
            break
        if per_source[r["source_id"]] >= 2:  # не даём одному источнику забить контекст
            continue
        chosen.append(r)
        per_source[r["source_id"]] += 1

    if len(chosen) == 1 and len(chosen[0]["text"] or "") >= LONGREAD_CHARS:
        r = chosen[0]
        return [Block(start_n, r["id"], r["source_name"], r["title"], r["url"], spread_sentences(split_sentences(r["text"])) or [r["title"]],
                      r["published_at"], r["article_content_status"], r["content_length"], r["image_url"], r["kind"],
                      list(r["links"] or []), r["media"], longread=True)]

    curated = cluster["mode"] == "curated"
    single = not curated and len(chosen) == 1
    if curated:
        k, scan = CURATED_CONTEXT_SENTENCES, CURATED_SCAN_SENTENCES
    elif single:
        k, scan = max(project["context_sentences"], SINGLE_CONTEXT_SENTENCES), SINGLE_SCAN_SENTENCES
    else:
        k, scan = max(1, project["context_sentences"]), 14
    per_item = []
    pool_sentences: list[str] = []
    for r in chosen:
        # Подборка («14 лучших скиллов») держит главное в пунктах далеко от начала: из первых 14 фраз модель видит
        # только вступление и пишет текст, который не сходится с заголовком. Такой материал читается как статья.
        item_k, item_scan = (CURATED_CONTEXT_SENTENCES, CURATED_SCAN_SENTENCES) if promised_count(r["title"]) else (k, scan)
        sents = split_sentences(r["text"])[:item_scan] or [r["title"]]
        rest = sents[1:] if len(sents) > item_k else []
        per_item.append((sents, rest, item_k))
        pool_sentences.extend(rest)

    vecs = None
    if pool_sentences:
        try:
            vecs = await provider.embed(pool_sentences, project_id=project["id"])
        except LLMUnavailable:
            log.warning("sentence embedding unavailable, falling back to lead sentences")

    blocks, offset = [], 0
    cap = max(300, MAX_CONTEXT_CHARS // max(len(chosen), 1))
    for idx, (r, (sents, rest, item_k)) in enumerate(zip(chosen, per_item)):
        if rest and vecs is not None:
            sims = [float(unit_vector(v) @ centroid) for v in vecs[offset : offset + len(rest)]]
            top = sorted(np.argsort(sims)[::-1][: item_k - 1])
            picked = [sents[0]] + [rest[j] for j in top]
        else:
            picked = sents[:item_k]
        offset += len(rest)
        text = truncate(" ".join(picked), cap)
        blocks.append(
            Block(
                start_n + idx,
                r["id"],
                r["source_name"],
                r["title"],
                r["url"],
                split_sentences(text) or [text],
                r["published_at"],
                r["article_content_status"],
                r["content_length"],
                r["image_url"],
                r["kind"],
                list(r["links"] or []),
                r["media"],
            )
        )
    return blocks
