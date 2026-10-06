# Donatix — Android-приложение (APK)

Сайт уже подготовлен к упаковке в приложение. Ничего на сайте менять не нужно —
приложение просто открывает его, а сайт сам переходит в «режим приложения».

## Что уже есть на сайте

| Что | Адрес |
|---|---|
| Манифест приложения (имя, иконки, цвета, ярлыки) | `https://donatix.tj/manifest.webmanifest` |
| Стартовая страница приложения | `https://donatix.tj/panel?source=app` |
| Service worker (экран «Нет интернета») | `https://donatix.tj/sw.js` |
| Связь сайта с APK (Digital Asset Links) | `https://donatix.tj/.well-known/assetlinks.json` |
| Иконки 192/512 и maskable | `https://donatix.tj/static/app/…` |

Режим приложения включается сам, если сайт открыт:
- из установленного приложения (`display-mode: standalone`, TWA);
- по ссылке с `?source=app` (запоминается);
- во WebView, где в User-Agent есть `DonatixApp`.

В режиме приложения: нижнее меню (Главная · Каталог · Пополнить · Заказы · Ещё),
учёт выреза экрана (safe area), без «резинки» при прокрутке.
Во WebView скрыта кнопка «Войти через Google» — Google запрещает вход во встроенных браузерах.

---

## Вариант 1 (рекомендуем): Trusted Web Activity — 15 минут

Приложение открывает сайт в Chrome без адресной строки. Работает всё: вход через Google,
загрузка чеков с камеры/галереи, копирование, оплата. Обновлять APK при изменениях сайта не нужно.

**Самый простой способ — PWABuilder:**
1. Открыть https://www.pwabuilder.com → ввести `https://donatix.tj/panel?source=app`.
2. «Package for stores» → **Android** → «Generate».
   - Package ID: `tj.donatix.app` (или свой)
   - App name: `Donatix`
   - Signing key: «Create new» — **сохраните файл ключа и пароли**, без них нельзя выпустить обновление.
3. В архиве будет `app-release-signed.apk` (для установки) и `.aab` (для Google Play),
   а также `assetlinks.json` с отпечатком ключа (`sha256_cert_fingerprints`).

**Или через Bubblewrap (командная строка):**
```
npm i -g @bubblewrap/cli
bubblewrap init --manifest https://donatix.tj/manifest.webmanifest
bubblewrap build
bubblewrap fingerprint list   # отпечаток SHA-256 ключа подписи
```

**Уведомления (обязательно включить):**
- PWABuilder → Android → «All settings» → **Notification delegation: ON** (галочка «Enable notifications»).
- Bubblewrap: в `twa-manifest.json` — `"enableNotifications": true`.
- На Android 13+ приложение само спросит разрешение на уведомления (POST_NOTIFICATIONS) — это нормально.

Сайт уже отправляет push-уведомления (Web Push): «✅ Заказ выполнен», «💰 Баланс пополнен»,
«❌ Заявка отклонена», «↩️ Деньги вернулись». В TWA они приходят от имени приложения Donatix,
с его иконкой, даже когда приложение закрыто. Нажатие открывает нужный заказ.
Проверить: в приложении → «Ещё» → «Уведомления» → «Включить» → «Проверить».

**Обязательно после сборки** — сообщите владельцу сайта имя пакета и отпечаток SHA-256.
Он добавит их в `donatix/.env` на сервере и перезапустит сайт:
```
DONATIX_ANDROID_PACKAGE=tj.donatix.app
DONATIX_ANDROID_SHA256=AB:CD:EF:...   (если ключей несколько — через запятую,
                                        для Google Play добавьте и отпечаток ключа из Play Console → App signing)
```
Проверка: `https://donatix.tj/.well-known/assetlinks.json` показывает пакет и отпечаток.
Без этого в приложении сверху будет видна адресная строка Chrome.

---

## Вариант 2: своё приложение на WebView

⚠️ **В WebView push-уведомления сайта не работают** (ограничение Android). Для уведомлений
пришлось бы отдельно подключать Firebase Cloud Messaging. Поэтому — **Вариант 1 (TWA)**.

Если нужен именно WebView (своя оболочка, свои экраны), обязательно:

1. **Адрес:** `https://donatix.tj/panel?source=app`
2. **User-Agent:** добавить в конец ` DonatixApp/1.0`
   ```kotlin
   webView.settings.userAgentString = webView.settings.userAgentString + " DonatixApp/1.0"
   ```
3. **Настройки:**
   ```kotlin
   settings.javaScriptEnabled = true
   settings.domStorageEnabled = true          // тема, корзина, режим приложения
   settings.mediaPlaybackRequiresUserGesture = true
   CookieManager.getInstance().setAcceptCookie(true)
   CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)
   // и CookieManager.getInstance().flush() в onPause — чтобы не разлогинивало
   ```
4. **Загрузка чека (фото/PDF)** — без этого кнопка «Чек об оплате» не работает:
   переопределить `WebChromeClient.onShowFileChooser` и открыть выбор файла
   (галерея + камера). Типы: `image/jpeg, image/png, image/webp, application/pdf`.
5. **Внешние ссылки** — открывать в системе через `Intent.ACTION_VIEW`, а не в WebView:
   `t.me`, `tg://`, `instagram.com`, `wa.me`, `binance` и любые домены кроме `donatix.tj`.
   ```kotlin
   override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
       val u = req.url
       if (u.host == "donatix.tj" || u.host == "www.donatix.tj") return false
       startActivity(Intent(Intent.ACTION_VIEW, u)); return true
   }
   ```
6. **Кнопка «Назад»:** `if (webView.canGoBack()) webView.goBack() else finish()`.
7. **Копирование** (ключи API, реквизиты, коды) работает через буфер обмена —
   ничего дополнительно не нужно.
8. **Вход через Google** во WebView не работает (запрет Google) — кнопка на сайте скрывается
   автоматически, пользователи входят по email/логину и паролю.
9. Желательно: `SwipeRefreshLayout` (потянуть вниз — обновить), splash-экран цвета `#4338ca`.

## Цвета и иконки

- Основной цвет: `#4338ca`, фон тёмной темы: `#0e1016`, светлой: `#ffffff`.
- Иконки: `https://donatix.tj/static/app/icon-512.png` (обычная),
  `https://donatix.tj/static/app/maskable-512.png` (адаптивная для Android).
