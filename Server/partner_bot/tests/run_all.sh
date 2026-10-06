#!/usr/bin/env bash
# Прогон всех проверок. Запуск: bash tests/run_all.sh
set -u
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
status=0
for suite in tests/test_money.py tests/test_texts.py tests/test_wiring.py \
             tests/test_panel.py tests/test_recipient.py \
             tests/test_apifrag.py tests/test_mystars.py tests/test_fazer.py \
             tests/test_wallet.py tests/test_design.py \
             tests/test_reports.py tests/test_dcpay.py \
             tests/test_clients.py tests/test_escape.py tests/test_top.py \
             tests/test_access.py \
             tests/test_links.py tests/test_promo.py \
             tests/test_packs.py tests/test_rates.py \
             tests/test_rounding.py tests/test_reviews.py \
             tests/test_steam.py tests/test_partners.py \
             tests/test_games.py tests/test_webhook.py \
             tests/test_api.py tests/test_userbot.py tests/test_deposit_resume.py \
             tests/test_sponsor.py tests/test_stale.py tests/test_txn.py tests/test_rate.py \
             tests/test_activate.py tests/test_slow_games.py \
             tests/test_flow.py; do
    echo ""
    echo "═══ $suite ═══"
    "$PY" "$suite" || status=1
done
echo ""
[ $status -eq 0 ] && echo "✅ Все проверки пройдены" || echo "❌ Есть провалы"
exit $status
