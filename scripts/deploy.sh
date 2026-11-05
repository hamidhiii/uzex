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

PORT_EXT="${HOST_PORT:-8000}"
MARK=".uzex-deploy"   # метка нашей папки: в чужую папку скрипт не пишет

echo "== 0. Безопасность: папка $DIR и порт $PORT_EXT"
$SSH "set -e
  if [ -e $DIR ] && [ ! -f $DIR/$MARK ]; then echo 'СТОП: $DIR уже существует и это не наша папка'; exit 3; fi
  if [ ! -f $DIR/$MARK ] && (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -qE '[:.]$PORT_EXT\s'; then
    echo 'СТОП: порт $PORT_EXT занят — задайте HOST_PORT=…'; exit 4; fi
  echo ok"

echo "== 1. Docker на сервере"
$SSH "set -e; S=$SUDO; if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | \$S sh; fi; \
      \$S mkdir -p $DIR/data $DIR/logs/bot && \$S chown -R \$(id -u):\$(id -g) $DIR && touch $DIR/$MARK; docker --version || \$S docker --version"

echo "== 2. Код и база"
rsync -az --delete \
  --exclude '.venv/' --exclude '**/__pycache__/' --exclude '.pytest_cache/' --exclude 'logs/' \
  --exclude 'data/*.csv' --exclude 'data/uzex_p2.duckdb' --exclude 'data/*.tmp' --exclude 'data/*.wal' \
  --exclude '.env' --exclude 'bot/.env' --exclude 'bot/logs/' --exclude '.claude/' --exclude 'eval/report*.md' --exclude '.git/' --exclude '.idea/' --exclude '.DS_Store' \
  --exclude "$MARK" \
  ./ "$TARGET:$DIR/"

echo "== 3. Секреты (.env, bot/.env), права 600"
$SSH "umask 077; cat > $DIR/.env && chmod 600 $DIR/.env" < .env
[ -n "${SKIP_BOT:-}" ] || $SSH "umask 077; cat > $DIR/bot/.env && chmod 600 $DIR/bot/.env" < bot/.env

echo "== 4. Сборка и запуск"
SERVICES="agent bot"; [ -z "${SKIP_BOT:-}" ] || SERVICES="agent"
$SSH "set -e; cd $DIR; S=$SUDO; \$S env HOST_PORT=$PORT_EXT docker compose up -d --build $SERVICES; \$S docker compose ps"

echo "== 5. Проверка"
for i in $(seq 1 30); do
  if $SSH "curl -fs localhost:$PORT_EXT/health" 2>/dev/null; then echo; break; fi; sleep 2
done
$SSH "curl -s -X POST localhost:$PORT_EXT/ask -H 'Content-Type: application/json' \
      -d '{\"question\":\"Сколько сделок было в 2021 году?\",\"user_id\":\"deploy\"}' | head -c 300"; echo
echo "Готово: http://${TARGET#*@}:$PORT_EXT  (POST /ask, GET /health)"
