#!/usr/bin/env bash
# Перевести Donatix на новый домен одной командой:
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/domain.sh | sudo DOMAIN=donatix.tj bash
#   DROP_OLD=1 — старый адрес (duckdns) отключить совсем, без переадресации
#
# Перед запуском у регистратора домена запись A (для «@» и «www») должна указывать на IP этого сервера.
# Скрипт: проверит DNS, выпустит HTTPS-сертификат, старый адрес (duckdns) и www будут
# переадресовывать на новый, пропишет домен в настройках сайта и перезапустит его.
set -euo pipefail

APP_USER="donatix"
APP_DIR="/home/$APP_USER/app"
ENV_FILE="$APP_DIR/donatix/.env"
BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден в $APP_DIR."
DOMAIN="$(printf '%s' "${DOMAIN:-}" | tr -d '[:space:]' | tr 'A-Z' 'a-z' | sed -e 's#^https\?://##' -e 's#/.*$##' -e 's#^www\.##')"
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || die "Укажите домен: sudo DOMAIN=donatix.tj bash domain.sh"
OLD="$(cat "/home/$APP_USER/.donatix_domain" 2>/dev/null || true)"
EMAIL="${EMAIL:-$(grep -E '^DONATIX_ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2-)}"

resolve() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}'; }

say "Проверяю, что $DOMAIN указывает на этот сервер"
MY_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me || true)"
[ -n "$MY_IP" ] || die "Не удалось узнать IP сервера."
GOT="$(resolve "$DOMAIN" || true)"
echo "IP сервера: $MY_IP   $DOMAIN → ${GOT:-не найден}"
if [ "$GOT" != "$MY_IP" ]; then
  die "Домен $DOMAIN пока не указывает на этот сервер.
У регистратора домена (где покупали) в управлении DNS добавьте запись:
   Тип A   имя @     значение $MY_IP
   Тип A   имя www   значение $MY_IP
Подождите 5–30 минут (иногда до пары часов) и запустите команду ещё раз."
fi
ok "DNS в порядке"

ALIASES=""
if [ "$(resolve "www.$DOMAIN" || true)" = "$MY_IP" ]; then ALIASES="www.$DOMAIN"; ok "www.$DOMAIN тоже работает"
else echo "www.$DOMAIN не указывает на сервер — без www (можно добавить позже, запустив скрипт ещё раз)"; fi
# DROP_OLD=1 — старый адрес не переадресовывать, а отключить совсем
if [ "${DROP_OLD:-0}" = "1" ]; then
  echo "Старые адреса отключаются — работать будет только $DOMAIN${ALIASES:+ (и $ALIASES)}"
elif [ -n "$OLD" ] && [ "$OLD" != "$DOMAIN" ] && [ "$(resolve "$OLD" || true)" = "$MY_IP" ]; then
  ALIASES="$ALIASES $OLD"; ok "старый адрес $OLD будет переадресовывать на $DOMAIN"
fi

say "Обновляю код"
sudo -u "$APP_USER" git -C "$APP_DIR" fetch -q origin "$BRANCH"
sudo -u "$APP_USER" git -C "$APP_DIR" checkout -q "$BRANCH"
sudo -u "$APP_USER" git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"

say "HTTPS для $DOMAIN"
if docker inspect "${CADDY_CONTAINER:-averix-caddy-1}" >/dev/null 2>&1; then
  DOMAIN="$DOMAIN" ALIASES="$ALIASES" EMAIL="$EMAIL" bash "$APP_DIR/donatix/deploy/caddy-cert.sh"
elif [ -f /etc/nginx/sites-available/donatix ]; then
  NAMES="$DOMAIN $ALIASES"
  sed -i -E "s#^(\s*server_name\s+).*;#\1$NAMES;#" /etc/nginx/sites-available/donatix
  nginx -t -q && systemctl reload nginx
  D_ARGS=(); for d in $NAMES; do D_ARGS+=(-d "$d"); done
  certbot --nginx -n --agree-tos --redirect --expand -m "$EMAIL" --cert-name "$DOMAIN" "${D_ARGS[@]}"
else
  die "Не нашёл ни Caddy (Docker), ни nginx — пришлите вывод: docker ps; ls /etc/nginx/sites-available"
fi

say "Прописываю домен в настройках сайта"
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
if grep -q '^DONATIX_BASE_URL=' "$ENV_FILE"; then
  sed -i "s#^DONATIX_BASE_URL=.*#DONATIX_BASE_URL=https://$DOMAIN#" "$ENV_FILE"
else
  echo "DONATIX_BASE_URL=https://$DOMAIN" >> "$ENV_FILE"
fi
echo "$DOMAIN" > "/home/$APP_USER/.donatix_domain"
systemctl restart donatix

sleep 3
code=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/" || true)
[ "$code" = "200" ] && ok "https://$DOMAIN открывается" || echo "⚠ https://$DOMAIN ответил кодом $code — пришлите вывод выше."
if [ -n "$OLD" ] && [ "$OLD" != "$DOMAIN" ] && [ "${DROP_OLD:-0}" != "1" ]; then
  loc=$(curl -s -o /dev/null -w "%{redirect_url}" "https://$OLD/login" || true)
  echo "Старый адрес https://$OLD/login → ${loc:-нет переадресации}"
fi
if [ "${DROP_OLD:-0}" = "1" ]; then
  for d in donatix.duckdns.org $OLD; do
    [ "$d" = "$DOMAIN" ] && continue
    c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "https://$d/" || true)
    echo "Старый адрес $d → ${c:-нет ответа} (должен не открываться)"
  done
fi

cat <<TXT

Готово. Осталось в Google (вход через Google):
  1) console.cloud.google.com/auth/clients → ваш клиент → «Authorized redirect URIs» → Add URI:
       https://$DOMAIN/auth/google/callback
     (старый адрес пока оставьте — удалите через пару дней)
  2) console.cloud.google.com/auth/branding → Home page / Privacy / Terms:
       https://$DOMAIN   https://$DOMAIN/privacy   https://$DOMAIN/terms
     «Authorized domains» → добавьте $DOMAIN
  3) Search Console: добавьте ресурс https://$DOMAIN и отправьте sitemap.xml
TXT
