#!/usr/bin/env bash
#
# Установка бота на сервер одной командой.
#
#   bash <(curl -sSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/telegram-stars-sales-bot-caqst0/stars_bot/install.sh)
#
# Повторный запуск обновляет бота до последней версии, настройки не трогает.
#
# Активация без вопросов — токен, ID админа и ключ поставщика сразу:
#
#   bash install.sh ТОКЕН ID_АДМИНА КЛЮЧ_ПОСТАВЩИКА
#
# Из распакованного архива (папка с app/ и этим файлом) ставится то, что
# лежит рядом, — GitHub не нужен. Так архив можно отдать другому человеку.

set -euo pipefail

# Всё переопределяется переменными окружения:
#   DIR=/srv/bot bash install.sh
REPO="${REPO:-https://github.com/alijon26062006-bit/AlijonMahmadjonov.git}"
BRANCH="${BRANCH:-claude/telegram-stars-sales-bot-caqst0}"
DIR="${DIR:-/opt/stars-bot}"
SERVICE="${SERVICE:-stars-bot}"
RUN_USER="${RUN_USER:-starsbot}"
RAW_INSTALLER="${RAW_INSTALLER:-https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/telegram-stars-sales-bot-caqst0/stars_bot/install.sh}"

# Откуда брать код. Рядом с установщиком лежит сам бот и это не рабочая
# копия git — значит, запустили из архива: ставим его, в сеть за кодом не
# ходим. По конвейеру из curl рядом ничего нет — тогда берём из GitHub.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
LOCAL_SRC=""
if [ -z "${FROM_GIT:-}" ] && [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/app" ] \
        && [ -f "$SELF_DIR/requirements.txt" ] && [ ! -d "$SELF_DIR/../.git" ]; then
    LOCAL_SRC="$SELF_DIR"
    # Обновлять такой бот тоже из архива: чужой GitHub тут ни при чём.
    RAW_INSTALLER=""
fi

# Ввод читаем из терминала, а не из stdin: скрипт мог прийти по конвейеру
# из curl, и тогда stdin занят самим скриптом. Если терминала нет
# (запуск из другого скрипта), остаёмся на обычном stdin.
if [ -r /dev/tty ] && { exec 3</dev/tty; } 2>/dev/null; then
    exec 3<&-
    HAS_TTY=true
else
    HAS_TTY=false
fi

say()  { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m  ✅ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m  ⚠️  %s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31m❌ %s\033[0m\n\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- проверки

[ "$(id -u)" -eq 0 ] || SUDO="sudo"
SUDO="${SUDO:-}"
if [ -n "$SUDO" ] && ! command -v sudo >/dev/null; then
    die "Нужны права root. Зайдите как root или установите sudo."
fi

command -v systemctl >/dev/null || die "На сервере нет systemd — этот установщик рассчитан на него."

say "Ставлю системные пакеты"
if command -v apt-get >/dev/null; then
    $SUDO apt-get update -qq
    # env нужен явно: если $SUDO пустой, bash разберёт VAR=... как имя команды.
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 python3-venv python3-pip git ca-certificates >/dev/null
elif command -v dnf >/dev/null; then
    $SUDO dnf install -y -q python3 python3-pip git ca-certificates
elif command -v yum >/dev/null; then
    $SUDO yum install -y -q python3 python3-pip git ca-certificates
else
    warn "Неизвестный менеджер пакетов — проверьте, что стоят python3, venv и git."
fi
ok "python $(python3 --version 2>&1 | cut -d' ' -f2), git $(git --version | cut -d' ' -f3)"

# ------------------------------------------------------- пользователь и код

if ! id "$RUN_USER" >/dev/null 2>&1; then
    say "Создаю системного пользователя $RUN_USER"
    $SUDO useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
        || $SUDO useradd --system --create-home --shell /sbin/nologin "$RUN_USER"
    ok "Бот будет работать не от root — так безопаснее"
fi

UPDATING=false
if [ -n "$LOCAL_SRC" ]; then
    APP="$DIR/stars_bot"
    if [ -d "$APP/app" ]; then
        UPDATING=true
        say "Обновляю код из архива"
    else
        say "Ставлю бота из архива"
    fi
    # Запуск из уже установленной папки (stars-bot update) — копировать
    # нечего, только пересобрать и перезапустить.
    if [ "$(realpath "$LOCAL_SRC")" != "$(realpath -m "$APP")" ]; then
        $SUDO mkdir -p "$APP"
        # Старый код убираем целиком: иначе удалённые в новой версии файлы
        # остались бы лежать. Настройки, база и окружение не трогаются.
        $SUDO rm -rf "$APP/app" "$APP/tests" "$APP/docs"
        tar -C "$LOCAL_SRC" --exclude=./.env --exclude=./data --exclude=./.venv \
            --exclude='*.sqlite3*' --exclude='*.session*' --exclude='__pycache__' -cf - . \
            | $SUDO tar -C "$APP" -xf -
    fi
elif [ -d "$DIR/.git" ]; then
    UPDATING=true
    say "Обновляю код"
    $SUDO git -C "$DIR" fetch --quiet origin "$BRANCH"
    $SUDO git -C "$DIR" checkout --quiet "$BRANCH"
    $SUDO git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
else
    say "Скачиваю бота"
    $SUDO rm -rf "$DIR"
    $SUDO git clone --quiet --branch "$BRANCH" --depth 1 "$REPO" "$DIR"
fi
ok "Код в $DIR"

APP="$DIR/stars_bot"
[ -d "$APP" ] || die "В репозитории нет папки stars_bot — что-то пошло не так."

say "Ставлю зависимости"
$SUDO python3 -m venv "$APP/.venv" \
    || die "Не удалось создать окружение. Поставьте пакет python3-venv."
$SUDO "$APP/.venv/bin/pip" install -q --upgrade pip
$SUDO "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
ok "Библиотеки установлены"

# ------------------------------------------------------------- настройка

# Три значения в командной строке — активация без вопросов, даже поверх
# уже настроенного бота (так меняют токен или ключ одной строкой).
if [ "$#" -ge 3 ]; then
    say "Активация бота"
    ( cd "$APP" && $SUDO "$APP/.venv/bin/python" "$APP/setup.py" --quick "$1" "$2" "$3" ) \
        || die "Активация не прошла — исправьте то, что написано выше, и запустите снова."
elif [ ! -f "$APP/.env" ]; then
    say "Активация бота"
    echo "  Понадобятся три вещи: токен от @BotFather, ваш ID от @userinfobot"
    echo "  и ключ поставщика. Реквизиты и цены — потом, прямо в боте."
    echo ""
    if $HAS_TTY; then
        ( cd "$APP" && $SUDO "$APP/.venv/bin/python" "$APP/setup.py" --quick < /dev/tty )
    else
        ( cd "$APP" && $SUDO "$APP/.venv/bin/python" "$APP/setup.py" --quick )
    fi || die "Активация не прошла — запустите установщик ещё раз."
else
    ok "Настройки уже есть, не трогаю ($APP/.env)"
    echo "     Поменять: sudo $APP/.venv/bin/python $APP/setup.py"
fi

[ -f "$APP/.env" ] || die "Настройка не завершена — .env не создан."

$SUDO mkdir -p "$APP/data"
$SUDO chown -R "$RUN_USER:$RUN_USER" "$APP"
$SUDO chmod 600 "$APP/.env"

# --------------------------------------------------------------- systemd

say "Настраиваю автозапуск"
$SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Telegram Stars Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m app.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Бот работает с деньгами — ограничиваем ему доступ к системе.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP/data
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT

# Юзербот — отдельная служба. Отдельная нарочно: он входит в Telegram
# под номером владельца, и если его уронит отзыв сессии, продажи в боте
# продолжатся — оплату просто придётся подтверждать руками.
# Запускается только когда настроен: без ключей Telegram делать ему нечего.
$SUDO tee "/etc/systemd/system/$SERVICE-userbot.service" >/dev/null <<UNIT
[Unit]
Description=Stars Bot — приём оплат от банковского бота
After=network-online.target $SERVICE.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m app.userbot
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP/data
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT

# Короткая команда управления, чтобы не помнить длинные пути.
$SUDO tee /usr/local/bin/stars-bot >/dev/null <<HELPER
#!/usr/bin/env bash
# Управление ботом. Создан установщиком, правится там же.
set -euo pipefail
APP="$APP"
SERVICE="$SERVICE"
RUN_USER="$RUN_USER"
INSTALLER="\$APP/install.sh"
RAW_URL="$RAW_INSTALLER"

case "\${1:-help}" in
    update)
        if [ -z "\$RAW_URL" ]; then
            # Бот поставлен из архива — новая версия тоже приходит архивом.
            echo "Бот поставлен из архива. Чтобы обновить:"
            echo "  1) распакуйте новый архив"
            echo "  2) в его папке: sudo bash install.sh"
            echo "Настройки и база сохранятся."
            exit 0
        fi
        # Копия до обновления: если новая версия окажется хуже, вернуться
        # будет к чему. Стоит секунду, а спасает всё.
        "\$0" backup >/dev/null 2>&1 && echo "Копия базы сделана."
        echo "Обновляю бота…"
        # Берём установщик из репозитория: он мог измениться вместе с ботом,
        # а локальная копия — это версия с прошлого обновления.
        FRESH=\$(mktemp)
        if curl -sSL "\$RAW_URL" -o "\$FRESH" 2>/dev/null && [ -s "\$FRESH" ]; then
            exec bash "\$FRESH"
        fi
        echo "Не скачался свежий установщик, беру локальный."
        rm -f "\$FRESH"
        exec bash "\$INSTALLER"
        ;;
    restart) systemctl restart "\$SERVICE" && echo "✅ Перезапущен" ;;
    stop)    systemctl stop "\$SERVICE"    && echo "⏹  Остановлен"  ;;
    start)   systemctl start "\$SERVICE"   && echo "▶️  Запущен"     ;;
    status)
        # Обе службы разом: спрашивают «как бот», а забывают про приём
        # оплат — а это ровно та половина, молчание которой не видно.
        systemctl status "\$SERVICE" --no-pager
        echo
        if systemctl list-unit-files | grep -q "\$SERVICE-userbot"; then
            systemctl status "\$SERVICE-userbot" --no-pager || true
        fi
        ;;
    logs)    journalctl -u "\$SERVICE" -f ;;
    errors)  journalctl -u "\$SERVICE" -p err -n 50 --no-pager ;;
    setup)
        sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py"
        systemctl restart "\$SERVICE" && echo "✅ Настройки применены"
        ;;
    activate)
        # Токен, ID админа, ключ поставщика — и бот работает. Без трёх
        # значений спрашивает их по одному.
        if [ -n "\${4:-}" ]; then
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --quick --replace "\$2" "\$3" "\$4"
        else
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --quick --replace
        fi
        systemctl restart "\$SERVICE"
        echo "✅ Бот активирован и перезапущен — он сам напишет вам в Telegram"
        ;;
    fazer|mystars|apifragment|delivery|pay|prices|telegram|links)
        # Без второго аргумента — обычный диалог: спрашивает по одному.
        # С ключом — сразу применяет, ничего не спрашивая.
        if [ -n "\${2:-}" ] && [ "\$1" = "fazer" ]; then
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
                FRAGMENT_MODE=fazer FAZER_API_KEY="\$2" \
                FAZER_BASE_URL=https://api.fzr.cards
        elif [ -n "\${2:-}" ] && [ "\$1" = "mystars" ]; then
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
                FRAGMENT_MODE=mystars MYSTARS_API_KEY="\$2" \
                MYSTARS_BASE_URL=https://api.mystars.tg/v1 \
                MYSTARS_CURRENCY="\${3:-ton}"
        elif [ -n "\${2:-}" ] && [ "\$1" = "apifragment" ]; then
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
                FRAGMENT_MODE=api FRAGMENT_API_KEY="\$2"
        else
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --only "\$1"
        fi
        systemctl restart "\$SERVICE"
        echo "✅ Готово, бот перезапущен"
        [ "\$1" = "mystars" ] || [ "\$1" = "delivery" ] && \
            echo "   Проверьте: /panel -> Проверить связь"
        ;;
    games)
        # Ключи второго поставщика одной командой: файлы руками не правим,
        # бот перезапускается сам.
        if [ -z "\${2:-}" ]; then
            echo "Использование: stars-bot games КЛЮЧ_ПОСТАВЩИКА [КЛЮЧ_НИКОВ]"
            echo "  КЛЮЧ_ПОСТАВЩИКА — с его счёта покупаются игры"
            echo "  КЛЮЧ_НИКОВ      — gameskinbo, ник по ID (необязательно)"
            exit 1
        fi
        sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
            FAZER_GAMES_KEY="\$2"
        if [ -n "\${3:-}" ]; then
            sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
                GAMESKINBO_KEY="\$3"
        fi
        systemctl restart "\$SERVICE"
        echo "✅ Ключи для игр записаны, бот перезапущен"
        echo "   Проверьте: /panel → Балансы ключей"
        ;;
    userbot)
        # Управление приёмом оплат от банка. Отдельной службой, чтобы
        # падение юзербота не трогало продажи.
        UB="\$SERVICE-userbot"
        case "\${2:-help}" in
            login)
                # Первый вход делается руками: Telegram пришлёт код.
                echo "Telegram пришлёт код в ваш же Telegram. Введите его здесь."
                # Запускать обязательно из папки бота: «python -m app...»
                # ищет пакет в текущем каталоге, а службы выше спасает
                # WorkingDirectory в unit-файле — здесь его нет.
                ( cd "\$APP" && sudo -u "\$RUN_USER" \
                    "\$APP/.venv/bin/python" -m app.userbot.login )
                ;;
            start)   systemctl enable --now "\$UB" && echo "✅ Юзербот запущен" ;;
            stop)    systemctl disable --now "\$UB" && echo "⏹  Юзербот остановлен" ;;
            restart) systemctl restart "\$UB" && echo "✅ Перезапущен" ;;
            logs)    journalctl -u "\$UB" -f ;;
            status)  systemctl status "\$UB" --no-pager ;;
            bank)
                if [ -z "\${3:-}" ]; then
                    echo "Использование: stars-bot userbot bank ЮЗЕРНЕЙМ_ИЛИ_ID"
                    echo "  Уведомления от кого-либо ещё разбираться не будут."
                    exit 1
                fi
                if ! echo "\${3#@}" | grep -qE '^[A-Za-z0-9_]{4,32}$'; then
                    echo "❌ Это не похоже на юзернейм или id: \$3"
                    echo "   Нужно вроде bank_notify_bot или числовой id."
                    exit 1
                fi
                sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" \
                    --set BANK_BOT="\${3#@}"
                systemctl restart "\$UB" 2>/dev/null || true
                echo "✅ Банковский бот: \${3#@}"
                ;;
            keys)
                if [ -z "\${4:-}" ]; then
                    echo "Использование: stars-bot userbot keys API_ID API_HASH"
                    echo "  Берутся на https://my.telegram.org → API development tools"
                    exit 1
                fi
                # Проверяем форму ключей, а не только их наличие: подставить
                # слово-заготовку вместо значения — самая частая ошибка, и
                # без проверки она всплывёт только при входе в Telegram.
                if ! echo "\$3" | grep -qE '^[0-9]{5,12}$'; then
                    echo "❌ API_ID — это число, например 12345678."
                    echo "   Вы прислали: \$3"
                    echo "   Возьмите его на https://my.telegram.org"
                    exit 1
                fi
                if ! echo "\$4" | grep -qiE '^[a-f0-9]{32}$'; then
                    echo "❌ API_HASH — 32 знака: цифры и буквы a-f."
                    echo "   Длина присланного: \${#4}"
                    echo "   Возьмите его на https://my.telegram.org"
                    exit 1
                fi
                sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" \
                    --set TG_API_ID="\$3" TG_API_HASH="\$4"
                echo "✅ Ключи Telegram записаны"
                echo "   Дальше: stars-bot userbot login"
                ;;
            *)
                echo "stars-bot userbot login     первый вход в Telegram"
                echo "stars-bot userbot keys ID HASH   ключи с my.telegram.org"
                echo "stars-bot userbot bank ИМЯ  чей уведомления слушать"
                echo "stars-bot userbot start     включить и запустить"
                echo "stars-bot userbot stop      остановить"
                echo "stars-bot userbot logs      смотреть работу живьём"
                echo "stars-bot userbot status    работает ли"
                ;;
        esac
        ;;
    api)
        # Адрес для API и вебхуков одной командой: .env руками не правим,
        # а порт с адресом связаны — забыть одно из двух проще всего.
        if [ -z "\${2:-}" ]; then
            echo "Использование: stars-bot api https://ваш-адрес [ПОРТ] [ХОСТ]"
            echo "  Адрес — тот, что видят разработчики и поставщик."
            echo "  ПОРТ  — какой слушает бот внутри сервера, по умолчанию 8443."
            echo "  ХОСТ  — на каком адресе слушать, по умолчанию 127.0.0.1."
            echo "          Если впереди веб-сервер в Docker, нужен адрес"
            echo "          его шлюза, например 172.20.0.1 — из контейнера"
            echo "          петля 127.0.0.1 ведёт в сам контейнер, не к нам."
            exit 1
        fi
        case "\$2" in
            https://*) ;;
            *) echo "❌ Адрес должен начинаться с https:// — иначе ключи"
               echo "   разработчиков полетят по сети открытым текстом."
               exit 1 ;;
        esac
        APIPORT="\${3:-8443}"
        APIHOST="\${4:-127.0.0.1}"
        # Наружу не слушаем никогда: снаружи в бота ходят через веб-сервер
        # с шифрованием. На 0.0.0.0 остался бы открытый порт без HTTPS,
        # и ключи разработчиков полетели бы по сети открытым текстом.
        if [ "\$APIHOST" = "0.0.0.0" ] || [ "\$APIHOST" = "::" ]; then
            echo "❌ Слушать на \$APIHOST нельзя: порт станет виден из"
            echo "   интернета без шифрования. Укажите 127.0.0.1 или адрес"
            echo "   шлюза Docker (docker inspect ИМЯ_КОНТЕЙНЕРА)."
            exit 1
        fi
        # Проверяем, только если есть чем: без утилиты ip отказывать
        # нельзя — она есть не в каждом образе, а адрес может быть верным.
        if command -v ip >/dev/null 2>&1 && ! ip -4 addr | grep -q "inet \$APIHOST/"; then
            echo "❌ Адреса \$APIHOST на этом сервере нет — бот не сможет"
            echo "   на нём слушать и не запустится. Проверьте: ip -4 addr"
            exit 1
        fi
        sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
            WEBHOOK_PUBLIC_URL="\${2%/}" WEBHOOK_PORT="\$APIPORT" \
            WEBHOOK_HOST="\$APIHOST"
        systemctl restart "\$SERVICE"
        echo "✅ Адрес записан, бот перезапущен"
        echo "   Слушает: \$APIHOST:\$APIPORT (снаружи напрямую не виден)"
        echo "   API:           \${2%/}/api/v1"
        echo "   Документация:  \${2%/}/api/v1/docs"
        echo "   Вебхук для поставщика: \${2%/}/webhook/fazer"
        echo ""
        echo "   Дальше в боте: /panel → 🧩 API → Включить API"
        echo "   и там же «За обратным прокси: да», раз впереди nginx."
        ;;
    caddy)
        # Блок веб-сервера легко потерять: Caddyfile нередко лежит внутри
        # git-репозитория соседнего проекта, и очередной git pull затирает его
        # вместе с нашим доменом — API снаружи отваливается молча, а бот при
        # этом работает и в логах чисто. Эта команда возвращает блок на место.
        DOMAIN="\${2:-}"
        if [ -z "\$DOMAIN" ]; then
            echo "Использование: stars-bot caddy ДОМЕН [ФАЙЛ] [КОНТЕЙНЕР]"
            echo "  ДОМЕН     — например mybot.duckdns.org"
            echo "  ФАЙЛ      — путь к Caddyfile; не указан — найдётся сам"
            echo "  КОНТЕЙНЕР — имя контейнера Caddy; не указано — найдётся само"
            echo ""
            echo "  Возвращает блок домена, если его затёрло обновлением"
            echo "  соседнего проекта, и перечитывает конфигурацию."
            echo "  Целый блок не трогает — запускать можно хоть каждый день."
            exit 1
        fi
        if ! command -v docker >/dev/null 2>&1; then
            echo "❌ docker не найден — команда рассчитана на Caddy в контейнере."
            exit 1
        fi
        CONT="\${4:-}"
        [ -n "\$CONT" ] || CONT=\$(docker ps --format '{{.Names}}' | grep -i caddy | head -1)
        if [ -z "\$CONT" ]; then
            echo "❌ Контейнер Caddy не найден. Укажите его имя четвёртым словом,"
            echo "   список: docker ps --format '{{.Names}}'"
            exit 1
        fi
        CFILE="\${3:-}"
        [ -n "\$CFILE" ] || CFILE=\$(docker inspect -f \
            '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}' \
            "\$CONT" 2>/dev/null)
        if [ -z "\$CFILE" ] || [ ! -f "\$CFILE" ]; then
            echo "❌ Caddyfile не нашёлся. Укажите путь к нему третьим словом."
            exit 1
        fi
        if grep -qF "\$DOMAIN {" "\$CFILE"; then
            echo "✅ Блок \$DOMAIN на месте, менять нечего"
            echo "   Файл: \$CFILE"
            exit 0
        fi
        # Куда проксировать — берём из настроек бота, а не вписываем на память:
        # адрес с портом меняются командой stars-bot api, и разойтись им нельзя.
        UPHOST=\$(sed -n 's/^WEBHOOK_HOST=//p' "\$APP/.env" | tail -1 | tr -d '"')
        UPPORT=\$(sed -n 's/^WEBHOOK_PORT=//p' "\$APP/.env" | tail -1 | tr -d '"')
        UPHOST="\${UPHOST:-127.0.0.1}"
        UPPORT="\${UPPORT:-8443}"
        # Из контейнера петля ведёт в сам контейнер, а не к нам: проксировать
        # на 127.0.0.1 бессмысленно, получится тот же молчаливый 502.
        if [ "\$UPHOST" = "127.0.0.1" ] || [ "\$UPHOST" = "::1" ]; then
            echo "❌ Бот слушает \$UPHOST — из контейнера это сам контейнер."
            echo "   Сначала переведите его на адрес шлюза Docker:"
            echo "   stars-bot api https://\$DOMAIN \$UPPORT АДРЕС_ШЛЮЗА"
            exit 1
        fi
        BAK="\$CFILE.bak-\$(date +%Y%m%d-%H%M%S)"
        cp "\$CFILE" "\$BAK"
        cat >> "\$CFILE" <<BLOCK

