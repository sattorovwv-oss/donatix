#!/usr/bin/env bash
# HTTPS для Donatix, когда сайты на сервере раздаёт Caddy в Docker, а у контейнера
# Caddy нет выхода в интернет (и он не может сам получить сертификат).
#
# Сертификат получает сам сервер (certbot), Caddy только отдаёт его.
# Продление — автоматически (certbot.timer + deploy-hook).
#
#   sudo DOMAIN=donatix.duckdns.org EMAIL=you@mail.com bash caddy-cert.sh
#   ALIASES="www.donatix.tj donatix.duckdns.org" — домены, которые переадресуются на DOMAIN
set -euo pipefail

DOMAIN="${DOMAIN:?укажите DOMAIN=...}"
ALIASES="${ALIASES:-}"
EMAIL="${EMAIL:?укажите EMAIL=...}"
C="${CADDY_CONTAINER:-averix-caddy-1}"
APP_PORT="${APP_PORT:-8000}"
ACME_PORT="${ACME_PORT:-8089}"

say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }

CF=$(docker inspect "$C" --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')
GW=$(docker inspect "$C" --format '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' | awk '{print $1}')
[ -n "$CF" ] && [ -f "$CF" ] && [ -n "$GW" ] || { echo "✖ Не нашёл Caddyfile или сеть контейнера $C"; exit 1; }
echo "Caddyfile: $CF   адрес сервера для контейнера: $GW"

command -v certbot >/dev/null || { apt-get update -qq && apt-get install -y -qq certbot >/dev/null; }

write_block() {  # $1 = with_https (0/1)
  python3 - "$CF" "$DOMAIN" "$GW" "$APP_PORT" "$ACME_PORT" "$1" "$ALIASES" <<'PY'
import re, sys
path, domain, gw, app, acme, https, aliases = sys.argv[1:]
aliases = [a for a in aliases.split() if a and a != domain]
names = " ".join(f"http://{d}" for d in [domain, *aliases])
s = open(path, encoding="utf-8").read()
# убрать прежние блоки Donatix (с маркерами и старый блок без них)
s = re.sub(r"\n?# donatix-begin.*?# donatix-end\n?", "\n", s, flags=re.S)
s = re.sub(r"\n?" + re.escape(domain) + r" \{\n\treverse_proxy [^\n]*\n\}\n?", "\n", s)
block = f"""
# donatix-begin
{names} {{
\thandle /.well-known/acme-challenge/* {{
\t\treverse_proxy {gw}:{acme}
\t}}
\thandle {{
\t\tredir https://{domain}{{uri}} permanent
\t}}
}}
"""
if https == "1":
    block += f"""https://{domain} {{
\ttls /data/donatix/fullchain.pem /data/donatix/privkey.pem
\treverse_proxy {gw}:{app}
}}
"""
    if aliases:  # старые и www-адреса ведут на основной домен
        block += f"""{" ".join("https://" + a for a in aliases)} {{
\ttls /data/donatix/fullchain.pem /data/donatix/privkey.pem
\tredir https://{domain}{{uri}} permanent
}}
"""
block += "# donatix-end\n"
open(path, "w", encoding="utf-8").write(s.rstrip() + "\n" + block)
PY
}

reload_caddy() {
  docker exec "$C" caddy reload --config /etc/caddy/Caddyfile 2>&1 | grep -v '"level":"info"' || true
}

cp "$CF" "$CF.bak.$(date +%s)"

say "Открываю путь для проверки домена"
write_block 0
reload_caddy

say "Получаю сертификат Let's Encrypt для $DOMAIN"
D_ARGS=(-d "$DOMAIN")
for a in $ALIASES; do D_ARGS+=(-d "$a"); done
# --cert-name: сертификат всегда лежит в live/$DOMAIN, даже когда в нём несколько доменов
certbot certonly --standalone --non-interactive --agree-tos -m "$EMAIL" --cert-name "$DOMAIN" "${D_ARGS[@]}" \
  --http-01-address "$GW" --http-01-port "$ACME_PORT" --keep-until-expiring --expand

say "Передаю сертификат в Caddy"
HOOK=/etc/letsencrypt/renewal-hooks/deploy/donatix-caddy.sh
mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<HOOKSH
#!/bin/sh
# Копирует сертификат $DOMAIN в контейнер Caddy после получения/продления.
docker exec $C mkdir -p /data/donatix
docker cp -L /etc/letsencrypt/live/$DOMAIN/fullchain.pem $C:/data/donatix/fullchain.pem
docker cp -L /etc/letsencrypt/live/$DOMAIN/privkey.pem $C:/data/donatix/privkey.pem
docker exec $C caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
HOOKSH
chmod +x "$HOOK"
"$HOOK"

say "Включаю HTTPS"
write_block 1
reload_caddy
systemctl enable --now certbot.timer >/dev/null 2>&1 || true

sleep 2
code=$(curl -s -o /dev/null -w "%{http_code}" --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/" || true)
if [ "$code" = "200" ]; then
  echo "✅ Готово: https://$DOMAIN открывается (код $code). Продление сертификата — автоматическое."
else
  echo "⚠ Проверка вернула код $code — пришлите вывод выше."
fi
