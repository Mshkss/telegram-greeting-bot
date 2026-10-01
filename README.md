# Простой бот с приветствием

`bot.py` отвечает «Привет! 👋» на входящие сообщения пользователя, включая
`/start`, фото и стикеры. Python-процесс запускается локально или на VPS;
Cloudflare Worker из соседнего проекта `../telegram-api-proxy` проксирует запросы Telegram API.
Эта папка разворачивается самостоятельно; файлы Worker для сборки контейнера не нужны.

## Запуск на сервере в Docker

На сервере нужны Docker Engine и Docker Compose v2. Скопируйте проект
или клонируйте репозиторий, затем в его каталоге:

```bash
# Только если .env ещё нет:
cp -n .env.example .env
nano .env
chmod 600 .env
docker compose up -d --build
docker compose logs --tail=100 -f bot
```

Заполните `BOT_TOKEN`, `TELEGRAM_API_BASE` и `TELEGRAM_PROXY_SECRET`.
Секрет прокси должен совпадать с `PROXY_SECRET` в Cloudflare Worker.
`.env` передаётся контейнеру при запуске и не попадает в образ или контекст
сборки. Для переноса без Git достаточно `Dockerfile`, `compose.yaml`,
`.dockerignore`, `requirements.txt`, `bot.py` и `.env.example`.

Контейнер работает от отдельного пользователя, автоматически перезапускается
после сбоя и перезагрузки сервера (если Docker запускается при загрузке ОС).
Входящие порты, домен и HTTPS-сертификат на VPS не нужны: бот использует
исходящие запросы к существующему Cloudflare Worker. Логи ограничены ротацией.
Перед запуском на сервере остановите локального бота с тем же токеном.

```bash
docker compose ps                    # состояние контейнера
docker compose down                  # остановить и удалить контейнер
docker compose up -d --build          # применить обновления кода или .env
```

Статус `running` сам по себе не подтверждает доступность Telegram:
проверьте логи и отправьте боту сообщение. Код Worker деплоится отдельно
в Cloudflare, в контейнер входит только Python-бот.

## Локальный запуск без Docker

Нужен Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Только если .env ещё нет:
cp -n .env.example .env
python bot.py
```

В `.env` заполните `BOT_TOKEN`, `TELEGRAM_API_BASE` (HTTPS-адрес Worker без
пути) и `TELEGRAM_PROXY_SECRET`. Последнее значение должно совпадать с
**PROXY_SECRET** в Cloudflare → Worker → Settings → Variables and Secrets.
Имеющийся `.env` не перезаписывайте. Остановка — Ctrl+C.

Сессия aiogram добавляет `Authorization: Bearer <секрет>` ко всем запросам,
включая long polling и скачивание файлов. Worker проверяет его и удаляет перед
отправкой запроса в Telegram. `X-Proxy-Secret` текущий код не использует.
При отсутствии секрета Worker отклоняет все запросы.

## Нюансы Cloudflare и Telegram

- Worker — HTTP-прокси; постоянно работает именно Python-процесс. При выключении
  компьютера или остановке процесса ответы прекращаются. Для постоянной работы
  запустите этот процесс на VPS под менеджером сервисов.
- Используется long polling с ожиданием до 30 секунд. Ожидание сети не считается
  CPU time Worker; HTTP Worker может ждать, пока подключён клиент.
- Workers Free: 100 000 запросов в сутки на аккаунт. В простое polling раз в
  30 секунд — примерно 2 880 запросов в сутки на бота; сообщения увеличивают
  число запросов, включая отдельный `sendMessage` на каждый ответ.
- Для одного токена запускайте один polling-процесс. Webhook несовместим с
  `getUpdates`: если он установлен, бот остановится с пояснением, не удаляя его
  и накопленные сообщения автоматически.
- В личке бот получает сообщения пользователя; в группах набор доступных
  сообщений зависит от privacy mode и прав бота. Ответы отправляются на новые
  `message`, не на правки или посты каналов. Ботам не отвечает.
- Cloudflare расшифровывает запросы и видит токен и содержимое сообщений.
  Не включайте логирование URL, заголовков и тела. `.env` не коммитится.
- Ошибка 401 от Worker обычно означает несовпадение секретов. Недоступность
  домена Worker из конкретной сети нужно проверять именно с будущего VPS.
- Изменение Python-кода не требует деплоя Worker. Для изменения `../telegram-api-proxy/src/index.js`
  нужен отдельный деплой (`npx wrangler deploy` или настроенная GitHub-сборка).

Документация: [aiogram: свой API-сервер](https://docs.aiogram.dev/en/latest/api/session/custom_server.html),
[лимиты Workers](https://developers.cloudflare.com/workers/platform/limits/),
[секреты Workers](https://developers.cloudflare.com/workers/configuration/secrets/),
[Telegram: получение обновлений](https://core.telegram.org/bots/api#getting-updates).


## Проверка

```bash
python -m unittest discover -s tests -v
docker compose config --quiet
```
