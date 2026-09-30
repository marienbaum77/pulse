"""News Weight Score (NWS): взвешенная сумма тематической близости и вторичных признаков сюжета."""
import math

import numpy as np

DEFAULT_WEIGHTS = {"coverage": 0.15, "authority": 0.075, "freshness": 0.10, "velocity": 0.075, "topic_fit": 0.60}
COMPONENTS = tuple(DEFAULT_WEIGHTS)
LABELS = {
    "coverage": "Охват источниками",
    "authority": "Авторитетность",
    "freshness": "Свежесть",
    "velocity": "Скорость роста",
    "topic_fit": "Близость к теме",
}


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


def compute_features(
    *,
    centroid: np.ndarray,
    source_count: int,
    authority: float,
    age_hours: float,
    recent_items: int,
    window_hours: int,
    topic_vec: np.ndarray | None,
) -> dict[str, float]:
    topic_fit = max(0.0, cosine(centroid, topic_vec)) if topic_vec is not None else 0.5
    return {
        "coverage": coverage(source_count),
        "authority": min(1.0, max(0.0, authority)),
        "freshness": freshness(age_hours, window_hours / 4),
        "velocity": velocity(recent_items),
        "topic_fit": topic_fit,
    }


def nws(features: dict[str, float], weights: dict[str, float], neutral: frozenset[str] = frozenset()) -> tuple[float, dict]:
    """NWS = Σ w_i·f_i. Возвращает общий балл и разложение для интерфейса."""
    w = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (weights or {}).items() if k in DEFAULT_WEIGHTS}}
    parts: dict[str, dict] = {}
    total = 0.0
    for name in COMPONENTS:
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
