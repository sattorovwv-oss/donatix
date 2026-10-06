#!/usr/bin/env bash
# Подключить Telegram-бота админки одной командой (на сервере, где уже стоит Donatix):
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/telegram.sh | sudo TG_TOKEN='123456:ABC...' bash
#
# Перед запуском напишите своему боту /start — тогда chat id найдётся сам.
# Можно указать его явно: TG_CHAT=123456789.
# Скрипт обновит код, запишет токен в .env, перезапустит сайт и пришлёт проверочное сообщение.
set -euo pipefail

APP_DIR="/home/donatix/app"
ENV_FILE="$APP_DIR/donatix/.env"
BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден в $APP_DIR — сначала установите его (install.sh)."
TG_TOKEN="$(printf '%s' "${TG_TOKEN:-}" | tr -d '[:space:]')"
TG_CHAT="$(printf '%s' "${TG_CHAT:-}" | tr -d '[:space:]')"
[[ "$TG_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "TG_TOKEN не похож на токен от @BotFather (вида 123456789:AAH...)."

say "Проверяю токен"
ME="$(curl -fsS "https://api.telegram.org/bot$TG_TOKEN/getMe" 2>/dev/null)" || die "Telegram не принял токен. Скопируйте его у @BotFather ещё раз."
BOT_NAME="$(printf '%s' "$ME" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')"
echo "Бот: @$BOT_NAME"

if [ -z "$TG_CHAT" ]; then
  say "Жду ваше сообщение боту"
  echo "Откройте в Telegram https://t.me/$BOT_NAME и нажмите «Старт» (или отправьте любое сообщение)."
  # Сайт мог уже забирать сообщения бота — на время поиска останавливаем его; вебхук мешает getUpdates
  systemctl stop donatix 2>/dev/null || true
  curl -fsS -o /dev/null "https://api.telegram.org/bot$TG_TOKEN/deleteWebhook" || true
  for _ in $(seq 1 6); do
    TG_CHAT="$(curl -sS "https://api.telegram.org/bot$TG_TOKEN/getUpdates?timeout=20" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    data = {}
chats = [u[k]["chat"]["id"] for u in data.get("result", []) for k in ("message", "edited_message")
         if k in u and u[k].get("chat", {}).get("type") == "private"]
print(chats[-1] if chats else "")')" || TG_CHAT=""
    [ -n "$TG_CHAT" ] && break
    printf '.'
  done
  echo
  [ -n "$TG_CHAT" ] || { systemctl start donatix || true; die "За 2 минуты сообщение не пришло. Напишите боту /start и запустите команду ещё раз — или укажите id сами: TG_CHAT=ваш_id (его покажет @userinfobot)."; }
fi
[[ "$TG_CHAT" =~ ^-?[0-9]+$ ]] || die "TG_CHAT должен быть числом."
echo "Chat id: $TG_CHAT"

say "Обновляю код"
sudo -u donatix git -C "$APP_DIR" fetch -q origin "$BRANCH"
sudo -u donatix git -C "$APP_DIR" checkout -q "$BRANCH"
sudo -u donatix git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"

say "Записываю настройки"
cp "$ENV_FILE" "$ENV_FILE.bak"
TG_TOKEN="$TG_TOKEN" TG_CHAT="$TG_CHAT" python3 - "$ENV_FILE" <<'PY'
import os, sys
path = sys.argv[1]
values = {"DONATIX_ALERT_TELEGRAM_TOKEN": os.environ["TG_TOKEN"], "DONATIX_ALERT_TELEGRAM_CHAT_ID": os.environ["TG_CHAT"]}
lines, seen = [], set()
for line in open(path, encoding="utf-8").read().splitlines():
    key = line.split("=", 1)[0].strip()
    if key in values:
        line = f"{key}={values[key]}"
        seen.add(key)
    lines.append(line)
lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
chown donatix:donatix "$ENV_FILE"
chmod 600 "$ENV_FILE"

say "Перезапускаю Donatix"
systemctl restart donatix
for _ in $(seq 1 20); do
  curl -fsS -o /dev/null http://127.0.0.1:8000/ 2>/dev/null && break
  sleep 1
done
systemctl is-active --quiet donatix || { journalctl -u donatix -n 30 --no-pager; die "Donatix не запустился — вывод выше."; }

curl -fsS -o /dev/null "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
  --data-urlencode "chat_id=$TG_CHAT" \
  --data-urlencode "text=✅ Бот админки Donatix подключён. Нажмите /start — появится меню: сводка, заявки, новые партнёры, проблемные заказы." \
  || die "Не удалось отправить сообщение в чат $TG_CHAT."

say "Готово: откройте @$BOT_NAME в Telegram"
