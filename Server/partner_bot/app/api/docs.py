"""Страница документации API.

Отдаётся тем же сервером, что и сам API: разработчик открывает адрес,
который ему дали, и сразу видит, что делать. Отдельного сайта для этого
не нужно, а документация, лежащая не там же, где API, устаревает первой.

Страница собирается строкой — без шаблонизатора и без внешних файлов:
лишняя зависимость ради одной страницы не стоит того, а картинок и
шрифтов со стороны здесь нет вовсе.
"""
from __future__ import annotations

from html import escape

from aiohttp import web

from app import runtime

#: Что показываем в примерах, пока владелец не задал публичный адрес.
FALLBACK = "https://ваш-адрес"


def base() -> str:
    from app.api import server

    return server.base_url() or (FALLBACK + server.PREFIX)


ENDPOINTS = [
    ("GET", "/products", "Список товаров",
     "?type=stars|premium|steam|game, ?game=ID игры, "
     "?limit=500&offset=0 — страницами, total в ответе"),
    ("GET", "/products/{id}", "Один товар", "id — из списка товаров"),
    ("GET", "/games", "Список игр без пакетов",
     "Названия, поля и число пакетов — по нему удобно листать каталог"),
    ("GET", "/balance", "Остаток на балансе", ""),
    ("GET", "/user", "Аккаунт, ключ, лимиты, вебхук", ""),
    ("GET", "/orders", "Список заказов",
     "?period=today|yesterday|7d|30d|all или ?from=&to=, ?limit=&offset=, "
     "?tz=смещение в минутах"),
    ("GET", "/orders/summary", "Сводка за период",
     "Сколько заказов, выполнено, возвратов, потрачено"),
    ("GET", "/transactions", "Движение денег",
     "Пополнения, списания, возвраты. Те же period, from, to"),
    ("GET", "/orders/{order_id}", "Один заказ", "order_id — ORD-000123"),
    ("GET", "/check-id", "Ник игрока по ID",
     "?product_id=…&customer=… — до оплаты, только для игр"),
    ("POST", "/order/create", "Создать заказ", "product_id, quantity, customer"),
    ("POST", "/order/refund", "Попросить возврат",
     "order_id, reason. Решает владелец, деньги сразу не возвращаются"),
    ("GET", "/order/status", "Статус заказа", "?order_id=ORD-000123"),
]

ERRORS = [
    ("400", "bad_json, bad_quantity, missing_customer", "Запрос не разобрать."),
    ("401", "invalid_key", "Ключ не принят."),
    ("402", "insufficient_funds", "Не хватает денег на балансе."),
    ("403", "key_disabled, account_blocked", "Ключ выключен или доступ закрыт."),
    ("404", "product_not_found, order_not_found", "Нет такого товара или заказа."),
    ("409", "in_progress", "Тот же idempotency_key ещё выполняется."),
    ("429", "rate_limited, too_many_attempts",
     "Слишком часто. В заголовке Retry-After — через сколько секунд повторить."),
    ("503", "api_disabled", "API временно выключен владельцем."),
    ("400", "bad_period", "period или from/to заданы не так."),
    ("400", "not_checkable", "У этого товара нет проверки ID."),
    ("400", "missing_reason", "В просьбе о возврате нет причины."),
    ("403", "read_only", "Вход ссылкой на кабинет — ей можно только смотреть."),
    ("409", "already_refunded", "Деньги за заказ уже вернулись."),
    ("409", "still_processing", "Заказ ещё в работе — заявка не нужна."),
]

STATUSES = [
    ("pending", "Заказ принят, ещё не в работе."),
    ("processing", "Отправлен поставщику, идёт выдача."),
    ("completed", "Выполнен."),
    ("failed", "Не выполнен, деньги не списаны или возвращены."),
    ("cancelled", "Отменён."),
    ("refunded", "Деньги вернулись на баланс."),
]

