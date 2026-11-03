import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app import db
from app.pipeline import editorial_model as em

from .helpers import make_project


def test_extract_hook_text_cleans_first_line_and_limits_length():
    text = "  \n🔥 **Важный** [заголовок](https://example.com) https://t.me/source\nЛид не нужен"
    assert em.extract_hook_text(text) == "Важный заголовок"
    assert len(em.extract_hook_text("Очень длинный заголовок " * 20)) <= 140
    assert "вторая строка" not in em.extract_hook_text("Первая строка\nвторая строка")


def test_ad_detector():
    assert em.is_ad("Скидка по промокоду PULSE")
    assert em.is_ad("#партнёрский пост")
    assert em.is_ad("Наш бот в закрепе")
    assert not em.is_ad("Исследование рынка рекламы")


def test_reference_engagement_weight_and_dead_zone():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    posts = []
    for i in range(61):
        views = 50
        if i == 10:
            views = 120
        elif i == 20:
            views = 20
        elif i == 30:
            views = 40
        posts.append({"text": f"Заголовок {i}", "title": f"Заголовок {i}", "views": views,
                      "published_at": now - timedelta(days=5), "url": f"https://t.me/test/{i}", "meta": {}})
    rows = em.classify_reference_posts(posts, now)
    by_url = {r["external_url"]: r for r in rows}
    assert by_url["https://t.me/test/10"]["label"] == 1
    assert by_url["https://t.me/test/10"]["sample_weight"] > 1
    assert by_url["https://t.me/test/20"]["source_kind"] == "reference_weak"
    assert "https://t.me/test/30" not in by_url


def test_reference_channel_with_few_views_is_all_positive_except_ads():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    posts = [{"text": f"Пост {i}", "views": 10 + i, "published_at": now - timedelta(days=3),
              "url": str(i), "meta": {}} for i in range(10)]
    posts.append({"text": "#реклама Пост", "views": 1000, "published_at": now - timedelta(days=3),
                  "url": "ad", "meta": {}})
    rows = em.classify_reference_posts(posts, now)
    assert sum(r["label"] == 1 for r in rows) == 10
    assert next(r for r in rows if r["source_kind"] == "reference_ad")["sample_weight"] == 2


def test_fisher_separates_gaussians_and_is_finite_when_dim_exceeds_rows():
    rng = np.random.default_rng(4)
    x = np.vstack((rng.normal(-1, 0.2, size=(12, 40)), rng.normal(1, 0.2, size=(12, 40))))
    y = np.asarray([0] * 12 + [1] * 12)
    w, b = em.fit_fisher(x, y, alpha=0.5)
    assert np.isfinite(w).all() and np.isfinite(b)
    assert float((x @ w + b)[y == 1].mean()) > float((x @ w + b)[y == 0].mean())


def test_platt_calibration_is_monotonic():
    raw = np.asarray([-3.0, -1.0, 1.0, 3.0])
    a, b = em.fit_platt(raw, [0, 0, 1, 1])
    probabilities = 1 / (1 + np.exp(-(a * raw + b)))
    assert a > 0
    assert np.all(np.diff(probabilities) > 0)


def test_t8_requires_gold_precision_and_ten_hits():
    p = np.asarray([0.95] * 10 + [0.8] * 10 + [0.1] * 10)
    y = np.asarray([1] * 10 + [0] * 10 + [0] * 10)
    gold = np.ones(30, dtype=bool)
    assert em.choose_t8(p, y, gold) == 0.95
    assert em.choose_t8(p[:9], y[:9], gold[:9]) is None


def test_piecewise_scale_is_monotonic_and_capped_without_t8():
    values = [em.probability_to_score(p / 100, 0.4, 0.8) for p in range(101)]
    assert values[0] == 0 and values[-1] == 10
    assert np.all(np.diff(values) >= 0)
    capped = [em.probability_to_score(p / 100, 0.4, None) for p in range(101)]
    assert capped[-1] == 7 and max(capped) <= 7


def test_negative_twin_caps_score_at_four():
    state = {"w_taste": [0.0, 0.0], "b": 10.0, "stage": "A_only", "platt": [1.0, 0.0],
             "scale_median": 0.2, "t8": None, "NEG_MARGIN": 0.03}
    bank = {"vectors": np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32), "labels": [1, 0],
            "source_kinds": ["reference_channel", "editor_rejected"], "hooks": ["позитив", "негатив"]}
    score, _ = em.score_features(state, bank, np.asarray([1.0, 0.0]), {})
    assert score <= 4


