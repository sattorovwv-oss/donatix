#!/usr/bin/env bash
# Бот поддержки с AI: токен Telegram-бота и ключ OpenAI. Ключи вводятся скрыто и пишутся только в .env на сервере.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/support.sh | sudo bash
set -euo pipefail
APP_DIR="/home/donatix/app"; ENV_FILE="$APP_DIR/donatix/.env"; BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден."

TOKEN="${SUPPORT_BOT_TOKEN:-}"; OKEY="${OPENAI_KEY:-}"; MODEL="${SUPPORT_MODEL:-}"; ADMIN="${SUPPORT_ADMIN:-}"
if [ -z "$TOKEN" ]; then printf 'Токен бота поддержки от @BotFather (скрыто): '; read -rs TOKEN < /dev/tty; echo; fi
if [ -z "$OKEY" ]; then printf 'Ключ OpenAI, sk-… (скрыто): '; read -rs OKEY < /dev/tty; echo; fi
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "Токен бота не похож на настоящий (вид 123456:ABC…)."
[[ "$OKEY" == sk-* ]] || die "Ключ OpenAI должен начинаться с sk-"
[ -z "$ADMIN" ] || [[ "$ADMIN" =~ ^[0-9]{5,15}$ ]] || die "ID админа — только цифры."

sudo -u donatix git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH" || true
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
TOKEN="$TOKEN" OKEY="$OKEY" MODEL="$MODEL" ADMIN="$ADMIN" python3 - "$ENV_FILE" <<'PY'
import os, re, sys
path = sys.argv[1]; s = open(path, encoding="utf-8").read()
for key, env in (("DONATIX_SUPPORT_BOT_TOKEN", "TOKEN"), ("DONATIX_OPENAI_API_KEY", "OKEY"), ("DONATIX_SUPPORT_MODEL", "MODEL"),
                 ("DONATIX_SUPPORT_ADMIN_ID", "ADMIN")):
    value = os.environ[env].strip()
    if not value:
        continue
    line = f"{key}={value}"
    s = re.sub(rf"^{key}=.*$", lambda m: line, s, flags=re.M) if re.search(rf"^{key}=", s, re.M) else s.rstrip("\n") + "\n" + line + "\n"
open(path, "w", encoding="utf-8").write(s)
PY
chown donatix:donatix "$ENV_FILE"; chmod 600 "$ENV_FILE"
systemctl restart donatix
sleep 4
NAME=$(curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["username"])' 2>/dev/null || true)
printf '\033[1;32m✔ Бот поддержки запущен%s.\033[0m\n' "${NAME:+: @$NAME}"
echo "1) Откройте бота и нажмите /start со своего админского Telegram — туда будут приходить обращения."
echo "2) Проверьте с другого аккаунта: напишите боту «заказ не пришёл»."
