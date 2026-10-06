#!/usr/bin/env bash
# Только посмотреть (ничего не меняет): какие способы покупки у Vendoria для Standoff 2 и Clash of Clans.
#   curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/vendoria-forms.sh | bash
set -uo pipefail
BASE="${VENDORIA_URL:-https://vendoria.amadeustech.dev}"
TOKEN="${VENDORIA_TOKEN:-}"
if [ -z "$TOKEN" ]; then printf 'Токен Vendoria (ввод скрыт): '; read -rs TOKEN < /dev/tty; echo; fi
TMP=$(mktemp -d)
for p in services categories forms "products?prices=true"; do
  curl -s -m 40 -H "Authorization: Shop $TOKEN" -H "Accept-Language: ru" "$BASE/api/$p" -o "$TMP/${p%%\?*}.json"
done
python3 - "$TMP" <<'PY'
import json, sys, os
d = sys.argv[1]
load = lambda n: json.load(open(os.path.join(d, n + ".json")))
try:
    services, cats, forms, products = load("services"), load("categories"), load("forms"), load("products")
except Exception as exc:
    print("Не прочиталось:", exc, open(os.path.join(d, "services.json")).read()[:300]); raise SystemExit
want = {s["id"]: s for s in services if any(k in (s.get("name", "") + " " + s.get("originalName", "")).lower()
                                               for k in ("standoff", "clash of clans"))}
for sid, s in want.items():
    print(f"\n=== {s['name']} (id {sid}){' — ЗАКРЫТА' if s.get('isClosed') else ''}")
    for f in [f for f in forms if f.get("serviceId") == sid]:
        flags = [x for x, on in (("просит КОД", f.get("hasRequest")), ("Google-подтверждение", f.get("hasGooglePrompt")),
                                  ("в архиве", f.get("isArchived"))) if on]
        fields = ", ".join(f"{x.get('label')}[{x.get('type')}{'' if x.get('required') else ', необяз.'}]"
                           for x in f.get("fields") or [])
        print(f"  форма {f['id']} «{f['name']}»: {fields}  {' · '.join(flags)}")
        n = sum(1 for p in products if str(f["id"]) in (p.get("prices") or {})
                and any(c["id"] == p.get("categoryId") and c.get("serviceId") == sid for c in cats))
        print(f"      пакетов с ценой: {n}")
    for p in [p for p in products if any(c["id"] == p.get("categoryId") and c.get("serviceId") == sid for c in cats)][:5]:
        print(f"    • {p['name']}: {p.get('prices')}")
PY
rm -rf "$TMP"
