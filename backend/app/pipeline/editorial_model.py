"""Локальная редакционная модель: эталонные посты, решения редактора и замкнутая математика без генеративной модели."""
import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime

import numpy as np
from psycopg.types.json import Jsonb

from .. import db, jobs
from ..providers import get_provider
from ..textutil import utcnow
from . import ingest, scoring, stats
from .connectors import telegram

log = logging.getLogger("pulse.editorial")
FEATURE_NAMES = (
    "taste", "sim_pos_top3", "sim_neg_top3", "contrast_margin", "recent_published_sim",
    "coverage", "velocity", "topic_fit", "source_perf", "anchor", "criteria_fit",
)
FEATURE_LABELS = {
    "taste": "вкус эталона", "sim_pos_top3": "сходство с позитивами", "sim_neg_top3": "сходство с негативами",
    "contrast_margin": "контраст примеров", "recent_published_sim": "повтор публикации", "coverage": "охват",
    "velocity": "скорость", "topic_fit": "близость к теме", "source_perf": "результативность источника",
    "anchor": "масштаб события", "criteria_fit": "критерии канала",
}
NEGATIVE_KINDS = frozenset({"reference_ad", "reference_weak", "editor_rejected"})
SOURCE_PRIORITY = {"stream_unlabeled": 0, "reference_weak": 1, "reference_ad": 1, "reference_channel": 1,
                   "editor_rejected": 2, "editor_approved": 2}
DEFAULT_DUP_SIM = 0.80
DEFAULT_MATCH_SIM = 0.80
DEFAULT_NEG_MARGIN = 0.03
MAX_STATE_BYTES = 20_000
MAX_CHANNELS = 5
MAX_POSTS_PER_CHANNEL = 300
EMBED_BATCH = 64
MIN_CLASS_EXAMPLES = 5
_BANK_CACHE: dict[tuple[int, str], dict] = {}

_URL = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.I)
_MARKUP = re.compile(r"[*_~`#\[\]<>()]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D]+")
_AD = re.compile(r"erid|#реклама|#партнерский|#партнёрский|t\.me/\+|бот\s+в\s+закрепе|промокод", re.I)
_SCALE = re.compile(r"\b(?:рекорд\w*|перв(?:ый|ого|ому)|#1|бенчмарк\w*|benchmark\w*)\b|\d+(?:[.,]\d+)?\s*(?:%|млн|млрд|тыс\.?|[kmb]\b|ГБ|ТБ|GB|TB|ГГц|GHz)", re.I)
_SELF_PROMO = re.compile(r"\b(?:мой|моя|моё|наш|наша|наше)\s+(?:репозитори\w*|сервис\w*|курс\w*|канал\w*)|\b(?:my|our)\s+(?:repo\w*|service|course|channel)\b", re.I)


def _unit(vec) -> np.ndarray | None:
    if vec is None:
        return None
    a = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(a))
    return a / norm if norm > 0 else None


def _normalize_rows(vectors) -> np.ndarray:
    x = np.asarray(vectors, dtype=np.float32)
    if x.ndim == 1:
        x = x.reshape(1, -1)
    return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)


def extract_hook_text(text: str) -> str:
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    first = _EMOJI.sub("", _MARKUP.sub("", _URL.sub("", first)))
    first = re.sub(r"\s+", " ", first).strip(" ,:;—–-|")
    if len(first) <= 140:
        return first
    cut = first[:140].rsplit(" ", 1)[0]
    return cut.rstrip(" ,:;—–-") + "…"


def is_ad(text: str) -> bool:
    return bool(_AD.search(text or ""))


def anchor_features(text: str) -> tuple[float, float]:
    scale = min(1.0, len(_SCALE.findall(text or "")) / 2)
    promo = 1.0 if _SELF_PROMO.search(text or "") else 0.0
    return scale, promo


def classify_reference_posts(posts: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    settled = [p for p in posts if p.get("views") is not None
               and (now - p["published_at"]).total_seconds() >= stats.SETTLED_HOURS * 3600]
    enough = len(settled) >= 60
    out = []
    for post in posts:
        text = post.get("text") or post.get("title") or ""
        hook = extract_hook_text(text)
        if not hook:
            continue
        meta = dict(post.get("meta") or {})
        if post.get("views") is not None:
            meta["views"] = post["views"]
        meta["published_at"] = post["published_at"].isoformat()
        row = {"hook_text": hook, "external_url": post.get("url"), "meta": meta}
        if is_ad(text):
            out.append({**row, "label": 0, "source_kind": "reference_ad", "sample_weight": 2.0})
            continue
        if post not in settled:
            continue
        if not enough:
            out.append({**row, "label": 1, "source_kind": "reference_channel", "sample_weight": 1.0})
            continue
        pos = settled.index(post)
        neighbors = [p["views"] for p in settled[max(0, pos - 10):pos + 11] if p is not post]
        if not neighbors:
            continue
        rel = post["views"] / max(float(np.median(neighbors)), 1.0)
        row["meta"]["relative"] = rel
        if rel >= 1.0:
            out.append({**row, "label": 1, "source_kind": "reference_channel", "sample_weight": min(2.5, max(1.0, rel))})
        elif rel <= 0.6:
            out.append({**row, "label": 0, "source_kind": "reference_weak", "sample_weight": 0.8})
    return out


def _weighted_mean(x: np.ndarray, weights: np.ndarray) -> np.ndarray:
    total = float(weights.sum())
    return np.average(x, axis=0, weights=weights) if total > 0 else np.zeros(x.shape[1], dtype=np.float64)


def fit_fisher(x, y, sample_weight=None, alpha: float = 0.2, pi: float = 0.3) -> tuple[np.ndarray, float]:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.int8)
    weights = np.ones(len(y), dtype=np.float64) if sample_weight is None else np.asarray(sample_weight, dtype=np.float64)
    pos, neg = y == 1, y == 0
    if not pos.any() or not neg.any():
        return np.zeros(x.shape[1]), math.log(pi / (1 - pi))
    mu1, mu0 = _weighted_mean(x[pos], weights[pos]), _weighted_mean(x[neg], weights[neg])
    centered = np.vstack((x[pos] - mu1, x[neg] - mu0))
    within_weights = np.concatenate((weights[pos], weights[neg]))
    covariance = (centered * within_weights[:, None]).T @ centered / max(float(within_weights.sum()), 1e-12)
    d = x.shape[1]
    shrink = float(np.trace(covariance)) / max(d, 1)
    regularized = (1 - alpha) * covariance + alpha * shrink * np.eye(d)
    regularized += np.eye(d) * 1e-8
    try:
        w = np.linalg.solve(regularized, mu1 - mu0)
    except np.linalg.LinAlgError:
        w = np.linalg.pinv(regularized) @ (mu1 - mu0)
    b = -0.5 * float((mu1 + mu0) @ w) + math.log(pi / (1 - pi))
    return np.asarray(w, dtype=np.float32), float(b)


