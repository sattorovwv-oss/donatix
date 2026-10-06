#!/usr/bin/env bash
# Охрана Donatix: сторож (каждую минуту), ночная копия базы в Telegram, обновления безопасности.
# Безопасно запускать повторно.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/guard.sh | sudo bash
#   Вход по SSH только по ключу (осторожно, см. вывод):  ... | sudo HARDEN_SSH=1 bash
set -uo pipefail
APP_DIR=/home/donatix/app; ENV_FILE="$APP_DIR/donatix/.env"
say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo"
[ -f "$ENV_FILE" ] || die "Не нашёл $ENV_FILE"
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
[ -n "$(get DONATIX_ALERT_TELEGRAM_TOKEN)" ] && [ -n "$(get DONATIX_ALERT_TELEGRAM_CHAT_ID)" ] \
  || die "Админ-бот не настроен (DONATIX_ALERT_TELEGRAM_TOKEN / _CHAT_ID в .env) — уведомлять некуда"
mkdir -p /var/lib/donatix-guard

say "1/4 Сторож: сайт не отвечает или его флудят — сообщение в админ-бот"
cat > /usr/local/bin/donatix-guard <<'GUARD'
#!/usr/bin/env bash
# Раз в минуту (cron). Сайт лежит — сообщить и после 3 проверок перезапустить; идёт флуд — сообщить с IP.
ENV_FILE=/home/donatix/app/donatix/.env; S=/var/lib/donatix-guard
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
TOKEN=$(get DONATIX_ALERT_TELEGRAM_TOKEN); CHAT=$(get DONATIX_ALERT_TELEGRAM_CHAT_ID)
send() { curl -s -m 15 "https://api.telegram.org/bot$TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT" \
          --data-urlencode "text=$1" >/dev/null; }
now=$(date +%s)

# — Жив ли сайт
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' http://127.0.0.1:8000/robots.txt)
fails=$(cat $S/fails 2>/dev/null || echo 0)
if [ "$code" = "200" ]; then
  if [ "$fails" -ge 2 ]; then send "✅ Donatix снова отвечает."; fi
  echo 0 > $S/fails
else
  fails=$((fails + 1)); echo $fails > $S/fails
  if [ "$fails" = 2 ]; then send "🔴 Donatix не отвечает уже 2 минуты (код: ${code:-нет ответа}). Проверяю дальше."; fi
  if [ "$fails" = 3 ]; then
    systemctl restart donatix
    send "🔁 Donatix не отвечал 3 минуты — перезапустил сайт автоматически."
  fi
fi

# — Флуд: сколько раз nginx отказал за прошлую минуту и кому
LOG=/var/log/nginx/error.log
if [ -r "$LOG" ]; then
  minute=$(date -d '1 minute ago' '+%Y/%m/%d %H:%M')
  lines=$(tail -n 200000 "$LOG" | grep -F "$minute" | grep -E "limiting (requests|connections)")
  n=$(printf '%s' "$lines" | grep -c . || true)
  last=$(cat $S/attack_at 2>/dev/null || echo 0)
  if [ "${n:-0}" -ge 300 ] && [ $((now - last)) -ge 900 ]; then
    echo $now > $S/attack_at
    top=$(printf '%s\n' "$lines" | grep -oE 'client: [0-9a-fA-F.:]+' | awk '{print $2}' | sort | uniq -c | sort -rn | head -5 \
          | awk '{printf "  %s — %s отказов\n", $2, $1}')
    banned=$(fail2ban-client status nginx-limit-req 2>/dev/null | grep -oE 'Currently banned:\s+[0-9]+' | grep -oE '[0-9]+$')
    send "⚠️ Сайт флудят: за минуту nginx отбил $n запросов. Сайт защищён и работает.
Больше всех:
$top
Заблокировано fail2ban сейчас: ${banned:-0}"
  fi
fi
GUARD
chmod 755 /usr/local/bin/donatix-guard

