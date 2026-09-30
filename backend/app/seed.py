"""Создание пустого стартового проекта, если в базе ещё ничего нет.
Запуск: python -m app.seed

Демо-новости и вымышленные ленты больше не загружаются — источники добавляются через интерфейс
(в том числе импортом OPML)."""
import argparse
import asyncio

from psycopg.types.json import Jsonb

from . import db
from .pipeline.scoring import DEFAULT_WEIGHTS


async def main() -> None:
    await db.open_pool()
    project = await db.fetchone("SELECT id FROM projects ORDER BY id LIMIT 1")
    if not project:
        project = await db.fetchone(
            "INSERT INTO projects(name, topic, generation_mode, sim_threshold, weights, min_items) VALUES (%s,%s,%s,%s,%s,2) RETURNING id",
            (
                "Мой канал",
                "",
                "extractive",
                0.72,
                Jsonb(DEFAULT_WEIGHTS),
            ),
        )
        await db.execute(
            "INSERT INTO channels(project_id, type, name, config) VALUES (%s, 'console', 'Консоль (проверка)', '{}')",
            (project["id"],),
        )
        print(f"Создан проект #{project['id']}. Добавьте RSS на странице «Источники».")
    else:
        print(f"Проект уже есть (#{project['id']}), ничего не создано.")
    await db.close_pool()


if __name__ == "__main__":
    argparse.ArgumentParser(description="Создать стартовый проект, если его ещё нет").parse_args()
    asyncio.run(main())
