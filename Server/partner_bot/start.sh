#!/usr/bin/env bash
# Запуск бота на Linux/macOS. Первый раз сам создаст окружение и спросит настройки.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Создаю окружение…"
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] || .venv/bin/python setup.py

.venv/bin/python -m app.main
