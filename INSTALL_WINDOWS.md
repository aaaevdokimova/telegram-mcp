# Telegram MCP в Codex и ChatGPT Work на Windows

Нужны Windows x64, 64-битный PowerShell, интернет и Codex либо ChatGPT в режиме Work с доступом к локальным плагинам. Для Codex desktop и Work используется один и тот же плагин из персонального каталога; выбирать другой установщик не нужно. Права администратора и WSL не нужны. В этой версии нет поддержки нативного ARM64 и автоматических обновлений Windows.

Можно дать локальному агенту ссылку на этот репозиторий и написать: «Установи Telegram MCP для Codex на этом компьютере». Агент выполняет загрузку, проверку и настройку ниже. Войти в Telegram нужно самому в локальном окне; секреты не передаются нейронке. Обычный облачный чат без доступа к компьютеру не может выполнить локальную установку.

## Установка

1. Откройте [последний выпуск](https://github.com/prabchevski/telegram-mcp/releases/latest), скачайте `telegram-mcp-windows.zip` и `telegram-mcp-windows.zip.sha256` из одного выпуска. Сравните `Get-FileHash .\telegram-mcp-windows.zip -Algorithm SHA256` с контрольной суммой, затем распакуйте архив. Встроенный `install-windows.ps1` находится в папке `telegram-mcp-macos` внутри архива: имя папки сохранено для совместимости обновлений, пакет содержит оба установщика. В стандартном GitHub **Source code (zip)** этого PowerShell-файла нет.
2. Откройте PowerShell в распакованной папке с `install-windows.ps1` и выполните:

   ```powershell
   powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
   ```

   `Bypass` действует только на этот процесс. Установщик проверяет SHA-256 закреплённого uv, устанавливает Python 3.13 и зависимости из `uv.lock`, затем проверяет версию и commit TDLib.
3. В этом же локальном окне введите свой `api_id` и `api_hash` с https://my.telegram.org. Подтвердите QR-вход в Telegram → Настройки → Устройства. Код входа и пароль 2FA вводятся скрыто. Не отправляйте их в чат ChatGPT.
4. Перезапустите приложение Codex или ChatGPT. В **Codex** либо **Work** откройте **Plugins**, выберите источник **Personal** (или название вашего существующего персонального каталога) и установите **Telegram MCP**. Начните новый чат с включённым плагином. В актуальном Codex CLI тот же каталог доступен через `/plugins`.
5. Например: «Найди в моём Telegram сообщения про встречу в пятницу».

Первичная установка предлагает 15 инструментов. Отправка сообщений и изменение черновиков выключены. Найденные сообщения и медиа передаются выбранному AI-клиенту при использовании инструментов. Секретные чаты не поддерживаются.

Этот пакет регистрирует локальный плагин. Он не опубликован в общем каталоге OpenAI. Доступ к плагинам также может ограничиваться политикой вашего рабочего пространства. Автотесты проверяют Windows-установку, созданную конфигурацию и MCP; работу интерфейса Codex/ChatGPT и вход реального аккаунта нужно проверить на целевом компьютере. Не добавляйте второй сервер вручную в `config.toml` поверх этого плагина.

Сначала агент ищет по доступной истории вашего аккаунта и подпискам. Затем
может предложить отдельный поиск публичных публикаций, включая каналы, на которые
вы не подписаны. Перед каждым новым запросом он обязан вызвать
`telegram_get_public_search_quota(query)`: проверка показывает текущую квоту
аккаунта и сама не расходует попытку поиска. Фиксированное число попыток в день
не предполагается.

Агент должен объяснить, сколько бесплатных попыток осталось, нужно ли ждать,
расходует ли предложенный запрос одну попытку или уже доступен бесплатно, и
спросить: «Могу так поискать?» Поиск начинается только после вашего ответа.
Например: «В ваших подписках нашёл эти публикации. Могу также поискать по запросу
“искусственный интеллект” в публичных каналах вне подписок. Осталось N бесплатных
попыток, этот запрос использует одну. Выполнить?» Число и условия берутся из
текущего ответа Telegram.

Сервер требует `confirmation_token` из проверки квоты и `user_confirmed=true`.
Токен действует пять минут, однократно, для этого аккаунта, запроса и состояния
квоты. При изменении квоты или истечении срока агент проверяет и спрашивает
заново. Флаг подтверждения — сообщение агента о вашем согласии: сервер сам не
видит диалог с человеком. Инструкции передаются через MCP всем клиентам.
Продолжение одобренного поиска через `next_cursor` бесплатно и не требует нового
согласия; новая формулировка запроса требует новой проверки и разрешения.

Оплата Stars не поддерживается и не предлагается: поиск всегда передаёт
`star_count=0`, без платных повторов или покупок. Лимит, отсутствие согласия или
ошибка возвращаются отдельно от успешного поиска без совпадений.

## Где находятся программа и данные

- Программа: `%LOCALAPPDATA%\Programs\TelegramMCP`, отдельная папка для каждой установленной версии.
- Локальный каталог: `%USERPROFILE%\.agents\plugins\marketplace.json`; плагин: `%USERPROFILE%\plugins\telegram-mcp-work`.
- Профиль: `%LOCALAPPDATA%\TelegramSearchMCPShared`, только для текущего пользователя.
- `api_hash` и ключ шифрования базы: Windows Credential Manager. Сеанс Telegram остаётся на этом компьютере.

Не копируйте профиль, базу, секреты или установленное окружение другому человеку. Передавайте только чистый исходный архив. Каждый входит в собственный Telegram.

## Проверка и управление

```powershell
$tg = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\TelegramMCP\tgsearch.ps1'
& $tg doctor --connect
& $tg service status
& $tg service stop
```

Вместо удаления `tdlib.lock` остановите сервис указанной командой и дождитесь завершения текущей операции. Один общий сервис обслуживает все окна и чаты.

При необходимости отправки:

```powershell
& $tg sending on
# Перезапустите Codex или ChatGPT и начните новый чат: теперь доступны 19 инструментов.
& $tg sending off
```

Отправка требует вашего явного указания адресата и содержимого; подготовка, подтверждённая отправка и проверка статуса используют один `draft_id`. При `pending`/`unknown` не создавайте повторную отправку.

## Обновление

Скачайте новую проверенную версию и снова запустите `install-windows.ps1`. Старые версии и вход Telegram сохраняются. Новые процессы используют новую версию; активная операция общего сервиса завершается перед переключением. Перезапустите приложение после обновления.

Установщик сохраняет остальные плагины и настройки каталога. Если вы удалили или изменили регистрацию Telegram, обновление её не восстановит; результат `preserved_user_changes` сообщает об этом. Дневного автообновления Windows пока нет.

## Удаление

Отключите/удалите Telegram MCP в Plugins. Выполните `service stop`. Удалите только запись `telegram-mcp-work` из персонального `marketplace.json` и папку `%USERPROFILE%\plugins\telegram-mcp-work`, затем папку программы. Сохранённый профиль и Credential Manager остаются для переустановки.

Для полного отзыва доступа завершите соответствующий сеанс в официальном Telegram → Настройки → Устройства. После остановки сервиса можно удалить `%LOCALAPPDATA%\TelegramSearchMCPShared` и принадлежащие ему записи `local.unofficial-telegram-search-mcp-shared` в Windows Credential Manager. Не удаляйте записи других программ.

## Проверка установщика без входа

```powershell
.\install-windows.ps1 -PrepareOnly -InstallDir 'C:\path\to\temporary\application' -MarketplacePath 'C:\path\to\temporary\home\.agents\plugins\marketplace.json'
```

Этот режим предназначен для CI и проверки пакета. Он не авторизует Telegram. Не используйте временный каталог плагинов для обычной установки: Codex и ChatGPT автоматически обнаруживают каталог своего пользователя.

## Для агента, выполняющего установку

Сначала прочитайте [INSTALL.md](INSTALL.md). Определите платформу и клиент, а также наличие `%LOCALAPPDATA%\Programs\TelegramMCP\windows-install.json` до установки: существующая установка требует сохранения пользовательского выбора плагинов. Не запускайте macOS-установщик или WSL для нативного Windows-клиента.

Этот PowerShell-блок скачивает ZIP и SHA-256 из одного конкретного последнего выпуска, сверяет их и распаковывает пакет в отдельную папку. Он не запускает установку и не запрашивает секреты:

```powershell
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/prabchevski/telegram-mcp/releases/latest'
$assets = @($release.assets | Where-Object { $_.name -in @('telegram-mcp-windows.zip', 'telegram-mcp-windows.zip.sha256') })
if ($assets.Count -ne 2) { throw 'The release is missing its Windows archive or checksum.' }
$setup = Join-Path ([IO.Path]::GetTempPath()) ('telegram-mcp-download-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $setup | Out-Null
foreach ($asset in $assets) {
    Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile (Join-Path $setup $asset.name)
}
$archive = Join-Path $setup 'telegram-mcp-windows.zip'
$checksum = (Get-Content -LiteralPath "$archive.sha256" -Raw).Trim()
if ($checksum -notmatch '^([a-fA-F0-9]{64})\s+telegram-mcp-windows\.zip$') { throw 'Invalid checksum file.' }
$expected = $Matches[1]
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw 'Archive checksum mismatch.' }
Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $setup 'extracted')
$installer = Join-Path $setup 'extracted\telegram-mcp-macos\install-windows.ps1'
if (-not (Test-Path -LiteralPath $installer)) { throw 'The release has no Windows installer.' }
Write-Output $installer
```

Запустите найденный файл в **интерактивном локальном** PowerShell:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer
```

Если инструмент агента не позволяет пользователю самому вводить секреты, подготовьте пакет через `-PrepareOnly`, затем откройте локальный терминал для `tgsearch.ps1 auth` и `tgsearch.ps1 doctor --connect`. Не просите прислать `api_hash`, код или пароль в чат и не сохраняйте их в командах, логах или файлах инструкций. `-PrepareOnly` ещё не означает готовность Telegram.

На **первой установке**, если доступен Codex CLI и `codex plugin add --help` подтверждает эту команду, агент может завершить включение плагина без ручного редактирования `config.toml`:

```powershell
$marketplace = Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.agents\plugins\marketplace.json') -Raw | ConvertFrom-Json
if ($marketplace.name -notmatch '^[A-Za-z0-9_-]+$') { throw 'Invalid marketplace name.' }
codex plugin add "telegram-mcp-work@$($marketplace.name)" --json
if ($LASTEXITCODE -ne 0) { throw 'Plugin activation failed; use Plugins in the app.' }
```

Если такой команды нет, используйте **Plugins → Personal → Telegram MCP** в приложении или `/plugins` в поддерживающем плагины Codex CLI. Не устанавливайте дополнительный CLI только ради этой команды. Идентификатор `telegram-mcp-work` сохранён для совместимости; этот же плагин используется в Codex. При обновлении не включайте отключённый/удалённый пользователем плагин заново и не восстанавливайте запись при `preserved_user_changes` без отдельного указания пользователя.

После перезапуска проверьте появление 15 инструментов и выполните `doctor --connect`. Для проверки установки не нужно читать переписку или расходовать публичную квоту. Если пользователь отдельно попросил пробный поиск, начните с истории аккаунта. Сообщайте отдельно, что установлено и проверено, а что ещё ждёт входа Telegram, перезапуска приложения или включения плагина.

## Официальные источники

- [Локальные MCP-серверы ChatGPT desktop](https://learn.chatgpt.com/docs/extend/mcp)
- [Плагины и локальные каталоги](https://developers.openai.com/plugins/build/plugins)
- [Установка плагинов в Codex CLI через /plugins](https://developers.openai.com/learn/developers-codex-plugin#install-the-plugin)
- [ChatGPT desktop на Windows](https://learn.chatgpt.com/docs/windows/windows-app)
