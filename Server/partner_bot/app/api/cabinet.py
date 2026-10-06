"""Личный кабинет разработчика — страница в браузере.

Ключ и есть пропуск: ни регистрации, ни пароля. Разработчик вставляет
его на странице и видит свой баланс, заказы за выбранный период, движение
денег, каталог и состояние вебхука.

Страница отдаётся тем же сервером, что и API, и ходит только на него же.
Собирается строкой, без шаблонизатора и без внешних файлов: лишняя
зависимость ради одной страницы не стоит того, а разметка, лежащая не
там же, где данные, устаревает первой.

Ключ живёт в памяти вкладки: обновление страницы его переживает, закрытие
вкладки — нет. Насовсем в браузере не сохраняем нарочно — это доступ к
деньгам, а рабочий компьютер бывает общим.
"""
from __future__ import annotations

from aiohttp import web

CSS = """
:root{--bg:#fff;--fg:#11161d;--muted:#5b6673;--line:#e4e8ee;--card:#f7f9fb;
--accent:#1f6feb;--ok:#1a7f37;--warn:#9a3412;--back:#7c3aed}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;
--muted:#9198a1;--line:#242b33;--card:#161b22;--accent:#58a6ff;
--ok:#3fb950;--warn:#f0883e;--back:#a78bfa}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:26px 16px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.4px}
.lead{color:var(--muted);margin:0 0 22px;font-size:15px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
.row input{flex:1 1 260px;min-width:0;font:14px/1 ui-monospace,monospace;
padding:12px 13px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--fg)}
button{font:600 14px/1 inherit;cursor:pointer}
.go{padding:12px 22px;border:0;border-radius:9px;background:var(--accent);color:#fff}
.hint{color:var(--muted);font-size:14px;margin:8px 0}
.hint.bad{color:var(--warn)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
padding:13px 15px}
.card b{display:block;font-size:20px;letter-spacing:-.3px;margin-top:2px}
.card span{color:var(--muted);font-size:12.5px;text-transform:uppercase;
letter-spacing:.4px}
nav{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--line);
margin:22px 0 0}
nav button{padding:10px 16px;border:0;border-bottom:2px solid transparent;
background:none;color:var(--muted)}
nav button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0}
.chips button{padding:7px 13px;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:20px;font-weight:500;font-size:13px;text-align:left}
.chips button[aria-pressed=true]{background:var(--accent);color:#fff;
border-color:var(--accent)}
.chips button i{font-style:normal;color:var(--muted);font-size:12px}
.chips button[aria-pressed=true] i{color:#dbe9ff}
.pick{max-height:300px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:14.5px;margin:8px 0}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
letter-spacing:.4px}
tr.line{cursor:pointer}
tr.line:hover{background:var(--card)}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.s-completed{color:var(--ok)}
.s-refunded{color:var(--back)}
.s-processing{color:var(--muted)}
.s-cancelled{color:var(--warn)}
.more{margin:14px 0;padding:10px 18px;border:1px solid var(--line);
border-radius:9px;background:var(--card);color:var(--fg)}
.deep td{background:var(--card);font-size:13.5px;color:var(--muted)}
.deep b{color:var(--fg)}
footer{margin-top:50px;padding-top:18px;border-top:1px solid var(--line);
color:var(--muted);font-size:13.5px}
footer a{color:var(--accent)}
@media(max-width:620px){
  th.hide,td.hide{display:none}
  .wrap{padding:18px 14px 70px}
}
"""

