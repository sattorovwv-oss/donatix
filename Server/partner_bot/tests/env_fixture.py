"""Фиксированное окружение для тестов.

Импортируется ПЕРВЫМ, до любого модуля из app: переменные окружения имеют
приоритет над .env, поэтому тесты не зависят от реальных настроек бота.
Без этого правка цены в .env ломала бы проверки.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_ENV = {
    "BOT_TOKEN": "123456:TEST",
    "ADMIN_IDS": "111,222",
    "CURRENCY": "с.",
    "STAR_PRICE_DIRAM": "20",      # 0.20 сомони за звезду
    "MIN_STARS": "50",
    "MAX_STARS": "10000",
    "MIN_DEPOSIT_DIRAM": "1000",   # 10.00 сомони
    "REFERRAL_PERCENT": "5",
    "PAY_CARD_NUMBER": "0000 1111 2222 3333",
    "PAY_CARD_HOLDER": "TEST HOLDER",
    "PAY_CARD_BANK": "Тестбанк",
    "PAY_CITY": "Душанбе",
    "FRAGMENT_MODE": "mock",
    "SUPPORT_USERNAME": "support",
    "DB_PATH": "data/test.sqlite3",
    "LOG_LEVEL": "WARNING",
}

for key, value in TEST_ENV.items():
    os.environ[key] = value
