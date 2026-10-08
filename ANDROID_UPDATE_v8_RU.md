# Обновление Android Donatix v8

Это обновление Flutter-клиента. На сервер его загружать не нужно:
работающее дополнение Google/FCM остаётся установленным. Исходники сайта,
его база, конфигурация и iOS-папка этим обновлением не меняются.
Удаление аккаунта отложено по решению владельца.

## Что исправлено

- «Купить быстро»: полное название из текста сайта вместо FF/PM из заглушки
  изображения. Карточка расширена и увеличивает высоту под полный текст,
  включая увеличенный шрифт телефона. Ссылки и регион покупки сохраняются.
- «Все»: категории собираются из всех разделов каталога. Страница сайта
  «Все» содержит первые 500 пакетов, поэтому она больше не используется
  как источник списка игр. Telegram и Steam открывают свои экраны покупки.
- Каталог: полные названия без сокращения, названия регионов из данных товара.
- Админка → Каталог: на телефоне видны название, ID, закупка и продажная цена
  каждого уровня, а также действие скрытия/показа. Кнопка «Изменить наценки»
  открывает действующий раздел настроек.
- Наценки: дробный ввод с запятой, проверка допустимых значений, пустое поле
  для наследования наценки уровня. После обновления страницы форма заново
  читает поля и переключатели, включая неизменённые настройки сайта.

Закупочную цену задаёт поставщик. Владелец меняет продажные цены через наценки
уровней, разделов и персональные наценки клиентов — так же, как на сайте.
Отдельного изменения закупочной цены каждого товара исходный сайт не имеет.
При сохранении наценок приложение отправляет существующую форму сайта;
сохранённые настройки применяются на действующем сервере.

## Установить в свой Git-репозиторий

Распаковать архив так, чтобы внутри `Donatix_Android_Update_v8` находились
`Donatix_Flutter` и `codemagic.yaml`. Сохранить свои неотправленные изменения
отдельным коммитом или копией, затем выполнить в PowerShell:

```powershell
$repoPath = "C:\Users\user\Desktop\Donatix_Android_iOS_v4"
$updatePath = "C:\Users\user\Desktop\Donatix_Android_Update_v8"
git -C $repoPath rev-parse --is-inside-work-tree
if ($LASTEXITCODE -ne 0) { throw "Укажите существующий Git-репозиторий" }
if (-not (Test-Path "$updatePath\Donatix_Flutter\pubspec.yaml")) { throw "Проверьте папку обновления" }

Copy-Item "$updatePath\Donatix_Flutter\lib\*" -Destination "$repoPath\Donatix_Flutter\lib" -Recurse -Force
Copy-Item "$updatePath\Donatix_Flutter\test\catalog_prices_test.dart" -Destination "$repoPath\Donatix_Flutter\test" -Force
Copy-Item "$updatePath\Donatix_Flutter\pubspec.yaml", "$updatePath\Donatix_Flutter\codemagic.yaml" -Destination "$repoPath\Donatix_Flutter" -Force
Copy-Item "$updatePath\codemagic.yaml", "$updatePath\ANDROID_UPDATE_v8_RU.md" -Destination $repoPath -Force
Set-Location $repoPath
git diff --stat
git add Donatix_Flutter/lib Donatix_Flutter/test/catalog_prices_test.dart Donatix_Flutter/pubspec.yaml Donatix_Flutter/codemagic.yaml codemagic.yaml ANDROID_UPDATE_v8_RU.md
git commit -m "Fix Android catalogue names and admin pricing"
git push origin main
```

Не создавать новый репозиторий через `git init`, не применять `--force`.
Серверные папки и файлы iOS эти команды не копируют. Архив не содержит
ключа подписи, паролей и Firebase JSON: используются уже настроенные секреты Codemagic.

## Собрать и установить

В Codemagic выбрать новый коммит ветки `main` и workflow `android-release`.
Получить подписанные **Donatix-release.apk** и **Donatix-release.aab**.
Сохранить существующий ключ `donatix_upload` и Firebase-настройки.
Номер сборки должен быть выше установленной версии; при необходимости задать
`DONATIX_BUILD_NUMBER` больше текущего versionCode.

Установить APK поверх предыдущего с той же подписью. Проверить Free Fire СНГ,
Free Fire Индонезия, PUBG Mobile, раздел «Все», поиск и переходы в Telegram/Steam.
Войти владельцем, открыть Админка → Каталог → Изменить наценки, сохранить
согласованную наценку и сверить цены в приложении и на сайте.

До публикации отдельно остаётся приёмка реальной оплаты/выдачи и согласованный
способ удаления аккаунта. Этот выпуск не подтверждает готовность к Google Play.
