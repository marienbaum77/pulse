"""Отложенная оценка локальной редакционной модели на numpy, без scikit-learn.

  python backend/scripts/eval_editorial_model.py --synthetic --out docs/thesis_figures
  python backend/scripts/eval_editorial_model.py --data editorial.jsonl --out docs/thesis_figures

Для реальных данных эмбеддинги создаёт выбранный provider. LLM-бейзлайн доступен только через --llm-scores
или явный --run-llm; без оценок он обозначается «н/д». Синтетический прогон не измеряет качество на реальных данных.
"""
import argparse
import asyncio
import csv
import io
import json
import os
import sys
import time
import tracemalloc
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pipeline.editorial_model import FEATURE_NAMES, anchor_features, score_features, serialized_state_size, train_model  # noqa: E402
from app.pipeline.editorial_model import FEATURE_LABELS, average_precision, roc_auc  # noqa: E402
from app.pipeline import interest, scoring  # noqa: E402

BOOTSTRAP_ROUNDS = 1000
LATENCY_RUNS = 1000
CASE_NAMES = ("Gemini 4 Carbon", "Qwen 4.8", "my-list", "ai-bro", "OpenClaw")
SOURCE_KINDS = {
    "reference_channel", "reference_ad", "reference_weak", "editor_approved", "editor_rejected", "stream_unlabeled",
}
METHOD_LABELS = {
    "proposed": "Предложенная модель", "anchor": "Без anchor", "knn": "Без kNN", "taste_only": "Только этап A",
    "nws": "NWS", "topic": "Косинус с темой", "llm": "LLM-судья",
}
COLORS = {
    "proposed": "#222222", "anchor": "#0072B2", "knn": "#D55E00", "taste_only": "#009E73",
    "nws": "#CC79A7", "topic": "#56B4E9", "llm": "#E69F00",
}
DASHES = {"proposed": "-", "anchor": "--", "knn": "-.", "taste_only": ":", "nws": "--", "topic": "-.", "llm": ":"}
DEMO_WARNING = "СИНТЕТИЧЕСКИЙ демо-прогон — не реальная оценка качества"


