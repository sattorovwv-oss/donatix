# FazerCards API v2 — конспект документации

Источник: страница API Reference на reseller.fazercards.com (текст прислал владелец проекта).
Это API **поставщика**, к которому подключается наш сервер. Полная схема — в их
OpenAPI (Swagger); здесь — всё, что было на странице.

## Основное

| | |
|---|---|
| Базовый URL | `https://api.fzr.cards/api/v2` |
| Ключ | `X-API-Key: KEY` или `Authorization: Bearer KEY` |
| Где взять ключ | Панель → Профиль |
| SDK | Python `pip install fazercards`, Node `npm install fazercards` (MIT; сами ставят ключ, Idempotency-Key, проверяют подпись webhook) |
| Деньги | строки с 4 знаками: `"100.0000"`, валюта USD |

### Соглашения

- Успех: `"ok": true`. Ошибка: `{"ok": false, "error": "текст", "code": "необяз_код"}`.
- Каталоги — `snake_case` (`category_id`, `price_usd`), профиль/подписка — `camelCase` (`planExpiresAt`).
- Пагинация: `/orders`, `/transactions` — `page` + `limit`; `/topups`, `/giftcards`, `/gamekeys` — курсор в `meta` (`next_cursor`, `has_more`).
- Доступ к продукту зависит от тарифа и включённых услуг, иначе **403**.
- id заказа: `ord-123` (через дефис). id транзакции: `tx` + цифры.
- **Idempotency-Key** (до 255 символов, например UUID) — на всех POST создания заказа:
  `/giftcards/order`, `/topups/order`, `/gamekeys/order`, `/steam-gifts/order`,
  `/steam-topup/order`, `/manual-services/order`. Повтор с тем же ключом возвращает
  исходный заказ без второго списания. (В примерах Telegram-покупок заголовка нет.)

### HTTP-коды

| Код | Значение |
|---|---|
| 400 | валидация / бизнес-правило |
| 401 | нет ключа или неверный |
| 403 | аккаунт заблокирован или продукт недоступен ключу |
| 404 | нет ресурса / маршрута |
| 409 | конфликт (дубликат, недопустимое состояние) |
| 429 | лимит частоты; заголовок `Retry-After` (сек) |
| 5xx | сбой у них или у провайдера — осторожные повторы |

### Лимиты частоты (sliding window, на API-ключ)

| Категория | Лимит |
|---|---|
| Чтение каталога | 120/мин |
| Создание заказа | 60/мин |
| Статус заказа (polling) | 120/мин |
| Аккаунт (`/me`, `/balance`, `/subscription`, `/transactions`) | 30/мин |
| Платежи (создание/verify) | 15/мин |
| Прочее | 120/мин |
| Логин `POST /partner/login` | 10 за 15 мин, по IP |

Рекомендации: при 429 ждать ≥ `Retry-After` + джиттер ±15%; каталог кешировать
локально на 5–15 минут, а не запрашивать перед каждым заказом.
(Пути в их таблице лимитов — `/catalog`, `/order` и т.п. — не совпадают с путями
методов; вероятно, таблица описывает категории, а не точные маршруты.)

## Аккаунт

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/me` | login, email, plan, planExpiresAt, planAutoRenew, subscriptionActive, summary{totalSpent,totalOrders}, createdAt, lastActiveAt |
| GET | `/balance` | `{"balance":"100.0000","currency":"USD"}` |
| GET | `/transactions?page&limit` | items[]: id, type (credit/debit), status, amount, balanceBefore, balanceAfter, note, createdAt; total/page/limit |
| GET | `/transactions/:id` | одна транзакция |

## Пополнение баланса (крипто)

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/payments/methods` | code, label, minAmountUsd, maxAmountUsd (пример: trc20, 10–50000) |
| POST | `/payments/create` | `{"method":"trc20","amount":100}`; method: trc20, bep20, ton, aptos, binancepay. Ответ: payment{id, network, amount, uniqueAmount, address, memo, binanceId, status, expiresAt…} |
| GET | `/payments/:paymentId` | статус (pending/completed…) |
| POST | `/payments/:paymentId/verify` | только Binance Pay: `{"binanceOrderId":"…"}`, лимит попыток |

## Подписка

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/subscription` | plan, planExpiresAt, planAutoRenew, subscriptionActive, currency |
| GET | `/subscription/plans` | цены за 30 дней |
| GET | `/subscription/activation-quote?plan=gold` | listPriceUsd, chargedAmountUsd, firstPurchase (есть promo_code) |
| POST | `/subscription/activate-from-balance` | `{"plan":"gold"}` — списание с баланса |
| PATCH | `/subscription/auto-renew` | `{"planAutoRenew":false}` |
| PATCH | `/subscription/plan` | `{"plan":"silver"}` |

Цены: в примерах API — bronze 29 / silver 49 / gold 99 USD, а на главной сайта —
$3.99 / $6.99 / $9.99 в месяц. **Реальные цены уточнять у них** (`GET /website/subscription-prices`, без ключа).

## Заказы (общие)

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/orders?page&limit` | items[]: id, kind, status, created_at |
| GET | `/orders/:orderId` | полный заказ; `payload` зависит от kind и статуса |

