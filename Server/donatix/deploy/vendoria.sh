#!/usr/bin/env bash
# Поставщик Vendoria: Standoff 2 и Clash of Clans. Токен магазина вводится скрыто и пишется только в .env.
# Заодно выключает CoinDrop (если у него нет незавершённых заказов). Оставить CoinDrop: KEEP_COINDROP=1
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/vendoria.sh | sudo bash
set -euo pipefail
APP_DIR="/home/donatix/app"; ENV_FILE="$APP_DIR/donatix/.env"; BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
BASE="${VENDORIA_URL:-https://vendoria.amadeustech.dev}"
say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден."
PY() { sudo -u donatix bash -c "cd '$APP_DIR' && .venv/bin/python -c \"$1\""; }

TOKEN="${VENDORIA_TOKEN:-}"
if [ -z "$TOKEN" ]; then printf 'Токен магазина Vendoria (вид 0:abcdef…, ввод скрыт): '; read -rs TOKEN < /dev/tty; echo; fi
[[ "$TOKEN" =~ ^[0-9]+:.+$ ]] || die "Токен должен быть вида 0:abcdef… (число, двоеточие, строка)."

say "Проверяю токен у Vendoria"
HTTP=$(curl -s -o /tmp/vd.json -w '%{http_code}' -m 20 -H "Authorization: Shop $TOKEN" "$BASE/api/shop/balance" || echo 000)
if [ "$HTTP" = "200" ]; then echo "✔ Токен работает. Баланс: $(cat /tmp/vd.json)"; else
  rm -f /tmp/vd.json; die "Vendoria ответила HTTP $HTTP — проверьте токен (и что он из боевого, а не тестового окружения)."; fi
rm -f /tmp/vd.json
curl -s -m 20 -H "Authorization: Shop $TOKEN" -H "Accept-Language: ru" "$BASE/api/services" | python3 -c '
import json, sys
try: s = json.load(sys.stdin)
except Exception: s = []
want = [x for x in s if any(k in (x.get("name","")+" "+x.get("originalName","")).lower() for k in ("standoff","clash of clans"))]
print("Игры у Vendoria:", len(s), "· нужные:", ", ".join(x.get("name","") for x in want) or "НЕ НАЙДЕНЫ")' || true

say "Обновляю код и сохраняю токен"
sudo -u donatix git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH" || true
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
DROP_CD=1
if [ "${KEEP_COINDROP:-0}" = "1" ]; then DROP_CD=0; fi
if [ "$DROP_CD" = "1" ]; then
  OPEN=$(PY "
import sqlite3
from donatix.config import Config
c = sqlite3.connect(Config.from_env().db_path)
print(c.execute(\\\"SELECT COUNT(*) FROM orders WHERE status IN ('processing','attention') AND supplier_order_id LIKE 'cd:%'\\\").fetchone()[0])" 2>/dev/null || echo 0)
  if [ "${OPEN:-0}" != "0" ]; then
    echo "⚠ У CoinDrop ещё $OPEN незавершённых заказов — CoinDrop пока оставляю. Запустите скрипт ещё раз позже."
    DROP_CD=0
  fi
fi
TOKEN="$TOKEN" DROP_CD="$DROP_CD" python3 - "$ENV_FILE" <<'PYEOF'
import os, re, sys
path = sys.argv[1]; s = open(path, encoding="utf-8").read()
line = "DONATIX_VENDORIA_TOKEN=" + os.environ["TOKEN"].strip()
s = re.sub(r"^DONATIX_VENDORIA_TOKEN=.*$", lambda m: line, s, flags=re.M) if re.search(r"^DONATIX_VENDORIA_TOKEN=", s, re.M) \
    else s.rstrip("\n") + "\n" + line + "\n"
if os.environ["DROP_CD"] == "1":
    s = re.sub(r"^DONATIX_COINDROP_(API_KEY|GAMES)=.*\n?", "", s, flags=re.M)
open(path, "w", encoding="utf-8").write(s)
PYEOF
chown donatix:donatix "$ENV_FILE"; chmod 600 "$ENV_FILE"
if [ "$DROP_CD" = "1" ]; then
  PY "
import sqlite3
from donatix.config import Config
c = sqlite3.connect(Config.from_env().db_path)
n = c.execute(\\\"UPDATE products SET active = 0 WHERE id LIKE 'cd-%' AND active = 1\\\").rowcount
c.commit(); print('CoinDrop выключен, скрыто его товаров:', n)" || true
fi

say "Перезапускаю сайт"
systemctl restart donatix
sleep 3

say "Загружаю каталог Vendoria (только Standoff 2 и Clash of Clans)"
sudo -u donatix bash -c "cd '$APP_DIR' && .venv/bin/python -m donatix sync --provider Vendoria" || \
  echo "⚠ Загрузка не удалась — повторите позже: sudo -u donatix bash -c 'cd $APP_DIR && .venv/bin/python -m donatix sync --provider Vendoria'"
say "Готово"
echo "Standoff 2 и Clash of Clans от Vendoria — в каталоге и в «Популярном». Заказы идут через Vendoria."