def _number(row: dict, key: str, default: float) -> float:
    try:
        return float(str(row.get(key, default)).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Поле {key} должно быть числом: {row.get(key)!r}") from exc


def read_rows(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    if path.lower().endswith(".csv"):
        sample = raw[:2048]
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
        source = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
    else:
        source = [json.loads(line) for line in raw.splitlines() if line.strip()]
    rows = []
    for number, row in enumerate(source, 1):
        if not str(row.get("title") or "").strip():
            raise ValueError(f"Строка {number}: пустой title")
        if str(row.get("label")) not in {"0", "1"}:
            raise ValueError(f"Строка {number}: label должен быть 0 или 1")
        default_kind = "editor_approved" if int(row["label"]) else "editor_rejected"
        kind = str(row.get("source_kind") or default_kind)
        if kind not in SOURCE_KINDS:
            raise ValueError(f"Строка {number}: неизвестный source_kind {kind!r}")
        rows.append({
            **row,
            "title": str(row["title"]).strip(),
            "label": int(row["label"]),
            "source_kind": kind,
            "hours_ago": _number(row, "hours_ago", 0),
            "source_count": max(0, int(_number(row, "source_count", 1))),
            "recent_items": max(0, int(_number(row, "recent_items", 0))),
            "topic_fit": min(1.0, max(0.0, _number(row, "topic_fit", 0.5))),
            "sample_weight": max(0.01, _number(row, "sample_weight", 1)),
            "created_at": str(row.get("created_at") or ""),
            "cluster_id": row.get("cluster_id"),
        })
    if len(rows) < 20:
        raise ValueError("Для разбиения нужно не менее 20 размеченных строк")
    return rows


def synthetic_data(seed: int) -> tuple[list[dict], np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    rows = []
    vectors = []
    for group in range(120):
        label = group % 2
        reference = group % 10 == 0
        kind = ("reference_channel" if label else "reference_weak") if reference else (
            "editor_approved" if label else "editor_rejected"
        )
        center = rng.normal(0, 0.65, 32)
        center[0] += 2.4 if label else -2.4
        center[1] += 0.8 if label else -0.3
        for duplicate in range(2):
            index = group * 2 + duplicate
            hours = (119 - group) * 2 + duplicate * 0.1
            rows.append({
                "title": f"Синтетический пример {index}", "label": label, "source_kind": kind,
                "hours_ago": hours, "created_at": f"2026-01-01T{group // 6:02d}:{(group % 6) * 10:02d}:00+00:00",
                "cluster_id": None if reference else f"synthetic:{group}", "group_id": f"synthetic:{group}",
                "source_count": 1 + (group % 7 if label else group % 3), "recent_items": group % 5 if label else group % 2,
                "topic_fit": min(1.0, max(0.0, 0.62 + 0.22 * label + float(rng.normal(0, 0.12)))),
                "sample_weight": 0.8 if kind == "reference_weak" else 1.0,
                "topic": "синтетическая тема", "interest_criteria": "синтетическая тема",
            })
            vectors.append(center + rng.normal(0, 0.10 if duplicate else 0.18, 32))
    topic = np.zeros(32, dtype=np.float32)
    topic[1] = 1.0
    return rows, np.asarray(vectors, dtype=np.float32), topic


async def embed_real(rows: list[dict], topic: str, criteria: str, provider_name: str | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if provider_name:
        os.environ["LLM_PROVIDER"] = provider_name
    from app.providers import get_provider

    provider = get_provider()
    embeddings = await provider.embed([row["title"] for row in rows] + [topic, criteria])
    vectors = np.asarray(embeddings[:-2], dtype=np.float32)
    if vectors.ndim != 2 or len(vectors) != len(rows) or not vectors.shape[1]:
        raise ValueError("Провайдер вернул некорректную матрицу эмбеддингов")
    return vectors, np.asarray(embeddings[-2], dtype=np.float32), np.asarray(embeddings[-1], dtype=np.float32)


def grouped_temporal_split(rows: list[dict], test_frac: float) -> tuple[list[int], list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[str(row["cluster_id"] or row.get("group_id") or f"row:{index}")].append(index)
    ordered = sorted(groups.values(), key=lambda ids: max(-rows[i]["hours_ago"] for i in ids))
    target = len(rows) * test_frac
    candidates = []
    for cut in range(1, len(ordered)):
        train = [i for group in ordered[:cut] for i in group]
        test = [i for group in ordered[cut:] for i in group]
        if len({rows[i]["label"] for i in train}) == 2 and len({rows[i]["label"] for i in test}) == 2:
            candidates.append((abs(len(test) - target), cut, train, test))
    if not candidates:
        raise ValueError("Не удалось разделить группы так, чтобы в train и test были оба класса")
    _, _, train, test = min(candidates, key=lambda item: (item[0], item[1]))
    return sorted(train), sorted(test)


def feature_row(row: dict, vector: np.ndarray, criteria: np.ndarray) -> dict:
    scale, promo = anchor_features(row["title"])
    denominator = max(float(np.linalg.norm(vector) * np.linalg.norm(criteria)), 1e-12)
    return {
        "title": row["title"], "recent_published_sim": float(row.get("recent_published_sim") or 0),
        "coverage": scoring.coverage(int(row["source_count"])), "velocity": scoring.velocity(int(row["recent_items"])),
        "topic_fit": float(row["topic_fit"]), "source_perf": float(row.get("source_perf") or 1.0),
        "criteria_fit": max(0.0, float(vector @ criteria / denominator)), "anchor": max(0.0, scale - promo),
    }


def make_bank(rows: list[dict], vectors: np.ndarray) -> dict:
    return {
        "vectors": np.asarray(vectors, dtype=np.float32), "labels": [row["label"] for row in rows],
        "source_kinds": [row["source_kind"] for row in rows], "hooks": [row["title"] for row in rows],
        "cluster_ids": [row["cluster_id"] for row in rows],
    }


def model_scores(state: dict, bank: dict, rows: list[dict], vectors: np.ndarray, features: list[dict]) -> np.ndarray:
    return np.asarray([score_features(state, bank, vectors[i], features[i])[0] for i in range(len(rows))], dtype=float)


def nws_scores(rows: list[dict]) -> np.ndarray:
    values = []
    for row in rows:
        features = {
            "coverage": scoring.coverage(int(row["source_count"])), "authority": 0.5,
            "freshness": scoring.freshness(float(row["hours_ago"]), 12), "velocity": scoring.velocity(int(row["recent_items"])),
            "topic_fit": float(row["topic_fit"]),
        }
        values.append(10 * scoring.nws(features, {})[0])
    return np.asarray(values)


def topic_scores(vectors: np.ndarray, topic: np.ndarray) -> np.ndarray:
    unit = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    topic_unit = topic / max(float(np.linalg.norm(topic)), 1e-12)
    return 10 * np.maximum(0.0, unit @ topic_unit)


def pr_curve(y: np.ndarray, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    positives = int(y.sum())
    if not positives:
        return np.asarray([0.0, 1.0]), np.asarray([1.0, 0.0])
    order = np.argsort(-scores, kind="stable")
    sorted_scores = scores[order]
    sorted_y = y[order]
    ends = np.flatnonzero(np.r_[sorted_scores[1:] != sorted_scores[:-1], True])
    hits = np.cumsum(sorted_y)[ends]
    recall = hits / positives
    precision = hits / (ends + 1)
    return np.r_[0.0, recall], np.r_[1.0, precision]


def point_metrics(y: np.ndarray, scores: np.ndarray, same_threshold: float) -> dict:
    selected = scores >= 8
    same = scores >= same_threshold
    order = np.argsort(-scores, kind="stable")
    negatives = max(1, int((y == 0).sum()))
    return {
        "pr_auc": average_precision(y, scores), "roc_auc": roc_auc(y, scores),
        "precision_at_5": float(y[order[:5]].mean()), "precision_at_10": float(y[order[:10]].mean()),
        "precision_at_8": float(y[selected].mean()) if selected.any() else 0.0,
        "fpr_at_8": float((selected & (y == 0)).sum()) / negatives,
        "fpr_same_coverage": float((same & (y == 0)).sum()) / negatives,
        "coverage_at_8": float(selected.mean()), "coverage_same": float(same.mean()),
    }


def evaluate_with_bootstrap(y: np.ndarray, scores: np.ndarray, same_threshold: float, seed: int) -> tuple[dict, dict]:
    metric_names = list(point_metrics(y, scores, same_threshold))
    samples = {name: [] for name in metric_names}
    grid = np.linspace(0, 1, 41)
    curves = []
    rng = np.random.default_rng(seed)
    for _ in range(BOOTSTRAP_ROUNDS):
        indices = rng.integers(0, len(y), len(y))
        sampled_y = y[indices]
        if len(np.unique(sampled_y)) < 2:
            continue
        values = point_metrics(sampled_y, scores[indices], same_threshold)
        for name in metric_names:
            samples[name].append(values[name])
        recall, precision = pr_curve(sampled_y, scores[indices])
        curves.append(np.interp(grid, recall, precision))
    point = point_metrics(y, scores, same_threshold)
    result = {name: float(value) for name, value in point.items()}
    result["ci95"] = {
        name: [float(value) for value in np.percentile(values, [2.5, 97.5])] if values else [0.0, 0.0]
        for name, values in samples.items()
    }
    result["n_test"] = len(y)
    result["same_coverage_threshold"] = float(same_threshold)
    curve = {"recall": grid.tolist(), "ci_low": np.percentile(curves, 2.5, axis=0).tolist(),
             "ci_high": np.percentile(curves, 97.5, axis=0).tolist()} if curves else {}
    recall, precision = pr_curve(y, scores)
    curve.update({"curve_recall": recall.tolist(), "curve_precision": precision.tolist()})
    return result, curve


def coverage_threshold(train_scores: np.ndarray, coverage: float) -> float:
    count = int(round(coverage * len(train_scores)))
    ordered = np.sort(train_scores)[::-1]
    if count <= 0:
        return float(ordered[0] + 1e-9)
    return float(ordered[min(count, len(ordered)) - 1])


def load_llm_scores(path: Path, rows: list[dict]) -> np.ndarray:
    source = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(source) == len(rows) and all("score" in row and "title" not in row for row in source):
        return np.asarray([min(10.0, max(0.0, float(row["score"]))) for row in source])
    by_title: dict[str, deque] = defaultdict(deque)
    for row in source:
        by_title[str(row.get("title") or "")].append(float(row["score"]))
    scores = []
    for row in rows:
        if not by_title[row["title"]]:
            raise ValueError(f"В {path} нет LLM-оценки для заголовка {row['title']!r}")
        scores.append(min(10.0, max(0.0, by_title[row["title"]].popleft())))
    return np.asarray(scores)


async def run_llm(rows: list[dict], topic: str, criteria: str, provider_name: str | None) -> np.ndarray:
    if provider_name:
        os.environ["LLM_PROVIDER"] = provider_name
    from app.providers import get_provider

    provider = get_provider()
    if not getattr(provider, "supports_chat", False):
        raise ValueError("Выбранный provider не поддерживает chat; LLM-бейзлайн остаётся н/д")
    project = {"id": 0, "topic": topic, "interest_criteria": criteria}
    scores = []
    for row in rows:
        cluster = {"title": row["title"], "mode": "news", "item_count": max(1, int(row["source_count"]))}
        items = [{"kind": "article", "source": "", "title": row["title"], "text": row["title"]}]
        system, prompt = interest.rating_prompt(project, cluster, items)
        raw = await provider.chat(system, prompt, max_tokens=80, temperature=0.1, project_id=0)
        parsed = interest.parse_rating(raw)
        if parsed is None:
            raise ValueError(f"LLM не вернула числовую оценку для {row['title']!r}")
        scores.append(parsed[0])
    return np.asarray(scores)


def measure_resources(state: dict, bank: dict, rows: list[dict], vectors: np.ndarray, features: list[dict]) -> dict:
    latencies = []
    tracemalloc.start()
    for run in range(LATENCY_RUNS):
        index = run % len(rows)
        started = time.perf_counter_ns()
        score_features(state, bank, vectors[index], features[index])
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "latency_median_ms": float(np.median(latencies)),
        "latency_ci95": [float(value) for value in np.percentile(latencies, [2.5, 97.5])],
        "rss_peak_mb": peak / (1024 * 1024), "tracemalloc_peak_mb": peak / (1024 * 1024),
        "bank_bytes": int(np.asarray(bank["vectors"], dtype=np.float32).nbytes), "state_bytes": serialized_state_size(state),
    }


def illustrative_cases(state: dict, bank: dict, rows: list[dict], vectors: np.ndarray,
                       features: list[dict], synthetic: bool, seed: int) -> list[dict]:
    cases = []
    for position, name in enumerate(CASE_NAMES):
        matches = [i for i, row in enumerate(rows) if name.casefold() in row["title"].casefold()]
        if matches:
            index = matches[0]
            vector, feats = vectors[index], features[index]
        elif synthetic:
            rng = np.random.default_rng(seed + 1000 + position)
            vector = rng.normal(0, 1, vectors.shape[1]).astype(np.float32)
            vector[0] = 2.0 if position in {0, 1, 4} else -2.0
            row = {"title": name, "source_count": 2, "recent_items": 1, "topic_fit": 0.6}
            feats = feature_row(row, vector, np.asarray(state.get("criteria_vector") or vector))
        else:
            cases.append({"name": name, "score": None})
            continue
        score, _ = score_features(state, bank, vector, feats)
        cases.append({"name": name, "score": float(score)})
    return cases


def make_figures(report: dict, y: np.ndarray, scores: dict[str, np.ndarray], curves: dict[str, dict],
                 state: dict, out: Path, synthetic: bool) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("Графики пропущены: установите matplotlib командой pip install -r backend/requirements-dev.txt")
        return False
    plt.rcParams.update({"font.family": "serif", "font.size": 10, "axes.grid": True, "grid.alpha": 0.25,
                         "figure.constrained_layout.use": True})
    available = [name for name in scores if name in report["methods"]]

    def mark(figure):
        if synthetic:
            figure.suptitle("ДЕМО, синтетические данные — не результат эксперимента",
                            color="#8B0000", fontsize=11)

    distribution_methods = ["proposed"] + (["llm"] if "llm" in available else [])
    fig, axes = plt.subplots(len(distribution_methods), 1, figsize=(9, 4 * len(distribution_methods)), squeeze=False)
    for axis, name in zip(axes[:, 0], distribution_methods):
        for label, title, hatch in ((1, "Позитивы", "//"), (0, "Негативы", "..")):
            axis.hist(scores[name][y == label], bins=np.linspace(0, 10, 21), alpha=0.55, label=title,
                      color=COLORS[name], hatch=hatch, edgecolor="white")
        axis.set(title=METHOD_LABELS[name], xlabel="Оценка интересности", ylabel="Число сюжетов", xlim=(0, 10))
        axis.legend()
    mark(fig)
    fig.savefig(out / "fig1_distributions.png", dpi=300)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(8, 6))
    for name in available:
        curve = curves[name]
        axis.plot(curve["curve_recall"], curve["curve_precision"], DASHES[name], color=COLORS[name],
                  label=f"{METHOD_LABELS[name]} (AP={report['methods'][name]['pr_auc']:.3f})")
        if curve.get("ci_low"):
            axis.fill_between(curve["recall"], curve["ci_low"], curve["ci_high"], color=COLORS[name], alpha=0.10)
    axis.set(title="Precision–Recall на отложенной выборке (95% ДИ)", xlabel="Полнота", ylabel="Чистота",
             xlim=(0, 1), ylim=(0, 1.02))
    axis.legend(fontsize=8)
    mark(fig)
    fig.savefig(out / "fig2_pr_curves.png", dpi=300)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    thresholds = np.linspace(0, 10, 101)
    for name in available:
        precision = []
        coverage = []
        for threshold in thresholds:
            selected = scores[name] >= threshold
            precision.append(float(y[selected].mean()) if selected.any() else 0.0)
            coverage.append(float(selected.mean()))
        axes[0].plot(thresholds, precision, DASHES[name], color=COLORS[name], label=METHOD_LABELS[name])
        axes[1].plot(thresholds, coverage, DASHES[name], color=COLORS[name], label=METHOD_LABELS[name])
    axes[0].set(title="Чистота от порога", xlabel="Порог автопубликации", ylabel="Precision", ylim=(0, 1.02))
    axes[1].set(title="Охват от порога", xlabel="Порог автопубликации", ylabel="Доля сюжетов", ylim=(0, 1.02))
    axes[0].legend(fontsize=8)
    mark(fig)
    fig.savefig(out / "fig3_threshold_curves.png", dpi=300)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6), gridspec_kw={"width_ratios": [1.25, 1]})
    if state.get("stage_b_weights"):
        contributions = np.asarray(state["stage_b_weights"]) * np.asarray(state["feature_scale"])
        names = list(FEATURE_NAMES)
    else:
        contributions = np.asarray([1.0, 2.0])
        names = ["taste", "contrast_margin"]
    order = np.argsort(np.abs(contributions))
    axes[0].barh([FEATURE_LABELS.get(names[i], names[i]) for i in order], contributions[order], color="#666666")
    axes[0].axvline(0, color="black", linewidth=0.7)
    axes[0].set(title="Вклад признаков: вес × разброс", xlabel="Стандартизованный вклад")
    axes[0].grid(axis="x", alpha=0.25)
    scored_cases = [case for case in report["illustrative_cases"] if case["score"] is not None]
    if scored_cases:
        positions = np.arange(len(scored_cases))
        values = [case["score"] for case in scored_cases]
        axes[1].scatter(values, positions, c="#222222", zorder=3)
        for position, case in zip(positions, scored_cases):
            axes[1].text(case["score"] + 0.15, position, f"{case['score']:.2f}", va="center", fontsize=9)
        axes[1].set_yticks(positions, [case["name"] for case in scored_cases])
    axes[1].set(title="Кейсы — только иллюстрация, вне метрик", xlabel="Оценка модели", xlim=(0, 11))
    axes[1].axvline(8, color="#8B0000", linestyle="--", linewidth=1)
    mark(fig)
    fig.savefig(out / "fig4_explanations.png", dpi=300)
    plt.close(fig)
    return True


def print_table(methods: dict) -> None:
    print(f"{'Метод':<24} {'PR-AUC':>8} {'ROC-AUC':>8} {'P@5':>7} {'P@10':>7} {'FPR@8':>8} {'FPR~охват':>11}")
    for name, values in methods.items():
        if values.get("status") == "н/д":
            print(f"{METHOD_LABELS[name]:<24} {'н/д':>8}")
            continue
        print(f"{METHOD_LABELS[name]:<24} {values['pr_auc']:>8.3f} {values['roc_auc']:>8.3f} "
              f"{values['precision_at_5']:>7.3f} {values['precision_at_10']:>7.3f} "
              f"{values['fpr_at_8']:>8.3f} {values['fpr_same_coverage']:>11.3f}")


async def run(args: argparse.Namespace) -> None:
    if not 0.05 <= args.test_frac <= 0.5:
        raise ValueError("--test-frac должен быть от 0.05 до 0.5")
    if args.synthetic:
        rows, vectors, topic = synthetic_data(args.seed)
        criteria = topic.copy()
        topic_text = criteria_text = "синтетическая тема"
        print(DEMO_WARNING)
    else:
        rows = read_rows(args.data)
        topic_text = args.topic or next((str(row.get("topic") or "") for row in rows if row.get("topic")), "")
        if not topic_text:
            raise ValueError("Для topic baseline укажите --topic или поле topic в данных")
        criteria_text = next((str(row.get("interest_criteria") or "") for row in rows if row.get("interest_criteria")), "")
        vectors, topic, criteria = await embed_real(rows, topic_text, criteria_text or topic_text, args.provider)
    features = [feature_row(row, vectors[i], criteria) for i, row in enumerate(rows)]
    case_indices = {i for i, row in enumerate(rows) if any(name.casefold() in row["title"].casefold() for name in CASE_NAMES)}
    metric_indices = [i for i in range(len(rows)) if i not in case_indices and rows[i]["source_kind"] != "stream_unlabeled"]
    metric_rows = [rows[i] for i in metric_indices]
    if len(metric_rows) < 20:
        raise ValueError("После исключения PU-примеров и иллюстративных кейсов осталось менее 20 строк")
    train_local, test_local = grouped_temporal_split(metric_rows, args.test_frac)
    train_ids = [metric_indices[i] for i in train_local]
    test_ids = [metric_indices[i] for i in test_local]
    oldest_train = min(rows[i]["hours_ago"] for i in train_ids)
    train_ids += [i for i in range(len(rows)) if i not in case_indices and rows[i]["source_kind"] == "stream_unlabeled"
                  and rows[i]["hours_ago"] >= oldest_train]
    train_rows = [rows[i] for i in train_ids]
    test_rows = [rows[i] for i in test_ids]
    train_vectors, test_vectors = vectors[train_ids], vectors[test_ids]
    train_features = [features[i] for i in train_ids]
    test_features = [features[i] for i in test_ids]
    bank = make_bank(train_rows, train_vectors)
    y_test = np.asarray([row["label"] for row in test_rows], dtype=int)
    scores: dict[str, np.ndarray] = {}
    curves: dict[str, dict] = {}
    methods: dict[str, dict] = {}
    states: dict[str, dict] = {}

    variants = [("proposed", None)] + [(name, name) for name in args.ablate]
    for offset, (name, ablation) in enumerate(variants):
        feature_rows = {
            local: dict(train_features[local]) for local, row in enumerate(train_rows)
            if not row["source_kind"].startswith("reference_") or row.get("cluster_id") is not None
        }
        state = train_model(train_rows, train_vectors, feature_rows, criteria, ablate=ablation)
        states[name] = state
        train_score = model_scores(state, bank, train_rows, train_vectors, train_features)
        test_score = model_scores(state, bank, test_rows, test_vectors, test_features)
        scores[name] = test_score
        methods[name], curves[name] = evaluate_with_bootstrap(y_test, test_score, 8.0, args.seed + offset)
        methods[name]["stage"] = state["stage"]
        if name == "proposed":
            proposed_train_coverage = float((train_score >= 8).mean())
            methods[name].update(measure_resources(state, bank, test_rows, test_vectors, test_features))

    baseline_train = {
        "nws": nws_scores(train_rows), "topic": topic_scores(train_vectors, topic),
    }
    baseline_test = {"nws": nws_scores(test_rows), "topic": topic_scores(test_vectors, topic)}
    llm_scores = None
    if args.llm_scores:
        llm_scores = load_llm_scores(args.llm_scores, rows)
    elif args.run_llm:
        llm_scores = await run_llm(rows, topic_text, criteria_text or topic_text, args.provider)
    if llm_scores is not None:
        baseline_train["llm"] = llm_scores[train_ids]
        baseline_test["llm"] = llm_scores[test_ids]
    for offset, name in enumerate(("nws", "topic", "llm")):
        if name not in baseline_test:
            methods[name] = {"status": "н/д", "reason": "Нет реальных LLM-оценок; используйте --llm-scores или --run-llm"}
            continue
        threshold = coverage_threshold(baseline_train[name], proposed_train_coverage)
        scores[name] = baseline_test[name]
        methods[name], curves[name] = evaluate_with_bootstrap(y_test, scores[name], threshold, args.seed + 20 + offset)

    report = {
        "parameters": {
            "data": args.data, "synthetic": args.synthetic, "test_frac": args.test_frac, "seed": args.seed,
            "ablate": args.ablate, "bootstrap_rounds": BOOTSTRAP_ROUNDS, "latency_runs": LATENCY_RUNS,
            "split": "grouped temporal by cluster_id; thresholds fitted on train only", "provider": args.provider,
        },
        "warning": DEMO_WARNING if args.synthetic else None,
        "n_rows": len(rows), "n_train": len(train_rows), "n_test": len(test_rows),
        "proposed_train_coverage_at_8": proposed_train_coverage,
        "memory_measurement": "rss_peak_mb — пиковый прирост Python-аллокаций по tracemalloc, не RSS всего процесса",
        "methods": methods, "illustrative_cases": [],
        "case_note": "Именованные кейсы только иллюстрируют ось решения; они исключены из train, test и агрегированных метрик.",
    }
    report["illustrative_cases"] = illustrative_cases(states["proposed"], bank, rows, vectors, features, args.synthetic, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Групповое временное разбиение: train={len(train_rows)}, test={len(test_rows)}; дубли одного cluster_id не пересекаются.")
    print("Именованные кейсы — только иллюстрация; в обучение, подбор порогов и метрики не входят.")
    print_table(methods)
    if not args.no_figures:
        if make_figures(report, y_test, scores, curves, states["proposed"], args.out, args.synthetic):
            print(f"Графики сохранены в {args.out}")
    print(f"Метрики сохранены: {args.out / 'metrics.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", help="CSV/JSONL с title, label, source_kind и признаками; «-» для stdin")
    source.add_argument("--synthetic", action="store_true", help="детерминированный демонстрационный набор")
    parser.add_argument("--test-frac", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("docs/thesis_figures"))
    parser.add_argument("--ablate", nargs="*", choices=["anchor", "knn", "taste_only"], default=[])
    llm = parser.add_mutually_exclusive_group()
    llm.add_argument("--llm-scores", type=Path, help="JSONL с реальными оценками LLM: title и score")
    llm.add_argument("--run-llm", action="store_true", help="выполнить реальные вызовы текущего промпта interest.py")
    parser.add_argument("--provider", help="переопределить LLM_PROVIDER для эмбеддингов и --run-llm")
    parser.add_argument("--topic", help="тема проекта, если её нет в данных")
    parser.add_argument("--no-figures", action="store_true", help="не строить PNG и не импортировать matplotlib")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Ошибка оценки: {exc}") from exc


if __name__ == "__main__":
    main()