def _log_loss(y: np.ndarray, raw: np.ndarray) -> float:
    p = 1 / (1 + np.exp(-np.clip(raw, -30, 30)))
    return -float(np.mean(y * np.log(np.maximum(p, 1e-9)) + (1 - y) * np.log(np.maximum(1 - p, 1e-9))))


def _folds(y: np.ndarray, k: int = 5) -> list[np.ndarray]:
    rng = np.random.default_rng(17)
    buckets = [[] for _ in range(k)]
    for label in (0, 1):
        indices = np.flatnonzero(y == label)
        rng.shuffle(indices)
        for n, idx in enumerate(indices):
            buckets[n % k].append(int(idx))
    return [np.asarray(sorted(bucket), dtype=int) for bucket in buckets if bucket]


def fit_taste(x, y, sample_weight=None, pi: float = 0.3, alphas=(0.05, 0.2, 0.5, 0.8, 1.0)) -> dict:
    x = _normalize_rows(x).astype(np.float64)
    y = np.asarray(y, dtype=np.int8)
    weights = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, dtype=np.float64)
    if len(np.unique(y)) < 2:
        w, b = fit_fisher(x, y, weights, pi=pi)
        return {"w": w, "b": b, "alpha": 1.0, "oof": np.full(len(y), b)}
    folds = _folds(y, min(5, max(1, min(int((y == 0).sum()), int((y == 1).sum())))))
    if not folds:
        w, b = fit_fisher(x, y, weights, pi=pi)
        return {"w": w, "b": b, "alpha": 1.0, "oof": np.full(len(y), b)}
    losses = {}
    for alpha in alphas:
        oof = np.zeros(len(y))
        for val in folds:
            train = np.setdiff1d(np.arange(len(y)), val)
            if len(np.unique(y[train])) < 2:
                oof[val] = math.log(pi / (1 - pi))
                continue
            w, b = fit_fisher(x[train], y[train], weights[train], alpha, pi)
            oof[val] = x[val] @ w + b
        losses[alpha] = _log_loss(y, oof)
    alpha = min(losses, key=losses.get)
    oof = np.zeros(len(y))
    for val in folds:
        train = np.setdiff1d(np.arange(len(y)), val)
        if len(np.unique(y[train])) < 2:
            oof[val] = math.log(pi / (1 - pi))
            continue
        w, b = fit_fisher(x[train], y[train], weights[train], alpha, pi)
        oof[val] = x[val] @ w + b
    w, b = fit_fisher(x, y, weights, alpha, pi)
    return {"w": w, "b": b, "alpha": alpha, "oof": oof}


def _sigmoid(value):
    return 1.0 / (1.0 + np.exp(-np.clip(value, -40, 40)))


def fit_logistic(x, y, sample_weight=None, l2: float = 5.0, max_iter: int = 40) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    weights = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, dtype=np.float64)
    design = np.column_stack((np.ones(len(x)), x))
    beta = np.zeros(design.shape[1])
    penalty = np.eye(design.shape[1]) * l2
    penalty[0, 0] = 0
    for _ in range(max_iter):
        p = _sigmoid(design @ beta)
        gradient = design.T @ (weights * (y - p)) - penalty @ beta
        variance = np.maximum(p * (1 - p), 1e-5) * weights
        hessian = (design * variance[:, None]).T @ design + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        beta += step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return beta


def fit_platt(raw, y, sample_weight=None) -> tuple[float, float]:
    raw = np.asarray(raw, dtype=np.float64).reshape(-1, 1)
    if len(raw) == 0 or len(np.unique(y)) < 2:
        return 1.0, 0.0
    beta = fit_logistic(raw, y, sample_weight, l2=0.01)
    return float(beta[1]), float(beta[0])


