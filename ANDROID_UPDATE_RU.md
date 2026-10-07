# Обновление Android-приложения Donatix 1.0.0+5

Папку `server_patch`, уже отправленную владельцу, сохраняем. Новая серверная
папка для этого обновления не нужна. В архиве Donatix_Android_Update_v5.zip
есть Flutter-клиент, Codemagic и документация; `Server` и `server_patch`
в него не входят. Архив накладывается на существующий проект.

Файлы iOS сохранены в Flutter-проекте. Отдельно iOS сейчас не настраиваем и
не собираем. Общие исправления Flutter-интерфейса используются на обеих платформах.

## Установить обновление в свою папку проекта

1. Распаковать архив в `C:\Users\user\Desktop\Donatix_Android_Update_v5`.
   Внутри этой папки должны находиться `codemagic.yaml` и `Donatix_Flutter`.
2. Открыть PowerShell. Проверить пути: существующий Git-репозиторий —
   `C:\Users\user\Desktop\Donatix_Android_iOS_v4`. Не выполнять `git init`
   в распакованной папке обновления.
3. Скопировать клиентское обновление в существующий репозиторий:

```powershell
$repoPath = "C:\Users\user\Desktop\Donatix_Android_iOS_v4"
$updatePath = "C:\Users\user\Desktop\Donatix_Android_Update_v5"
git -C $repoPath rev-parse --is-inside-work-tree
if ($LASTEXITCODE -ne 0) { throw "Укажите папку существующего Git-репозитория" }
if (-not (Test-Path "$updatePath\Donatix_Flutter\pubspec.yaml")) { throw "Укажите папку распакованного обновления" }

Copy-Item "$updatePath\Donatix_Flutter\*" -Destination "$repoPath\Donatix_Flutter" -Recurse -Force
foreach ($file in @("codemagic.yaml", "ANDROID_UPDATE_RU.md", "README_RU.md", "FCM_SETUP_RU.md", "DESIGN_PARITY_RU.md", "RELEASE_STATUS.md", "RELEASE_STATUS_v4.md", "verification.json")) {
    Copy-Item (Join-Path $updatePath $file) -Destination (Join-Path $repoPath $file) -Force
}
Set-Location $repoPath
git status --short
```

Серверные папки эти команды не копируют. Если в своей папке есть более новые
неотправленные изменения кода, сначала сохранить их отдельным коммитом или
копией. Ключ подписи, key.properties и Firebase JSON в архив не включены;
они используются из уже настроенного защищённого окружения Codemagic.

4. Отправить исправления в GitHub:

```powershell
git add Donatix_Flutter codemagic.yaml ANDROID_UPDATE_RU.md README_RU.md FCM_SETUP_RU.md DESIGN_PARITY_RU.md RELEASE_STATUS.md RELEASE_STATUS_v4.md verification.json
git commit -m "Fix Android UI, purchase recovery, push and release artifacts"
git push origin main
```

Не использовать `--force`, не удалять репозиторий и не отправлять в GitHub
upload keystore или пароли. Обычная отправка сохраняет историю изменений.

## Собрать Android release в Codemagic

- Обновить выбранную ветку `main`, выбрать новый коммит.
- Выбрать workflow **android-release** из `codemagic.yaml`.
- Уже настроенные `donatix_upload` и защищённая группа `donatix_android`
  с `GOOGLE_SERVICES_JSON` остаются нужны. Другие Firebase JSON/ключ подписи
  без причины заменять не нужно.
- Запустить сборку. Анализ и тесты проверяют продукт перед созданием
  `--release` APK/AAB; они не превращают выходные файлы в debug-сборку.
- После успешного завершения получить из Artifacts:

| Файл | Назначение |
|---|---|
| Donatix-release.apk | Установить на Android-телефон для проверки реального приложения |
| Donatix-release.aab | Загружать в Google Play Console |
| release-verification.json | Результат проверки подписи, applicationId и библиотек полученной сборки |

Все три файла собираются из постоянной папки `$CM_BUILD_DIR/donatix-release`.
Если проверка подписи/выравнивания или тесты завершатся ошибкой, сборка остановится
с причиной в логе. Зелёная сборка не подтверждает работу поставщиков/платежей.

## Когда владелец установит уже полученную папку на сервер

Сервер остаётся `https://donatix.tj`, база/аккаунты — существующие. Владелец
использует ранее отправленную инструкцию установки, обновляет зависимости
в существующем venv и перезапускает свой сервис. Мобильная конфигурация должна
возвращать успешный JSON с `version: 4`. Это версия API, а не номер Android-сборки.

В установленном приложении нажать «Повторить подключение» или открыть его заново.
Вход/каталог/баланс смогут использовать мобильный API. Google-кнопка появится,
если сервер вернёт `google_enabled: true`: для этого нужны действующие настройки
Google OAuth сайта. Установка файлов не создаёт Google Client ID/Secret.

Для Android push владелец добавляет серверный Firebase service-account JSON и
`DONATIX_FIREBASE_CREDENTIALS`, как описано в FCM_SETUP_RU.md. Этот ключ относится
к тому же Firebase-проекту, что JSON текущей Android-сборки; в APK он не нужен.
Apple-вход и APNs для Android не настраиваются. Затем пользователь разрешает
уведомления и включает их в приложении. Реальную доставку нужно проверить.

## До Google Play

Проверить release APK на настоящем телефоне, вход/Google, каталог/цены/баланс,
покупку/выдачу/возврат, чеки/ботов, потерю сети, повторные нажатия, уведомления,
удаление аккаунта и визуальное соответствие сайту. Использовать согласованные
владельцем операции: проверка денежных интеграций может действительно списывать деньги.

Материалы карточки уже находятся в `Play_Market` исходного проекта. Остаются
реальные скриншоты принятой сборки, утверждённые сведения о данных/политике и
поддержке, аккаунт проверки и оценка схемы внешних платежей для каталога/рынков.
В этом обновлении Google Play Billing не добавлен. Итоговый статус и границы
проверок указаны в RELEASE_STATUS.md; выпуск в магазин ещё не подтверждён.
