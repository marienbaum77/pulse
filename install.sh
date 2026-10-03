#!/usr/bin/env bash
# Автоустановка Pulse на Linux/macOS: Docker -> .env -> запуск -> (опционально) HTTPS и firewall.
# Запуск: bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
rand_hex() { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

step "Проверка Docker"
if ! command -v docker >/dev/null 2>&1; then
  [ "$(uname -s)" = "Linux" ] || die "Установите Docker Desktop: https://www.docker.com/products/docker-desktop/ и запустите скрипт снова."
  command -v curl >/dev/null 2>&1 || die "Нужен curl."
  echo "Устанавливаю Docker (официальный скрипт get.docker.com)..."
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker 2>/dev/null || true
fi
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if $SUDO docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"; else die "Docker не запущен или нет доступа."; fi
fi
$DOCKER compose version >/dev/null 2>&1 || die "Не найден docker compose (нужен Compose plugin 2.24+)."
COMPOSE="$DOCKER compose"

step "Настройка .env"
DOMAIN=""
if [ -f .env ]; then
  echo ".env уже существует — оставляю без изменений."
  DOMAIN=$(grep -E '^PULSE_DOMAIN=' .env | head -1 | cut -d= -f2 | sed 's/[[:space:]]*#.*//; s/[[:space:]]//g' || true)
else
  read -r -p "Email администратора [admin@example.com]: " EMAIL; EMAIL=${EMAIL:-admin@example.com}
  read -r -s -p "Пароль администратора (Enter — сгенерировать): " APASS; echo
  GENERATED=0; [ -z "$APASS" ] && { APASS=$(rand_hex 8); GENERATED=1; }
  read -r -p "API-ключ чат-модели Groq (Enter — пропустить): " APIKEY
  read -r -p "Токен Telegram-бота (Enter — пропустить): " TG
  read -r -p "Домен для HTTPS на сервере, напр. pulse.example.com (Enter — локально без HTTPS): " DOMAIN

  cp .env.example .env
  setv() { # setv KEY VALUE — без sed, чтобы спецсимволы в значении не ломали замену
    local k="$1" v="$2" tmp; tmp=$(mktemp)
    awk -v k="$k" -v v="$v" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > "$tmp" && cat "$tmp" > .env; rm -f "$tmp"
  }
  setv POSTGRES_PASSWORD "$(rand_hex 16)"
  setv SECRET_KEY "$(rand_hex 32)"
  setv ADMIN_EMAIL "$EMAIL"
  setv ADMIN_PASSWORD "$APASS"
  setv LLM_API_KEY "$APIKEY"
  setv TELEGRAM_BOT_TOKEN "$TG"
  [ -z "$APIKEY" ] && setv LLM_PROVIDER stub
  if [ -n "$DOMAIN" ]; then setv PULSE_DOMAIN "$DOMAIN"; setv COOKIE_SECURE true; fi
  chmod 600 .env
  echo ".env создан (секреты сгенерированы). Не публикуйте этот файл."
  [ "$GENERATED" = 1 ] && printf '\033[33mПароль администратора: %s  (запишите его)\033[0m\n' "$APASS"
fi

FILES="-f docker-compose.yml"
[ -n "$DOMAIN" ] && FILES="$FILES -f docker-compose.prod.yml"

if [ -n "$DOMAIN" ] && command -v ufw >/dev/null 2>&1; then
  step "Firewall (UFW): открываю 22, 80, 443"
  $SUDO ufw allow OpenSSH >/dev/null; $SUDO ufw allow 80/tcp >/dev/null; $SUDO ufw allow 443/tcp >/dev/null
  $SUDO ufw --force enable >/dev/null
fi

step "Сборка и запуск (первый раз занимает несколько минут)"
$COMPOSE $FILES up -d --build

step "Загрузка модели эмбеддингов bge-m3 (~1.2 ГБ, один раз)"
$COMPOSE $FILES exec -T ollama ollama pull bge-m3
$COMPOSE $FILES exec -T api python -m app.seed

PORT=$(grep -E '^PULSE_PORT=' .env | head -1 | cut -d= -f2 | sed 's/[[:space:]]*#.*//; s/[[:space:]]//g'); PORT=${PORT:-8080}
if [ -n "$DOMAIN" ]; then URL="https://$DOMAIN"; CHECK="http://127.0.0.1:$PORT"; else URL="http://localhost:$PORT"; CHECK="$URL"; fi

step "Жду готовности интерфейса"
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "$CHECK" 2>/dev/null && { OK=1; break; }; sleep 3
done
[ "${OK:-0}" = 1 ] || die "Интерфейс не отвечает. Логи: $COMPOSE $FILES logs --tail=100 api web"

printf '\n\033[32mГотово. Pulse: %s\033[0m\n' "$URL"
[ -n "$DOMAIN" ] && echo "Для HTTPS DNS-запись A домена должна указывать на IP этого сервера; сертификат Caddy выдаст сам."
(command -v xdg-open >/dev/null && [ -z "$DOMAIN" ] && xdg-open "$URL" >/dev/null 2>&1) || (command -v open >/dev/null && [ -z "$DOMAIN" ] && open "$URL") || true
