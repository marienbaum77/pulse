"""Оценка качества кластеризации на размеченном наборе («золотом датасете»). Работает без scikit-learn — прямо в контейнере api.

Формат: JSONL или CSV (UTF-8), по строке на материал. Обязательные поля: title, hours_ago, gold; желательно text.
  gold — ваша метка события: материалы об одном и том же событии должны иметь одинаковую метку (любая строка).

Что делает: прогоняет ровно тот онлайн-алгоритм, что работает в продукте, при разных порогах сходства, выбирает порог на dev-половине
данных и показывает метрики на test-половине. Для сравнения — пакетный baseline (average linkage без учёта времени).
Метрики: B-cubed P/R/F1, NMI, ARI (чем ближе к 1, тем лучше).

  docker compose exec -T api python scripts/eval_clustering.py --data - --provider openai < my_gold.csv
  python backend/scripts/eval_clustering.py --data sample_data/demo_news_hard.jsonl --provider stub
"""
import argparse
import asyncio
import csv
import io
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pipeline.clustering_core import OnlineClusterer  # noqa: E402

MAX_BATCH = 1500  # пакетный baseline — O(n³), для больших наборов пропускается


# ---------- метрики (без sklearn) ----------
def bcubed(gold: list[str], pred: list[int]) -> tuple[float, float, float]:
    n = len(gold)
    pair = Counter(zip(pred, gold))
    pred_size, gold_size = Counter(pred), Counter(gold)
    p = sum(pair[(pr, g)] / pred_size[pr] for pr, g in zip(pred, gold)) / n
    r = sum(pair[(pr, g)] / gold_size[g] for pr, g in zip(pred, gold)) / n
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def nmi(gold: list[str], pred: list[int]) -> float:
    n = len(gold)
    cg, cp, joint = Counter(gold), Counter(pred), Counter(zip(gold, pred))
    mi = sum(c / n * math.log(c * n / (cg[g] * cp[p])) for (g, p), c in joint.items())
    hg = -sum(c / n * math.log(c / n) for c in cg.values())
    hp = -sum(c / n * math.log(c / n) for c in cp.values())
    if hg == 0 and hp == 0:
        return 1.0
    denom = (hg + hp) / 2
    return mi / denom if denom > 0 else 0.0


def ari(gold: list[str], pred: list[int]) -> float:
    n = len(gold)
    c2 = lambda x: x * (x - 1) / 2  # noqa: E731
    cg, cp, joint = Counter(gold), Counter(pred), Counter(zip(gold, pred))
    sum_ij, sum_a, sum_b = (sum(c2(c) for c in x.values()) for x in (joint, cg, cp))
    total = c2(n)
    expected = sum_a * sum_b / total if total else 0.0
    denom = (sum_a + sum_b) / 2 - expected
    return (sum_ij - expected) / denom if denom != 0 else 1.0


def metrics(gold: list[str], pred: list[int]) -> dict:
    p, r, f = bcubed(gold, pred)
    return {"bcubed_p": round(p, 4), "bcubed_r": round(r, 4), "bcubed_f1": round(f, 4),
            "nmi": round(nmi(gold, pred), 4), "ari": round(ari(gold, pred), 4), "clusters": len(set(pred))}


# ---------- алгоритмы ----------
def run_online(vectors: np.ndarray, times: list[datetime], threshold: float, window_hours: float) -> list[int]:
    order = sorted(range(len(times)), key=lambda i: times[i])  # поток в порядке публикации
    clusterer = OnlineClusterer(threshold, timedelta(hours=window_hours))
    ids: dict[int, int] = {}
    out = [0] * len(times)
    for i in order:
        c, _ = clusterer.add(vectors[i], times[i])
        out[i] = ids.setdefault(id(c), len(ids))
    return out


def run_batch(vectors: np.ndarray, threshold: float) -> list[int]:
    """Агломеративная кластеризация, average linkage по косинусному сходству; объединяем, пока лучшая пара похожа не меньше порога."""
    v = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    n = len(v)
    S = v @ v.T
    np.fill_diagonal(S, -np.inf)
    size = np.ones(n)
    members = [[i] for i in range(n)]
    while n > 1:
        flat = int(np.argmax(S))
        i, j = divmod(flat, len(S))
        if S[i, j] < threshold:
            break
        new = (size[i] * S[i] + size[j] * S[j]) / (size[i] + size[j])  # Lance–Williams для average linkage
        S[i, :], S[:, i] = new, new
        S[i, i] = -np.inf
        S[j, :], S[:, j] = -np.inf, -np.inf
        size[i] += size[j]
        members[i] += members[j]
        members[j] = []
        n -= 1
    labels = [0] * len(v)
    for k, m in enumerate(x for x in members if x):
        for idx in m:
            labels[idx] = k
    return labels


# ---------- данные ----------
def read_rows(path: str, fmt: str | None) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    raw = raw.lstrip("\ufeff")
    fmt = fmt or ("csv" if path.lower().endswith(".csv") else "jsonl")
    if fmt == "csv":
        sample = raw[:2048]
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
        rows = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
    else:
        rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
    out = []
    for i, r in enumerate(rows, 1):
        gold = str(r.get("gold", "")).strip()
        if not gold:
            continue  # не размечено — пропускаем
        try:
            hours = float(str(r.get("hours_ago", 0)).replace(",", ".") or 0)
        except ValueError:
            raise SystemExit(f"Строка {i}: hours_ago должно быть числом, получено {r.get('hours_ago')!r}")
        out.append({**r, "gold": gold, "hours_ago": hours, "title": r.get("title", ""), "text": r.get("text", "") or ""})
    if len(out) < 10:
        raise SystemExit(f"Размечено слишком мало материалов ({len(out)}): заполните колонку gold хотя бы для нескольких десятков строк.")
    return out


