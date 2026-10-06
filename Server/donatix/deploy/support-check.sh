#!/usr/bin/env bash
# Проверка бота поддержки: код, ключи, Telegram, OpenAI, журнал. Ключи не печатаются.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/support-check.sh | sudo bash
set -uo pipefail
APP_DIR="/home/donatix/app"; ENV_FILE="$APP_DIR/donatix/.env"
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2-; }
echo "== Код"; sudo -u donatix git -C "$APP_DIR" log -1 --format='%h %s' ; ls "$APP_DIR/donatix/supportbot.py" >/dev/null 2>&1 && echo "supportbot.py: есть" || echo "supportbot.py: НЕТ — код старый"
TOKEN="$(get DONATIX_SUPPORT_BOT_TOKEN)"; OKEY="$(get DONATIX_OPENAI_API_KEY)"
echo "== Настройки"; echo "токен бота: ${TOKEN:+задан}${TOKEN:-НЕ ЗАДАН}"; echo "ключ OpenAI: ${OKEY:+задан}${OKEY:-НЕ ЗАДАН}"
echo "админ: $(get DONATIX_SUPPORT_ADMIN_ID)  модель: $(get DONATIX_SUPPORT_MODEL)"
echo "== Сайт"; systemctl is-active donatix
if [ -n "$TOKEN" ]; then
  echo "== Telegram"; curl -s "https://api.telegram.org/bot$TOKEN/getMe" | head -c 300; echo
  curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo" | head -c 300; echo
fi
if [ -n "$OKEY" ]; then
  echo "== OpenAI"; curl -s -o /tmp/oa.json -w "HTTP %{http_code}\n" https://api.openai.com/v1/chat/completions \
    -H "Authorization: Bearer $OKEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$(get DONATIX_SUPPORT_MODEL | sed 's/^$/gpt-4o-mini/')\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}"
  head -c 400 /tmp/oa.json; echo; rm -f /tmp/oa.json
fi
echo "== Журнал (поддержка)"; journalctl -u donatix --since "2 hours ago" --no-pager | grep -iE "поддержк|support|openai" | tail -20
