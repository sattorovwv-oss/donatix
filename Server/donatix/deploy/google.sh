#!/usr/bin/env bash
# Включить «Войти через Google» одной командой (на сервере, где уже стоит Donatix):
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/google.sh | sudo bash
#
# Скрипт спросит Client ID и Client secret (секрет вводится скрыто и не попадает в историю),
# обновит код, запишет ключи в .env, перезапустит сайт и проверит, что кнопка появилась.
set -euo pipefail

APP_DIR="/home/donatix/app"
ENV_FILE="$APP_DIR/donatix/.env"
BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден в $APP_DIR — сначала установите его (install.sh)."

GID="${GOOGLE_CLIENT_ID:-}"
GSECRET="${GOOGLE_CLIENT_SECRET:-}"
if [ -z "$GID" ]; then
  printf 'Client ID (…apps.googleusercontent.com): '
  read -r GID < /dev/tty
fi
if [ -z "$GSECRET" ]; then
  printf 'Client secret (GOCSPX-…, не отображается при вводе): '
  read -rs GSECRET < /dev/tty
  echo
fi
GID="$(printf '%s' "$GID" | tr -d '[:space:]')"
GSECRET="$(printf '%s' "$GSECRET" | tr -d '[:space:]')"
[[ "$GID" =~ ^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$ ]] || die "Client ID не похож на ключ Google (…apps.googleusercontent.com)."
[[ "$GSECRET" =~ ^GOCSPX-[A-Za-z0-9_-]{20,}$ ]] || die "Client secret не похож на ключ Google (GOCSPX-…)."

say "Обновляю код"
sudo -u donatix git -C "$APP_DIR" fetch -q origin "$BRANCH"
sudo -u donatix git -C "$APP_DIR" checkout -q "$BRANCH"
sudo -u donatix git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
ok "код обновлён: $(sudo -u donatix git -C "$APP_DIR" log --oneline -1)"

say "Записываю ключи в .env"
cp "$ENV_FILE" "$ENV_FILE.bak"
GID="$GID" GSECRET="$GSECRET" python3 - "$ENV_FILE" <<'PY'
import os, re, sys
path = sys.argv[1]
s = open(path, encoding="utf-8").read()
for key, value in (("DONATIX_GOOGLE_CLIENT_ID", os.environ["GID"]),
                   ("DONATIX_GOOGLE_CLIENT_SECRET", os.environ["GSECRET"])):
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", s, flags=re.M):
        s = re.sub(rf"^{key}=.*$", lambda m: line, s, flags=re.M)
    else:
        s = s.rstrip("\n") + "\n" + line + "\n"
open(path, "w", encoding="utf-8").write(s)
PY
chown donatix:donatix "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "ключи записаны (старый файл — $ENV_FILE.bak)"

say "Перезапускаю сайт"
systemctl restart donatix
for _ in $(seq 1 20); do
  curl -fsS -o /dev/null http://127.0.0.1:8000/login 2>/dev/null && break
  sleep 1
done

say "Проверяю"
if curl -fsS http://127.0.0.1:8000/login | grep -q "Войти через Google"; then
  ok "кнопка «Войти через Google» на сайте"
else
  die "Кнопки нет. Посмотрите журнал: journalctl -u donatix -n 40 --no-pager"
fi
BASE="$(grep -E '^DONATIX_BASE_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
cat <<TXT

Готово. В Google Cloud → Clients → ваш клиент в «Authorized redirect URIs» должен быть ровно:
  ${BASE:-https://ВАШ-ДОМЕН}/auth/google/callback
Откройте ${BASE:-сайт}/login и нажмите «Войти через Google».
TXT