CSS = """
:root{--bg:#fff;--fg:#11161d;--muted:#5b6673;--line:#e4e8ee;--card:#f7f9fb;
--accent:#1f6feb;--code:#0d1117;--codefg:#e6edf3;--get:#1a7f37;--post:#9a3412}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;
--muted:#9198a1;--line:#242b33;--card:#161b22;--accent:#58a6ff;
--code:#010409;--codefg:#e6edf3;--get:#3fb950;--post:#f0883e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.4px}
h2{font-size:21px;margin:44px 0 12px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:16px;margin:26px 0 8px}
p,li{color:var(--fg)}
.lead{color:var(--muted);margin:0 0 28px;font-size:17px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13.5px}
p code,li code,td code{background:var(--card);border:1px solid var(--line);
border-radius:5px;padding:1px 5px}
pre{background:var(--code);color:var(--codefg);padding:16px 18px;border-radius:10px;
overflow-x:auto;font-size:13.5px;line-height:1.55}
pre code{background:none;border:0;padding:0;color:inherit}
table{width:100%;border-collapse:collapse;margin:14px 0;font-size:14.5px;
display:block;overflow-x:auto}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px;text-transform:uppercase;
letter-spacing:.4px}
.m{font-family:ui-monospace,monospace;font-weight:700;font-size:12.5px}
.get{color:var(--get)}.post{color:var(--post)}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:0 8px 8px 0;padding:13px 16px;margin:18px 0;font-size:15px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin:16px 0 0}
.tabs button{font:600 13px/1 inherit;padding:8px 14px;border:1px solid var(--line);
background:var(--card);color:var(--muted);border-radius:8px 8px 0 0;cursor:pointer}
.tabs button[aria-selected=true]{background:var(--code);color:var(--codefg);
border-color:var(--code)}
.tabs+pre{border-radius:0 10px 10px 10px;margin-top:0}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--line);
color:var(--muted);font-size:14px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 6px}
.row input{flex:1 1 260px;min-width:0;font:14px/1 ui-monospace,monospace;
padding:11px 13px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--fg)}
.row button{font:600 14px/1 inherit;padding:11px 20px;border:0;border-radius:9px;
background:var(--accent);color:#fff;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}
.chips button{font:500 13px/1.3 inherit;padding:7px 12px;border:1px solid var(--line);
background:var(--card);color:var(--fg);border-radius:20px;cursor:pointer;
text-align:left}
.chips button[aria-pressed=true]{background:var(--accent);color:#fff;
border-color:var(--accent)}
.chips button span{color:var(--muted);font-size:12px}
.chips button[aria-pressed=true] span{color:#dbe9ff}
.hint{color:var(--muted);font-size:14px;margin:6px 0}
.hint.bad{color:var(--post)}
.pick{max-height:320px;overflow-y:auto;padding-right:4px}
"""