def test_loo_knn_excludes_self_and_same_cluster():
    vectors = np.asarray([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    wanted = np.ones(3, dtype=bool)
    assert em.loo_top3(vectors, 0, wanted, [10, 10, 20]) == 0


def test_fallback_never_allows_autopublish():
    score, _ = em.fallback_score({"topic_fit": 1, "coverage": 1, "anchor": 1, "criteria_fit": 1})
    assert score <= 6.5


def test_state_serialization_stays_below_twenty_kb():
    rng = np.random.default_rng(2)
    state = {"w_taste": rng.normal(size=1024).tolist(), "criteria_vector": rng.normal(size=1024).tolist(),
             "metrics": {"n_pos": 100}, "stage": "A_only"}
    compact = em._compact_state(state)
    assert len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode()) <= em.MAX_STATE_BYTES
    assert em.serialized_state_size(state) <= em.MAX_STATE_BYTES


def test_training_with_reference_only_in_stage_a_and_ablations():
    rng = np.random.default_rng(8)
    examples = []
    vectors = []
    features = {}
    for i in range(90):
        label = i % 2
        reference = i >= 80
        kind = "reference_channel" if reference else ("editor_approved" if label else "editor_rejected")
        examples.append({"label": label, "source_kind": kind, "cluster_id": None if reference else i,
                         "created_at": f"2026-01-01T00:{i:02d}:00"})
        vectors.append(rng.normal(size=4) + (1 if label else -1) * np.asarray([2, 0, 0, 0]))
        if not reference:
            features[i] = {"anchor": 1.0, "coverage": 0.5}
    state = em.train_model(examples, np.asarray(vectors), features)
    assert state["stage"] == "AB"
    anchor = em.train_model(examples, np.asarray(vectors), features, ablate="anchor")
    knn = em.train_model(examples, np.asarray(vectors), features, ablate="knn")
    taste = em.train_model(examples, np.asarray(vectors), features, ablate="taste_only")
    assert anchor["feature_mean"][em.FEATURE_NAMES.index("anchor")] == 0
    assert all(knn["feature_mean"][em.FEATURE_NAMES.index(name)] == 0
               for name in ("sim_pos_top3", "sim_neg_top3", "contrast_margin"))
    assert taste["stage"] == "A_only" and not taste["stage_b_weights"]


def test_training_with_single_class_is_untrained_and_scores_via_fallback():
    rng = np.random.default_rng(9)
    examples = [{"label": 1, "source_kind": "reference_channel", "cluster_id": None,
                 "created_at": f"2026-01-01T00:{i:02d}:00"} for i in range(33)]
    vectors = np.asarray([rng.normal(size=4) for _ in examples])
    state = em.train_model(examples, vectors, {})
    assert state["stage"] == "untrained"
    assert not any(state["w_taste"])
    score, contributions = em.score_features(state, {"vectors": np.empty((0, 4)), "hooks": []},
                                             vectors[0], {"title": "Новый рекорд: 50% роста", "coverage": 0.5})
    assert score <= 6.5
    assert "taste" not in contributions
    few_negatives = examples + [{"label": 0, "source_kind": "reference_ad", "cluster_id": None,
                                 "created_at": "2026-01-02T00:00:00"}]
    vectors2 = np.vstack((vectors, rng.normal(size=(1, 4))))
    assert em.train_model(few_negatives, vectors2, {})["stage"] == "untrained"


@pytest.mark.usefixtures("clean")
async def test_sync_examples_uses_rejected_drafts_not_ignored_items_and_deduplicates():
    pid = await make_project(channel=False)
    accepted = await db.fetchone(
        "INSERT INTO clusters(project_id, title, first_seen, last_seen) VALUES (%s, 'Решение редактора', now(), now()) RETURNING id",
        (pid,),
    )
    ignored = await db.fetchone(
        "INSERT INTO clusters(project_id, title, first_seen, last_seen) VALUES (%s, 'Очищенный поток', now(), now()) RETURNING id",
        (pid,),
    )
    vector = np.zeros(256, dtype=np.float32)
    vector[0] = 1
    for cid, status, title, suffix in ((accepted["id"], "clustered", "Решение редактора", "a"),
                                       (ignored["id"], "ignored", "Очищенный поток", "b")):
        await db.execute(
            "INSERT INTO items(project_id, url_hash, title, published_at, status, cluster_id, title_embedding) "
            "VALUES (%s,%s,%s,now(),%s,%s,%s)", (pid, suffix, title, status, cid, vector),
        )
    for cid in (accepted["id"], ignored["id"]):
        await db.execute("INSERT INTO drafts(project_id, cluster_ids, status) VALUES (%s,%s,'rejected')", (pid, [cid]))
    await em.sync_examples(pid)
    await em.sync_examples(pid)
    rows = await db.fetchall("SELECT hook_text, source_kind FROM editorial_examples WHERE project_id = %s", (pid,))
    assert [(r["hook_text"], r["source_kind"]) for r in rows] == [("Решение редактора", "editor_rejected")]