\$DOMAIN {
        encode zstd gzip

        reverse_proxy \$UPHOST:\$UPPORT {
                header_up X-Real-IP {remote_host}
        }

        log {
                output stdout
                format json
        }
}
BLOCK
        ERR=\$(docker exec "\$CONT" caddy validate --config /etc/caddy/Caddyfile 2>&1) || {
            cp "\$BAK" "\$CFILE"
            echo "❌ Caddy забраковал конфигурацию — файл вернул как был."
            echo "\$ERR" | tail -5
            exit 1
        }
        docker exec "\$CONT" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
            || docker restart "\$CONT" >/dev/null
        echo "✅ Блок \$DOMAIN возвращён и применён"
        echo "   Файл:       \$CFILE"
        echo "   Проксирует: \$UPHOST:\$UPPORT"
        echo "   Копия прежнего файла: \$BAK"
        echo ""
        echo "   Проверка: curl -s https://\$DOMAIN/api/v1/health"
        ;;
    checker)
        # Ключ проверки ID: он показывает клиенту ник до оплаты.
        # Вписываем на сервере, а не в репозиторий: репозиторий открытый.
        if [ -z "\${2:-}" ]; then
            echo "Использование: stars-bot checker КЛЮЧ"
            echo "  КЛЮЧ — pk_live_… из кабинета volsever.com"
            echo "  Проверяет ID игрока и показывает его ник перед покупкой."
            exit 1
        fi
        sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set \
            VOLSEVER_KEY="\$2"
        systemctl restart "\$SERVICE"
        echo "✅ Ключ проверки ID записан, бот перезапущен"
        echo "   Дальше: /panel → Игры → Проверка ID игрока →"
        echo "           Привязать игры к проверке"
        ;;
    mock)
        sudo -u "\$RUN_USER" "\$APP/.venv/bin/python" "\$APP/setup.py" --set FRAGMENT_MODE=mock
        systemctl restart "\$SERVICE" && echo "✅ Режим проверки: звёзды не отправляются"
        ;;
    backup)
        # Простым cp живую базу копировать нельзя: при WAL часть записей
        # лежит в отдельном файле, и копия получается рваной — ровно в
        # тот единственный раз, когда она понадобится. Поэтому просим
        # сам SQLite сделать согласованный снимок.
        DIR="/root/stars-bot-backups"
        mkdir -p "\$DIR"
        DEST="\$DIR/bot-\$(date +%Y%m%d-%H%M%S).sqlite3"
        "\$APP/.venv/bin/python" - "\$APP/data/bot.sqlite3" "\$DEST" <<'PYBK'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
