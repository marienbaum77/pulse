# Безопасность

Как Pulse защищает данные и инфраструктуру. О найденной уязвимости сообщайте
через [SECURITY.md](../SECURITY.md).

- **Сессии**: `HttpOnly`/`SameSite=Lax` cookie, подпись `SECRET_KEY`, `COOKIE_SECURE=true` в production, пароли — Argon2id.
- **Выход из всех сессий**: профиль завершает все сессии пользователя через `session_version`.
- **Секреты каналов**: `secret` (webhook), `access_token` (VK) и Discord `webhook_url` хранятся в JSONB как `enc:v1:…` (AES-256-GCM, ключ = SHA-256 от `SECRET_KEY`). В ответах API поля не отдаются, только флаг `has_secret`. Смена `SECRET_KEY` сделает старые секреты нечитаемыми — не ротируйте ключ без пересохранения каналов.
- **Webhook**: секрет в заголовке `X-Pulse-Signature` (`sha256=<hex>`; HMAC-SHA256 от raw-body), идемпотентность по `Idempotency-Key`.
- **SSRF**: адреса RSS, картинок и исходящих webhook проверяются на каждом шаге редиректа; приватные, loopback и link-local адреса запрещены (`ALLOW_PRIVATE_URLS=false` по умолчанию). Исходящий HTTP идёт через `GuardedAsyncTransport`: после резолва соединение устанавливается к уже проверенному IP, заголовки `Host` и TLS SNI сохраняют исходный hostname (защита от DNS-rebinding / TOCTOU).
- **CSP и заголовки**: `Content-Security-Policy`, `X-Frame-Options: DENY`, `Referrer-Policy`, `X-Content-Type-Options` добавляются reverse-proxy.
- **Изображения**: не хранятся в БД и отдаются через allowlist-прокси, который заново проверяет адрес.
- **SQL и миграции**: параметризованные запросы; миграции — пронумерованные SQL-файлы в `backend/app/migrations`, применяются при старте API и worker и фиксируются в таблице `schema_migrations`.
- **Резервные копии**: автоматические дампы в `./backups` с ротацией, см. [backup.md](backup.md).