def choose_t8(probabilities, labels, gold_mask, min_precision: float = 0.90, min_hits: int = 10) -> float | None:
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(labels, dtype=int)
    gold = np.asarray(gold_mask, dtype=bool)
    candidates = sorted(set(float(v) for v in p[gold]))
    for threshold in candidates:
        selected = gold & (p >= threshold)
        hits = int(selected.sum())
        if hits >= min_hits and float(y[selected].mean()) >= min_precision:
            return threshold
    return None


def probability_to_score(probability: float, median: float, t8: float | None) -> float:
    p = min(1.0, max(0.0, float(probability)))
    middle = min(0.999, max(0.001, float(median)))
    points = [(0.0, 0.0), (middle, 5.0)]
    if t8 is not None and t8 > middle:
        points.append((min(0.9999, t8), 8.0))
    points.append((1.0, 10.0 if t8 is not None else 7.0))
    points = sorted(points)
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if p <= x1:
            return y0 + (p - x0) * (y1 - y0) / max(x1 - x0, 1e-9)
    return points[-1][1]


def loo_top3(bank_vectors, index: int, wanted: np.ndarray, cluster_ids=None) -> float:
    if not wanted.any():
        return 0.0
    vectors = _normalize_rows(bank_vectors)
    sims = vectors @ vectors[index]
    allowed = wanted.copy()
    allowed[index] = False
    if cluster_ids is not None and cluster_ids[index] is not None:
        allowed &= np.asarray([cid != cluster_ids[index] for cid in cluster_ids])
    values = sorted((float(sims[i]) for i in np.flatnonzero(allowed)), reverse=True)[:3]
    return float(np.mean(values)) if values else 0.0


def top3_similarity(bank_vectors, vec, wanted: np.ndarray, exclude: np.ndarray | None = None) -> tuple[float, int | None]:
    indices = np.flatnonzero(wanted)
    if exclude is not None:
        indices = indices[~exclude[indices]]
    if not len(indices):
        return 0.0, None
    unit = _unit(vec)
    if unit is None:
        return 0.0, None
    matrix = _normalize_rows(bank_vectors[indices])
    sims = matrix @ unit
    order = np.argsort(-sims)[:3]
    best = int(indices[int(order[0])])
    return float(np.mean(sims[order])), best


def average_precision(y, scores) -> float:
    y = np.asarray(y, dtype=int)
    order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")
    positives = int(y.sum())
    if not positives:
        return 0.0
    hits = 0
    total = 0.0
    for rank, idx in enumerate(order, 1):
        if y[idx]:
            hits += 1
            total += hits / rank
    return total / positives