JS = """
(function(){
  var api = location.pathname.replace(/\\/cabinet\\/?$/, '');
  var tz  = -new Date().getTimezoneOffset();
  var key = '';
  var tab = 'orders';
  var period = 'today';
  var shown = 0;            // сколько строк уже на экране: для «показать ещё»

  var $ = function(id){ return document.getElementById(id); };

  // Разметку строим узлами, а не строками: названия товаров приходят от
  // поставщика, и кавычка внутри чужого названия не должна стать разметкой.
  function el(tag, text, cls){
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function say(text, bad){
    $('msg').textContent = text || '';
    $('msg').className = bad ? 'hint bad' : 'hint';
  }

  function ask(path){
    var join = path.indexOf('?') < 0 ? '?' : '&';
    return fetch(api + path + join + 'tz=' + tz,
                 {headers: {Authorization: 'Bearer ' + key}})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.success) throw new Error((d.error && d.error.message) || 'Ошибка');
        return d;
      });
  }

  function money(m){ return m ? m.amount_text + ' ' + m.currency : '—'; }

  function when(iso){
    if (!iso) return '';
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  }

  var STATUS = {
    completed:  '✅ выполнен',
    processing: '⏳ в работе',
    refunded:   '↩️ возврат',
    cancelled:  '✖️ отменён',
    failed:     '⚠️ разбирается'
  };

  // ── вход ───────────────────────────────────────────────────────────────
  function shut(){
    // Не пустило — значит на экране не должно остаться ничего от входа:
    // иначе видно вкладки и периоды при неработающем ключе.
    $('gate').hidden = false;
    $('out').hidden = true;
    $('top').hidden = true;
    $('nav').hidden = true;
    $('periods').hidden = true;
    $('top').textContent = '';
    $('stats').textContent = '';
    $('view').textContent = '';
    try { sessionStorage.removeItem('k'); } catch (e) {}
  }

  function forget(){
    try { localStorage.removeItem('pass'); } catch (e) {}
    key = '';
    shut();
    say('Вышли. Чтобы войти снова — кнопка «Открыть кабинет» в боте: /api.');
  }

  function enter(){
    key = ($('apikey').value || '').trim();
    if (!key) { say('Вставьте ключ.', true); return; }
    say('Проверяю ключ…');
    ask('/user').then(function(d){
      try { sessionStorage.setItem('k', key); } catch (e) {}
      say('');
      $('gate').hidden = true;
      $('top').hidden = false;
      $('nav').hidden = false;
      header(d.user);
      open('orders');
    }).catch(function(e){
      shut();
      say(e.message + ' Подойдёт любой ваш ключ — данные всё равно '
          + 'показываются по всему аккаунту.', true);
    });
  }

  function header(u){
    var box = $('top');
    box.textContent = '';
    [['Баланс', money(u.balance)],
     ['Аккаунт', u.username ? '@' + u.username : String(u.id)],
     ['Запросов', String((u.requests && u.requests.total) || 0)],
     ['Лимит', (u.rate_limit_per_minute || 0) + ' /мин']
    ].forEach(function(pair){
      var card = el('div', undefined, 'card');
      card.appendChild(el('span', pair[0]));
      card.appendChild(el('b', pair[1]));
      box.appendChild(card);
    });
    $('whose').textContent =
      'Весь аккаунт целиком: заказы и деньги по всем вашим ключам. '
      + 'Вошли ключом ' + ((u.key && u.key.masked) || '—') + '.';
    $('whose').hidden = false;
    $('out').hidden = false;
  }

  // ── вкладки ────────────────────────────────────────────────────────────
  function open(name){
    tab = name;
    shown = 0;
    document.querySelectorAll('nav button').forEach(function(b){
      b.setAttribute('aria-selected', b.getAttribute('data-tab') === name);
    });
    $('periods').hidden = (name !== 'orders' && name !== 'money');
    $('stats').textContent = '';
    $('view').textContent = '';
    if (name === 'orders')  return orders(true);
    if (name === 'money')   return money_tab(true);
    if (name === 'catalog') return catalog();
    if (name === 'hook')    return hook();
  }

  function pickPeriod(name){
    period = name;
    document.querySelectorAll('#periods button').forEach(function(b){
      b.setAttribute('aria-pressed', b.getAttribute('data-p') === name);
    });
    shown = 0;
    if (tab === 'orders') orders(true); else money_tab(true);
  }

  function moreButton(total, load){
    var old = document.querySelector('.more');
    if (old) old.remove();
    if (shown >= total) return;
    var b = el('button', 'Показать ещё (' + (total - shown) + ')', 'more');
    b.addEventListener('click', function(){ load(false); });
    $('view').appendChild(b);
  }

  // ── заказы ─────────────────────────────────────────────────────────────
  function orders(fresh){
    if (fresh) { $('view').textContent = ''; shown = 0; }
    say('Загружаю…');

    ask('/orders/summary?period=' + period).then(function(s){
      var box = $('stats');
      box.textContent = '';
      [['Заказов', String(s.orders)],
       ['Выполнено', String(s.completed)],
       ['В работе', String(s.processing)],
       ['Возвратов', String(s.refunded)],
       ['Потрачено', money(s.spent)],
       ['Вернулось', money(s.returned)]
      ].forEach(function(pair){
        var card = el('div', undefined, 'card');
        card.appendChild(el('span', pair[0]));
        card.appendChild(el('b', pair[1]));
        box.appendChild(card);
      });
      return ask('/orders?period=' + period + '&limit=50&offset=' + shown);
    }).then(function(d){
      say(d.total ? '' : 'За этот период заказов не было.');
      var table = document.querySelector('#view table');
      if (!table) {
        table = el('table');
        var head = el('thead'), hr = el('tr');
        ['Заказ', 'Товар', 'Сумма', 'Статус'].forEach(function(h, i){
          hr.appendChild(el('th', h, i === 1 ? 'hide' : ''));
        });
        head.appendChild(hr);
        table.appendChild(head);
        table.appendChild(el('tbody'));
        $('view').appendChild(table);
      }
      var body = table.querySelector('tbody');
      d.orders.forEach(function(o){ body.appendChild(orderRow(o, body)); });
      shown += d.orders.length;
      moreButton(d.total, orders);
    }).catch(function(e){ say(e.message, true); });
  }

  function orderRow(o, body){
    var tr = el('tr', undefined, 'line');
    tr.appendChild(el('td', o.order_id));
    tr.appendChild(el('td', o.product_id, 'hide'));
    tr.appendChild(el('td', money(o), 'num'));
    tr.appendChild(el('td', STATUS[o.status] || o.status, 's-' + o.status));

    var open = false, deep = null;
    tr.addEventListener('click', function(){
      if (open) { deep.remove(); open = false; return; }
      deep = el('tr', undefined, 'deep');
      var cell = el('td');
      cell.colSpan = 4;
      [['Товар', o.product_id],
       ['Кому', o.customer || '—'],
       ['Количество', String(o.quantity)],
       ['Создан', when(o.created_at)],
       ['Обновлён', when(o.updated_at)],
       ['Причина', o.reason || '']
      ].forEach(function(pair){
        if (!pair[1]) return;
        var line = el('div');
        line.appendChild(el('span', pair[0] + ': '));
        line.appendChild(el('b', pair[1]));
        cell.appendChild(line);
      });
      deep.appendChild(cell);
      tr.after(deep);
      open = true;
    });
    return tr;
  }

  // ── деньги ─────────────────────────────────────────────────────────────
  function money_tab(fresh){
    if (fresh) { $('view').textContent = ''; shown = 0; }
    say('Загружаю…');
    ask('/transactions?period=' + period + '&limit=50&offset=' + shown)
    .then(function(d){
      say(d.total ? '' : 'За этот период движения денег не было.');
      var table = document.querySelector('#view table');
      if (!table) {
        table = el('table');
        var head = el('thead'), hr = el('tr');
        ['Когда', 'Что', 'Сумма', 'Остаток'].forEach(function(h, i){
          hr.appendChild(el('th', h, i === 0 ? 'hide' : ''));
        });
        head.appendChild(hr);
        table.appendChild(head);
        table.appendChild(el('tbody'));
        $('view').appendChild(table);
      }
      var body = table.querySelector('tbody');
      d.transactions.forEach(function(t){
        var tr = el('tr');
        tr.appendChild(el('td', when(t.created_at), 'hide'));
        tr.appendChild(el('td', t.kind_text + (t.order_id ? ' · ' + t.order_id : '')));
        tr.appendChild(el('td', money(t), 'num'));
        tr.appendChild(el('td', (t.balance_after / 100).toFixed(2), 'num'));
        body.appendChild(tr);
      });
      shown += d.transactions.length;
      moreButton(d.total, money_tab);
    }).catch(function(e){ say(e.message, true); });
  }

  // ── каталог ────────────────────────────────────────────────────────────
  function catalog(){
    say('Загружаю каталог…');
    var chips = el('div', undefined, 'chips');
    var items = el('div');
    $('view').appendChild(chips);
    $('view').appendChild(items);

    function chip(label, note, load){
      var b = el('button', label);
      b.setAttribute('aria-pressed', 'false');
      if (note) { b.appendChild(document.createTextNode(' ')); b.appendChild(el('i', note)); }
      b.addEventListener('click', function(){
        chips.querySelectorAll('button').forEach(function(o){
          o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
        });
        items.textContent = '';
        say('Загружаю…');
        load().then(function(list){
          say(list.length ? '' : 'Тут пока пусто.');
          items.appendChild(goods(list));
        }).catch(function(e){ say(e.message, true); });
      });
      return b;
    }

    function goods(list){
      var table = el('table');
      var head = el('thead'), hr = el('tr');
      ['product_id', 'Название', 'Цена', 'Что спросить'].forEach(function(h, i){
        hr.appendChild(el('th', h, i === 3 ? 'hide' : ''));
      });
      head.appendChild(hr);
      table.appendChild(head);
      var body = el('tbody');
      list.forEach(function(p){
        var tr = el('tr');
        var id = el('td');
        id.appendChild(el('code', p.id));
        tr.appendChild(id);
        tr.appendChild(el('td', p.name));
        // Рядом с сомони — доллары. Разработчику называют цены в
        // долларах, и без этого он не может сравнить наши со своими.
        var price = p.amount_text
          ? p.amount_text + ' ' + p.currency
          : (p.unit_price / 10000).toFixed(4) + ' за шт.';
        if (p.usd) price += '  ≈ $' + p.usd.toFixed(2);
        else if (p.usd_per_unit) price += '  ≈ $' + p.usd_per_unit.toFixed(5);
        tr.appendChild(el('td', price, 'num'));
        tr.appendChild(el('td', p.customer || '—', 'hide'));
        body.appendChild(tr);
      });
      table.appendChild(body);
      return table;
    }

    ask('/products?limit=1000').then(function(d){
      [['Telegram Stars', 'stars'], ['Telegram Premium', 'premium'],
       ['Steam', 'steam']].forEach(function(g){
        var found = d.products.filter(function(p){ return p.type === g[1]; });
        if (!found.length) return;
        chips.appendChild(chip(g[0], found.length + ' шт.', function(){
          return Promise.resolve(found);
        }));
      });
      return ask('/games');
    }).then(function(d){
      (d.games || []).forEach(function(g){
        chips.appendChild(chip(g.name, g.packs + ' пак.', function(){
          return ask('/products?game=' + encodeURIComponent(g.id) + '&limit=1000')
            .then(function(r){ return r.products; });
        }));
      });
      if ((d.games || []).length > 30) chips.className = 'chips pick';
      say('Нажмите на раздел или игру.');
    }).catch(function(e){ say(e.message, true); });
  }

  // ── вебхук ─────────────────────────────────────────────────────────────
  function hook(){
    say('');
    ask('/user').then(function(d){
      var h = d.user.webhook || {};
      var box = $('view');
      var table = el('table');
      var body = el('tbody');
      [['Адрес', h.url || 'не задан'],
       ['Состояние', h.url ? (h.enabled ? 'включён' : 'выключен') : '—']
      ].forEach(function(pair){
        var tr = el('tr');
        tr.appendChild(el('td', pair[0]));
        tr.appendChild(el('td', pair[1]));
        body.appendChild(tr);
      });
      table.appendChild(body);
      box.appendChild(table);
      box.appendChild(el('p',
        'Адрес вебхука задаётся в боте: команда /api → «Вебхук». '
        + 'Туда приходит событие на каждую смену статуса заказа — '
        + 'выдачу, отказ и возврат.', 'hint'));
    }).catch(function(e){ say(e.message, true); });
  }

  // ── запуск ─────────────────────────────────────────────────────────────
  $('show').addEventListener('click', enter);
  $('out').addEventListener('click', forget);
  $('apikey').addEventListener('keydown', function(e){
    if (e.key === 'Enter') enter();
  });
  document.querySelectorAll('nav button').forEach(function(b){
    b.addEventListener('click', function(){ open(b.getAttribute('data-tab')); });
  });
  document.querySelectorAll('#periods button').forEach(function(b){
    b.addEventListener('click', function(){ pickPeriod(b.getAttribute('data-p')); });
  });

  // Вход по ссылке из бота. Пропуск приходит после решётки: такую часть
  // адреса браузер не отправляет на сервер, поэтому она не оседает ни в
  // нашем журнале, ни в чужом Referer. Из адресной строки убираем сразу.
  function fromLink(){
    var m = (location.hash || '').match(/[#&]t=([A-Za-z0-9_]+)/);
    if (!m) return false;
    var token = m[1];
    try { history.replaceState(null, '', location.pathname); } catch (e) {}
    try { localStorage.setItem('pass', token); } catch (e) {}
    key = token;
    say('Открываю кабинет…');
    ask('/user').then(function(d){
      say('');
      $('gate').hidden = true;
      $('top').hidden = false;
      $('nav').hidden = false;
      header(d.user);
      open('orders');
    }).catch(function(e){
      try { localStorage.removeItem('pass'); } catch (x) {}
      shut();
      say(e.message + ' Откройте кабинет заново кнопкой в боте: /api.', true);
    });
    return true;
  }

  // Порядок такой: сначала свежая ссылка, потом запомненный пропуск,
  // и только потом ключ, введённый руками в этой вкладке.
  if (!fromLink()) {
    var stored = null;
    try { stored = localStorage.getItem('pass'); } catch (e) {}
    if (stored) {
      location.hash = 't=' + stored;
      fromLink();
    } else {
      try {
        var saved = sessionStorage.getItem('k');
        if (saved) { $('apikey').value = saved; enter(); }
      } catch (e) {}
    }
  }
})();
"""

