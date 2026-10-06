# Donatix

Оптовая площадка цифровых товаров для игровых проектов. Работает поверх
поставщика **FazerCards**: клиент заказывает у Donatix, Donatix в ту же секунду
заказывает у FazerCards, отдаёт результат клиенту и оставляет себе наценку.

```
Сайт / Telegram-бот клиента ──(ключ клиента)──▶ Donatix ──(ваш ключ)──▶ FazerCards
                                              баланс, наценка,
                                              заказы, возвраты
```

## Что уже есть

- **Сайт:** главная, регистрация с одобрением, вход, документация API (`/docs`).
- **Кабинет клиента:** обзор, каталог с ценами клиента, покупка в панели,
  заказы, транзакции, API-ключи, webhook.
- **API для клиентов** `/api/v1`: баланс, каталог, заказы, транзакции.
  Формат ответов как у FazerCards, чтобы клиентам было легко переехать.
  Swagger: `/api/swagger`.
- **Админка** `/admin`:
  - сводка: баланс у поставщика, выручка и прибыль;
  - клиенты: одобрение, уровень, своя наценка, пополнение баланса;
  - заказы: ручное выполнение или возврат;
  - каталог: скрыть или показать товар.
- **Товары:** Telegram Stars, Telegram Premium, пополнение Steam по логину, Steam-гифты, пополнения игр по ID, подарочные карты.
- **Фоновый обработчик:** обновляет каталог раз в 15 минут и доводит заказы до
  конца. Если баланс у FazerCards кончается, пишет вам в Telegram.

## Как не теряются деньги

1. Деньги списываются у клиента и заказ создаётся в одной транзакции базы.
2. Заказ уходит в FazerCards с уникальным `Idempotency-Key`.
3. Поставщик **точно отказал** → деньги сразу возвращаются клиенту.
4. **Непонятно**, прошёл ли заказ (таймаут, сеть):
   - пополнения игр и подарочные карты: запрос повторяется с тем же ключом,
     и FazerCards не создаст второй заказ;
   - Telegram Stars/Premium: в документации FazerCards `Idempotency-Key` для них
     не указан, поэтому такой заказ **не повторяется**. Он попадает в админку
     со статусом «Требует внимания». Вы проверяете его в панели FazerCards и
     жмёте «Отметить выполненным» или «Отменить и вернуть деньги».
5. Возврат делается ровно один раз, даже если нажать дважды.
6. Клиент тоже передаёт свой `Idempotency-Key`, и повтор его запроса не спишет
   деньги второй раз. Двойное нажатие кнопки «Оплатить» в панели тоже защищено.

## Установка на сервер — одна команда

На чистом сервере Ubuntu 22.04/24.04, у которого домен уже указывает на его IP:

```bash
curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/install.sh | sudo bash
```

Скрипт спросит домен, почту и пароль админа, ключ FazerCards, всё установит,
проверит ключ, получит HTTPS и включит ежедневные копии базы. Реквизиты для
пополнения (Алиф, DC, Эсхата, USDT, Binance) потом вписываются в
`/home/donatix/app/donatix/.env`, затем `sudo systemctl restart donatix`.

Посмотреть цены поставщика и свои (с наценкой 8%):

```bash
sudo -u donatix bash -c 'cd ~/app && .venv/bin/python -m donatix prices'
```

## Пополнение баланса клиентами

Клиент: Кабинет → Пополнить баланс → способ → сумма → переводит по реквизитам →
«Я оплатил». Вам приходит сообщение в Telegram, заявка видна в Админке →
Пополнения. Проверили поступление → «Подтвердить»: баланс зачисляется, клиенту
приходит уведомление (в кабинете и на почту, если настроен SMTP).

Полностью автоматическое зачисление подключается, когда банк выдаст API для
бизнеса (у Алифа, DC и др. это отдельный договор с мерчант-доступом).

## Запуск у себя (проверить)

