#!/usr/bin/env bash
# Второй поставщик CoinDrop (Standoff 2 и другие игры по ID). Ключ вводится скрыто и пишется только в .env.
# Игры по желанию можно ограничить списком: COINDROP_GAMES="standoff-2,pubg-mobile"
#
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/coindrop.sh | sudo bash
set -euo pipefail
APP_DIR="/home/donatix/app"; ENV_FILE="$APP_DIR/donatix/.env"; BRANCH="${DONATIX_BRANCH:-claude/website-api-sales-96wxcs}"
die() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" = 0 ] || die "Запустите через sudo."
[ -f "$ENV_FILE" ] || die "Donatix не найден."

KEY="${COINDROP_KEY:-}"; GAMES="${COINDROP_GAMES:-}"
if [ -z "$KEY" ]; then printf 'API-ключ CoinDrop (cd_…, скрыто): '; read -rs KEY < /dev/tty; echo; fi
[[ "$KEY" == cd_* ]] || die "Ключ CoinDrop должен начинаться с cd_"

sudo -u donatix git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH" || true
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
KEY="$KEY" GAMES="$GAMES" python3 - "$ENV_FILE" <<'PY'
import os, re, sys
path = sys.argv[1]; s = open(path, encoding="utf-8").read()
for key, env in (("DONATIX_COINDROP_API_KEY", "KEY"), ("DONATIX_COINDROP_GAMES", "GAMES")):
    value = os.environ[env].strip()
    if env == "GAMES" and not value and not re.search(r"^DONATIX_COINDROP_GAMES=", s, re.M):
        continue
    line = f"{key}={value}"
    s = re.sub(rf"^{key}=.*$", lambda m: line, s, flags=re.M) if re.search(rf"^{key}=", s, re.M) else s.rstrip("\n") + "\n" + line + "\n"
open(path, "w", encoding="utf-8").write(s)
PY
chown donatix:donatix "$ENV_FILE"; chmod 600 "$ENV_FILE"

# Проверка ключа до перезапуска: живой ли баланс
echo "Проверяю ключ у CoinDrop…"
HTTP=$(curl -s -o /tmp/cd.json -w '%{http_code}' -H "X-API-Key: $KEY" https://coindrop.uz/api/v1/balance || echo 000)
if [ "$HTTP" = "200" ]; then echo "✔ Ключ работает. Баланс: $(cat /tmp/cd.json)"; else
  echo "⚠ CoinDrop ответил HTTP $HTTP: $(head -c 200 /tmp/cd.json). Ключ всё равно сохранён — проверьте в профиле CoinDrop, включён ли API."; fi
rm -f /tmp/cd.json
echo "Ищу Standoff 2 в каталоге CoinDrop…"
curl -s -m 30 -H "X-API-Key: $KEY" https://coindrop.uz/api/v1/games -o /tmp/cdg.json
KEY="$KEY" python3 - <<'PY' || true
import json, os, urllib.request
try:
    d = json.load(open("/tmp/cdg.json"))
except Exception:
    print("⚠ список игр не прочитался:", open("/tmp/cdg.json").read()[:200]); raise SystemExit
def lst(x, *keys):
    if isinstance(x, list): return x
    for k in keys + ("data", "items", "results"):
        v = x.get(k) if isinstance(x, dict) else None
        if isinstance(v, list): return v
        if isinstance(v, dict):
            r = lst(v, *keys)
            if r: return r
    return []
games = lst(d, "games")
print(f"игр у CoinDrop: {len(games)}")
so = [g for g in games if "standoff" in json.dumps(g).lower()]
for g in so:
    key = g.get("game_key") or g.get("key") or g.get("slug")
    print(f"✔ Standoff: game_key={key}  id_type={g.get('id_type')}  amount_based={g.get('amount_based')}")
    req = urllib.request.Request(f"https://coindrop.uz/api/v1/games/{key}/products", headers={"X-API-Key": os.environ["KEY"]})
    try:
        prods = lst(json.load(urllib.request.urlopen(req, timeout=30)), "products")
        print(f"  пакетов: {len(prods)}; например: " + "; ".join(
            f"{p.get('name')} — ${p.get('price_usd', p.get('price'))}" for p in prods[:3]))
    except Exception as e:
        print("  ⚠ пакеты не прочитались:", e)
if not so:
    print("⚠ Standoff 2 в списке не найден. Первые игры:", ", ".join(str(g.get('game_key') or g.get('key')) for g in games[:15]))
PY
rm -f /tmp/cdg.json
systemctl restart donatix; sleep 4
printf '\033[1;32m✔ CoinDrop подключён.\033[0m\n'
echo "1) В админке → «Загрузка каталога» нажмите обновить — появятся игры CoinDrop (Standoff 2 и др.)."
echo "2) Баланс CoinDrop виден в админке на «Сводке» отдельной карточкой."
echo "3) Пополните баланс CoinDrop в их кабинете — иначе заказы будут возвращаться клиентам."
