"""Каждый шаблон в texts.py должен форматироваться теми полями,
которыми его реально вызывают в коде."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from string import Formatter

import env_fixture  # noqa: F401  — фиксирует настройки до импорта app

from app import texts

FAIL = []


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


# Какие поля код передаёт в каждый шаблон — вытаскиваем из исходников.
def call_sites() -> dict[str, set[str]]:
    calls: dict[str, set[str]] = {}
    pattern = re.compile(r"texts\.([A-Z_]+)\.format\((.*?)\)\s*$", re.S | re.M)
    for path in list(Path("app").rglob("*.py")):
        source = path.read_text()
        # Ищем texts.NAME.format(...) с балансировкой скобок.
        for match in re.finditer(r"texts\.([A-Z_]+)\.format\(", source):
            name, start = match.group(1), match.end()
            depth, index = 1, start
            while index < len(source) and depth:
                depth += (source[index] == "(") - (source[index] == ")")
                index += 1
            args = source[start:index - 1]
            keys = set(re.findall(r"(\w+)\s*=", args))
            calls.setdefault(name, set()).update(keys)
    return calls


calls = call_sites()
print(f"Найдено {len(calls)} шаблонов с вызовами .format()\n")

for name, passed in sorted(calls.items()):
    template = getattr(texts, name, None)
    if template is None:
        FAIL.append(f"{name}: нет такого шаблона")
        print(f"❌ {name}: шаблона не существует")
        continue

    needed = placeholders(template)
    missing = needed - passed
    extra = passed - needed
    if missing:
        FAIL.append(f"{name}: не хватает {missing}")
        print(f"❌ {name}: код не передаёт {sorted(missing)}")
        continue
    try:
        template.format(**{key: "X" for key in needed})
    except (KeyError, IndexError, ValueError) as exc:
        FAIL.append(f"{name}: {exc}")
        print(f"❌ {name}: {exc}")
        continue
    note = f" (лишние: {sorted(extra)})" if extra else ""
    print(f"✅ {name}{note}")

# Шаблоны без плейсхолдеров тоже не должны содержать битых скобок
for name in dir(texts):
    value = getattr(texts, name)
    if name.isupper() and isinstance(value, str) and not placeholders(value):
        if "{" in value or "}" in value:
            FAIL.append(f"{name}: осталась фигурная скобка")
            print(f"❌ {name}: подозрительная фигурная скобка")

# Инструкция для оплаты из России. Человек делает перевод в чужом
# приложении, и каждое пропущенное слово — это письмо в поддержку:
# «а где найти», «а какую сумму». Поэтому проверяем, что ключевые
# ориентиры на месте и названы так же, как в самом Сбербанке.
MUST = {
    "DEPOSIT_RU_HOW": ["Душанбе Сити", "за рубеж", "{number}",
                       "Зачислено получателю", "в рублях"],
    # «Зачислено получателю» — надпись из самого чека, её не переводим:
    # человек ищет её глазами. «Тангаҳо» — наше пояснение, оно на языке
    # клиента.
    "DEPOSIT_RU_ASK": ["Зачислено получателю", "TJS", "тангаҳо"],
}
for name, words in MUST.items():
    template = getattr(texts, name, "")
    lost = [word for word in words if word not in template]
    if lost:
        FAIL.append(f"{name}: пропали ориентиры {lost}")
        print(f"❌ {name}: в инструкции нет {lost}")
    else:
        print(f"✅ {name}: все ориентиры на месте")

print(f"\n{'=' * 46}")
if FAIL:
    print("ПРОВАЛЫ:")
    for item in FAIL:
        print(" •", item)
    sys.exit(1)
print("Все шаблоны текстов согласованы с кодом")