JS = """
document.querySelectorAll('.sample').forEach(function(box){
  var tabs = box.querySelectorAll('.tabs button');
  var panes = box.querySelectorAll('pre');
  tabs.forEach(function(tab, i){
    tab.addEventListener('click', function(){
      tabs.forEach(function(t,j){ t.setAttribute('aria-selected', j===i); });
      panes.forEach(function(p,j){ p.hidden = j!==i; });
    });
  });
});

// ── обозреватель каталога ───────────────────────────────────────────────
// Ключ живёт только в этой переменной: ни в localStorage, ни в адресе
// строки — закрыл вкладку, и его нет.
(function(){
  var box = document.getElementById('browser');
  if (!box) return;

  var api   = location.pathname.replace(/\/docs\/?$/, '');
  var field = document.getElementById('apikey');
  var msg   = document.getElementById('msg');
  var chips = document.getElementById('chips');
  var items = document.getElementById('items');
  var key   = '';

  function say(text, bad){
    msg.textContent = text || '';
    msg.className = bad ? 'hint bad' : 'hint';
  }

  function ask(path){
    return fetch(api + path, {headers: {Authorization: 'Bearer ' + key}})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.success) throw new Error((d.error && d.error.message) || 'Ошибка');
        return d;
      });
  }

  function el(tag, text, cls){
    var node = document.createElement(tag);
    // Только textContent: названия приходят от поставщика, и кавычка
    // внутри такого названия не должна превращаться в разметку.
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function chip(label, note, load){
    var b = el('button', label);
    b.setAttribute('aria-pressed', 'false');
    if (note) { b.appendChild(document.createTextNode(' ')); b.appendChild(el('span', note)); }
    b.addEventListener('click', function(){
      chips.querySelectorAll('button').forEach(function(o){
        o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
      });
      items.textContent = '';
      say('Загружаю…');
      load().then(function(list){
        say(list.length ? '' : 'У этой игры сейчас нет пакетов.');
        items.appendChild(table(list));
      }).catch(function(e){ say(e.message, true); });
    });
    return b;
  }

  function price(p){
    if (p.amount_text) return p.amount_text + ' ' + p.currency;
    if (p.unit_price)  return (p.unit_price / 10000).toFixed(4) + ' за штуку';
    return '—';
  }

  function table(list){
    var t = el('table');
    var head = el('thead'), hr = el('tr');
    ['product_id', 'Название', 'Цена', 'Что спросить у клиента']
      .forEach(function(h){ hr.appendChild(el('th', h)); });
    head.appendChild(hr); t.appendChild(head);

    var body = el('tbody');
    list.forEach(function(p){
      var tr = el('tr');
      var id = el('td'); id.appendChild(el('code', p.id)); tr.appendChild(id);
      tr.appendChild(el('td', p.name));
      tr.appendChild(el('td', price(p)));
      tr.appendChild(el('td', p.customer || '—'));
      body.appendChild(tr);
    });
    t.appendChild(body);
    return t;
  }

  function start(){
    key = (field.value || '').trim();
    chips.textContent = '';
    items.textContent = '';
    if (!key) { say('Вставьте ключ.', true); return; }
    say('Загружаю…');

    ask('/products?limit=1000').then(function(d){
      var groups = [
        ['Telegram Stars', 'stars'],
        ['Telegram Premium', 'premium'],
        ['Steam', 'steam'],
      ];
      groups.forEach(function(g){
        var found = d.products.filter(function(p){ return p.type === g[1]; });
        if (!found.length) return;
        chips.appendChild(chip(g[0], found.length + ' шт.', function(){
          return Promise.resolve(found);
        }));
      });
      return ask('/games');
    }).then(function(d){
      var games = d.games || [];
      games.forEach(function(g){
        chips.appendChild(chip(g.name, g.packs + ' пак.', function(){
          return ask('/products?game=' + encodeURIComponent(g.id) + '&limit=1000')
            .then(function(r){ return r.products; });
        }));
      });
      chips.className = games.length > 30 ? 'chips pick' : 'chips';
      say(games.length
          ? 'Игр: ' + games.length + '. Нажмите на любую.'
          : 'Игр сейчас нет — выберите раздел выше.');
    }).catch(function(e){ say(e.message, true); });
  }

  document.getElementById('show').addEventListener('click', start);
  field.addEventListener('keydown', function(e){ if (e.key === 'Enter') start(); });
})();
"""


def _samples(url: str) -> str:
    """Один и тот же запрос на четырёх языках — чтобы просто скопировать."""
    curl = f"""curl -X POST {url}/order/create \\\\
  -H "Authorization: Bearer ВАШ_КЛЮЧ" \\\\
  -H "Content-Type: application/json" \\\\
  -H "Idempotency-Key: my-order-1001" \\\\
  -d '{{"product_id":"stars","quantity":100,"customer":"@durov"}}'"""

    php = f"""<?php
$ch = curl_init('{url}/order/create');
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER     => [
        'Authorization: Bearer ' . getenv('SHOP_API_KEY'),
        'Content-Type: application/json',
        'Idempotency-Key: my-order-1001',
    ],
    CURLOPT_POSTFIELDS => json_encode([
        'product_id' => 'stars',
        'quantity'   => 100,
        'customer'   => '@durov',
    ]),
]);
$answer = json_decode(curl_exec($ch), true);
curl_close($ch);

if (empty($answer['success'])) {{
    throw new RuntimeException($answer['error']['message'] ?? 'unknown');
}}
echo $answer['order_id'];   // ORD-000123"""

    python = f"""import os, requests

answer = requests.post(
    "{url}/order/create",
    headers={{
        "Authorization": f"Bearer {{os.environ['SHOP_API_KEY']}}",
        "Idempotency-Key": "my-order-1001",
    }},
    json={{"product_id": "stars", "quantity": 100, "customer": "@durov"}},
    timeout=30,
).json()

if not answer["success"]:
    raise RuntimeError(answer["error"]["message"])
print(answer["order_id"])   # ORD-000123"""

    js = f"""const answer = await fetch("{url}/order/create", {{
  method: "POST",
  headers: {{
    "Authorization": `Bearer ${{process.env.SHOP_API_KEY}}`,
    "Content-Type": "application/json",
    "Idempotency-Key": "my-order-1001",
  }},
  body: JSON.stringify({{
    product_id: "stars",
    quantity: 100,
    customer: "@durov",
  }}),
}}).then((r) => r.json());

if (!answer.success) throw new Error(answer.error.message);
console.log(answer.order_id);   // ORD-000123"""

    names = ["cURL", "PHP", "Python", "JavaScript"]
    blocks = [curl, php, python, js]
    tabs = "".join(
        f'<button aria-selected="{"true" if i == 0 else "false"}">{name}</button>'
        for i, name in enumerate(names)
    )
    panes = "".join(
        f'<pre{"" if i == 0 else " hidden"}><code>{escape(code)}</code></pre>'
        for i, code in enumerate(blocks)
    )
    return f'<div class="sample"><div class="tabs">{tabs}</div>{panes}</div>'


