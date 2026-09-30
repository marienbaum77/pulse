"""Выгружает материалы проекта в CSV для разметки «золотого датасета».

Колонка gold заполнена текущими сюжетами Pulse (c123). Открыть в Excel / LibreOffice / Google Таблицах, отсортировано по сюжетам:
  • если материал попал не в тот сюжет — измените его gold на метку нужного (или придумайте новую, например «утечка-аэрокласт»);
  • если два сюжета — на самом деле одно событие — дайте им одинаковый gold;
  • материалы без значения gold в оценке не участвуют.
Внимание: предзаполненные метки идут от самого алгоритма, поэтому оценка «по умолчанию» будет завышена. Разметке помогает только честный просмотр
каждой группы; для беспристрастной оценки для части материалов ставьте метки с нуля (флаг --blank).

  docker compose exec -T api python scripts/export_for_labeling.py --project 1 > to_label.csv
  docker compose exec -T api python scripts/eval_clustering.py --data - --format csv --provider openai < to_label.csv
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402
from app.textutil import utcnow  # noqa: E402


async def main(project_id: int, days: int, limit: int, blank: bool) -> None:
    await db.open_pool()
    try:
        rows = await db.fetchall(
            "SELECT i.id, i.title, left(i.text, 1200) AS text, i.published_at, COALESCE(s.name, '') AS source, COALESCE(s.authority, 0.5) AS authority, "
            "i.cluster_id, COALESCE(c.title, '') AS cluster_title "
            "FROM items i LEFT JOIN sources s ON s.id = i.source_id LEFT JOIN clusters c ON c.id = i.cluster_id "
            "WHERE i.project_id = %s AND i.status = 'clustered' AND i.published_at >= now() - make_interval(days => %s) "
            "ORDER BY i.cluster_id, i.published_at LIMIT %s",
            (project_id, days, limit),
        )
    finally:
        await db.close_pool()
    now = utcnow()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write("\ufeff")  # BOM, чтобы Excel правильно открыл кириллицу
    w = csv.writer(sys.stdout, lineterminator="\n")
    w.writerow(["gold", "title", "text", "source", "authority", "hours_ago", "current_cluster", "id"])
    for r in rows:
        hours = round((now - r["published_at"]).total_seconds() / 3600, 2)
        w.writerow(["" if blank else f"c{r['cluster_id']}", r["title"], r["text"], r["source"], r["authority"], hours, r["cluster_title"], r["id"]])
    print(f"Выгружено {len(rows)} материалов", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True, help="номер проекта")
    ap.add_argument("--days", type=int, default=14, help="за сколько последних дней (по умолчанию 14)")
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--blank", action="store_true", help="оставить gold пустым (разметка с нуля, без подсказок алгоритма)")
    a = ap.parse_args()
    asyncio.run(main(a.project, a.days, a.limit, a.blank))