source.close()
target.close()
PYBK
        # Держим две недели: старше не нужно, а место на диске конечно.
        ls -1t "\$DIR"/bot-*.sqlite3 2>/dev/null | tail -n +15 | xargs -r rm -f
        echo "✅ Копия базы: \$DEST"
        echo "   Всего копий: \$(ls -1 "\$DIR"/bot-*.sqlite3 2>/dev/null | wc -l)"
        ;;
    *)
        cat <<TXT
Управление ботом:

  stars-bot update    обновить код и перезапустить
  stars-bot restart   перезапустить
  stars-bot stop      остановить
  stars-bot start     запустить
  stars-bot status    работает ли
  stars-bot logs      смотреть логи живьём (Ctrl+C — выйти)
  stars-bot errors    последние ошибки
  stars-bot setup     изменить настройки и перезапустить
  stars-bot activate [ТОКЕН ID КЛЮЧ]
                      активация: токен, ID админа, ключ поставщика
  stars-bot api АДРЕС задать адрес для API и вебхуков
  stars-bot caddy ДОМЕН
                      вернуть блок веб-сервера, если его затёрло
                      обновлением соседнего проекта
  stars-bot userbot   приём оплат от банка (login/start/stop/logs)
  stars-bot games КЛЮЧ [КЛЮЧ_НИКОВ]
                      ключи второго поставщика: с него идут игры
  stars-bot checker КЛЮЧ
                      ключ проверки ID: ник игрока до оплаты
  stars-bot backup    сохранить копию базы

