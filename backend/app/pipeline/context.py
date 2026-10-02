"""Подготовка контекста для генерации: из кластера берём несколько репрезентативных материалов и в каждом —
предложения, ближайшие к центроиду. Так модель получает 2–5 тыс. токенов вместо полных текстов, а у каждого предложения известен источник."""
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .. import db
from ..providers import LLMUnavailable
from ..textutil import split_sentences, truncate
from .clustering_core import unit_vector

log = logging.getLogger("pulse.context")
MAX_CONTEXT_CHARS = 7000


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
    cluster = await db.fetchone("SELECT centroid FROM clusters WHERE id = %s", (cluster_id,))
    rows = await db.fetchall(
        "SELECT i.id, i.source_id, i.title, i.url, i.text, i.published_at, i.embedding, "
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

    k = max(1, project["context_sentences"])
    per_item = []
    pool_sentences: list[str] = []
    for r in chosen:
        sents = split_sentences(r["text"])[:14] or [r["title"]]
        rest = sents[1:] if len(sents) > k else []
        per_item.append((sents, rest))
        pool_sentences.extend(rest)

    vecs = None
    if pool_sentences:
        try:
            vecs = await provider.embed(pool_sentences, project_id=project["id"])
        except LLMUnavailable:
            log.warning("sentence embedding unavailable, falling back to lead sentences")

    blocks, offset = [], 0
    cap = max(300, MAX_CONTEXT_CHARS // max(len(chosen), 1))
    for idx, (r, (sents, rest)) in enumerate(zip(chosen, per_item)):
        if rest and vecs is not None:
            sims = [float(unit_vector(v) @ centroid) for v in vecs[offset : offset + len(rest)]]
            top = sorted(np.argsort(sims)[::-1][: k - 1])
            picked = [sents[0]] + [rest[j] for j in top]
        else:
            picked = sents[:k]
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
            )
        )
    return blocks