say "2/4 Ночная копия базы — в Telegram (и 14 дней на сервере)"
cat > /usr/local/bin/donatix-backup <<'BACKUP'
#!/usr/bin/env bash
# Копия базы без остановки сайта (SQLite backup API), сжатая, — в админ-бот и в /home/donatix/backup.
set -uo pipefail
APP=/home/donatix/app; ENV_FILE=$APP/donatix/.env; OUT=/home/donatix/backup
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
TOKEN=$(get DONATIX_ALERT_TELEGRAM_TOKEN); CHAT=$(get DONATIX_ALERT_TELEGRAM_CHAT_ID)
DB=$(get DONATIX_DB); DB=${DB:-$APP/donatix/data/donatix.db}
mkdir -p $OUT; F="$OUT/donatix-$(date +%F).db"
if ! "$APP/.venv/bin/python" - "$DB" "$F" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA quick_check").fetchone()[0]
dst.close(); src.close()
sys.exit(0 if ok == "ok" else 1)
PY
then
  curl -s -m 30 "https://api.telegram.org/bot$TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT" \
       --data-urlencode "text=🔴 Ночная копия базы Donatix НЕ получилась — проверьте сервер." >/dev/null
  exit 1
fi
gzip -f "$F"; chown -R donatix:donatix $OUT; chmod 600 "$F.gz"
find $OUT -name 'donatix-*.db*' -mtime +14 -delete
size=$(stat -c %s "$F.gz")
if [ "$size" -lt 49000000 ]; then
  curl -s -m 300 "https://api.telegram.org/bot$TOKEN/sendDocument" -F "chat_id=$CHAT" -F "document=@$F.gz" \
       -F "caption=💾 Копия базы Donatix за $(date +%d.%m.%Y), $((size / 1024)) КБ. Храните этот чат закрытым." >/dev/null
else
  curl -s -m 30 "https://api.telegram.org/bot$TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT" \
       --data-urlencode "text=💾 Копия базы сделана ($((size / 1048576)) МБ — больше лимита Telegram), лежит на сервере: $F.gz" >/dev/null
fi
BACKUP
chmod 755 /usr/local/bin/donatix-backup
rm -f /etc/cron.d/donatix-backup   # старая копия через sqlite3 (его может не быть на сервере)
cat > /etc/cron.d/donatix-guard <<'CRON'
* * * * * root /usr/local/bin/donatix-guard >/dev/null 2>&1
40 3 * * * root /usr/local/bin/donatix-backup >/dev/null 2>&1
CRON
chmod 644 /etc/cron.d/donatix-guard
echo "Делаю первую копию сейчас — придёт файлом в админ-бот…"
/usr/local/bin/donatix-backup && echo "копия отправлена" || echo "⚠ копия не получилась — пришлите вывод"

say "3/4 Автоматические обновления безопасности Ubuntu"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades >/dev/null 2>&1
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable -q --now unattended-upgrades 2>/dev/null && echo "включены"

say "4/4 Вход на сервер по SSH"
PW=$(sshd -T 2>/dev/null | awk '/^passwordauthentication/ {print $2}')
KEYS=$(cat /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys 2>/dev/null | grep -c -E '^(ssh|ecdsa)-' || true)
echo "вход по паролю: ${PW:-?}   ключей для входа на сервере: $KEYS"
if [ "${HARDEN_SSH:-0}" = "1" ]; then
  if [ "$KEYS" -ge 1 ]; then
    printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/99-donatix.conf
    if sshd -t; then systemctl reload ssh 2>/dev/null || systemctl reload sshd; echo "✅ теперь вход только по ключу"
    else rm -f /etc/ssh/sshd_config.d/99-donatix.conf; echo "⚠ sshd не принял настройку — вернул как было"; fi
  else
    echo "⚠ Ключей нет — вход по паролю НЕ выключаю, иначе вы потеряете доступ. Сначала добавьте ключ."
  fi
elif [ "$PW" = "yes" ]; then
  echo "Совет: выключите вход по паролю (сначала убедитесь, что входите по ключу): ... | sudo HARDEN_SSH=1 bash"
fi

TOKEN=$(get DONATIX_ALERT_TELEGRAM_TOKEN); CHAT=$(get DONATIX_ALERT_TELEGRAM_CHAT_ID)
curl -s -m 15 "https://api.telegram.org/bot$TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT" \
     --data-urlencode "text=🛡 Охрана Donatix включена: проверка сайта каждую минуту, сообщения об атаках, копия базы каждую ночь в 03:40." >/dev/null
say "Готово. В админ-бот пришло сообщение «Охрана включена» и файл с копией базы."
