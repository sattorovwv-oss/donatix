#!/usr/bin/env bash
# Установка бота одной командой: ставит всё нужное, спрашивает ключи, запускает.
#
#   bash setup.sh
#
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

BOLD=$'\033[1m'; DIM=$'\033[90m'; GREEN=$'\033[32m'; RED=$'\033[31m'; OFF=$'\033[0m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$*" >&2; }
hint() { printf '  %s%s%s\n' "$DIM" "$*" "$OFF"; }
step() { printf '\n%s[%s/4] %s%s\n' "$BOLD" "$1" "$2" "$OFF"; }

die() { bad "$*"; exit 1; }

# sudo нужен только если мы не root
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

say ""
say "${BOLD}Установка телеграм-бота${OFF}"
hint "Поставлю всё нужное и спрошу ключи. Это займёт пару минут."

# ── 1. Системные пакеты ────────────────────────────────────────────────────
step 1 "Системные пакеты"

command -v python3 >/dev/null 2>&1 || die "Не найден python3. Установи его: apt install python3"

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Нужен Python 3.10 или новее, а стоит $PY_VERSION."
ok "Python $PY_VERSION"

NEEDED=()
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || NEEDED+=("python3-venv")
# Шрифт с кириллицей — без него в PDF будут чёрные квадраты вместо букв.
ls /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf >/dev/null 2>&1 \
  || ls /usr/share/fonts/dejavu/DejaVuSans.ttf >/dev/null 2>&1 \
  || NEEDED+=("fonts-dejavu-core")

if [ ${#NEEDED[@]} -gt 0 ]; then
  hint "Доставляю: ${NEEDED[*]}"
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq "${NEEDED[@]}" >/dev/null
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q python3-venv dejavu-sans-fonts >/dev/null || true
  else
    die "Не знаю твой пакетный менеджер. Поставь вручную: ${NEEDED[*]}"
  fi
fi
ok "Пакеты на месте"

# ── 2. Виртуальное окружение ───────────────────────────────────────────────
step 2 "Виртуальное окружение"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || die "Не удалось создать .venv. Поставь пакет python3-venv."
  ok "Создано .venv"
else
  ok "Уже есть .venv"
fi

# ── 3. Зависимости ─────────────────────────────────────────────────────────
step 3 "Зависимости"
hint "Качаю библиотеки, это самая долгая часть…"

.venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
.venv/bin/python -m pip install --quiet -r requirements.txt \
  || die "Не удалось поставить зависимости. Проверь интернет и попробуй ещё раз."

.venv/bin/python -c 'import bot.main' >/dev/null 2>&1 \
  || die "Код бота не импортируется — что-то не так с установкой."
ok "Всё поставлено и проверено"

# ── 4. Ключи и запуск ──────────────────────────────────────────────────────
step 4 "Ключи и запуск"

exec .venv/bin/python -m bot.setup
