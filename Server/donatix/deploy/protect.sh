#!/usr/bin/env bash
# Защита сайта от флуда (DDoS на уровне HTTP) и перебора паролей: nginx + fail2ban.
# Безопасно запускать повторно. При ошибке в настройках nginx всё возвращается как было.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/protect.sh | sudo bash
set -uo pipefail
SITE=/etc/nginx/sites-available/donatix
say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo"
[ -f "$SITE" ] || die "Не нашёл $SITE — пришлите вывод: ls /etc/nginx/sites-available"

# Снять все прежние блокировки: прошлая версия могла заблокировать обычных посетителей
command -v fail2ban-client >/dev/null && fail2ban-client unban --all >/dev/null 2>&1 && echo "все блокировки fail2ban сняты"

say "1/4 Ограничения nginx: не больше 15 запросов в секунду с одного IP (всплеск до 100)"
BACKUP="/root/nginx-backup-$(date +%F-%H%M%S)"; mkdir -p "$BACKUP"; cp -a /etc/nginx "$BACKUP/"
echo "копия настроек: $BACKUP"
cat > /etc/nginx/conf.d/donatix-limits.conf <<'CONF'
# Donatix: зоны учёта запросов по IP посетителя.
# Картинки и файлы оформления не считаем: страница каталога грузит их десятками сразу.
map $uri $dx_limit_key {
    ~^/(static|media|pay-icons)/  "";
    ~^/favicon                    "";
    default                       $binary_remote_addr;
}
limit_req_zone  $dx_limit_key       zone=dx_req:20m   rate=15r/s;
limit_req_zone  $binary_remote_addr zone=dx_auth:10m  rate=12r/m;
limit_conn_zone $binary_remote_addr zone=dx_conn:10m;
limit_req_status  429;
limit_conn_status 429;
CONF
mkdir -p /etc/nginx/snippets
cat > /etc/nginx/snippets/donatix-protect.conf <<'CONF'
# Donatix: защита от флуда. Подключается внутри server { } сайта.
limit_conn dx_conn 120;
limit_req  zone=dx_req burst=100 nodelay;
client_header_timeout 10s;
client_body_timeout   15s;
send_timeout          20s;
keepalive_timeout     20s;
# Вход, регистрация и код 2FA — перебор паролей: не больше 12 в минуту с IP
location ~ ^/(login|register|login/code)$ {
    limit_req zone=dx_auth burst=8 nodelay;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
CONF
# Подключаем в каждый server { } сайта (и :80, и :443 от certbot) — один раз
if ! grep -q "donatix-protect.conf" "$SITE"; then
  sed -i -E 's#^(\s*)server_name(\s+[^;]*;)#&\n\1include /etc/nginx/snippets/donatix-protect.conf;#' "$SITE"
fi
if nginx -t 2>/tmp/nginx-test.txt; then
  systemctl reload nginx && echo "nginx: защита включена"
else
  cat /tmp/nginx-test.txt
  rm -f /etc/nginx/conf.d/donatix-limits.conf /etc/nginx/snippets/donatix-protect.conf
  cp -a "$BACKUP/nginx/sites-available/donatix" "$SITE"
  nginx -t -q && systemctl reload nginx
  die "nginx не принял настройки — вернул как было. Пришлите вывод выше."
fi

say "Ядро Linux: защита от SYN-флуда и подменённых адресов"
cat > /etc/sysctl.d/99-donatix-ddos.conf <<'CONF'
# Donatix: устойчивость к флуду соединений (SYN flood) и подмене адресов
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.tcp_synack_retries = 2
net.core.somaxconn = 8192
net.core.netdev_max_backlog = 8192
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.tcp_fin_timeout = 15
CONF
sysctl -q --system >/dev/null 2>&1 && echo "ядро: защита включена (syncookies=$(sysctl -n net.ipv4.tcp_syncookies))"

say "2/4 fail2ban: кто продолжает флуд — блокируется на уровне сервера"
if ! command -v fail2ban-client >/dev/null; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban >/dev/null || die "fail2ban не установился"
fi
MYIP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v ':' | head -1)
cat > /etc/fail2ban/jail.d/donatix.conf <<CONF
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 ${MYIP}
bantime.increment = true
bantime.maxtime = 1d

# Флуд: nginx отказал этому IP 400+ раз за минуту (человек так не может) — блок на 10 минут, повторно — дольше
[nginx-limit-req]
enabled  = true
port     = http,https
logpath  = /var/log/nginx/error.log
findtime = 60
maxretry = 400
bantime  = 600

# Подбор паролей к SSH
[sshd]
enabled  = true
maxretry = 6
bantime  = 3600
CONF
systemctl enable -q fail2ban; systemctl restart fail2ban; sleep 2
fail2ban-client status 2>/dev/null | sed -n '1,3p'

say "3/4 Проверка: сайт отвечает"
for i in 1 2 3; do curl -s -o /dev/null -w "%{http_code} %{time_total}s  " -H "Host: $(grep -m1 -oP 'server_name\s+\K[^ ;]+' "$SITE")" http://127.0.0.1/; done; echo

echo "Сайт изнутри: $(curl -s -o /dev/null -m 15 -w '%{http_code} за %{time_total}s' http://127.0.0.1:8000/robots.txt)   служба: $(systemctl is-active donatix)"

say "4/4 Безопасность сервера — посмотрите сами"
echo "Последние входы по SSH:"; last -n 8 -a 2>/dev/null | head -9
echo; echo "Вход по паролю в SSH: $(sshd -T 2>/dev/null | grep -i '^passwordauthentication' || echo 'не удалось проверить')"
echo "(лучше 'no' — вход только по ключу; не меняйте, пока не убедились, что ключ у вас есть)"
echo; echo "Готово. Заблокированные сейчас: sudo fail2ban-client status nginx-limit-req"
echo "Снять блок с IP:          sudo fail2ban-client set nginx-limit-req unbanip 1.2.3.4"
echo "Откатить защиту nginx:    sudo cp -a $BACKUP/nginx/sites-available/donatix $SITE && sudo rm /etc/nginx/conf.d/donatix-limits.conf && sudo nginx -s reload"
