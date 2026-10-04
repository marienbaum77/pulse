# Разработка

## Локальный запуск

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build   # интерфейс :5173, API :8000 (/api/docs)
```

## Тесты

Тесты выполняются на отдельной БД (они очищают данные):

```powershell
.\backend\scripts\test-docker.ps1      # Linux/macOS: sh backend/scripts/test-docker.sh
```

Без Docker нужны Python 3.12, Node 22, PostgreSQL 16 + pgvector:

```bash
pip install -r backend/requirements-dev.txt
uvicorn app.main:app --reload
python -m app.worker
cd web && npm install && npm run dev
```

При прямом запуске `pytest` `DATABASE_URL` должен указывать на базу с именем на `_test`.

## Архитектура

| Компонент | Что делает |
|---|---|
| `backend/app/api` | FastAPI-роутеры по областям: `projects`, `sources`, `channels`, `schedules`, `content`, `auth`, `system` |
| `backend/app/pipeline` | сбор, кластеризация, оценка, генерация, публикация черновиков |
| `backend/app/worker.py` | планировщик, очередь задач, обработка, отправка |
| PostgreSQL + pgvector | данные, векторы, очередь (`SKIP LOCKED`), события (`LISTEN/NOTIFY`) |
| `web` (React, Vite) | интерфейс редактора за nginx |

- **Сюжет** — группа материалов об одном событии (с весом); **черновик** — текст поста по сюжету или дайджесту.
- **Вес сюжета** — взвешенная сумма признаков; главный — сходство заголовков с темой проекта, вторичные — охват, авторитетность источников, свежесть, скорость роста. Тексты статей не участвуют в оценке соответствия теме, чтобы посторонний текст страницы не перевешивал заголовок.
- Источники в тексте помечаются `[n]`, по ним строятся автопроверки.
- Картинки не хранятся в БД: берутся из RSS или `og:image` и отдаются через прокси API.

## Ограничения

- Только RSS/Atom и поиск через Google News RSS; полный обход сайтов и JavaScript-страницы не поддерживаются.
- Автопроверки эвристические.
- Слабые локальные модели пишут посты хуже; на CPU генерация медленная.
