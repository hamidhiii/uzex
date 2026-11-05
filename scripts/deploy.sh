#!/usr/bin/env bash
# Деплой agent (API + веб) и Telegram-бота на VPS (Ubuntu/Debian) через Docker Compose.
#   scripts/deploy.sh user@IP            — первый раз и любые обновления
#   SKIP_BOT=1 scripts/deploy.sh user@IP — только API (если бот запущен у участника 1)
set -euo pipefail

TARGET="${1:?usage: scripts/deploy.sh user@host}"
DIR="${REMOTE_DIR:-/opt/uzex}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f data/uzex.duckdb ] || { echo "нет data/uzex.duckdb"; exit 1; }
[ -f .env ] || { echo "нет .env"; exit 1; }
if [ -z "${SKIP_BOT:-}" ] && [ ! -f bot/.env ]; then echo "нет bot/.env (или SKIP_BOT=1)"; exit 1; fi

SSH="ssh -o StrictHostKeyChecking=accept-new $TARGET"
SUDO='$( [ "$(id -u)" = 0 ] || echo sudo )'

echo "== 1. Docker на сервере"
$SSH "set -e; S=$SUDO; if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | \$S sh; fi; \
      \$S mkdir -p $DIR/data $DIR/logs/bot && \$S chown -R \$(id -u):\$(id -g) $DIR; docker --version || \$S docker --version"

echo "== 2. Код и база"
rsync -az --delete \
  --exclude '.venv/' --exclude '**/__pycache__/' --exclude '.pytest_cache/' --exclude 'logs/' \
  --exclude 'data/*.csv' --exclude 'data/uzex_p2.duckdb' --exclude 'data/*.tmp' --exclude 'data/*.wal' \
  --exclude '.env' --exclude 'bot/.env' --exclude 'bot/logs/' --exclude '.claude/' --exclude 'eval/report*.md' \
  ./ "$TARGET:$DIR/"

echo "== 3. Секреты (.env, bot/.env), права 600"
rsync -az --chmod=F600 .env "$TARGET:$DIR/.env"
[ -n "${SKIP_BOT:-}" ] || rsync -az --chmod=F600 bot/.env "$TARGET:$DIR/bot/.env"

echo "== 4. Сборка и запуск"
SERVICES="agent bot"; [ -z "${SKIP_BOT:-}" ] || SERVICES="agent"
$SSH "set -e; cd $DIR; S=$SUDO; \$S docker compose up -d --build $SERVICES; \$S docker compose ps"

echo "== 5. Проверка"
for i in $(seq 1 30); do
  if $SSH "curl -fs localhost:8000/health" 2>/dev/null; then echo; break; fi; sleep 2
done
$SSH "curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
      -d '{\"question\":\"Сколько сделок было в 2021 году?\",\"user_id\":\"deploy\"}' | head -c 300"; echo
echo "Готово: http://${TARGET#*@}:8000  (POST /ask, GET /health)"
