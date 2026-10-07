# Donatix для iPhone и iPad

Android и iOS используют один Flutter-клиент и один сервер. Поддерживаются iOS 15+,
Android 7+ (API 24). Идентификатор приложения на обеих платформах: `tj.donatix.app`.
Сервер по умолчанию: `https://donatix.tj`. Это исходники версии 1.0.0+4;
подписанные APK/AAB/IPA получают в Codemagic после настройки владельца.

## Что добавлено для iOS

- Полный проект Xcode, workspace, схема Runner, CocoaPods и оригинальная иконка Donatix.
- Общие Flutter-экраны каталога, покупок, пополнений, заказов, аккаунта, ботов и админки.
- Камера и выбор фотографии/PDF чека, нужные описания разрешений.
- Отправка документов и PNG прайс-листа через меню iOS, включая iPad.
- Возврат из браузера после Google-входа; cookie не передаётся в URL.
- Sign in with Apple: системный вход, проверка JWT/nonce/audience на сервере,
  одноразовый обмен кода, сохранение refresh token в зашифрованном виде.
- Привязка Apple ID к существующему аккаунту через меню «Ещё» после свежего входа.
  Аккаунты с совпавшей почтой не объединяются автоматически. Скрытая почта Apple поддерживается.
- При входе администратора сохраняется исходная проверка кода Telegram.
- Удаление аккаунта ожидает завершения расчётов и отзыва разрешения Apple; при ошибке
  отзыва сервер повторяет запрос, аккаунт остаётся заблокированным.
- Уведомления APNs через Firebase, отказ/повтор разрешения, регистрация и отзыв токена,
  переход в нужный раздел, проверка текущей сессии и смены пользователя.
- PrivacyInfo.xcprivacy и защищённое хранение нативной сессии в Keychain.

APNs показывает нейтральное «Новое уведомление в Donatix». Суммы и текст заказов
видны после входа в приложение. Уже отправленный push может прийти после выхода;
поэтому личный текст не включается в iOS payload. Перед открытием ссылки приложение
проверяет пользователя и привязку к текущей серверной сессии.

На iOS нет обещания периодической фоновой проверки каждые 15 минут. Без APNs
список и счётчик обновляются при использовании приложения. Android сохраняет
существующую резервную проверку WorkManager.

## Настройки владельца Apple

1. В Apple Developer зарегистрировать явный App ID `tj.donatix.app`.
2. Включить Push Notifications и Sign in with Apple.
3. Создать Apple Distribution certificate и App Store provisioning profile с этими
   возможностями. Загрузить их в Codemagic → Team settings → Code signing identities.
   Нужны сертификат с приватным ключом (.p12) и подходящий .mobileprovision.
4. Создать приложение в App Store Connect с тем же Bundle ID.
5. В Firebase добавить iOS-приложение с этим Bundle ID. Скачать GoogleService-Info.plist.
6. Загрузить APNs authentication key владельца в Firebase → Project settings → Cloud Messaging.
7. В Codemagic → приложение Donatix → Environment variables создать группу
   `donatix_ios`. В ней сохранить защищённую переменную `GOOGLE_SERVICE_INFO_PLIST`:
   полное XML-содержимое plist (без base64). `ios-release` импортирует эту группу
   через `environment.groups` и помещает файл в ios/Runner при сборке.

Файлы сертификатов, Apple .p8, пароль keystore и сервисный аккаунт Firebase в исходники
не включаются. Нужные возможности уже прописаны в Runner.entitlements; подпись и профили
предоставляет Apple Developer аккаунт владельца. Для симулятора сертификат не нужен,
но проверка настоящего APNs и Apple-входа требует конфигурации владельца.

## Сервер Apple-входа

Обновить существующий сервер через server_patch/apply.py и установить requirements.
В его защищённой среде задать:

```dotenv
DONATIX_APPLE_CLIENT_ID=tj.donatix.app
DONATIX_APPLE_TEAM_ID=ВАШ_TEAM_ID
DONATIX_APPLE_KEY_ID=ВАШ_KEY_ID
DONATIX_APPLE_PRIVATE_KEY_FILE=/приватный/путь/AuthKey.p8
DONATIX_FIREBASE_CREDENTIALS=/приватный/путь/firebase-service-account.json
```