Настройка по частям (спрашивает по одному вопросу):
  stars-bot fazer         подключить FazerCards (баланс, без сид-фразы)
  stars-bot mystars       подключить MyStars
  stars-bot apifragment   подключить apifragment.online
  stars-bot delivery      выбрать способ выдачи
  stars-bot telegram      токен бота и ваш ID
  stars-bot pay           реквизиты карты
  stars-bot prices        цены
  stars-bot links         поддержка и отзывы
  stars-bot mock          режим проверки, звёзды не отправляются

Быстро, без вопросов:
  stars-bot fazer КЛЮЧ         подключить FazerCards одной строкой
  stars-bot mystars КЛЮЧ       подключить MyStars одной строкой

Все команды запускать через sudo.
TXT
        ;;
esac
HELPER
$SUDO chmod +x /usr/local/bin/stars-bot

# Ежедневная копия базы. Без неё «сделаю потом» превращается в «не было
# ни одной» — а узнают об этом в тот день, когда база уже потеряна.
$SUDO tee /etc/systemd/system/stars-bot-backup.service >/dev/null <<UNIT
[Unit]
Description=Копия базы Telegram Stars Bot

[Service]
Type=oneshot
ExecStart=/usr/local/bin/stars-bot backup
UNIT

$SUDO tee /etc/systemd/system/stars-bot-backup.timer >/dev/null <<UNIT
[Unit]
Description=Копия базы раз в сутки

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable --quiet --now stars-bot-backup.timer
ok "База копируется каждую ночь, хранятся две недели"

