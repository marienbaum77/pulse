"""Инкрементальная кластеризация по косинусному сходству.

Один и тот же код используется в конвейере (app.pipeline.process) и в скрипте оценки
(scripts/eval_clustering.py), поэтому метрики на размеченном наборе описывают именно то, что работает в продукте.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np


@dataclass(eq=False)
class Cluster:
    id: int | None
    sum: np.ndarray  # сумма единичных векторов членов
    n: int
    first_seen: datetime
    last_seen: datetime
    is_new: bool = False
    touched: bool = False
    _unit: np.ndarray | None = None

    @property
    def unit(self) -> np.ndarray:
        if self._unit is None:
            norm = float(np.linalg.norm(self.sum))
            self._unit = self.sum / norm if norm > 0 else self.sum
        return self._unit

    @property
    def centroid(self) -> np.ndarray:
        """Среднее единичных векторов; именно оно хранится в БД."""
        return self.sum / max(self.n, 1)

    @classmethod
    def from_db(cls, cid: int, centroid: np.ndarray, n: int, first_seen: datetime, last_seen: datetime) -> "Cluster":
        return cls(cid, np.asarray(centroid, dtype=np.float32) * n, n, first_seen, last_seen)


def unit_vector(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class OnlineClusterer:
    """Single-pass кластеризация в скользящем временном окне.

    Материал присоединяется к ближайшему кластеру, если сходство с его центроидом >= threshold
    и кластер обновлялся не раньше, чем window назад; иначе создаётся новый кластер.
    """

    def __init__(self, threshold: float, window: timedelta, clusters: list[Cluster] | None = None):
        self.threshold = threshold
        self.window = window
        self.clusters: list[Cluster] = list(clusters or [])

    def add(self, vec: np.ndarray, ts: datetime) -> tuple[Cluster, float]:
        v = unit_vector(vec)
        best: Cluster | None = None
        best_sim = -1.0
        floor = ts - self.window
        for c in self.clusters:
            if c.last_seen < floor:
                continue
            sim = float(c.unit @ v)
            if sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= self.threshold:
            best.sum = best.sum + v
            best.n += 1
            best.last_seen = max(best.last_seen, ts)
            best.first_seen = min(best.first_seen, ts)
            best._unit = None
            best.touched = True
            return best, best_sim
        c = Cluster(None, v.copy(), 1, ts, ts, is_new=True, touched=True)
        self.clusters.append(c)
        return c, max(best_sim, 0.0)