Apple .p8 — ключ с включённой возможностью Sign in with Apple для этого App ID.
Это отдельная настройка от ключа App Store Connect и ключа APNs. Права на файлы
должны разрешать чтение серверному сервису и закрывать чтение посторонним пользователям.
После перезапуска `/api/v1/mobile/config` возвращает `version: 4`, `platforms` с обеими
платформами и `apple_enabled: true`, когда настройки присутствуют.

Apple-вход не использует Firebase Auth. Firebase здесь доставляет уведомления;
профиль, баланс, цены и доступ сохраняются в существующем сервере Donatix.
Если пользователь уже начал удаление аккаунта, нельзя отключать Apple-ключ до
успешного отзыва его разрешения. Ошибки отзыва видны в apple_identities.state/error.

## Codemagic

Если в GitHub лежит весь комплект, использовать корневой codemagic.yaml.
Если в корне лежит только содержимое Donatix_Flutter, использовать YAML из этой папки.
Не загружать ZIP вместо распакованных исходников. В Codemagic выбрать Flutter,
переключиться на codemagic.yaml и затем выбрать workflow:

| Workflow | Результат | Требует подпись владельца |
|---|---|---|
| android-release | app-release.apk и app-release.aab для Play | Android upload keystore |
| ios-release | IPA для App Store/TestFlight | Apple Distribution и App Store profile |
| android-check | APK для проверки установки | Нет |
| ios-check | ZIP приложения для симулятора, XCTest result | Нет |

Режимы проверки используют тот же клиент с настоящими серверными функциями.
Никаких фиктивных платежей в клиент они не добавляют. Для публикации использовать release.
Перед release проверяется наличие Firebase-конфига нужного приложения и сервер версии 4.
Для iOS с Google-входом дополнительно требуется включённый Apple-вход на сервере.

Группы Application/Team environment variables должны существовать в Codemagic:
`donatix_android` для android-release, `donatix_ios` для ios-release. Каждая группа
уже подключена в соответствующем YAML через environment.groups.
DONATIX_URL задан в YAML: заменить на HTTPS-домен сервера владельца при необходимости.
Версия сборки растёт с PROJECT_BUILD_NUMBER Codemagic. При переносе проекта или ранее
опубликованной более высокой версии задать DONATIX_BUILD_NUMBER больше предыдущего номера.

Workflow ios-release не публикует приложение автоматически. App Store IPA нельзя просто
установить на любой iPhone как APK: загрузить его в App Store Connect для TestFlight,
либо отдельно собрать с профилем Development/Ad Hoc для зарегистрированных устройств.
Подписанный IPA и принятие App Store в этой сессии не подтверждены.

## Локально на Mac

```bash
cd Donatix_Flutter
flutter pub get
flutter analyze
flutter build ios --simulator --debug --dart-define=DONATIX_URL=https://ВАШ-ДОМЕН
```

Для release открыть ios/Runner.xcworkspace, выбрать Team и профили владельца,
положить его GoogleService-Info.plist в ios/Runner, затем:

```bash
python3 tool/release_preflight.py --platform ios --check-server
flutter build ipa --release --dart-define=DONATIX_URL=https://ВАШ-ДОМЕН
```

Firebase/Messaging закреплён на 12.4.0 в Podfile. Flutter закреплён на 3.35.4.
Podfile.lock появляется после настоящего pod install; CI сохраняет его как артефакт.

## Что проверить на настоящих устройствах

Вход/возврат из браузера/Apple и админский код, восстановление сессии,
фото и PDF чека, отправку файла с iPad, push при открытом/закрытом приложении,
отказ разрешения, выход и смену пользователя, реальные операции на согласованном
сервере и визуальное совпадение с исходным сайтом. См. RELEASE_STATUS.md.
В этой среде нет Xcode и подключённого iPhone; компиляция Swift и устройство не проверены.

Для App Store владельцу также нужны сведения о данных, действующая политика,
контакт поддержки, скриншоты и доступ для проверки. Исходная схема внешней оплаты
цифровых товаров не подтверждает допустимость публикации в App Store/Google Play.
Общие правила и исключения зависят от каталога, региона и способа распространения.

Официальные источники:

- https://docs.flutter.dev/platform-integration/ios/setup
- https://docs.codemagic.io/yaml-code-signing/signing-ios/
- https://firebase.google.com/docs/cloud-messaging/ios/get-started
- https://developer.apple.com/documentation/signinwithapplerestapi
- https://developer.apple.com/documentation/technotes/tn3194-handling-account-deletions-and-revoking-tokens-for-sign-in-with-apple
- https://developer.apple.com/app-store/review/guidelines/