Созданный заказ обычно возвращается в статусе `processing` → итог узнаём через
`GET /orders/:id` или webhook. Ручные услуги стартуют со статуса `created`.
В примерах `kind` у всех заказов часто `gift_card` — вероятно, неточность документации.

## Картинки каталога

С параметром `include_ui=1` (или `true`) списки категорий и ответы с товарами получают поля
`imageurl` — путь к обложке (может быть без домена, тогда дописываем домен API или
`FAZER_IMAGE_BASE`) и `appid` — Steam AppID. Если картинки нет, а `appid` есть, берём обложку
из Steam CDN (`/steam/apps/{appid}/header.jpg`). См. `donatix/suppliers/base.py: pick_image`.

## Игровые пополнения (по ID игрока)

1. `GET /topups?limit=50` — категории: category_id, name, note (+ `include_ui` для обложек).
2. `GET /topups/offers?category_id=…` — offers[]: offer_id, name, price_usd; fields[]: key, label, type.
3. (необяз.) `GET /topups/validate-id` — игры с проверкой ID; `POST /topups/validate-id`
   `{"category_id":"pubg_mobile","fields":{"player_id":"…"}}` → valid, player_name, region.
4. `POST /topups/order` — `{"category_id","offer_id","fields":{…}}`.

### Поля и проверка аккаунта — у каждой игры и сервиса свои

У каждой игры/сервиса свой набор полей для получателя: Player ID, иногда ещё
сервер/зона, username, логин и т.п. Их список приходит в `fields` оффера — форма
заказа строится по нему (в Donatix это уже так). Для части игр (PUBG Mobile,
Free Fire, Mobile Legends…) поставщик умеет проверить аккаунт до оплаты:
`GET /topups/validate-id` — список игр с проверкой, `POST /topups/validate-id` —
вернёт `valid` и `player_name`. Нужно показывать клиенту ник игрока перед оплатой.

## Подарочные карты

1. `GET /giftcards?limit=50` — категории.
2. `GET /giftcards/cards?category_id=…` — offers[]: card_id, name, price_usd, stock, min/max_order_quantity.
3. `POST /giftcards/order` — `{"category_id","card_id","quantity"}` (1–100).

## Ключи игр

1. `GET /gamekeys?limit=200&include_ui=1` — только категории, где есть что продать (stock > 0).
   items[]: name, game_id, region, platform, region_restriction, appid, imageurl. Курсор: meta.next_cursor / has_more. limit до 500.
2. `GET /gamekeys/keys?game_id=…&include_ui=1` — GameName (с большой буквы), region, platform,
   region_restriction, appid, imageurl; keys[]: key_id (может быть null — такой не продаём), name,
   price_usd, stock, min_order_quantity, max_order_quantity.
3. `GET /gamekeys/region-restriction?game_id=…` — region_type, has_availability,
   available[] / unavailable[]: {code (ISO), name (англ.)}.
4. `POST /gamekeys/order` — `{"game_id","key_id","quantity"}`.

## Steam

Кошелёк:
- `GET /steam-topup/rates` — курсы USD/RUB/UAH/KZT; `GET /steam-topup/public-rates` — то же без ключа.
- `POST /steam-topup/check-login` `{"steamLogin":"…"}` → `can_refill`.
- `POST /steam-topup/order` `{"steamLogin","currency":"USD|RUB|UAH|KZT","amount"}`.

Подарки:
- `GET /steam-gifts/games?limit=100` — name, appid (каталог ~12 000 игр).
- `GET /steam-gifts/games/:appid` — offers[]: sub_id, name, regions[]{region, price}.
- `POST /steam-gifts/order` `{"invite_url","sub_id","app_id","region"}`.

## Telegram

- `GET /telegram/stars` — price_per_star, min_amount 50, max_amount 10000.
- `GET /telegram/premium` — plans[]: months 3/6/12, price_usd.
- `POST /telegram/stars/buy` `{"telegram_username","quantity"}`.
- `POST /telegram/premium/buy` `{"telegram_username","months"}`.

## Ручные услуги (выдаёт оператор)

- `GET /manual-services` — id, name, kind, chat, info.
- `GET /manual-services/:id/offers` — items[]: id, name, price_usd, delivery_minutes (+ fields для replenishment).
- `POST /manual-services/order` `{"manual_service_id","product_id", fields?}` →
  order_id, status `created`, deadline_at, chat_required, chat_status.
- Чат по заказу: `GET/POST /manual-services/orders/:orderId/chat` (POST — multipart).

## Публичные методы (без ключа)

- `GET /website/subscription-prices` — цены тарифов.
- Регистрация, OTP, сброс пароля, рефералы — в OpenAPI.

## Webhooks

Настраиваются в панели реселлера (не через API). Формат тела, заголовок подписи
и проверка — на отдельной странице «Руководство по вебхукам». **Её ещё нужно получить.**

## Чего пока не хватает

- Страница «Руководство по вебхукам» (формат и подпись).
- Полная OpenAPI-схема: точные статусы заказов и содержимое `payload` с кодами.
- Страницы «Партнёрство», «White Label», «Поставщикам» — условия перепродажи через API.
- Скриншоты кабинета реселлера после входа.
