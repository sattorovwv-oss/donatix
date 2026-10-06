#!/usr/bin/env bash
# Сервер за Cloudflare. Шаг 1 (можно сразу, ничего не ломает): nginx узнаёт настоящий IP посетителя
# из заголовка Cloudflare — лимиты и журнал работают по людям, а не по серверам Cloudflare.
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/cloudflare.sh | sudo bash
# Шаг 2 — ТОЛЬКО после того, как домен уже работает через Cloudflare (оранжевое облако):
# пускать на сайт только через Cloudflare, прямые атаки на IP сервера не пройдут.
#   ... | sudo LOCK=1 bash        отменить:  ... | sudo LOCK=0 bash
set -uo pipefail
say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo"
V4=$(curl -fsS -m 20 https://www.cloudflare.com/ips-v4) || die "Не скачал список адресов Cloudflare"
V6=$(curl -fsS -m 20 https://www.cloudflare.com/ips-v6) || V6=""
BACKUP="/root/nginx-backup-cf-$(date +%F-%H%M%S)"; mkdir -p "$BACKUP"; cp -a /etc/nginx "$BACKUP/"

say "Настоящий IP посетителя из заголовка Cloudflare"
{ echo "# Donatix: адреса Cloudflare (обновлено $(date +%F)); только им верим в CF-Connecting-IP"
  for ip in $V4 $V6; do echo "set_real_ip_from $ip;"; done
  echo "real_ip_header CF-Connecting-IP;"; } > /etc/nginx/conf.d/donatix-cloudflare.conf

LOCKF=/etc/nginx/conf.d/donatix-cf-only.conf
if [ "${LOCK:-}" = "1" ]; then
  # Защита от ошибки: если домен ещё смотрит прямо на сервер, «только Cloudflare» отрежет всех посетителей
  DOMAIN=$(grep -m1 -oP 'server_name\s+\K[^ ;]+' /etc/nginx/sites-available/donatix)
  DIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
  if ! python3 -c "import ipaddress,sys; ip=ipaddress.ip_address(sys.argv[1]); nets=[ipaddress.ip_network(n) for n in sys.argv[2].split()]; sys.exit(0 if any(ip in n for n in nets) else 1)" "${DIP:-0.0.0.0}" "$V4" 2>/dev/null; then
    rm -f /etc/nginx/conf.d/donatix-cf-only.conf; sed -i '/dx_not_cf/d' /etc/nginx/snippets/donatix-protect.conf 2>/dev/null
    nginx -t -q && systemctl reload nginx
    die "Домен $DOMAIN ещё указывает прямо на сервер ($DIP), а не на Cloudflare. «Только через Cloudflare» НЕ включаю — иначе сайт станет недоступен. Сначала смените DNS у регистратора и дождитесь «Active» в Cloudflare."
  fi
  say "Пускать на сайт только через Cloudflare"
  { echo "# Donatix: прямые заходы на IP сервера (не через Cloudflare) — отказ"
    echo "geo \$realip_remote_addr \$dx_not_cf {"; echo "    default 1;"
    for ip in $V4 $V6; do echo "    $ip 0;"; done; echo "    127.0.0.1 0;"; echo "}"; } > "$LOCKF"
  grep -q "dx_not_cf" /etc/nginx/snippets/donatix-protect.conf 2>/dev/null || \
    printf '\nif ($dx_not_cf) { return 444; }\n' >> /etc/nginx/snippets/donatix-protect.conf
elif [ "${LOCK:-}" = "0" ]; then
  rm -f "$LOCKF"; sed -i '/dx_not_cf/d' /etc/nginx/snippets/donatix-protect.conf 2>/dev/null
  echo "Ограничение «только через Cloudflare» снято"
fi
if nginx -t 2>/tmp/nginx-cf.txt; then systemctl reload nginx && echo "nginx: готово"
else
  cat /tmp/nginx-cf.txt; rm -rf /etc/nginx && cp -a "$BACKUP/nginx" /etc/nginx && nginx -t -q && systemctl reload nginx
  die "nginx не принял настройки — вернул как было. Пришлите вывод выше."
fi
