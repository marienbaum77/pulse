# Устранение неполадок

## Caddy не выдаёт сертификат

Проверьте `PULSE_DOMAIN`, DNS-записи `A`/`AAAA` и доступность TCP 80/443
снаружи, затем посмотрите `logs caddy`.

## Не открывается сайт

Выполните `ps`, проверьте журналы `web`, `api` и `caddy`; в production порт
`PULSE_PORT` привязан только к loopback, внешний доступ идёт через домен и Caddy.

## Порт уже занят

`port is already allocated`: порт `PULSE_PORT` занят другим стеком — укажите в
`.env` свободный и выполните `docker compose up -d`. Десктоп-приложение
подбирает порт само.

## Нет генерации или эмбеддингов

Проверьте ключ/URL/название чат-модели, наличие `bge-m3` командой
`docker compose exec ollama ollama list` и логи `api`/`worker`.

## Не хватает места

Проверьте `df -h` и содержимое `backups/`; настройки ротации ограничивают
количество и возраст бэкапов, но не заменяют контроль свободного места.

## Сброс пароля администратора

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.admin reset-password [--email user@example.com]
```

Для локальной установки опустите `-f docker-compose.yml -f docker-compose.prod.yml`.
