#!/usr/bin/env bash
# Автоблокировка атакующих навсегда. Дополняет protect.sh (должен быть запущен раньше).
# - IP флудит → fail2ban блокирует; повторно → дольше; несколько раз за день → на неделю.
# - Список заблокированных сохраняется после перезагрузки сервера.
# - При каждой блокировке — сообщение в админ-бот с IP.
# Безопасно запускать повторно.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/autoban.sh | sudo bash
set -uo pipefail
ENV_FILE=/home/donatix/app/donatix/.env
say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo"
command -v fail2ban-client >/dev/null || die "Сначала запустите protect.sh (он ставит fail2ban)"
get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
TOKEN=$(get DONATIX_ALERT_TELEGRAM_TOKEN); CHAT=$(get DONATIX_ALERT_TELEGRAM_CHAT_ID)

say "1/3 Блокировки сохраняются после перезагрузки"
# fail2ban хранит блокировки в своей базе и восстанавливает их при старте
if ! grep -q "^dbfile" /etc/fail2ban/fail2ban.conf 2>/dev/null; then
  sed -i 's#^\[Definition\]#[Definition]\ndbfile = /var/lib/fail2ban/fail2ban.sqlite3#' /etc/fail2ban/fail2ban.conf 2>/dev/null || true
fi
sed -i 's/^dbpurgeage.*/dbpurgeage = 30d/' /etc/fail2ban/fail2ban.conf 2>/dev/null || \
  echo "dbpurgeage = 30d" >> /etc/fail2ban/fail2ban.conf

say "2/3 Повторным атакующим — блок на неделю; уведомление в админ-бот"
# Действие: писать в админ-бот при блокировке (если бот настроен)
if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
  cat > /etc/fail2ban/action.d/donatix-telegram.conf <<CONF
[Definition]
actionban = curl -s -m 15 "https://api.telegram.org/bot${TOKEN}/sendMessage" --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=⛔ Заблокирован атакующий <ip> (правило <name>). Сайт защищён."
actionunban =
CONF
  TG_LINE="
         donatix-telegram"
else
  TG_LINE=""
fi
# «Рецидив»: кого fail2ban уже блокировал 3 раза за сутки — банить на неделю
cat > /etc/fail2ban/jail.d/donatix-recidive.conf <<CONF
[recidive]
enabled  = true
logpath  = /var/log/fail2ban.log
findtime = 1d
bantime  = 1w
maxretry = 3
action   = iptables-allports[name=recidive]${TG_LINE}
CONF
# Флуд: чуть агрессивнее и с уведомлением
cat > /etc/fail2ban/jail.d/donatix.conf <<CONF
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 $(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v ':' | head -1)
bantime.increment = true
bantime.factor = 4
bantime.maxtime = 30d

[nginx-limit-req]
enabled   = true
port      = http,https
logpath   = /var/log/nginx/error.log
findtime  = 60
maxretry  = 400
bantime   = 1h
action    = iptables-multiport[name=nginx-limit-req, port="http,https", protocol=tcp]${TG_LINE}

[sshd]
enabled  = true
maxretry = 6
bantime  = 1h
CONF

say "3/3 Применяю"
systemctl enable -q fail2ban
if ! fail2ban-client -t >/dev/null 2>&1; then
  fail2ban-client -t; die "fail2ban не принял настройки (см. выше). Ничего не запущено — сообщите мне вывод."
fi
systemctl restart fail2ban; sleep 2
echo "Правила fail2ban:"; fail2ban-client status 2>/dev/null | grep "Jail list"
echo "Флуд:";    fail2ban-client status nginx-limit-req 2>/dev/null | grep -E "Currently banned|Total banned"
echo "Рецидив:"; fail2ban-client status recidive 2>/dev/null | grep -E "Currently banned|Total banned"
[ -n "$TOKEN" ] && curl -s -m 15 "https://api.telegram.org/bot$TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT" \
  --data-urlencode "text=🛡 Автоблокировка усилена: флуд — блок на час (при повторах до 30 дней), повторные атаки — на неделю. Блокировки сохраняются после перезагрузки." >/dev/null
say "Готово. Кто заблокирован: sudo fail2ban-client status nginx-limit-req"
echo "Заблокировать вручную: sudo fail2ban-client set nginx-limit-req banip 1.2.3.4"
echo "Снять блок:            sudo fail2ban-client set nginx-limit-req unbanip 1.2.3.4"
