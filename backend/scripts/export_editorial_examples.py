"""Выгружает примеры редакционной модели проекта в JSONL для отложенной оценки.

  docker compose exec -T api python scripts/export_editorial_examples.py --project 1 > editorial.jsonl
  python backend/scripts/export_editorial_examples.py --project 1 --out editorial.jsonl

Статус items.status='ignored' здесь не используется: это очистка потока, а не отрицательное решение редактора.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402
from app.textutil import utcnow  # noqa: E402


async def export(project_id: int) -> list[dict]:
    await db.open_pool()
    try:
        rows = await db.fetchall(
            "SELECT e.id, e.label, e.source_kind, e.hook_text, e.sample_weight, e.cluster_id, "
            "COALESCE(NULLIF(e.meta->>'published_at', '')::timestamptz, e.created_at) AS observed_at, "
            "COALESCE(c.source_count, 0) AS source_count, "
            "COALESCE((SELECT count(*) FROM items i WHERE i.cluster_id = e.cluster_id AND i.status = 'clustered' "
            "AND i.published_at >= now() - interval '6 hours'), 0) AS recent_items, "
            "COALESCE((SELECT avg(i.topic_score) FROM items i WHERE i.cluster_id = e.cluster_id "
            "AND i.status = 'clustered' AND i.topic_score IS NOT NULL), 0.5) AS topic_fit, "
            "p.topic, COALESCE(p.interest_criteria, '') AS interest_criteria "
            "FROM editorial_examples e JOIN projects p ON p.id = e.project_id "
            "LEFT JOIN clusters c ON c.id = e.cluster_id WHERE e.project_id = %s ORDER BY e.created_at, e.id",
            (project_id,),
        )
    finally:
        await db.close_pool()
    now = utcnow()
    return [{
        "title": row["hook_text"],
        "label": int(row["label"]),
        "source_kind": row["source_kind"],
        "hours_ago": round(max(0.0, (now - row["observed_at"]).total_seconds() / 3600), 3),
        "source_count": int(row["source_count"]),
        "recent_items": int(row["recent_items"]),
        "topic_fit": float(row["topic_fit"]),
        "cluster_id": row["cluster_id"],
        "created_at": row["observed_at"].isoformat(),
        "sample_weight": float(row["sample_weight"]),
        "topic": row["topic"],
        "interest_criteria": row["interest_criteria"],
    } for row in rows]


async def main(project_id: int, output: Path | None) -> None:
    rows = await export(project_id)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    print(f"Выгружено {len(rows)} примеров", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=int, required=True, help="номер проекта")
    parser.add_argument("--out", type=Path, help="файл JSONL; по умолчанию stdout")
    args = parser.parse_args()
    asyncio.run(main(args.project, args.out))