def roc_auc(y, scores) -> float:
    y = np.asarray(y, dtype=int)
    n_pos, n_neg = int(y.sum()), int((1 - y).sum())
    if not n_pos or not n_neg:
        return 0.0
    order = np.argsort(np.asarray(scores), kind="stable")
    ranks = np.empty(len(y), dtype=float)
    sorted_scores = np.asarray(scores)[order]
    start = 0
    while start < len(y):
        end = start + 1
        while end < len(y) and sorted_scores[end] == sorted_scores[start]:
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2
        start = end
    return (float(ranks[y == 1].sum()) - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def fallback_score(feats: dict) -> tuple[float, dict[str, float]]:
    anchor, promo = anchor_features(str(feats.get("title") or ""))
    values = {
        "topic_fit": float(feats.get("topic_fit", 0.5)),
        "coverage": float(feats.get("coverage", 0.0)),
        "anchor": float(feats.get("anchor", anchor)),
        "criteria_fit": float(feats.get("criteria_fit", 0.5)),
        "self_promo": promo,
    }
    score = 10 * (0.35 * values["topic_fit"] + 0.20 * values["coverage"] + 0.20 * values["anchor"]
                  + 0.25 * max(0.0, values["criteria_fit"]) - 0.35 * values["self_promo"])
    return min(6.5, max(0.0, score)), values


def score_features(state: dict, bank: dict, vec, feats: dict) -> tuple[float, dict[str, float]]:
    unit = _unit(vec)
    w_taste = np.asarray(state.get("w_taste") or [], dtype=np.float32)
    if state.get("stage") == "untrained" or unit is None or not len(w_taste) or unit.shape != w_taste.shape:
        return fallback_score(feats)
    ablation = state.get("ablation")
    matrix = np.asarray(bank.get("vectors", np.empty((0, len(unit)))), dtype=np.float32)
    labels = np.asarray(bank.get("labels") or [], dtype=int)
    kinds = np.asarray(bank.get("source_kinds") or [], dtype=object)
    valid = matrix.ndim == 2 and len(matrix) == len(labels) and matrix.shape[1] == len(unit)
    if valid and len(matrix) and ablation != "knn":
        pos_sim, pos_idx = top3_similarity(matrix, unit, labels == 1)
        neg_wanted = np.asarray([kind in NEGATIVE_KINDS for kind in kinds])
        neg_sim, neg_idx = top3_similarity(matrix, unit, neg_wanted)
    else:
        pos_sim = neg_sim = 0.0
        pos_idx = neg_idx = None
    taste_raw = float(unit @ w_taste + float(state.get("b", 0.0)))
    values = dict(feats)
    if ablation == "anchor":
        values["anchor"] = 0.0
    values.update({"taste": taste_raw, "sim_pos_top3": pos_sim, "sim_neg_top3": neg_sim,
                   "contrast_margin": pos_sim - neg_sim})
    if state.get("stage") == "AB" and state.get("stage_b_weights"):
        means = np.asarray(state["feature_mean"], dtype=float)
        scales = np.asarray(state["feature_scale"], dtype=float)
        coefficients = np.asarray(state["stage_b_weights"], dtype=float)
        row = np.asarray([float(values.get(name, 0.0)) for name in FEATURE_NAMES])
        standardized = (row - means) / np.maximum(scales, 1e-6)
        raw = float(state.get("stage_b_intercept", 0.0)) + float(standardized @ coefficients)
        contributions = {name: float(standardized[i] * coefficients[i]) for i, name in enumerate(FEATURE_NAMES)}
    else:
        raw = taste_raw + 2 * values["contrast_margin"]
        contributions = {"taste": taste_raw, "contrast_margin": 2 * values["contrast_margin"]}
    a, b = state.get("platt", [1.0, 0.0])
    probability = float(_sigmoid(float(a) * raw + float(b)))
    score = probability_to_score(probability, float(state.get("scale_median", 0.5)), state.get("t8"))
    if state.get("stage") != "AB" or state.get("t8") is None:
        score = min(score, 7.0)
    if neg_sim - pos_sim > float(state.get("NEG_MARGIN", DEFAULT_NEG_MARGIN)):
        score = min(score, 4.0)
    contributions.update({"_probability": probability, "_pos_idx": pos_idx, "_neg_idx": neg_idx,
                          "_pos_sim": pos_sim, "_neg_sim": neg_sim})
    return min(10.0, max(0.0, score)), contributions


def _compact_state(state: dict) -> dict:
    result = dict(state)
    for precision in (5, 4, 3, 2, 1):
        for key in ("w_taste", "criteria_vector"):
            if result.get(key) is not None:
                result[key] = [round(float(v), precision) for v in result[key]]
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= MAX_STATE_BYTES:
            return result
    raise ValueError("Состояние редакционной модели превышает 20 КБ")


def serialized_state_size(state: dict) -> int:
    return len(json.dumps(_compact_state(state), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def train_model(examples: list[dict], vectors, feature_rows: dict[int, dict], criteria_vector=None,
                *, dup_sim: float = DEFAULT_DUP_SIM, match_sim: float = DEFAULT_MATCH_SIM,
                neg_margin: float = DEFAULT_NEG_MARGIN, pi: float = 0.3, ablate: str | None = None) -> dict:
    if ablate not in {None, "anchor", "knn", "taste_only"}:
        raise ValueError(f"Неизвестный вариант ablation: {ablate}")
    x = _normalize_rows(vectors)
    y = np.asarray([int(e["label"]) for e in examples], dtype=np.int8)
    weights = np.asarray([float(e.get("sample_weight", 1.0)) for e in examples])
    taste = fit_taste(x, y, weights, pi=pi)
    labels = y
    kinds = [e["source_kind"] for e in examples]
    cluster_ids = [e.get("cluster_id") for e in examples]
    rows = sorted(feature_rows)
    matrix = []
    for idx in rows:
        if ablate == "knn":
            pos = neg = 0.0
        else:
            pos = loo_top3(x, idx, labels == 1, cluster_ids)
            neg = loo_top3(x, idx, np.asarray([kind in NEGATIVE_KINDS for kind in kinds]), cluster_ids)
        values = dict(feature_rows[idx])
        if ablate == "anchor":
            values["anchor"] = 0.0
        values.update({"taste": float(taste["oof"][idx]), "sim_pos_top3": pos, "sim_neg_top3": neg,
                       "contrast_margin": pos - neg})
        matrix.append([float(values.get(name, 0.0)) for name in FEATURE_NAMES])
    gold_all = np.asarray([e["source_kind"] in {"editor_approved", "editor_rejected"} for e in examples])
    gold_rows = [n for n, idx in enumerate(rows) if gold_all[idx]]
    val_rows: list[int] = []
    if gold_rows:
        ordered_gold = sorted(gold_rows, key=lambda n: examples[rows[n]].get("created_at") or "")
        count = min(len(ordered_gold), max(20, int(math.ceil(0.2 * len(ordered_gold)))))
        chosen = set(ordered_gold[-count:])
        cutoff = min(examples[rows[n]].get("created_at") or "" for n in chosen)
        val_rows = [n for n in range(len(rows)) if n in chosen or (examples[rows[n]].get("created_at") or "") >= cutoff]
    elif rows:
        val_rows = list(range(max(0, len(rows) - max(1, int(math.ceil(0.2 * len(rows))))), len(rows)))
    validation = set(val_rows)
    fit_rows = [n for n in range(len(rows)) if n not in validation]
    labeled = [n for n in fit_rows if examples[rows[n]]["source_kind"] != "stream_unlabeled"]
    n_pos = sum(int(y[rows[n]]) for n in labeled)
    n_neg = len(labeled) - n_pos
    trained = min(int((y == 1).sum()), int((y == 0).sum())) >= MIN_CLASS_EXAMPLES
    stage = "A_only" if trained else "untrained"
    means = np.zeros(len(FEATURE_NAMES))
    scales = np.ones(len(FEATURE_NAMES))
    coefficients = np.zeros(len(FEATURE_NAMES))
    intercept = 0.0
    raw_all = taste["oof"][rows] + 2 * np.asarray([row[3] for row in matrix]) if matrix else np.asarray([])
    if matrix and n_pos >= 30 and n_neg >= 30 and ablate != "taste_only":
        stage = "AB"
        full = np.asarray(matrix, dtype=np.float64)
        fit = full[fit_rows]
        means, scales = fit.mean(axis=0), fit.std(axis=0)
        scales = np.maximum(scales, 1e-3)
        design = (full - means) / scales
        fit_indices = np.asarray(rows)[fit_rows]
        beta = fit_logistic(design[fit_rows], y[fit_indices], weights[fit_indices], l2=8.0)
        intercept, coefficients = float(beta[0]), beta[1:]
        raw_all = intercept + design @ coefficients
    if val_rows:
        val_indices = np.asarray(rows)[val_rows]
        val_y = y[val_indices]
        val_raw = np.asarray(raw_all)[val_rows]
        val_weights = weights[val_indices]
        platt_a, platt_b = fit_platt(val_raw, val_y, val_weights)
        val_p = _sigmoid(platt_a * val_raw + platt_b)
        val_gold = gold_all[val_indices]
        t8 = choose_t8(val_p, val_y, val_gold)
        median = float(np.median(_sigmoid(np.asarray(raw_all))))
        gold_p = val_p[val_gold]
        gold_y = val_y[val_gold]
        selected = gold_p >= t8 if t8 is not None else np.zeros(len(gold_p), dtype=bool)
        precision = float(gold_y[selected].mean()) if selected.any() else None
        hits = int(selected.sum())
        if hits:
            z = 1.96
            denominator = 1 + z * z / hits
            centre = (precision + z * z / (2 * hits)) / denominator
            margin = z * math.sqrt(precision * (1 - precision) / hits + z * z / (4 * hits * hits)) / denominator
            precision_ci = [max(0.0, centre - margin), min(1.0, centre + margin)]
        else:
            precision_ci = None
        pr_auc = average_precision(gold_y, gold_p) if len(gold_y) else 0.0
        auc = roc_auc(gold_y, gold_p) if len(np.unique(gold_y)) == 2 else 0.0
    else:
        platt_a, platt_b, t8, median, precision, precision_ci, pr_auc, auc = 1.0, 0.0, None, 0.5, None, None, 0.0, 0.0
    top = sorted(({"name": name, "weight": round(float(coefficients[i] * scales[i]), 5)}
                  for i, name in enumerate(FEATURE_NAMES)), key=lambda r: -abs(r["weight"]))[:6]
    state = {
        "version": "", "trained_at": "", "dim": int(x.shape[1]), "stage": stage, "ablation": ablate,
        "w_taste": taste["w"].tolist(), "b": float(taste["b"]), "alpha": float(taste["alpha"]), "pi": pi,
        "stage_b_weights": coefficients.tolist() if stage == "AB" else [],
        "stage_b_intercept": intercept, "feature_mean": means.tolist(), "feature_scale": scales.tolist(),
        "platt": [platt_a, platt_b], "t8": t8, "scale_median": median,
        "DUP_SIM": dup_sim, "MATCH_SIM": match_sim, "NEG_MARGIN": neg_margin,
        "criteria_vector": _unit(criteria_vector).tolist() if _unit(criteria_vector) is not None else [],
        "metrics": {"pr_auc": float(pr_auc), "roc_auc": float(auc), "precision_at_8": precision,
                    "precision_at_8_ci": precision_ci, "n_pos": int((y == 1).sum()), "n_neg": int((y == 0).sum()),
                    "n_gold": int(gold_all.sum()), "n_val": len(val_rows)},
        "top_features": top,
    }
    return _compact_state(state)


async def _cluster_vectors(cluster_ids: list[int]) -> dict[int, dict]:
    if not cluster_ids:
        return {}
    clusters = await db.fetchall(
        "SELECT id, project_id, title, centroid, source_count, item_count, last_seen FROM clusters WHERE id = ANY(%s)",
        (list(set(cluster_ids)),),
    )
    items = await db.fetchall(
        "SELECT cluster_id, title_embedding FROM items WHERE cluster_id = ANY(%s) AND status = 'clustered' "
        "AND title_embedding IS NOT NULL ORDER BY cluster_id, published_at DESC, id DESC",
        (list({r["id"] for r in clusters}),),
    )
    grouped: dict[int, list] = {r["id"]: [] for r in clusters}
    for item in items:
        if len(grouped[item["cluster_id"]]) < 8:
            grouped[item["cluster_id"]].append(item["title_embedding"])
    out = {}
    for row in clusters:
        vectors = [_unit(v) for v in grouped[row["id"]]]
        vectors = [v for v in vectors if v is not None]
        vec = _unit(np.mean(vectors, axis=0)) if vectors else None
        out[row["id"]] = {**row, "vector": vec}
    return out


async def cluster_vector(cluster_id: int) -> np.ndarray | None:
    row = (await _cluster_vectors([cluster_id])).get(cluster_id)
    return row["vector"] if row else None


async def _upsert_example(project_id: int, example: dict, vector=None) -> None:
    await db.execute(
        "INSERT INTO editorial_examples(project_id, label, source_kind, hook_text, hook_embedding, sample_weight, cluster_id, external_url, meta) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, md5(hook_text)) DO UPDATE SET "
        "label = EXCLUDED.label, hook_embedding = EXCLUDED.hook_embedding, sample_weight = EXCLUDED.sample_weight, "
        "cluster_id = EXCLUDED.cluster_id, external_url = EXCLUDED.external_url, meta = EXCLUDED.meta, source_kind = EXCLUDED.source_kind "
        "WHERE CASE editorial_examples.source_kind "
        "WHEN 'editor_approved' THEN 2 WHEN 'editor_rejected' THEN 2 WHEN 'stream_unlabeled' THEN 0 ELSE 1 END < %s",
        (project_id, example["label"], example["source_kind"], example["hook_text"], vector,
         example.get("sample_weight", 1.0), example.get("cluster_id"), example.get("external_url"),
         Jsonb(example.get("meta") or {}), SOURCE_PRIORITY[example["source_kind"]]),
    )


async def _reference_examples(channel: str) -> list[dict]:
    url = f"https://t.me/s/{channel}"
    posts: dict[str, dict] = {}
    for _ in range(30):
        response = await ingest.http_get(url, {"User-Agent": ingest.BROWSER_UA})
        response.raise_for_status()
        page = telegram.parse_channel_page(response.text, channel)
        if not page:
            break
        for post in page:
            views = (post.get("meta") or {}).get("views")
            post["views"] = views
            posts[post["url"]] = post
        if len(posts) >= MAX_POSTS_PER_CHANNEL:
            break
        oldest = min(int(p["url"].rsplit("/", 1)[1]) for p in page)
        url = f"https://t.me/s/{channel}?before={oldest}"
        await asyncio.sleep(0.35)
    newest = sorted(posts.values(), key=lambda p: p["published_at"], reverse=True)[:MAX_POSTS_PER_CHANNEL]
    return classify_reference_posts(sorted(newest, key=lambda p: p["published_at"]))


async def sync_examples(project_id: int) -> dict:
    project = await db.fetchone("SELECT * FROM projects WHERE id = %s", (project_id,))
    if not project:
        return {"reference": 0, "editor": 0, "stream": 0}
    provider = get_provider()
    reference_count = 0
    channels = []
    for value in (project.get("reference_tg_channels") or [])[:MAX_CHANNELS]:
        try:
            channels.append(telegram.normalize_channel(value))
        except ValueError as e:
            log.info("project %s reference channel skipped: %s", project_id, e)
    for channel in channels:
        try:
            examples = await _reference_examples(channel)
            for start in range(0, len(examples), EMBED_BATCH):
                batch = examples[start:start + EMBED_BATCH]
                vectors = await provider.embed([e["hook_text"] for e in batch], project_id=project_id)
                for example, vector in zip(batch, vectors):
                    await _upsert_example(project_id, example, vector)
            reference_count += len(examples)
        except Exception as e:
            log.info("reference channel %s not synchronized: %s", channel, e)
    rejected = await db.fetchall(
        "SELECT id, cluster_ids, created_at FROM drafts WHERE project_id = %s AND status = 'rejected' "
        "AND cardinality(cluster_ids) > 0", (project_id,),
    )
    approved = await db.fetchall(
        "SELECT DISTINCT d.id, d.cluster_ids, d.created_at FROM drafts d JOIN publications p ON p.draft_id = d.id "
        "WHERE d.project_id = %s AND p.status = 'sent' AND cardinality(d.cluster_ids) > 0", (project_id,),
    )
    relatives = {p["draft_id"]: p["relative"] for p in await stats.settled_posts(project_id)}
    decisions = [("editor_rejected", row, 3.0) for row in rejected]
    decisions += [("editor_approved", row, 3.0 * min(1.5, max(0.5, relatives.get(row["id"], 1.0)))) for row in approved]
    vectors_by_cluster = await _cluster_vectors([row["cluster_ids"][0] for _, row, _ in decisions])
    editor_count = 0
    for kind, row, weight in decisions:
        cid = row["cluster_ids"][0]
        cluster = vectors_by_cluster.get(cid)
        if not cluster or cluster["vector"] is None:
            continue
        example = {"label": int(kind == "editor_approved"), "source_kind": kind, "hook_text": extract_hook_text(cluster["title"]),
                   "sample_weight": weight, "cluster_id": cid, "meta": {"draft_id": row["id"]}}
        if example["hook_text"]:
            await _upsert_example(project_id, example, cluster["vector"])
            editor_count += 1
    positives = await db.fetchall(
        "SELECT hook_embedding FROM editorial_examples WHERE project_id = %s AND label = 1 AND hook_embedding IS NOT NULL",
        (project_id,),
    )
    stream_count = 0
    if positives:
        limit = 3 * len(positives)
        stream = await db.fetchall(
            "SELECT c.id, c.title FROM clusters c WHERE c.project_id = %s AND c.last_seen < now() - interval '3 days' "
            "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE c.id = ANY(d.cluster_ids)) "
            "ORDER BY c.last_seen DESC LIMIT %s", (project_id, limit),
        )
        stream_vectors = await _cluster_vectors([r["id"] for r in stream])
        positive_matrix = _normalize_rows([r["hook_embedding"] for r in positives])
        dup_sim = float(os.getenv("DUP_SIM", DEFAULT_DUP_SIM))
        for row in stream:
            cluster = stream_vectors.get(row["id"])
            if not cluster or cluster["vector"] is None:
                continue
            if float(np.max(positive_matrix @ cluster["vector"])) >= dup_sim:
                continue
            hook = extract_hook_text(row["title"])
            if not hook:
                continue
            await _upsert_example(project_id, {"label": 0, "source_kind": "stream_unlabeled", "hook_text": hook,
                                               "sample_weight": 0.3, "cluster_id": row["id"]}, cluster["vector"])
            stream_count += 1
    return {"reference": reference_count, "editor": editor_count, "stream": stream_count}


async def _feature_data(project_id: int, cluster_ids: list[int]) -> dict[int, dict]:
    clusters = await _cluster_vectors(cluster_ids)
    if not clusters:
        return {}
    items = await db.fetchall(
        "SELECT i.cluster_id, i.topic_score, i.published_at, s.performance FROM items i "
        "LEFT JOIN sources s ON s.id = i.source_id WHERE i.cluster_id = ANY(%s) AND i.status = 'clustered'",
        (list(clusters),),
    )
    published = await db.fetchall(
        "SELECT published_centroid FROM clusters WHERE project_id = %s AND published_centroid IS NOT NULL "
        "AND published_at > now() - interval '72 hours'", (project_id,),
    )
    published_matrix = _normalize_rows([r["published_centroid"] for r in published]) if published else np.empty((0, 0))
    by_cluster: dict[int, list] = {cid: [] for cid in clusters}
    for item in items:
        by_cluster[item["cluster_id"]].append(item)
    now = utcnow()
    out = {}
    for cid, cluster in clusters.items():
        rows = by_cluster[cid]
        topic_values = [float(r["topic_score"]) for r in rows if r["topic_score"] is not None]
        performance = [float(r["performance"]) for r in rows if r["performance"] is not None]
        recent = sum((now - r["published_at"]).total_seconds() <= 6 * 3600 for r in rows)
        centroid = _unit(cluster.get("centroid"))
        if centroid is None:
            centroid = cluster["vector"]
        recent_sim = 0.0
        if centroid is not None and len(published_matrix) and published_matrix.shape[1] == len(centroid):
            recent_sim = max(0.0, float(np.max(published_matrix @ centroid)))
        out[cid] = {
            "recent_published_sim": recent_sim,
            "coverage": scoring.coverage(cluster["source_count"]),
            "velocity": scoring.velocity(recent),
            "topic_fit": float(np.mean(topic_values)) if topic_values else 0.5,
            "source_perf": float(np.mean(performance)) if performance else 1.0,
        }
    return out


async def load_bank(project_id: int, version: str) -> dict:
    key = (project_id, version)
    if key in _BANK_CACHE:
        return _BANK_CACHE[key]
    rows = await db.fetchall(
        "SELECT id, label, source_kind, hook_text, hook_embedding, cluster_id FROM editorial_examples "
        "WHERE project_id = %s AND hook_embedding IS NOT NULL ORDER BY id", (project_id,),
    )
    dim_row = next((r for r in rows if r["hook_embedding"] is not None), None)
    dim = len(dim_row["hook_embedding"]) if dim_row else 0
    rows = [r for r in rows if len(r["hook_embedding"]) == dim]
    bank = {"vectors": np.asarray([r["hook_embedding"] for r in rows], dtype=np.float32).reshape(-1, dim),
            "labels": [r["label"] for r in rows], "source_kinds": [r["source_kind"] for r in rows],
            "hooks": [r["hook_text"] for r in rows], "cluster_ids": [r["cluster_id"] for r in rows]}
    _BANK_CACHE.clear()
    _BANK_CACHE[key] = bank
    return bank


def local_reason(score: float, contributions: dict, bank: dict, fallback: bool = False) -> str:
    if fallback:
        return "Модель вкуса ещё не обучена: оценка по теме, охвату и критериям канала, максимум 6,5."
    idx = contributions.get("_pos_idx") if score >= 5 else contributions.get("_neg_idx")
    if idx is None:
        idx = contributions.get("_pos_idx") if contributions.get("_pos_idx") is not None else contributions.get("_neg_idx")
    prefix = "Нет близкого примера эталона или редактора"
    if idx is not None and 0 <= int(idx) < len(bank.get("hooks") or []):
        hook = bank["hooks"][int(idx)][:80]
        similarity = contributions.get("_pos_sim" if idx == contributions.get("_pos_idx") else "_neg_sim", 0.0)
        prefix = f"Похоже на пример «{hook}» (сходство {similarity:.2f})"
    ignored = {"_probability", "_pos_idx", "_neg_idx", "_pos_sim", "_neg_sim"}
    largest = sorted(((name, value) for name, value in contributions.items() if name not in ignored),
                     key=lambda pair: -abs(pair[1]))[:2]
    suffix = "; ".join(f"{FEATURE_LABELS.get(name, name)} {value:+.2f}" for name, value in largest)
    return f"{prefix}. {suffix}"[:200]


async def evaluate_interest_local(project: dict, cluster_ids: list[int]) -> dict[int, tuple[float, str]]:
    if not cluster_ids:
        return {}
    state = project.get("editorial_model_state") or {}
    provider = get_provider()
    data = await _feature_data(project["id"], cluster_ids)
    clusters = await _cluster_vectors(cluster_ids)
    version = str(state.get("version") or "")
    bank = await load_bank(project["id"], version) if version else {"vectors": np.empty((0, 0)), "hooks": []}
    criteria = _unit(state.get("criteria_vector"))
    out = {}
    for cid in cluster_ids:
        cluster = clusters.get(cid)
        if not cluster:
            continue
        feats = dict(data.get(cid) or {})
        vec = cluster["vector"]
        if criteria is not None and vec is not None and criteria.shape == vec.shape:
            feats["criteria_fit"] = max(0.0, float(criteria @ vec))
        else:
            feats["criteria_fit"] = 0.5
        feats["title"] = cluster["title"]
        scale, promo = anchor_features(cluster["title"])
        feats["anchor"] = max(0.0, scale - promo)
        cold = (not state or provider.name == "stub" or vec is None or not state.get("w_taste")
                or state.get("stage") == "untrained")
        if cold:
            score, contributions = fallback_score(feats)
            reason = local_reason(score, contributions, bank, fallback=True)
        else:
            score, contributions = score_features(state, bank, vec, feats)
            reason = local_reason(score, contributions, bank)
        out[cid] = (round(float(score), 3), reason)
    return out


async def train_project(project_id: int) -> dict:
    project = await db.fetchone("SELECT * FROM projects WHERE id = %s", (project_id,))
    examples = await db.fetchall(
        "SELECT * FROM editorial_examples WHERE project_id = %s AND hook_embedding IS NOT NULL ORDER BY id", (project_id,),
    )
    if not project or not examples:
        raise ValueError("Нет примеров для обучения редакционной модели")
    dim = len(examples[0]["hook_embedding"])
    examples = [e for e in examples if len(e["hook_embedding"]) == dim]
    vectors = np.asarray([e["hook_embedding"] for e in examples], dtype=np.float32)
    feature_rows: dict[int, dict] = {}
    direct_ids = [e["cluster_id"] for e in examples if e.get("cluster_id") is not None]
    reference_indices = [i for i, e in enumerate(examples) if e["source_kind"] == "reference_channel" and e.get("cluster_id") is None]
    candidates = await db.fetchall("SELECT id FROM clusters WHERE project_id = %s", (project_id,))
    candidate_data = await _cluster_vectors([r["id"] for r in candidates])
    matched_ids = set(direct_ids)
    matches: dict[int, int] = {}
    match_sim = float(os.getenv("MATCH_SIM", DEFAULT_MATCH_SIM))
    for idx in reference_indices:
        best, best_sim = None, match_sim
        for cid, cluster in candidate_data.items():
            if cluster["vector"] is None or len(cluster["vector"]) != dim:
                continue
            age = abs((examples[idx]["created_at"] - cluster["last_seen"]).total_seconds())
            if age > 3 * 86400:
                continue
            similarity = float(_unit(vectors[idx]) @ cluster["vector"])
            if similarity >= best_sim:
                best, best_sim = cid, similarity
        if best is not None:
            matches[idx] = best
            matched_ids.add(best)
    cluster_features = await _feature_data(project_id, list(matched_ids))
    for idx, example in enumerate(examples):
        cid = example.get("cluster_id") or matches.get(idx)
        allowed = example["source_kind"] != "reference_ad" and example["source_kind"] != "reference_weak"
        if cid in cluster_features and allowed:
            feature_rows[idx] = dict(cluster_features[cid])
            scale, promo = anchor_features(example["hook_text"])
            feature_rows[idx]["anchor"] = max(0.0, scale - promo)
    provider = get_provider()
    criteria_vector = None
    if provider.name != "stub" and project.get("interest_criteria"):
        criteria_vector = (await provider.embed([project["interest_criteria"]], project_id=project_id))[0]
    criteria_unit = _unit(criteria_vector)
    if criteria_unit is not None and len(criteria_unit) == dim:
        for idx in feature_rows:
            feature_rows[idx]["criteria_fit"] = max(0.0, float(criteria_unit @ _unit(vectors[idx])))
    state = train_model(examples, vectors, feature_rows, criteria_vector,
                        dup_sim=float(os.getenv("DUP_SIM", DEFAULT_DUP_SIM)), match_sim=match_sim,
                        neg_margin=float(os.getenv("NEG_MARGIN", DEFAULT_NEG_MARGIN)))
    now = utcnow()
    state["trained_at"] = now.isoformat()
    state["version"] = now.strftime("%Y%m%d%H%M%S")
    state = _compact_state(state)
    await db.execute("UPDATE projects SET editorial_model_state = %s, editorial_actions_since_train = 0 WHERE id = %s",
                     (Jsonb(state), project_id))
    _BANK_CACHE.pop((project_id, state["version"]), None)
    return state


async def schedule_training(project_id: int) -> None:
    await jobs.enqueue("train_editorial_model", {"project_id": project_id}, dedupe_key=f"editorial:{project_id}")


async def register_editorial_action(project_id: int) -> None:
    row = await db.fetchone(
        "UPDATE projects SET editorial_actions_since_train = editorial_actions_since_train + 1 WHERE id = %s "
        "RETURNING editorial_actions_since_train", (project_id,),
    )
    if row and row["editorial_actions_since_train"] >= 5:
        await schedule_training(project_id)


@jobs.handler("train_editorial_model")
async def train_editorial_model(payload: dict) -> None:
    project_id = int(payload["project_id"])
    async with db.advisory_lock(db.AdvisoryLock.PROJECT_EDITORIAL_TRAIN, project_id) as got:
        if not got:
            raise jobs.Retry(5)
        try:
            await sync_examples(project_id)
            state = await train_project(project_id)
            log.info("editorial model for project %s trained: %s", project_id, state["stage"])
        except ValueError as e:
            log.info("editorial model for project %s not trained: %s", project_id, e)
