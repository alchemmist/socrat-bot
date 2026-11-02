# socrat-bot

Управляющий контур домашнего кластера Socrat. Один образ, три процесса.

## Процессы

- `python -m socrat_bot` — Telegram-бот и HTTP API управляющего контура. Эта команда стоит в образе по умолчанию, процесс слушает порт 8080.
- `python -m uvicorn socrat_bot.broker:api --host 0.0.0.0 --port 8080` — брокер sudo. Принимает запрос клиента, спрашивает владельца в Telegram и отдаёт разрешение на запуск.
- `python -m socrat_bot.updater` — проверка и обновление образа панели 3X-UI.

## Переменные окружения

Секреты остаются вне git. В репозитории только имена; значения задаются окружением процесса или хранилищем секретов.

Бот:

- `DATABASE_URL`
- `PORT`
- `ALEMBIC_INI`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_OWNER_ID`
- `INTERNAL_TOKEN`
- `ALERTMANAGER_URL`
- `BROADCAST_PAUSE_SECONDS`
- `XUI_UPDATER_URL`

Брокер:

- `DATABASE_URL`
- `INTERNAL_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_ID`
- `SUDO_TTL_MINUTES`

Обновлятор:

- `PORT`
- `XUI_PANEL_PORT`
- `XUI_HEALTH_PATH`
- `KUBERNETES_SERVICE_HOST`
- `KUBERNETES_SERVICE_PORT`
- `XUI_UPGRADE_TIMEOUT_SECONDS`
- `XUI_POLL_SECONDS`

Клиент `socrat-sudo`:

- `BROKER_URL`
- `INTERNAL_TOKEN`
- `SUDO_WAIT_SECONDS`

Для выкладки образа в GitHub Actions живут секреты `HARBOR_USERNAME`, `HARBOR_PASSWORD` и `HOMELAB_DEPLOY_KEY`.

## socrat-sudo

Команда `socrat-sudo` стоит рядом с `sudo`. Она отправляет аргументы брокеру и вызывает `sudo` с теми же аргументами только после подтверждения. sudoers пока не ужат: прямой `sudo` по-прежнему доступен, клиент добавляет подтверждение в Telegram и не заменяет политику прав.