async def embed(rows: list[dict], provider_name: str) -> np.ndarray:
    import os
    os.environ["LLM_PROVIDER"] = provider_name
    from app.providers import get_provider

    return await get_provider().embed([f"{r['title']}. {r['text'][:1200]}" for r in rows])


def explain_errors(rows: list[dict], gold: list[str], pred: list[int], limit: int = 5) -> None:
    """Показывает, где именно алгоритм ошибся: какие события раскололись и какие разные события склеились."""
    by_gold, by_pred = defaultdict(set), defaultdict(set)
    for g, p in zip(gold, pred):
        by_gold[g].add(p)
        by_pred[p].add(g)
    split = sorted(((g, ps) for g, ps in by_gold.items() if len(ps) > 1), key=lambda x: -len(x[1]))[:limit]
    merged = sorted(((p, gs) for p, gs in by_pred.items() if len(gs) > 1), key=lambda x: -len(x[1]))[:limit]
    print("  Ошибки на полном наборе:")
    if not split and not merged:
        print("    нет — все события собраны точно")
    for g, ps in split:
        print(f"    раскололось: событие «{g}» → {len(ps)} сюжетов (порог слишком строгий или тексты сильно различаются)")
    for p, gs in merged:
        titles = [rows[i]["title"][:50] for i, x in enumerate(pred) if x == p][:3]
        print(f"    склеилось: {len(gs)} разных событий ({', '.join(sorted(gs)[:4])}) в один сюжет, напр.: {' | '.join(titles)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Оценка качества кластеризации", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--data", required=True, help="файл JSONL/CSV или «-» для stdin")
    ap.add_argument("--format", choices=["jsonl", "csv"], help="по умолчанию по расширению; для stdin — jsonl")
    ap.add_argument("--provider", default="stub", choices=["stub", "openai"], help="stub — демо-эмбеддинги; openai — модель из LLM_BASE_URL/EMBED_MODEL")
    ap.add_argument("--window-hours", type=float, default=48)
    ap.add_argument("--thresholds", default="0.2,0.3,0.4,0.5,0.55,0.6,0.65,0.7,0.75,0.8,0.85,0.9")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, help="сохранить отчёт в JSON")
    a = ap.parse_args()

    rows = read_rows(a.data, a.format)
    gold = [r["gold"] for r in rows]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    max_h = max(r["hours_ago"] for r in rows)
    times = [base + timedelta(hours=max_h - r["hours_ago"]) for r in rows]
    vectors = asyncio.run(embed(rows, a.provider))
    thresholds = [float(x) for x in a.thresholds.split(",")]

    idx = list(range(len(rows)))
    random.Random(a.seed).shuffle(idx)
    dev, test = sorted(idx[: len(idx) // 2]), sorted(idx[len(idx) // 2:])
    sub = lambda ids, arr: [arr[i] for i in ids]  # noqa: E731
    evaluate = lambda ids, fn: metrics(sub(ids, gold), fn(ids))  # noqa: E731
    online = lambda t: (lambda ids: run_online(vectors[ids], sub(ids, times), t, a.window_hours))  # noqa: E731
    batch = lambda t: (lambda ids: run_batch(vectors[ids], t))  # noqa: E731

    print(f"Материалов: {len(rows)}, событий в разметке: {len(set(gold))}, эмбеддинги: {a.provider}, окно: {a.window_hours:g} ч")
    print("Порог выбирается на половине данных (dev), итоговые цифры — на другой половине (test).\n")
    report = {"n": len(rows), "true_events": len(set(gold)), "provider": a.provider, "window_hours": a.window_hours, "methods": {}}
    methods = [("online (как в продукте)", online)]
    if len(rows) <= MAX_BATCH:
        methods.append(("batch (эталон для сравнения)", batch))
    else:
        print(f"Пакетный baseline пропущен: материалов больше {MAX_BATCH}.\n")
    for name, factory in methods:
        dev_scores = {t: evaluate(dev, factory(t))["bcubed_f1"] for t in thresholds}
        best = max(dev_scores, key=lambda t: (dev_scores[t], -abs(t - 0.7)))
        full = list(range(len(rows)))  # весь набор в исходном порядке — так метки и предсказания гарантированно соответствуют друг другу
        res_test, res_all = evaluate(test, factory(best)), evaluate(full, factory(best))
        report["methods"][name] = {"threshold_from_dev": best, "test": res_test, "all": res_all, "dev_f1_by_threshold": dev_scores}
        print(f"{name}\n  лучший порог: {best} (F1 на dev = {dev_scores[best]:.3f})")
        print("  F1 по порогам: " + "  ".join(f"{t:g}→{f:.2f}" for t, f in dev_scores.items()))
        print(f"  на test: B-cubed P={res_test['bcubed_p']} R={res_test['bcubed_r']} F1={res_test['bcubed_f1']} | NMI={res_test['nmi']} | ARI={res_test['ari']} | сюжетов: {res_test['clusters']}")
        if name.startswith("online"):
            explain_errors(rows, gold, factory(best)(full), limit=4)
        print()
    print("Как читать: P — «в сюжете нет чужого» (чистота), R — «событие не раскололось» (полнота), F1 — их баланс.")
    print("Перенесите лучший порог в «Настройки проекта → Порог сходства». Если F1 низкий при любом пороге, дело в модели эмбеддингов или в разметке.")
    if a.out:
        a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт сохранён: {a.out}")


if __name__ == "__main__":
    main()