def _hook_sample() -> str:
    check = """# Python — проверка подписи вебхука
import hmac, hashlib, time

def genuine(body: bytes, signature: str, stamp: str, secret: str) -> bool:
    # Старое событие не принимаем: без этого перехваченный однажды
    # запрос можно повторять сколько угодно, и подпись сойдётся.
    if abs(time.time() - int(stamp)) > 300:
        return False
    signed = stamp.encode() + b"." + body
    mine = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, signature)  # сравнение постоянное по времени

# genuine(request.body,
#         request.headers["X-Signature"],
#         request.headers["X-Timestamp"],
#         "whsec_…")"""
    return f"<pre><code>{escape(check)}</code></pre>"


def html() -> str:
    url = base()
    rows = "".join(
        f'<tr><td class="m {method.lower()}">{method}</td>'
        f"<td><code>{escape(path)}</code></td><td>{escape(what)}</td>"
        f"<td>{escape(note)}</td></tr>"
        for method, path, what, note in ENDPOINTS
    )
    errors = "".join(
        f"<tr><td class='m'>{code}</td><td><code>{escape(names)}</code></td>"
        f"<td>{escape(what)}</td></tr>"
        for code, names, what in ERRORS
    )
    statuses = "".join(
        f"<tr><td><code>{name}</code></td><td>{escape(what)}</td></tr>"
        for name, what in STATUSES
    )
    limit = runtime.get_int("api_rate_per_min", 60)

    order_answer = """{
  "success": true,
  "order_id": "ORD-000123",
  "status": "processing",
  "product_id": "stars",
  "quantity": 100,
  "amount": 1931,
  "amount_text": "19.31",
  "currency": "TJS",
  "customer": "@durov",
  "transaction_id": "TX-9F2A71B4C8D0",
  "created_at": "2026-09-15T10:04:11+00:00"
}"""
    status_answer = """{
  "success": true,
  "order_id": "ORD-000123",
  "status": "completed",
  "result": "@durov",
  "amount": 1931,
  "amount_text": "19.31",
  "currency": "TJS"
}"""
    error_answer = """{
  "success": false,
  "error": {
    "code": "insufficient_funds",
    "message": "Недостаточно средств.",
    "required": 1931,
    "balance": 400,
    "currency": "TJS"
  }
}"""
    hook_body = """{
  "event": "order.completed",
  "order_id": "ORD-000123",
  "status": "completed",
  "product_id": "stars",
  "quantity": 100,
  "amount": 1931,
  "customer": "@durov",
  "result": "@durov"
}"""

    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>API — документация</title>
<style>{CSS}</style>
</head><body><div class="wrap">

<h1>API</h1>
<p class="lead">Покупка звёзд Telegram, Premium, Steam и игровых пакетов
из вашего кода — сайта, бота или приложения.</p>

<div class="note"><b>Базовый адрес:</b> <code>{escape(url)}</code><br>
Все запросы — по HTTPS. Ответ всегда JSON с полем <code>success</code>.</div>

<div class="note"><b>Личный кабинет:</b> <a href="cabinet">{escape(url)}/cabinet</a><br>
Вставьте там ключ — и увидите заказы за любой период, движение денег,
каталог и состояние вебхука. Без регистрации.</div>

<h2>Как начать</h2>
<ol>
<li>Напишите владельцу боту <code>/api</code> и создайте ключ.</li>
<li>Скопируйте ключ — он показывается <b>один раз</b>.</li>
<li>Пополните баланс — с него списываются покупки.</li>
<li>Передавайте ключ в каждом запросе:
<code>Authorization: Bearer ВАШ_КЛЮЧ</code></li>
</ol>

