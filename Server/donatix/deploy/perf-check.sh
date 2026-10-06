#!/usr/bin/env bash
# Почему тормозит сервер: процессор, память, боты, база, медленные запросы. Ничего не меняет.
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/perf-check.sh | sudo bash
set -uo pipefail
APP_DIR="/home/donatix/app"
echo "== Сервер"; echo "ядер: $(nproc)   нагрузка (1/5/15 мин): $(cut -d' ' -f1-3 /proc/loadavg)"
free -m | awk 'NR==1||/Mem|Swap/'
echo "== Диск"; df -h / | tail -1
echo "== Кто ест память (топ 12)"
ps -eo rss,pcpu,etime,args --sort=-rss | head -13 | awk 'NR==1{print;next}{printf "%6.0f MB %5s%% %10s  %s\n",$1/1024,$2,$3,substr($0,index($0,$4),90)}'
BOTS=$(pgrep -fc "app.main" || true)
BOTMEM=$(ps -eo rss,args | awk '/app\.main/ && !/awk/ {s+=$1} END {printf "%.0f", s/1024}')
echo "== Боты партнёров: процессов $BOTS, память всего ${BOTMEM:-0} MB"
echo "== Сайт"; systemctl is-active donatix; systemctl show donatix -p MainPID -p MemoryCurrent | tr '\n' ' '; echo
DB=$(ls "$APP_DIR"/donatix/data/*.db 2>/dev/null | head -1)
[ -n "$DB" ] && echo "== База: $(du -h "$DB" | cut -f1) (+ журнал WAL $(du -h "$DB-wal" 2>/dev/null | cut -f1))"
echo "== Скорость ответа сайта (5 раз)"
for i in 1 2 3 4 5; do curl -s -o /dev/null -w "%{time_total}s  " http://127.0.0.1:8000/ ; done; echo
echo "== Медленные запросы за 24 часа (топ адресов)"
journalctl -u donatix --since "24 hours ago" --no-pager 2>/dev/null | grep -o "медленно: [A-Z]* [^ ]*" | sort | uniq -c | sort -rn | head -15
echo "== Ошибки за час"; journalctl -u donatix --since "1 hour ago" --no-pager -p err 2>/dev/null | tail -8
