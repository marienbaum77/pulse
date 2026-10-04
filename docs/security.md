# Безопасность

Как Pulse защищает данные и инфраструктуру. О найденной уязвимости сообщайте
через [SECURITY.md](../SECURITY.md).

- **Сессии**: `HttpOnly`/`SameSite=Lax` cookie, подпись `SECRET_KEY`, `COOKIE_SECURE=true` в production, пароли — Argon2id.
- **Выход из всех сессий**: профиль завершает все сессии пользователя через `session_version`.
- **Webhook**: обязательный HTTPS, секрет в заголовке `X-Pulse-Signature` (`t=,<hex>`; HMAC-SHA256 от `t + "." + raw-body`, окно пять минут), идемпотентность по `event_id`.
- **SSRF**: адреса RSS и картинок проверяются на каждом шаге редиректа, приватные, loopback и link-local адреса запрещены (`ALLOW_PRIVATE_URLS=false` по умолчанию).
- **CSP и заголовки**: `Content-Security-Policy`, `X-Frame-Options: DENY`, `Referrer-Policy`, `X-Content-Type-Options` добавляются reverse-proxy.
- **Изображения**: не хранятся в БД и отдаются через allowlist-прокси, который заново проверяет адрес.
- **SQL и миграции**: параметризованные запросы; миграции — пронумерованные SQL-файлы в `backend/app/migrations`, применяются при старте API и worker и фиксируются в таблице `schema_migrations`.
- **Резервные копии**: автоматические дампы в `./backups` с ротацией, см. [backup.md](backup.md).
