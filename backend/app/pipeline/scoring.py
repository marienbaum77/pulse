"""News Weight Score (NWS): взвешенная сумма тематической близости и вторичных признаков сюжета.
Для подборочного (curated) контента набор признаков другой: охват и скорость роста у одиночного видео
или статьи не измеряются, зато важны вовлечённость аудитории и насыщенность материала."""
import math

import numpy as np

DEFAULT_WEIGHTS = {"coverage": 0.15, "authority": 0.075, "freshness": 0.10, "velocity": 0.075, "topic_fit": 0.60}
COMPONENTS = tuple(DEFAULT_WEIGHTS)
CURATED_WEIGHTS = {"topic_fit": 0.55, "engagement": 0.15, "depth": 0.10, "authority": 0.10, "freshness": 0.10}
LABELS = {
    "coverage": "Охват источниками",
    "authority": "Авторитетность",
    "freshness": "Свежесть",
    "velocity": "Скорость роста",
    "topic_fit": "Близость к теме",
    "engagement": "Вовлечённость",
    "depth": "Насыщенность",
}
# Насыщение по метрикам площадок: миллион просмотров на YouTube и 10 тысяч апвоутов на Reddit — уже «максимум».
ENGAGEMENT_SATURATION = {"views": 1_000_000, "likes": 50_000, "score": 10_000, "comments": 2_000}
DEPTH_SATURATION_CHARS = 4000


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def coverage(source_count: int, saturation: int = 8) -> float:
    return min(1.0, math.log1p(max(source_count, 0)) / math.log1p(saturation))


def freshness(age_hours: float, half_life_hours: float) -> float:
    return 0.5 ** (max(age_hours, 0.0) / max(half_life_hours, 0.1))


def velocity(recent_items: int, saturation: int = 5) -> float:
    return min(1.0, max(recent_items, 0) / saturation)


def engagement(metas: list[dict]) -> float | None:
    """Лучшая из метрик вовлечённости материалов сюжета в логарифмической шкале; None — площадка их не отдаёт."""
    best: float | None = None
    for meta in metas:
        for key, saturation in ENGAGEMENT_SATURATION.items():
            value = (meta or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                score = min(1.0, math.log1p(value) / math.log1p(saturation))
                best = score if best is None else max(best, score)
    return best


def depth(content_length: int) -> float:
    """Насыщенность: сниппет из поисковой выдачи слабее полной статьи или расшифровки видео."""
    return min(1.0, max(content_length, 0) / DEPTH_SATURATION_CHARS)


def _topic_fit(centroid: np.ndarray, topic_vec: np.ndarray | None, topic_fit: float | None) -> float:
    if topic_fit is not None:
        return max(0.0, min(1.0, float(topic_fit)))
    return max(0.0, cosine(centroid, topic_vec)) if topic_vec is not None else 0.5


def compute_curated_features(
    *,
    centroid: np.ndarray,
    authority: float,
    age_hours: float,
    window_hours: int,
    topic_vec: np.ndarray | None,
    topic_fit: float | None,
    metas: list[dict],
    content_length: int,
) -> tuple[dict[str, float], frozenset[str]]:
    """Признаки curated-кандидата и множество неизмеренных (подставлено нейтральное 0.5)."""
    eng = engagement(metas)
    feats = {
        "topic_fit": _topic_fit(centroid, topic_vec, topic_fit),
        "engagement": 0.5 if eng is None else eng,
        "depth": depth(content_length),
        "authority": min(1.0, max(0.0, authority)),
        "freshness": freshness(age_hours, window_hours / 4),
    }
    return feats, frozenset({"engagement"}) if eng is None else frozenset()


def compute_features(
    *,
    centroid: np.ndarray,
    source_count: int,
    authority: float,
    age_hours: float,
    recent_items: int,
    window_hours: int,
    topic_vec: np.ndarray | None,
    topic_fit: float | None = None,
) -> dict[str, float]:
    # topic_fit — готовое значение (средний topic_score материалов сюжета); иначе считается по центроиду.
    fit = _topic_fit(centroid, topic_vec, topic_fit)
    return {
        "coverage": coverage(source_count),
        "authority": min(1.0, max(0.0, authority)),
        "freshness": freshness(age_hours, window_hours / 4),
        "velocity": velocity(recent_items),
        "topic_fit": fit,
    }


def nws(
    features: dict[str, float], weights: dict[str, float], neutral: frozenset[str] = frozenset(), defaults: dict[str, float] = DEFAULT_WEIGHTS
) -> tuple[float, dict]:
    """NWS = Σ w_i·f_i. Возвращает общий балл и разложение для интерфейса."""
    w = {**defaults, **{k: float(v) for k, v in (weights or {}).items() if k in defaults}}
    parts: dict[str, dict] = {}
    total = 0.0
    for name in defaults:
        value = float(features.get(name, 0.0))
        contribution = w[name] * value
        parts[name] = {"value": round(value, 4), "weight": w[name], "contribution": round(contribution, 4)}
        if name in neutral:
            parts[name]["neutral"] = True  # признак не измерен, подставлено нейтральное 0.5 — одинаково для всех сюжетов
        total += contribution
    return round(total, 4), parts


def topic_passes_threshold(topic_fit: dict | None, threshold: float) -> bool:
    """Unmeasured topic fit is neutral; otherwise enforce the configured minimum."""
    if threshold <= 0 or not topic_fit or topic_fit.get("neutral"):
        return True
    return float(topic_fit.get("value", 0.5)) >= threshold
