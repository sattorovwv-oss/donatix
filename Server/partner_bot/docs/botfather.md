# Оформление бота в BotFather

Всё это заполняется вручную: `@BotFather` → `/mybots` → выбрать бота → **Edit Bot**.

---

## 1. Welcome Picture (Set Welcome Picture)

Требования Telegram: **640 × 360**, JPG или PNG (16:9, горизонтальная).
Показывается один раз — на экране «What can this bot do?» до нажатия «Запустить».

Промт для генератора (Midjourney / DALL·E / Sora):

```
A luxury 16:9 banner for a Telegram bot that sells Telegram Stars and Premium.
Deep matte black background with a soft charcoal-to-obsidian gradient and a faint
diagonal light sweep. In the center-left: a large 3D golden five-pointed star,
polished metal with warm champagne-gold reflections, floating and slightly tilted,
casting a soft golden glow. Around it a few smaller golden stars and fine sparkling
particles drifting upward. On the right side: a slim golden crown outline and a
delicate golden Telegram paper-plane icon, both thin-line, elegant, not cartoonish.
A very subtle thin gold hairline frame near the edges. Rich, cinematic studio
lighting, shallow depth of field, soft bokeh, premium classic style — like a private
bank card advertisement. Palette strictly: black, graphite, champagne gold, warm
amber highlights. Clean empty space in the lower area. No text, no letters, no
watermark, no logos. Ultra sharp, high detail, 8k render, 16:9 aspect ratio.
```

Если генератор просит формат отдельно — `--ar 16:9 --style raw --quality 2`.

---

## 2. Description (Enter bot description)

Лимит Telegram — **512 символов**. Текст ниже помещается.

```
⭐️ Telegram Stars и 👑 Telegram Premium — за сомони, без карт и обменников.

• Пополняешь баланс через Душанбе Сити — по ссылке в один клик
• Выбираешь количество звёзд или срок Premium
• Проверяешь имя аккаунта получателя и подтверждаешь
• Заказ приходит автоматически за 1–3 минуты

Данные от аккаунта не нужны — только @username. Если что-то пошло не так, деньги сразу возвращаются на баланс.

Нажми «Запустить», чтобы начать 👇
```

---

## 3. About (Edit About) — короткий текст под аватаркой

Лимит **120 символов**.

```
⭐️ Звёзды Telegram и Premium за сомони. Оплата Душанбе Сити, доставка за 1–3 минуты, без данных аккаунта.
```

---

## 4. Commands (Edit Commands)

```
start - Главное меню
profile - Мой профиль и баланс
support - Поддержка
```
