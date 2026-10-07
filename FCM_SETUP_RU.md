# Включение FCM владельца

Код Android-регистрации, обработки push, server outbox, повторов, отзыва устройств
и уведомлений добавлен. Доставка через настоящий Firebase пока не проверена.

1. В Firebase-проекте владельца зарегистрировать Android package `tj.donatix.app`.
   Полученный `google-services.json` положить в
   `Donatix_Flutter/android/app/google-services.json`. Не использовать пример или
   чужой проект. Если файла нет, клиент сохраняет резервную фоновую проверку.
2. В Codemagic → приложение Donatix → Environment variables создать группу
   `donatix_android`. В ней задать защищённую переменную `GOOGLE_SERVICES_JSON`
   с полным содержимым `google-services.json` (обычный JSON, без base64).
   `android-release` импортирует эту группу через `environment.groups`.
   Release workflow проверит совпадение package name.
3. В том же Firebase-проекте получить service-account JSON с правом отправки FCM.
   Хранить его только на сервере, например `/etc/donatix/firebase-service-account.json`.
   Доступ должен быть только у пользователя сервиса. Этот JSON не включать в APK,
   Git или проектный архив.
4. В окружении существующего systemd-сервиса задать
   `DONATIX_FIREBASE_CREDENTIALS=/etc/donatix/firebase-service-account.json`.
   Установить обновлённые requirements и перезапустить сервер. Код использует
   только указанный файл; ambient credentials и metadata discovery не используются.
5. Пользователь включает уведомления в приложении. Android регистрирует токен
   в фоне с действующей cookie/CSRF, сервер привязывает устройство к сессии.
   `/api/v1/mobile/push/status` показывает, настроена ли серверная часть.
6. Проверить реальное уведомление заказа, повтор, холодный запуск, выключение,
   выход и переход в другой аккаунт. Для Android без Google Play services
   сохраняется периодическая проверка. FCM зависит от сети и ограничений Android;
   принудительно остановленное приложение нужно снова открыть.

Токены шифруются на сервере. Уведомления data-only проверяют владельца в Android.
Сообщение другого аккаунта не показывается. Фоновые уведомления и FCM используют
один notification ID, чтобы не показывать одно событие дважды.

Источники:
https://firebase.google.com/docs/android/setup
https://firebase.google.com/docs/cloud-messaging/android/receive
https://firebase.google.com/docs/cloud-messaging/send/admin-sdk

## iOS в версии 4

Подключение Firebase iOS и APNs, профиль Push Notifications и особенности фоновой доставки описаны в IOS_SETUP_RU.md. Обе платформы используют одну серверную очередь и собственные токены. iOS-пакет не включает личные суммы/текст: подробности доступны в приложении.

## Android 1.0.0+5 — уже отправленная серверная папка

Серверный установщик не изменён: владелец использует папку, которую уже
получил. Android-регистрация повторяется при ошибках, выход останавливает
старую конфигурацию, уведомления проверяют владельца, а нажатие открывает
нужный экран и при уже запущенном приложении. Эти изменения находятся в клиенте.

Для текущей настройки используется Firebase-проект `donatix-660fc` и package
`tj.donatix.app`. Серверный service-account должен относиться к тому же проекту,
что и защищённая переменная GOOGLE_SERVICES_JSON выбранной сборки. Если
владелец решит использовать другой Firebase-проект, нужно заменить клиентский
JSON в Codemagic и пересобрать APK/AAB с соответствующим серверным ключом.

google-services.json — конфигурация Android-приложения; service-account JSON —
серверный закрытый ключ. Это разные файлы. Google OAuth для входа — отдельная
настройка исходного сайта; Firebase-конфигурация не включает Google-вход сама.
Для Android не требуются APNs и Apple-вход. iOS пока отложен.
