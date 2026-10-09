# Подписанные Android APK/AAB через GitHub Actions

Workflow `Android release APK + AAB` запускается вручную из `main` на стандартной Linux-машине GitHub. Он использует Flutter 3.35.4, Java 17, Android SDK 36 и существующие скрипты проекта для настройки подписи и проверки APK/AAB. Сборка не публикует приложение в Google Play автоматически.

## Один раз добавить ключи

Откройте https://github.com/sattorovwv-oss/donatix/settings/secrets/actions и нажмите **New repository secret** для каждого значения:

| Name | Secret |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | Base64 содержимого **того же** файла `.jks`/`.keystore`, который уже использовали в Codemagic |
| `ANDROID_KEYSTORE_PASSWORD` | Прежний пароль хранилища — Keystore password |
| `ANDROID_KEY_ALIAS` | Прежнее имя ключа — Key alias |
| `ANDROID_KEY_PASSWORD` | Прежний пароль ключа — Key password |
| `GOOGLE_SERVICES_JSON` | Всё содержимое существующего Android-файла `google-services.json` |

Новый ключ создавать не нужно: существующие установки и обновления должны использовать согласованную подпись. Это клиентский Firebase JSON; серверный Firebase Admin JSON сюда не подходит. Ключ и пароли сохраняются через **Secrets**, а не в файлах репозитория.

### Скопировать JKS в Base64 без вывода ключа на экран

В обычном Windows PowerShell выполните код. Появится окно выбора файла: выберите прежний ключ подписи, который загружали в Codemagic. Команда скопирует Base64 в буфер обмена; затем вставьте его в поле **Secret** с именем `ANDROID_KEYSTORE_BASE64`.

```powershell
Add-Type -AssemblyName System.Windows.Forms
$picker = New-Object System.Windows.Forms.OpenFileDialog
$picker.Title = "Select the EXISTING Donatix upload/release keystore"
$picker.Filter = "Keystore (*.jks;*.keystore)|*.jks;*.keystore|All files (*.*)|*.*"
try {
    if ($picker.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($picker.FileName)) | Set-Clipboard
        Write-Host "Copied. Paste into the ANDROID_KEYSTORE_BASE64 repository secret."
    }
} finally { $picker.Dispose() }
```

Остальные пароли и alias — те же, что уже вводили для этого ключа. Значения Secrets скрыты после сохранения, поэтому GitHub и Codemagic не показывают сохранённые пароли для копирования. Используйте свои сохранённые значения.

## Запустить сборку

1. Откройте https://github.com/sattorovwv-oss/donatix/actions/workflows/android-release.yml.
2. Нажмите **Run workflow**, выберите `main`.
3. Поле номера версии можно оставить пустым: будет использовано `1000 + номер запуска`. Если в Google Play уже есть более высокий versionCode, укажите следующий свободный номер вручную.
4. Нажмите зелёную кнопку **Run workflow**. Откройте появившийся запуск, чтобы видеть шаги и лог.
5. После успешного завершения внизу страницы найдите **Artifacts → Donatix-Android-release-N-A** (номер запуска и попытки). Скачайте ZIP: внутри `Donatix-release.apk`, `Donatix-release.aab` и отчёт проверки.

APK предназначен для установки на телефон, AAB — для Google Play. Проверки выполняются перед упаковкой: сборка является `--release`, APK не допускает debug-подпись, APK и AAB должны иметь один сертификат, проверяются ARM64 и совместимость библиотек с размером страницы 16 KB.

Если первый шаг пишет `Add repository Actions secrets`, добавьте перечисленные значения и запустите workflow снова. Если произошла другая ошибка, откройте красный шаг и скопируйте его текст. Пароли и содержимое ключа в сообщения не отправляйте.

Сайт, серверное дополнение, функции приложения, Codemagic и iOS этой конфигурацией не изменяются. Полученные бинарные файлы требуют установки и проверки на Android-телефоне перед публикацией.
