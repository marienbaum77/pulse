# Pulse

[![Tests](https://github.com/marienbaum77/pulse/actions/workflows/test.yml/badge.svg)](https://github.com/marienbaum77/pulse/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-required-blue?logo=docker)](https://www.docker.com/products/docker-desktop/)

Self-hosted платформа, которая превращает поток новостей в посты для Telegram и webhook.
Читает подключённые RSS-источники, группирует материалы об одном событии в **сюжеты**, оценивает их вес, готовит черновик
и после проверки редактором (или автоматически) публикует.

![Главная страница](docs/images/main-page.png)

## Возможности

- **Сбор**: чтение RSS/Atom и поиск через Google News RSS
- **Сюжеты**: автоматическая группировка материалов об одном событии с весовой оценкой
- **Генерация**: синтез поста из нескольких источников через LLM (Groq, Ollama, vLLM)
- **Публикация**: в Telegram-канал, ручной / полуавтоматический / автоматический режимы
- **Десктоп**: установщик Windows с встроенным стеком Docker
- **Безопасность**: защита от SSRF, кэши изображений, ротация бэкапов

## Установка

Приложение работает внутри Docker — нужен только он: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
на Windows/macOS либо Docker Engine с Compose plugin **2.24+** на Linux.

### Windows: установщик

Скачайте [`Pulse-Setup-<версия>.exe`](https://github.com/marienbaum77/pulse/releases) и запустите его — исходники стека
встроены в пакет, поэтому git, Python и `npm` не нужны. Приложение установится в `%LOCALAPPDATA%\Programs\Pulse`,
на рабочем столе появится ярлык.

При первом запуске оно:

1. Проверяет Docker Desktop и, если его нет, предлагает скачать.
2. Создаёт `.env` со случайными паролями и секретами. Существующий `.env` не перезаписывается, поэтому приложение
   безопасно переустанавливать и обновлять.
3. Подбирает свободный порт: если `8080` занят другим экземпляром Pulse, берёт следующий свободный.
4. Собирает контейнеры, загружает модель эмбеддингов `bge-m3` и открывает интерфейс в отдельном окне.

#### Экраны первого запуска

![Docker Desktop требуется](docs/images/desktop-docker-required.png)

![Первоначальная настройка](docs/images/desktop-first-run.png)

![Готово к запуску](docs/images/desktop-ready-to-start.png)

![После запуска](docs/images/desktop-running.png)

Приложение спросит email и пароль администратора, ключ Groq и токен Telegram-бота; всё, кроме email, можно
пропустить — без ключа используется упрощённая генерация. Рабочая папка со стеком и `.env` — `%APPDATA%\Pulse\runtime`:
при желании тем же стеком можно управлять командами `docker compose` из этой папки.

Windows покажет предупреждение «Неизвестный издатель»: установщик не подписан сертификатом издателя, это ожидаемо.

### Linux / macOS

Ручной запуск — для Linux, macOS и для тех, кто предпочитает команду двойному клику.

```bash
cp .env.example .env
# задайте уникальные значения POSTGRES_PASSWORD и ADMIN_PASSWORD
# SECRET_KEY: openssl rand -hex 32
# LLM_API_KEY: ключ выбранного OpenAI-совместимого API (по умолчанию Groq)
docker compose up -d --build
docker compose exec ollama ollama pull bge-m3
docker compose exec api python -m app.seed      # опционально: пустой стартовый проект
```

В PowerShell вместо `cp` используйте `Copy-Item .env.example .env`. Откройте <http://localhost:8080>
и войдите под `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Если не запускали `app.seed`, создайте проект
в интерфейсе; затем добавьте RSS-ленты на странице «Источники» (по одной или импортом OPML).
Для этого локального варианта HTTPS не настроен: не выставляйте его напрямую в интернет.
На локальном компьютере сбор и расписание работают только пока компьютер и Docker запущены;
для круглосуточной работы используйте VPS.

## Развёртывание на VPS

Production-установка на Ubuntu с доменом и автоматическим HTTPS от Caddy:
[docs/deploy-vps.md](docs/deploy-vps.md).

## Настройка

Полное описание переменных окружения, моделей, режимов публикации и Telegram:
[docs/configuration.md](docs/configuration.md).

### Источники

Pulse читает только подключённые вами ленты:
- **Добавить RSS-ленту** или **импортировать OPML** (из Feedly, Inoreader и др.).
- **Искать статьи** — поисковый запрос превращается в Google News RSS-ленту; найденные URL проходят обычный конвейер. Уточняйте широкие запросы (`"Python разработка" -змея`, `site:habr.com`).
- Короткие RSS-фрагменты Pulse дозагружает со страницы статьи (лимит 2 МБ, защита от SSRF).
- Материалы старше окна проекта (`window_hours`, по умолчанию 48 ч) считаются устаревшими и в сюжеты не попадают.

### Модели

| Компонент | Назначение |
|---|---|
| Эмбеддер (по умолчанию bge-m3 через Ollama) | группировка материалов в сюжеты и сравнение заголовков сюжета с темой проекта |
| Чат-модель (по умолчанию Groq) | синтез одного поста из нескольких источников, перевод |

Режимы:
- **По умолчанию** — локальный bge-m3 + облачный чат по OpenAI-совместимому API.
- **`LLM_PROVIDER=stub`** — без чат-модели: пост собирается из предложений источников, сюжеты находятся хуже.
- **Своя модель** — любой OpenAI-совместимый сервер (Ollama, vLLM, облако): `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`; для эмбеддингов отдельно `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_MODEL`.

Модели можно менять в интерфейсе: **Система → Состояние → Модели**. Смена эмбеддера безопасна: векторы и сюжеты пересчитываются автоматически.
Тексты источников отправляются выбранному провайдеру — учитывайте его политику конфиденциальности. Для хорошего синтеза нужны модели 14B+ или внешний API.

### Режимы публикации

Задаются в «Настройки проекта → Публикация»:
- **Ручное утверждение** — каждый пост ждёт решения редактора («Черновики → На проверке»).
- **Полуавтоматически** — пост проходит автопроверки (источники, длина, заголовок, язык); без замечаний уходит в каналы сам, иначе остаётся на проверке с причиной.
- **Автопубликация** — обычные замечания не задерживают пост; на проверке остаются только пустой/слишком короткий текст, неполученная короткая статья (при единственном источнике) и несоответствие языку.

Нужен хотя бы один включённый канал. Автопроверки не заменяют проверку фактов. Расписание генерации задаётся в «Каналы и расписание».

### Telegram

1. Создайте бота у [@BotFather](https://t.me/BotFather) и добавьте его администратором канала с правом публикации.
2. Укажите `TELEGRAM_BOT_TOKEN` в `.env` и пересоздайте сервисы. Для production VPS:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
   ```
   Для локального запуска используйте `docker compose up -d`.
3. «Каналы и расписание → Добавить канал → Telegram», введите `@имя_канала`, нажмите «Проверить доступ» (ничего не публикует).

Подписчикам уходят заголовок, текст и названия источников; номера `[1]`, `[2]` убираются. Картинка идёт тем же сообщением, а если текст длиннее лимита подписи — отдельным сообщением перед текстом. Повторная отправка того же текста в канал блокируется на 24 часа.

### Пароль администратора

`ADMIN_PASSWORD` применяется только при первом запуске. Сброс:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.admin reset-password [--email user@example.com]
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.admin list-users
```

Для локальной установки опустите `-f docker-compose.yml -f docker-compose.prod.yml`.

### Порог сходства

«Порог сходства» определяет, когда два материала считаются одним событием. Подобрать его на размеченных данных:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python scripts/eval_clustering.py --data /app/sample_data/demo_news_hard.jsonl --provider stub   # проверка скрипта
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api python scripts/export_for_labeling.py --project 1 > to_label.csv                              # выгрузка для разметки
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api python scripts/eval_clustering.py --data - --format csv --provider openai < to_label.csv     # оценка
```

В `to_label.csv` исправьте колонку `gold` (одинаковая метка = одно событие), затем перенесите лучший порог в настройки проекта. Для локального запуска используйте `docker compose` без production overlay.

## Разработка

### Docker (рекомендуется)

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
# интерфейс: http://localhost:5173, API: http://localhost:8000/api/docs
```

Тесты выполняются на отдельной БД (они очищают данные):

```bash
# Linux / macOS:
sh backend/scripts/test-docker.sh
```

```powershell
# Windows (прямые слеши валидны в PowerShell):
pushd backend/scripts; ./test-docker.ps1; popd
```

### Без Docker

Нужны Python 3.12, Node 22, PostgreSQL 16 + pgvector.

```bash
# терминал 1 — API
cd backend && pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000

# терминал 2 — worker
python -m app.worker

# терминал 3 — интерфейс
cd web && npm install && npm run dev
```

При прямом запуске `pytest` `DATABASE_URL` должен указывать на базу с именем на `_test`.

### Десктоп-приложение (Electron)

```bash
cd desktop && npm install
npm start         # запуск из исходников, работает со стеком этого репозитория
npm run dist      # установщик: desktop/release/Pulse-Setup-<версия>.exe
```

См. [desktop/README.md](desktop/README.md).

## Архитектура и модули

Подробное описание модулей и конвейера: [docs/development.md](docs/development.md#архитектура).

| Компонент | Что делает |
|---|---|
| `backend/app/api` | FastAPI-роутеры по областям: `projects`, `sources`, `channels`, `schedules`, `content`, `auth`, `system` |
| `backend/app/pipeline` | сбор, кластеризация, оценка, генерация, публикация черновиков |
| `backend/app/worker.py` | планировщик, очередь задач, обработка, отправка |
| PostgreSQL + pgvector | данные, векторы, очередь (`SKIP LOCKED`), события (`LISTEN/NOTIFY`) |
| `web` (React, Vite) | интерфейс редактора за nginx |

## Безопасность

- **Сессии**: `HttpOnly`/`SameSite=Lax` cookie, подпись `SECRET_KEY`, `COOKIE_SECURE=true` в production, пароли — Argon2id.
- **Выход из всех сессий**: профиль убивает все сессии пользователя через `session_version`.
- **Webhook**: обязательный HTTPS, секрет в заголовке `X-Pulse-Signature` (`t=,<hex>`; HMAC-SHA256 от `t + "." + raw-body`, окно пять минут), идемпотентность по `event_id`.
- **SSRF**: адреса RSS и картинок проверяются на каждой редирект-ступени, приватные/loopback/link-local адреса запрещены (`ALLOW_PRIVATE_URLS=false` по умолчанию).
- **CSP и заголовки**: `Content-Security-Policy`, `X-Frame-Options: DENY`, `Referrer-Policy`, `X-Content-Type-Options` добавляются reverse-proxy.
- **Мультимодельный ввод**: картинки не хранятся в БД и отдаются через allowlist-прокси, который заново проверяет адрес.
- **SQL и миграции**: параметрированные запросы; Alembic настроен через `render_as_batch`.
- **Резервные копии**: автоматические дампы в `./backups` с ротацией (см. [docs/backup.md](docs/backup.md)).

Подробности: [docs/development.md](docs/development.md).

## Ограничения

- Только RSS/Atom и поиск через Google News RSS; полный обход сайтов и JavaScript-страницы не поддерживаются.
- Автопроверки эвристические.
- Слабые локальные модели пишут посты хуже; на CPU генерация медленная.
- Поиск опирается на Google News RSS и может не найти узкие/локальные сюжеты.
- Локальный bge-m3 на CPU замедляется при разовом разборе тысяч материалов.
- Локальный вариант без reverse proxy не имеет HTTPS.

## Лицензия

MIT. См. [LICENSE](LICENSE).
