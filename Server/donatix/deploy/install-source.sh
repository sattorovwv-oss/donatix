#!/usr/bin/env bash
# Установка Donatix на чистый сервер Ubuntu 22.04/24.04 одной командой:
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/install.sh | sudo bash
#
# Или, если репозиторий уже скачан:  sudo bash donatix/deploy/install.sh
#
# Скрипт спросит домен, почту админа, пароль и ключ FazerCards, всё установит,
# получит HTTPS-сертификат и запустит сайт. Повторный запуск обновляет код.
set -euo pipefail

REPO="https://github.com/alijon26062006-bit/AlijonMahmadjonov.git"
BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
APP_USER="donatix"
APP_DIR="/home/${APP_USER}/app"
ENV_FILE="${APP_DIR}/donatix/.env"

say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
# Данные можно передать заранее (тогда вопросов не будет):
#   sudo DOMAIN=... ADMIN_EMAIL=... ADMIN_PASS=... FAZER_KEY=... bash install.sh
ask() {
  local q="$1" def="${2:-}" v
  if [ ! -r /dev/tty ]; then echo "$def"; return; fi
  read -r -p "$q${def:+ [$def]}: " v </dev/tty || true; echo "${v:-$def}"
}
ask_secret() { local q="$1" v; [ -r /dev/tty ] || { echo ""; return; }; read -r -s -p "$q: " v </dev/tty || true; echo >&2; echo "$v"; }
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

[ "$(id -u)" = 0 ] || { echo "Запустите через sudo."; exit 1; }

say "Устанавливаю пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nginx certbot \
  python3-certbot-nginx sqlite3 ufw >/dev/null

id "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$APP_USER"

say "Скачиваю код ($BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch -q origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" checkout -q "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
else
  sudo -u "$APP_USER" git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"
fi
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/donatix/requirements.txt"

check_values() {
  case "$DOMAIN$ADMIN_EMAIL$ADMIN_PASS$FAZER_KEY" in *ВАШ*|*ВАША*)
    echo "✖ В команде остались шаблоны ВАША_ПОЧТА / ВАШ_ПАРОЛЬ / ВАШ_КЛЮЧ — замените их своими данными."; exit 1;; esac
  if ! printf '%s' "$FAZER_KEY" | LC_ALL=C grep -q '^[[:print:]]*$'; then
    echo "✖ Ключ FazerCards должен быть латиницей и цифрами — скопируйте его из панели FazerCards."; exit 1
  fi
}

# Повторный запуск с новыми данными — переписываем настройки.
if [ -f "$ENV_FILE" ] && [ -n "${FAZER_KEY:-}" ]; then
  say "Обновляю настройки"
  mv "$ENV_FILE" "$ENV_FILE.old"
fi