BODY = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Кабинет</title>
<style>__CSS__</style>
</head><body><div class="wrap">

<h1>Кабинет</h1>
<p class="lead">Заказы, деньги и каталог вашего аккаунта. Регистрация не нужна.</p>

<div id="gate">
<div class="row">
<input id="apikey" type="password" autocomplete="off" spellcheck="false"
 placeholder="sk_live_…">
<button id="show" class="go">Войти</button>
</div>
<p class="hint"><b>Проще всего — не вводить ничего.</b> Откройте бота,
команда <code>/api</code>, кнопка «🖥 Открыть кабинет» — она узнает вас
сама и запомнит это устройство.<br><br>
Поле выше — для тех, кто пришёл с рабочим ключом. Данные всё равно
показываются по всему аккаунту, поэтому подойдёт <b>любой</b> ваш ключ.</p>
</div>

<p id="msg" class="hint"></p>
<p id="whose" class="hint" hidden></p>
<p><button id="out" class="more" hidden>Выйти на этом устройстве</button></p>
<div id="top" class="cards" hidden></div>

<nav id="nav" hidden>
<button data-tab="orders" aria-selected="true">Заказы</button>
<button data-tab="money" aria-selected="false">Деньги</button>
<button data-tab="catalog" aria-selected="false">Каталог</button>
<button data-tab="hook" aria-selected="false">Вебхук</button>
</nav>

<div id="periods" class="chips" hidden>
<button data-p="today" aria-pressed="true">Сегодня</button>
<button data-p="yesterday" aria-pressed="false">Вчера</button>
<button data-p="7d" aria-pressed="false">7 дней</button>
<button data-p="30d" aria-pressed="false">30 дней</button>
<button data-p="all" aria-pressed="false">Всё время</button>
</div>

<div id="stats" class="cards"></div>
<div id="view"></div>

<footer>Как подключиться к API — <a href="docs">документация</a>.</footer>

</div><script>__JS__</script></body></html>"""


def html() -> str:
    return BODY.replace("__CSS__", CSS).replace("__JS__", JS)


async def page(request: web.Request) -> web.Response:
    return web.Response(text=html(), content_type="text/html", charset="utf-8")