```bash
cd AlijonMahmadjonov
python3 -m venv .venv && source .venv/bin/activate
pip install -r donatix/requirements.txt
cp donatix/.env.example donatix/.env        # заполните: DONATIX_SUPPLIER=mock для пробы
python -m donatix serve                      # http://127.0.0.1:8000
```

С `DONATIX_SUPPLIER=mock` работает демо-каталог, а заказы выполняются понарошку.
Так удобно всё проверить без денег. Для работы по-настоящему поставьте
`DONATIX_SUPPLIER=fazer` и `FAZER_API_KEY=` ключ из панели FazerCards.

```bash
python -m donatix check      # ключ FazerCards работает? какой баланс?
python -m donatix sync       # загрузить каталог прямо сейчас
python -m donatix create-admin you@example.com
```

## Сервер

Хватит одного VPS: 2 ядра, 4 ГБ, Ubuntu 24.04. Лучше в Европе (например,
Hetzner, Германия), ближе к FazerCards.

```bash
sudo apt install -y python3-venv nginx certbot python3-certbot-nginx
sudo useradd -m donatix && sudo -iu donatix
git clone <репозиторий> app && cd app
python3 -m venv .venv && .venv/bin/pip install -r donatix/requirements.txt
cp donatix/.env.example donatix/.env && nano donatix/.env
```

`/etc/systemd/system/donatix.service`:

```ini
[Unit]
Description=Donatix
After=network-online.target

[Service]
User=donatix
WorkingDirectory=/home/donatix/app
ExecStart=/home/donatix/app/.venv/bin/python -m donatix serve --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Nginx (`/etc/nginx/sites-available/donatix`):

```nginx
server {
    server_name donatix.gg;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/donatix /etc/nginx/sites-enabled/
sudo certbot --nginx -d donatix.gg
sudo systemctl enable --now donatix
```

**Запускайте один процесс** (без `--workers`): фоновый обработчик и ограничение
частоты запросов живут внутри процесса. Для вашего объёма этого с запасом хватит.

**Резервные копии.** Каждый день копируйте `donatix/data/donatix.db` на другой
сервер: там балансы клиентов. Пример строки для cron:

```bash
sqlite3 /home/donatix/app/donatix/data/donatix.db ".backup /home/donatix/backup/donatix-$(date +\%F).db"
```

## Как работать каждый день

1. Клиент регистрируется, и вы видите заявку в админке. Проверяете и ставите «Активен».
2. Клиент присылает деньги (перевод или USDT), вы зачисляете: админка →
   Клиенты → клиент → «Баланс» → сумма и комментарий.
3. Следите, чтобы **баланс у FazerCards был больше суммы балансов клиентов**.
   Оба числа есть на сводке админки.
4. Раз в день загляните в «Требуют внимания».

## Тесты

```bash
pip install pytest
python -m pytest donatix/tests -q
```

## Что дальше

- Ключи игр и ручные услуги: API FazerCards это уже умеет.
- Автоматическое пополнение баланса клиентами (USDT или местные платёжки).
- Смена пароля и восстановление по почте.
- Telegram-бот для клиентов поверх этого же API.
- Переход на PostgreSQL, когда заказов станет очень много.

Документация поставщика: [`docs/fazercards/`](../docs/fazercards/).

## Telegram-бот админки

1. Создайте бота у @BotFather и скопируйте токен.
2. Откройте своего бота и нажмите «Старт» (/start).
3. На сервере:

```
curl -fsSL https://raw.githubusercontent.com/alijon26062006-bit/AlijonMahmadjonov/claude/website-api-sales-96wxcs/donatix/deploy/telegram.sh | sudo TG_TOKEN='ТОКЕН_ОТ_BOTFATHER' bash
```

Скрипт сам найдёт ваш chat id, обновит код, перезапустит сайт и пришлёт проверочное сообщение.
Бот присылает заявки на пополнение, новых партнёров и проблемные заказы с кнопками;
команды: /stats, /payments, /users, /orders. Отвечает только вам.