$SUDO systemctl daemon-reload
$SUDO systemctl enable --quiet "$SERVICE"
$SUDO systemctl restart "$SERVICE"
ok "Бот будет сам подниматься после перезагрузки и падений"

# Юзербот — отдельная служба, и её тоже надо поднять на новый код.
# Без этого бот обновляется, а приём оплат продолжает крутить старый до
# перезагрузки сервера: расхождение, которое никак себя не проявляет,
# пока однажды не разойдутся разбор уведомления и то, что ждёт бот.
# Трогаем только если она уже работает: не настроен — нечего и запускать.
if $SUDO systemctl is-active --quiet "$SERVICE-userbot"; then
    $SUDO systemctl restart "$SERVICE-userbot"
    ok "Приём оплат от банка перезапущен на новом коде"
elif $SUDO systemctl is-enabled --quiet "$SERVICE-userbot" 2>/dev/null; then
    $SUDO systemctl start "$SERVICE-userbot"
    ok "Приём оплат от банка запущен"
fi

# ---------------------------------------------------------------- проверка

say "Проверяю запуск"
sleep 5

if $SUDO systemctl is-active --quiet "$SERVICE"; then
    printf "\n\033[1;32m═══════════════════════════════════════════\033[0m\n"
    if $UPDATING; then
        printf "\033[1;32m  ✅ Бот обновлён и работает\033[0m\n"
    else
        printf "\033[1;32m  ✅ Бот запущен\033[0m\n"
    fi
    printf "\033[1;32m═══════════════════════════════════════════\033[0m\n\n"
    echo "  Откройте своего бота в Telegram и нажмите /start —"
    echo "  бот сам напишет, что ещё задать в /panel (реквизиты карты)."
    echo ""
    echo "  Управление — команда stars-bot:"
    echo ""
    echo "    sudo stars-bot update     обновить и перезапустить"
    echo "    sudo stars-bot logs       смотреть логи живьём"
    echo "    sudo stars-bot status     работает ли"
    echo "    sudo stars-bot restart    перезапустить"
    echo "    sudo stars-bot setup      изменить настройки"
    echo "    sudo stars-bot backup     копия базы"
    echo ""
    echo "  Цены, реквизиты и рассылка — прямо в боте: /panel"
    echo ""
else
    printf "\n\033[1;31m❌ Бот не запустился. Последние строки лога:\033[0m\n\n"
    $SUDO journalctl -u "$SERVICE" -n 25 --no-pager | sed 's/^/    /'
    echo ""
    echo "  Чаще всего причина — неверный токен или ID админа."
    echo "  Исправить: sudo stars-bot activate"
    echo ""
    exit 1
fi
