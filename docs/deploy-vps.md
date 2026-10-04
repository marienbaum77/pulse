# Развёртывание на VPS

Ниже — production-установка на Ubuntu с доменом и автоматическим HTTPS от Caddy.
В командах используется production Compose overlay; выполняйте их из корня
клонированного репозитория.

## 1. Подготовьте сервер и домен

- Ubuntu 22.04/24.04 и Docker Engine с Compose plugin **2.24+** ([официальная установка Docker](https://docs.docker.com/engine/install/ubuntu/)).
- Для небольшого сервера ориентируйтесь на 1–2 vCPU, 2 ГБ RAM для приложения и ещё около 2 ГБ для локальной модели эмбеддингов `bge-m3`. Большая нагрузка или локальная чат-модель требуют дополнительных ресурсов.
- Создайте DNS-запись `A` для домена, указывающую на публичный IPv4 сервера. Если настроена запись `AAAA`, она тоже должна вести на этот сервер.
- Разрешите входящие TCP-порты 80 и 443 в firewall провайдера и на сервере; порт 22 оставьте доступным для SSH. Например, с UFW (перед включением убедитесь, что SSH разрешён):

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## 2. Скачайте проект и подготовьте секреты

```bash
git clone https://github.com/marienbaum77/pulse.git
cd pulse
cp .env.example .env
openssl rand -hex 32
```

Отредактируйте `.env`:

- `POSTGRES_PASSWORD` — уникальный пароль базы. Для простого и надёжного значения сгенерируйте hex: `openssl rand -hex 32`.
- `SECRET_KEY` — вставьте отдельный результат `openssl rand -hex 32` (не используйте пароль базы повторно).
- `ADMIN_EMAIL` и `ADMIN_PASSWORD` — адрес и сильный пароль первого администратора.
- `PULSE_DOMAIN` — домен без `https://`, например `pulse.example.com`.
- `COOKIE_SECURE=true` — обязательно для production HTTPS.
- `LLM_API_KEY` — ключ провайдера чат-модели. По умолчанию используется Groq; для другого OpenAI-совместимого API задайте также его URL и имя модели. Без чат-модели можно выбрать `LLM_PROVIDER=stub`, но синтез будет упрощённым.

Не публикуйте `.env` и не отправляйте его в репозиторий. Не меняйте `SECRET_KEY` после запуска: это инвалидирует подписанные им сессии. `ADMIN_PASSWORD` используется только при создании первого администратора; последующее редактирование `.env` не сбросит существующий пароль.

## 3. Запустите Pulse

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec ollama ollama pull bge-m3
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.seed
```

`app.seed` необязателен: он создаёт пустой стартовый проект с консольным каналом, но не добавляет демонстрационные ленты или новости. Миграции базы применяются автоматически при старте API и worker.

Проверьте запуск и журналы:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=100 api worker caddy
```

Когда сервисы запущены и DNS уже обновился, откройте `https://<PULSE_DOMAIN>` и войдите под `ADMIN_EMAIL` / `ADMIN_PASSWORD`. В приложении задайте тему проекта, добавьте RSS-источники и настройте канал и расписание публикаций. Pulse не начинает публиковать сам, пока не настроены проект, источники, канал и расписание.

## 4. Настройте публикацию

Для Telegram создайте бота через [@BotFather](https://t.me/BotFather), добавьте его администратором канала с правом публикации и задайте `TELEGRAM_BOT_TOKEN` в `.env`. После изменения конфигурации пересоздайте сервисы:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Затем в интерфейсе откройте «Каналы и расписание → Добавить канал → Telegram», укажите `@имя_канала` и проверьте доступ. Подробности режимов публикации приведены в разделе [Конфигурация](configuration.md).

## Обновление, остановка и резервные копии

Данные PostgreSQL и Ollama хранятся в Docker volumes и переживают пересоздание контейнеров. Для остановки production-стека выполните `docker compose -f docker-compose.yml -f docker-compose.prod.yml down`; эта команда сохраняет volumes. **Не добавляйте `-v`**, если не хотите удалить базу и загруженные модели.

Для обновления из Git:

```bash
git pull --ff-only
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=100 api worker
```

Перед крупным обновлением убедитесь, что есть свежий дамп. Миграции запускаются автоматически при старте; дамп позволяет восстановить данные, если обновление не удалось.

Контейнер `backup` создаёт сжатый дамп PostgreSQL в `./backups` примерно раз в сутки и удаляет старые по `BACKUP_KEEP_DAYS` / `BACKUP_KEEP_COUNT`. Первая копия появляется вскоре после запуска backup-сервиса. Дампы не содержат векторы (они пересчитываются) и таблицы `jobs` / `llm_calls`; каталог находится на том же диске, что и сервер, поэтому периодически копируйте дампы в отдельное защищённое хранилище.

Проверьте наличие файлов:

```bash
ls -lh backups/
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=50 backup
```

Для подробностей по бэкапам и восстановлению см. [docs/backup.md](backup.md).