if [ ! -f "$ENV_FILE" ]; then
  say "Настройка (один раз)"
  DOMAIN="${DOMAIN:-$(ask "Домен сайта, например donatix.gg")}"
  ADMIN_EMAIL="${ADMIN_EMAIL:-$(ask "Email админа (ваш)")}"
  ADMIN_PASS="${ADMIN_PASS:-$(ask_secret "Пароль админа (мин. 8 символов)")}"
  FAZER_KEY="${FAZER_KEY:-$(ask_secret "API-ключ FazerCards (Панель → Профиль)")}"
  SUPPORT="${SUPPORT:-$(ask "Контакт поддержки для клиентов" "@donatix_support")}"
  TG_TOKEN="${TG_TOKEN:-}"
  TG_CHAT="${TG_CHAT:-}"
  for v in DOMAIN ADMIN_EMAIL ADMIN_PASS FAZER_KEY; do
    [ -n "${!v}" ] || { echo "Не задано $v. Пример: sudo DOMAIN=donatix.duckdns.org ADMIN_EMAIL=you@mail.com ADMIN_PASS=... FAZER_KEY=... bash install.sh"; exit 1; }
  done
  [ ${#ADMIN_PASS} -ge 8 ] || { echo "Пароль админа — минимум 8 символов."; exit 1; }
  check_values
  SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
  sudo -u "$APP_USER" cp "$APP_DIR/donatix/.env.example" "$ENV_FILE"
  SECRET="$SECRET" DOMAIN="$DOMAIN" ADMIN_EMAIL="$ADMIN_EMAIL" ADMIN_PASS="$ADMIN_PASS" FAZER_KEY="$FAZER_KEY" \
  SUPPORT="$SUPPORT" TG_TOKEN="$TG_TOKEN" TG_CHAT="$TG_CHAT" APP_DIR="$APP_DIR" python3 - "$ENV_FILE" <<'PY'
import os, re, sys
path, e = sys.argv[1], os.environ
s = open(path, encoding="utf-8").read()
vals = {
    "DONATIX_SECRET_KEY": e["SECRET"], "DONATIX_BASE_URL": "https://" + e["DOMAIN"],
    "DONATIX_ADMIN_EMAIL": e["ADMIN_EMAIL"], "DONATIX_ADMIN_PASSWORD": e["ADMIN_PASS"],
    "DONATIX_SUPPLIER": "fazer", "FAZER_API_KEY": e["FAZER_KEY"],
    "DONATIX_SUPPORT_CONTACT": e["SUPPORT"], "DONATIX_ALERT_TELEGRAM_TOKEN": e["TG_TOKEN"],
    "DONATIX_ALERT_TELEGRAM_CHAT_ID": e["TG_CHAT"], "DONATIX_COOKIE_SECURE": "1",
    "DONATIX_DB": e["APP_DIR"] + "/donatix/data/donatix.db",
}
for k, v in vals.items():
    s = re.sub(rf"^{k}=.*$", lambda m, k=k, v=v: f"{k}={v}", s, flags=re.M)
open(path, "w", encoding="utf-8").write(s)
PY
  chmod 600 "$ENV_FILE"; chown "$APP_USER:$APP_USER" "$ENV_FILE"
  echo "$DOMAIN" > "/home/$APP_USER/.donatix_domain"
fi
DOMAIN=$(cat "/home/$APP_USER/.donatix_domain")

say "Проверяю ключ FazerCards"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python -m donatix check" || \
  echo "⚠ Ключ не прошёл проверку. Исправьте FAZER_API_KEY в $ENV_FILE и запустите скрипт ещё раз."

say "Запускаю сервис"
cat > /etc/systemd/system/donatix.service <<UNIT
[Unit]
Description=Donatix
After=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m donatix serve --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q donatix
systemctl restart donatix

say "Настраиваю nginx и HTTPS для $DOMAIN"
cat > /etc/nginx/sites-available/donatix <<NGINX
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;
    client_max_body_size 5m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/donatix /etc/nginx/sites-enabled/donatix
if ! nginx -t -q; then
  echo "✖ Ошибка в настройках nginx (см. выше). Сайт Donatix работает на 127.0.0.1:8000, но снаружи недоступен."; exit 1
fi
systemctl enable -q nginx
if ! systemctl restart nginx; then
  echo "✖ nginx не запускается. Скорее всего порт 80/443 занят другой программой:"
  ss -tlnp | grep -E ':(80|443) ' || true
  echo "Пришлите этот вывод — подскажу, как быть."; exit 1
fi
# Фаервол не включаем сами (на сервере могут быть другие сервисы) — только открываем порты, если он уже включён.
if ufw status 2>/dev/null | grep -q "Status: active"; then ufw allow 'Nginx Full' >/dev/null; fi
certbot --nginx -n --agree-tos --redirect -m "$(grep ^DONATIX_ADMIN_EMAIL= "$ENV_FILE" | cut -d= -f2)" \
  -d "$DOMAIN" || echo "⚠ HTTPS не получен: проверьте, что домен $DOMAIN указывает на IP этого сервера, и запустите скрипт ещё раз."

say "Ежедневная резервная копия базы"
mkdir -p "/home/$APP_USER/backup"; chown "$APP_USER:$APP_USER" "/home/$APP_USER/backup"
cat > /etc/cron.d/donatix-backup <<CRON
30 3 * * * $APP_USER sqlite3 $APP_DIR/donatix/data/donatix.db ".backup /home/$APP_USER/backup/donatix-\$(date +\\%F).db" && find /home/$APP_USER/backup -name 'donatix-*.db' -mtime +14 -delete
CRON

say "Готово! Сайт: https://$DOMAIN   Вход в админку: https://$DOMAIN/login"
echo "Логи:        journalctl -u donatix -f"
echo "Цены:        sudo -u $APP_USER bash -c 'cd $APP_DIR && .venv/bin/python -m donatix prices'"
echo "Обновление:  запустите этот скрипт ещё раз."
