# Donatix: подписанные Android APK и AAB в Bitrise

## 1. Вставить конфигурацию

Открой Workflow Editor → YAML. Замени весь текст содержимым файла `bitrise.yml`, нажми Save changes. Конфигурация должна содержать workflow `android-release`. В проекте сейчас выбрано хранение конфигурации на Bitrise: добавление файла в GitHub само по себе не заменяет текст в редакторе.

Используется Android Linux stack `linux-docker-android-22.04`, машина `standard` (Linux Medium), Flutter 3.35.4, Java 17 и Android SDK 36. Если Bitrise сообщает, что этот стек недоступен для аккаунта, в Stacks & Machines выбери доступный Ubuntu Android stack и Linux Medium, сохрани новый идентификатор стека. Наличие бесплатных минут/кредитов зависит от текущего плана аккаунта.

## 2. Загрузить существующий ключ подписи

Открой **Project settings → Code signing → Android → Add keystore file**. Загрузи тот же файл `.jks` / `.keystore`, которым подписывал приложение в Codemagic. Укажи существующие Keystore password, Key alias и Private key password. Сохрани с именами переменных по умолчанию, без собственного Custom ID:

| Переменная, которую создаёт Bitrise | Значение |
| --- | --- |
| `BITRISEIO_ANDROID_KEYSTORE_URL` | Bitrise создаёт после загрузки файла |
| `BITRISEIO_ANDROID_KEYSTORE_PASSWORD` | Пароль хранилища |
| `BITRISEIO_ANDROID_KEYSTORE_ALIAS` | Alias ключа |
| `BITRISEIO_ANDROID_KEYSTORE_PRIVATE_KEY_PASSWORD` | Пароль ключа |

Не создавай новый ключ вместо прежнего для уже выпущенного приложения. Если раньше загружал несколько ключей, используй именно переменные без числового суффикса; они должны указывать на прежний ключ Donatix.

## 3. Добавить Firebase-конфигурацию Android

Открой **Workflow Editor → Secrets → Add new**:

| Поле | Что указать |
| --- | --- |
| Key / Variable name | `GOOGLE_SERVICES_JSON` |
| Value | Всё содержимое существующего Android-файла `google-services.json` |

Это клиентский файл Firebase для пакета `tj.donatix.app`, который уже использовал в Codemagic. Серверный Firebase Admin JSON сюда не подходит. Ключ подписи, пароли и серверные ключи не добавляй в GitHub и не присылай в чат.

## 4. Запустить release-сборку

Нажми Start/Schedule a build (или Start a build), выбери ветку **main** и workflow **android-release**. Для этих изменений не нужно загружать файлы на сервер. Автоматические push/PR-триггеры в данной конфигурации отсутствуют.

По умолчанию номер Android-версии: `1000 + BITRISE_BUILD_NUMBER`. Если в Google Play уже есть больший versionCode, перед запуском укажи в **Env Vars** `DONATIX_BUILD_NUMBER` — целое число больше максимального загруженного versionCode. При следующих выпусках также увеличивай его; номера сборок разных CI не синхронизируются. APK и AAB одной сборки получают одинаковый номер.

После успешной сборки открой её **Artifacts / Apps & Artifacts** и скачай:

- `Donatix-release.apk` — для установки и проверки на Android-телефоне.
- `Donatix-release.aab` — для загрузки в Google Play.
- `release-verification.json` — результат проверки подписи и Android-артефактов.

Сборка выполняет анализ кода, существующие проверки проекта и затем `flutter build ... --release`. Проверки не переводят приложение в тестовый режим. Перед публикацией проверяются подпись, совпадение сертификатов APK/AAB, пакет приложения, отсутствие debug-сборки, ARM64 и совместимость нативных библиотек с 16 KB страницами. Если проверка не проходит, файлы не публикуются как готовый релиз.

Конфигурация CI не меняет сайт, сервер, функции приложения или iOS. Подписанные артефакты появятся после реального успешного запуска Bitrise; наличие YAML не означает, что сборка уже выполнена или приложение принято Google Play.
