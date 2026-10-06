#!/usr/bin/env bash
# Найти игру у FazerCards: в пополнениях, подарочных картах, ключах и ручных услугах. Ничего не меняет.
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/find-game.sh | sudo GAME="clash of clans" bash
set -uo pipefail
ENV_FILE=/home/donatix/app/donatix/.env
KEY=$(grep -E '^FAZER_API_KEY=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'")
[ -n "$KEY" ] || { echo "✖ FAZER_API_KEY не найден в $ENV_FILE"; exit 1; }
GAME="${GAME:-clash of clans}" KEY="$KEY" python3 - <<'PY'
import json, os, urllib.request, urllib.parse
key, game = os.environ["KEY"], os.environ["GAME"].lower()
BASE = "https://api.fzr.cards/api/v2"
def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        return json.load(urllib.request.urlopen(req, timeout=40))
    except Exception as e:
        return {"ok": False, "error": str(e)}
def paged(path):
    out, cursor = [], None
    for _ in range(40):
        p = {"limit": 500}
        if cursor: p["cursor"] = cursor
        d = get(path, **p)
        if not d.get("ok", True) and "error" in d:
            print(f"  ⚠ {path}: {d['error'][:120]}"); break
        out += d.get("items") or []
        meta = d.get("meta") or {}
        cursor = meta.get("next_cursor")
        if not meta.get("has_more") or not cursor: break
    return out
found = False
for title, path, idkey in (("Пополнения по ID (/topups)", "/topups", "category_id"),
                           ("Подарочные карты (/giftcards)", "/giftcards", "category_id"),
                           ("Ключи игр (/gamekeys)", "/gamekeys", "game_id")):
    hits = [i for i in paged(path) if game in json.dumps(i, ensure_ascii=False).lower()]
    print(f"== {title}: найдено {len(hits)}")
    for h in hits[:10]:
        found = True
        print(f"   ✔ {h.get('name')}  ({idkey}={h.get(idkey)})")
d = get("/manual-services")
items = d.get("items") or []
hits = [i for i in items if game in json.dumps(i, ensure_ascii=False).lower()]
print(f"== Ручные услуги (/manual-services): найдено {len(hits)}" + ("" if d.get("ok", True) else f"  ⚠ {d.get('error','')[:120]}"))
for h in hits[:10]:
    found = True
    print(f"   ✔ {h.get('name')}  (id={h.get('id')}, kind={h.get('kind')})")
    offers = get(f"/manual-services/{h.get('id')}/offers").get("items") or []
    for o in offers[:5]:
        print(f"      • {o.get('name')} — ${o.get('price_usd')} (~{o.get('delivery_minutes')} мин)")
if not found:
    print(f"\n✖ «{game}» у FazerCards сейчас нет. Возможно, ещё не добавили — проверьте позже.")
PY
