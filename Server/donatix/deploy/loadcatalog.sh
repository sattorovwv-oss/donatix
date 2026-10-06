#!/usr/bin/env bash
# Загрузить ВЕСЬ каталог (FazerCards + CoinDrop) через сервер и сохранить в базу проекта.
# Работает в фоне — можно закрыть SSH, загрузка продолжится. В конце шлёт итог в админ-бот.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/loadcatalog.sh | sudo bash
set -uo pipefail
APP_DIR="/home/donatix/app"; ENV_FILE="$APP_DIR/donatix/.env"; BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
LOG="/home/donatix/catalog-load.log"
say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден."
get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"; }

say "Обновляю код"
sudo -u donatix git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH" || true

say "Запускаю загрузку каталога в фоне"
: > "$LOG"; chown donatix:donatix "$LOG"
# setsid+nohup — загрузка переживёт закрытие SSH; вывод в журнал
sudo -u donatix bash -c "cd '$APP_DIR' && setsid nohup .venv/bin/python -m donatix sync >> '$LOG' 2>&1 &"
echo "Журнал: $LOG"
say "Прогресс (можно закрыть окно — загрузка продолжится)"
for i in $(seq 1 150); do            # показываю до ~5 минут, потом отпускаю
  sleep 2
  tail -n 1 "$LOG" 2>/dev/null | grep -qi "Готово и сохранено" && break
  tail -n 1 "$LOG" 2>/dev/null | grep -qi "уже загружается" && break
done
echo "──────── последние строки журнала ────────"
tail -n 6 "$LOG" 2>/dev/null || true
echo "──────────────────────────────────────────"

if grep -qi "Готово и сохранено" "$LOG"; then
  RESULT=$(grep -i "Готово и сохранено" "$LOG" | tail -1)
  say "Каталог загружен и сохранён в базу"
  echo "$RESULT"
  TOKEN=$(get DONATIX_ALERT_TELEGRAM_TOKEN); CHAT=$(get DONATIX_ALERT_TELEGRAM_CHAT_ID)
  [ -n "$TOKEN" ] && curl -s -m 15 "https://api.telegram.org/bot$TOKEN/sendMessage" \
    --data-urlencode "chat_id=$CHAT" --data-urlencode "text=📦 Каталог загружен через сервер. $RESULT" >/dev/null
  SO=$(sudo -u donatix bash -c "cd '$APP_DIR' && .venv/bin/python -c \"
import sqlite3, os
from donatix.config import Config
db=Config.from_env().db_path
c=sqlite3.connect(db)
n=c.execute(\\\"SELECT COUNT(*) FROM products WHERE active=1 AND lower(category_name) LIKE '%standoff%'\\\").fetchone()[0]
print(n)\"" 2>/dev/null || echo 0)
  echo "Пакетов Standoff 2 в каталоге: ${SO:-0}"
  if [ "${SO:-0}" != "0" ]; then
    echo "✔ Standoff 2 загружен и уже попадёт в «Популярное» на сайте."
  else
    echo "⚠ Standoff 2 пока не найден. Проверьте, что ключ CoinDrop подключён (deploy/coindrop.sh) и на аккаунте есть эта игра."
  fi
else
  say "Загрузка ещё идёт"
  echo "Она продолжается в фоне. Проверить позже:  tail -f $LOG"
  echo "Когда увидите «Готово и сохранено» — каталог загружен."
fi
