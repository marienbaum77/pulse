# Бэкапы и восстановление

## Резервное копирование

Контейнер `backup` создаёт сжатый дамп PostgreSQL в `./backups` примерно раз в
сутки и удаляет старые по `BACKUP_KEEP_DAYS` / `BACKUP_KEEP_COUNT`.

Первая копия появляется вскоре после запуска backup-сервиса. Дампы не содержат
векторы (они пересчитываются) и таблицы `jobs` / `llm_calls`; каталог находится
на том же диске, что и сервер, поэтому периодически копируйте дампы в отдельное
защищённое хранилище.

Проверьте наличие файлов:

```bash
ls -lh backups/
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=50 backup
```

Настройки ротации задаются в `.env`:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BACKUP_KEEP_DAYS` | 14 | Сколько дней хранить дампы |
| `BACKUP_KEEP_COUNT` | 7 | Максимум файлов бэкапа |
| `BACKUP_INTERVAL_HOURS` | 24 | Интервал создания дампа |
| `BACKUP_STRIP_VECTORS` | true | Исключить пересчитываемые векторы |

## Восстановление базы

**Восстановление заменяет текущую базу целиком. Сначала сохраните текущие данные.**

1. Выберите нужный файл дампа, остановите сервисы, подключающиеся к базе:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api worker backup
```

2. Пересоздайте пустую БД:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db dropdb -U pulse pulse
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db createdb -U pulse pulse
```

3. Импортируйте SQL (замените `pulse-ДАТА.sql.gz` на имя нужного файла):

```bash
gzip -dc backups/pulse-ДАТА.sql.gz | docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db psql -v ON_ERROR_STOP=1 -U pulse -d pulse
```

4. Перезапустите стек:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

API автоматически применит миграции; векторы будут созданы заново.