<div class="note">Ключ — это доступ к вашим деньгам. Держите его в
переменной окружения, не кладите в git и не показывайте в браузере:
запрос из JavaScript на странице отдаёт ключ каждому посетителю.
Вызывайте API со своего сервера.</div>

<h2>Каталог живьём</h2>
<p>Вставьте свой ключ — ниже появятся все игры и разделы. Нажмите на
любой, и увидите его товары: <code>product_id</code> для запроса,
название, цену и что спросить у клиента.</p>

<div id="browser">
<div class="row">
<input id="apikey" type="password" autocomplete="off" spellcheck="false"
 placeholder="sk_live_…">
<button id="show">Показать</button>
</div>
<p id="msg" class="hint"></p>
<div id="chips" class="chips"></div>
<div id="items"></div>
</div>

<div class="note">Ключ остаётся в этой вкладке: он не сохраняется, не
попадает в адресную строку и уходит только на этот же адрес API.
Закрыли страницу — его больше нет.</div>

<h2>Точки</h2>
<table><thead><tr><th>Метод</th><th>Путь</th><th>Что делает</th>
<th>Параметры</th></tr></thead><tbody>{rows}</tbody></table>

<h2>Как встроить в свой проект</h2>
<p>Весь путь — пять шагов. Ни базы товаров у себя, ни склада, ни
согласований: цены и наличие живут у нас и меняются сами.</p>

<h3>1. Возьмите список игр</h3>
<pre><code>GET {escape(url)}/games</code></pre>
<p>Вернётся <code>id</code> игры, её название, сколько у неё пакетов и
какие поля спросить у клиента (<code>user_id</code>, у части игр ещё и
<code>server_id</code>).</p>

<h3>2. Возьмите товары этой игры</h3>
<pre><code>GET {escape(url)}/products?game=free_fire_cis</code></pre>
<p>У каждого товара есть <code>id</code> — это и есть
<code>product_id</code> для заказа. <b>Сохраняйте у себя только его.</b>
Цену берите из ответа каждый раз: она меняется вместе с курсом, и
запомненная у вас цена разойдётся с той, что спишется.</p>
<p>Для звёзд цена другая по устройству: <code>unit_price</code> — цена
одной звезды в десятитысячных сомони. 1000 звёзд стоят
<code>unit_price × 1000 ÷ 10000</code> сомони.</p>
<p>Рядом с каждой ценой лежит <code>usd</code> — она же в долларах, по
нашему курсу. Сам курс приходит в ответе полем <code>usd_rate</code>.
Это для сравнения: если вам называют цены в долларах, пересчитывать
ничего не нужно.</p>

<h3>3. Покажите клиенту, кому он платит</h3>
<pre><code>GET {escape(url)}/check-id?product_id=game:free_fire_cis:110_diamonds&amp;customer=1724367212</code></pre>
<p>Ответ: <code>nickname</code> и <code>verdict</code> —
<code>ok</code> (ник нашли), <code>bad</code> (такого игрока нет, продавать
не стоит), <code>unknown</code> (не смогли проверить — продавать можно,
пополнение идёт по ID).</p>
<div class="note">Проверка расходует чужие справочники и ограничена жёстче
обычных запросов. Не зовите её в цикле и не проверяйте один и тот же ID
повторно — ответ и так помнится полчаса.</div>

<h3>4. Сделайте заказ</h3>
<p>Тот же <code>product_id</code>, а в <code>customer</code> — то, что
ввёл клиент. Для игры с двумя полями числа пишутся подряд:
<code>"1724367212 2001"</code>.</p>
<p><b>Всегда присылайте <code>Idempotency-Key</code></b> — свой номер
заказа. Если ответ не дошёл и вы повторили запрос, вернётся тот же заказ,
а не второй: деньги спишутся один раз.</p>

<h3>5. Узнайте исход</h3>
<p>Не опрашивайте <code>/order/status</code> в цикле — укажите адрес
вебхука в боте, и мы сами сообщим о каждой смене статуса.</p>

