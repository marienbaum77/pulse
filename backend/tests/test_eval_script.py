"""Метрики и baseline из scripts/eval_clustering.py (написаны без sklearn) сверяются со scikit-learn."""
import importlib.util
from pathlib import Path

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

spec = importlib.util.spec_from_file_location("eval_clustering", Path(__file__).resolve().parents[1] / "scripts" / "eval_clustering.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_nmi_and_ari_match_sklearn():
    rng = np.random.default_rng(0)
    for _ in range(100):
        n = int(rng.integers(5, 40))
        a, b = list(rng.integers(0, 5, n).astype(str)), list(rng.integers(0, 6, n))
        assert abs(ev.nmi(a, b) - normalized_mutual_info_score(a, b)) < 1e-9
        assert abs(ev.ari(a, b) - adjusted_rand_score(a, b)) < 1e-9


def test_batch_baseline_matches_sklearn_agglomerative():
    rng = np.random.default_rng(1)
    X = np.vstack([c + 0.15 * rng.normal(size=(12, 24)) for c in rng.normal(size=(5, 24))])
    mine = ev.run_batch(X, 0.6)
    ref = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average", distance_threshold=0.4).fit_predict(X)
    assert adjusted_rand_score(ref, mine) == 1.0


def test_bcubed_perfect_and_worst_cases():
    assert ev.bcubed(["a", "a", "b"], [0, 0, 1]) == (1.0, 1.0, 1.0)
    p, r, _ = ev.bcubed(["a", "b", "c"], [0, 0, 0])  # всё склеено: чистота падает, полнота полная
    assert r == 1.0 and abs(p - 1 / 3) < 1e-9
