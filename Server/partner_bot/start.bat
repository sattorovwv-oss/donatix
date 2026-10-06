@echo off
REM Запуск бота на Windows. Первый раз сам создаст окружение и спросит настройки.
cd /d "%~dp0"

if not exist .venv (
    echo Sozdayu okruzhenie...
    python -m venv .venv
    .venv\Scripts\python -m pip install -q --upgrade pip
    .venv\Scripts\pip install -q -r requirements.txt
)

if not exist .env .venv\Scripts\python setup.py

.venv\Scripts\python -m app.main
pause