<h2 id="statuses">Статусы и деньги</h2>
<table><thead><tr><th>Статус</th><th>Что это значит</th></tr></thead>
<tbody>{statuses}</tbody></table>
<ul>
<li><b>Деньги списываются сразу</b> при создании заказа.</li>
<li><b>Если заказ не прошёл</b> — поставщик отказал, ID оказался чужим,
время вышло — деньги возвращаются <b>сами</b>, без единой просьбы.
Статус станет <code>refunded</code>, причина будет в поле
<code>reason</code>.</li>
<li><b>Если заказ выполнен, а клиент недоволен</b> — оставьте просьбу:
<code>POST /order/refund</code> с <code>order_id</code> и внятной
<code>reason</code>. Товар поставщику уже оплачен, поэтому решает
владелец: деньги вернутся, только если он согласится. Ответ приходит
вебхуком.</li>
</ul>

<h2>Создать заказ</h2>
{_samples(url)}

<h3>Ответ</h3>
<pre><code>{escape(order_answer)}</code></pre>

<div class="note"><b>Деньги — целые числа</b> в дирамах
(1 сомони = 100 дирам). <code>amount: 1931</code> — это 19.31 сомони.
Так сделано нарочно: дробные числа при сложении дают копеечные
расхождения в любом языке.</div>

<h3>Повторный запрос</h3>
<p>Передайте <code>Idempotency-Key</code> (заголовком или полем
<code>idempotency_key</code>). Если тот же запрос уйдёт дважды —
из-за обрыва связи или повтора очереди, — второй раз деньги
<b>не спишутся</b>: вернётся тот же заказ.</p>

<h2>Статус заказа</h2>
<pre><code>curl "{escape(url)}/order/status?order_id=ORD-000123" \\
  -H "Authorization: Bearer ВАШ_КЛЮЧ"</code></pre>
<pre><code>{escape(status_answer)}</code></pre>
<p>Что означает каждый статус — в разделе
<a href="#statuses">«Статусы и деньги»</a> выше.</p>

<h2>Вебхук</h2>
<p>Укажите свой адрес в боте (<code>/api</code> → Вебхук) — и мы сами
сообщим, когда статус заказа изменится. Опрашивать
<code>/order/status</code> в цикле тогда не нужно.</p>
<pre><code>POST ваш-адрес
X-Timestamp: 1789600000
X-Signature: 3b099d0edd…
X-Signature-Algorithm: hmac-sha256

{escape(hook_body)}</code></pre>

<h3>Проверьте подпись</h3>
<p>Подпись — HMAC-SHA256 на вашем секрете <code>whsec_…</code> от строки
<code>X-Timestamp</code> + точка + <b>сырое тело</b> запроса. Без проверки
любой, кто узнает ваш адрес, пришлёт «заказ выполнен» сам. Время в подписи
затем, чтобы перехваченное однажды событие нельзя было повторять: считайте
событие чужим, если <code>X-Timestamp</code> старше пяти минут.</p>
{_hook_sample()}

<div class="note">Отвечайте <code>200</code>, как только приняли событие.
Если ответа нет, мы повторим — до 8 раз. Адрес должен быть
<code>https://</code> и вести наружу: внутренние адреса
(<code>127.0.0.1</code>, <code>10.x</code>) мы не вызываем.</div>

<h2>Ошибки</h2>
<pre><code>{escape(error_answer)}</code></pre>
<table><thead><tr><th>Код</th><th>error.code</th><th>Что значит</th></tr>
</thead><tbody>{errors}</tbody></table>

<h2>Лимиты</h2>
<ul>
<li><b>{limit} запросов в минуту</b> на ключ. Сверх — <code>429</code>
и заголовок <code>Retry-After</code>.</li>
<li>Короткий всплеск проходит целиком: лимит считается «дырявым ведром»,
а не жёстко по минутам.</li>
<li>Десять неверных ключей подряд с одного адреса закрывают его
на 5 минут.</li>
<li>В каждом ответе есть <code>X-Request-Id</code>. Если что-то не
работает — пришлите этот номер владельцу, по нему запрос найдётся
в журнале.</li>
</ul>

<footer>Вопросы — владельцу бота. Ключ в переписке не присылайте:
если он утёк, отзовите его в <code>/api</code> и создайте новый.</footer>

</div><script>{JS}</script></body></html>"""


async def page(request: web.Request) -> web.Response:
    return web.Response(text=html(), content_type="text/html", charset="utf-8")
