---
title: "Socrat Bot"
weight: 35
---

Команда `/unsubscribe` и кнопка **Unsubscribe** отключают алерты, сохраняя одобрение владельца.
Через `/start` → **Alerts** одобренный пользователь подписывается снова без нового запроса
и без рассылки старых активных алертов. Независимый интерес **VPN** и уровни при этом не меняются.

`/start` приветствует **Welcome to Socrat Bot!** и предлагает **I’m interested in…**.
Независимые флажки **VPN**, **Alerts**, **LLM** сохраняются в `user_interests`; можно выбрать несколько.
`/mailing` сначала предлагает владельцу группу **VPN / Alerts / LLM**, затем текст и время.
Группа сохраняется в рассылке; получатели определяются по текущим интересам при отправке.
Для Alerts также нужно одобрение. Старые рассылки сохраняют группу VPN.
LLM управляет LLM-рассылками, не выдаёт API-ключ.
VPN управляет VPN-рассылками, не выдаёт и не отзывает VPN-доступ. Alerts требует одобрения
владельца; до него интерес остаётся выбранным, но доставка выключена.
Роль `vpn` больше не используется: роли описывают полномочия, интересы — функции.

Socrat Bot — это Telegram-бот кластера. Через него приходят карточки алертов,
список людей на VPN и служебные ответы владельцу.

Алерт приходит одним запросом `POST /api/v1/messages`. Бот рисует карточку и
отправляет копию одобренным подписчикам с receive_level >= level инцидента. Повтор того же алерта правит
ту же карточку, в том числе когда он закрылся. Кнопки Mute ставят одну тишину
в Alertmanager и убирают кнопки у всех копий. Отдельный процесс может позже
найти эту карточку и дописать ссылку SPI через `POST /api/v1/cards/links`.

`ALERT_LEVELS=10,20`: инцидент начинает с максимального уровня. Escalate под Mute
переводит его ровно на один уровень выше по полномочиям (к меньшему числу).
Получение требует `receive_level >= level`, полномочия эскалации —
`admin_level <= level`. `/alerts` показывает настройки в личном чате,
`/alertlevel TELEGRAM_ID N` меняет admin tier только владельцем. Изменение
receive mode не переигрывает существующие firing; новые получатели появляются
лишь в новой generation или при явной эскалации. Уже доставленные копии
продолжают обновляться, включая Resolved, независимо от receive mode.
Начальная аудитория и delivery intents сохраняются атомарно до HTTP. Все обновления
карточек используют общий DB lease и content revisions, включая SPI, notes и mute.
Эскалация сохраняет actor/time и durable outbox; Telegram timeout может дать
повтор доставки (at least once).

Нативный снимок Grafana содержит дополнительную ссылку **Chart** на снятую панель
с фиксированным окном и переменными. Для произвольных inline images источник не
угадывается. Ключ провайдера `nodes` сохранён, видимая подпись — **Node**.

## Куда ходить

**Оператор:** откройте [консоль токенов](https://bot.socrat.alchemmist.xyz/).
Вход через Authentik: достаточно членства в `socrat-bot-admin` **или** `homelab-owner`.
Интерфейс на английском. Укажите **Token name**, **Expires in (days)** и **Permissions**.
Для обычных сообщений выберите `messages:send` и перечислите разрешённые **Chat IDs**.
Нажмите **Issue token** и сразу сохраните секрет: повторно он не показывается.
Для ротации выдайте новый токен, переключите клиент и нажмите **Revoke** у старого.
**Sign out** завершает сеанс консоли, но не отзывает токены интеграций.

**Интеграция:** базовый URL — `https://bot.socrat.alchemmist.xyz/api/v1`;
передавайте `Authorization: Bearer TOKEN` в заголовке, не в URL. Каждому клиенту — свой токен.
Два workflow n8n используют отдельные `messages:send` + `messages:advanced`, без `messages:edit`.
SPI использует только `cards:read` и `cards:link`. [Таблица разрешений](https://github.com/alchemmist/socrat-bot/blob/main/docs/public-api.md).

Старые `/v1/*` отключены. `POST /telegram/webhook` по-прежнему проверяет
`X-Telegram-Bot-Api-Secret-Token`; `/healthz` доступен только внутри кластера.

Команды `/start`, `/mailing`, `/users`, `/alerts` и `/alertlevel` в этот API не входят: их нажимают люди
в Telegram. Sudo и обновление 3X-UI живут в отдельных процессах и сюда тоже не входят.

## Методы

### `POST /api/v1/messages`

Отправить сообщение или карточку алерта

Этот метод вызывает workflow алертов. Один запрос — один алерт.
Новая firing generation отправляется одобренным подписчикам с receive_level >= level.
Начальный level — максимум ALERT_LEVELS (по умолчанию 20); Escalate понижает на один tier.
Повторы/Resolved обновляют только доставленные копии текущей generation, даже после disable.
Изменение receive_level не переигрывает активные инциденты; эскалация явно допускает новых получателей.
Начальный снимок получателей и delivery intents коммитятся вместе с инцидентом до Telegram HTTP.
Ошибка первой отправки или restart не теряет исходных получателей; поздние подписчики не добавляются.
Webhook, outbox, SPI, notes и mute используют общий DB lease на копию и content revisions;
новые тексты/аннотации не теряются при запросе в полёте. DB transaction во время HTTP не удерживается.
Без получателей возвращается copies: [], без fallback на chat_id.
Если получатели есть, но доставка никому не завершена, возвращается 502 с durable retry.
При частичном успехе copies содержит завершённые копии.
Поле chat_id в этом случае не выбирает получателей.
images работает с card, alert и singleton alerts, но не с обычным text (400).
До 3 inline PNG/JPEG, strict canonical base64, до 4 MiB decoded каждый (12 MiB суммарно).
URL не принимаются и не загружаются; скриншоты генерирует вызывающий процесс.
Фото отправляется multipart sendPhoto ответом на исходную текстовую карточку,
allow_sending_without_reply=false. Текст/кнопки/ID карточки не меняются.
attachments содержит status pending/sent/superseded по каждому получателю и слоту; успех текста
не означает успех фото. Ошибки фото сохраняют независимый durable retry после restart.
Изменение bytes в слоте редактирует прежнее фото (editMessageMedia); ошибка edit не вызывает resend.
Одинаковые bytes дедуплицируются. Отсутствующее/null/пустое images ничего не удаляет;
более короткий список сохраняет прежние оставшиеся слоты. Новая generation начинает без фото.
Фото получают только исходные admitted intents текущей generation и новые intents эскалации;
перед HTTP проверяется интерес alerts и сохранённое одобрение владельца. Изменение настроек не создаёт backlog.
Lease finite, ownership fenced; Telegram accepted-but-untracked может дать повтор после timeout,
exactly-once не гарантируется. Payload images не возвращается в list API.
Повтор того же алерта правит уже отправленную карточку. Resolved дописывается
в ту же строку, что и название. Следующее срабатывание после закрытия создаёт новое сообщение.

Доступ: bearerToken.

Тело: `Message`.

- `200` — Сообщение отправлено или обновлено
- `400` — В запросе больше одного алерта или нет текста
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату

### `POST /api/v1/cards/links`

Дописать ссылку SPI к уже отправленной карточке

Для workflow, который разбирает алерт позже и заводит GitHub issue.
Бот находит все копии карточки и добавляет ссылку в строку ссылок, не убирая кнопки Mute.
Повторный вызов с тем же URL ничего не дублирует. Следующее обновление алерта сохраняет ссылку.
Если карточка ещё не отправлялась после появления этого метода, ответ 409:
нужно дождаться следующего обновления этого алерта.

Доступ: bearerToken.

Тело: `CardLink`.

- `200` — Ссылка записана и проставлена в те копии, где это удалось
- `400` — Не переданы ни key, ни alert
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату
- `404` — Карточка с таким ключом ещё не отправлялась
- `409` — У карточки ещё не сохранён текст, повторите после следующего обновления алерта

### `PATCH /api/v1/messages/{message_id}`

Заменить текст одного сообщения

Доступ: bearerToken.

Тело: `object`.

- `200` — Сообщение изменено
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату

### `POST /api/v1/link-codes`

Выдать одноразовый код привязки клиента панели

Код не добавляет человека в список VPN сам по себе. Список VPN-рассылки — те, кто выбрал интерес VPN.

Доступ: bearerToken.

Тело: `object`.

- `200` — Код и команда /start, которой его нужно передать
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату

### `POST /api/v1/broadcasts`

Создать черновик рассылки

Обычная рассылка запускается командой /mailing в Telegram, не этим методом.

Доступ: bearerToken.

Тело: `object`.

- `200` — Черновик создан и показан владельцу
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату

### `GET /api/v1/actions/{action_id}`

Статус кнопки в сообщении

Доступ: bearerToken.

- `200` — Текущий статус действия
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату
- `404` — Действие не найдено

### `POST /telegram/webhook`

Принять обновление Telegram

Этот метод вызывает Telegram; клиентский Bearer-токен здесь не используется.

Доступ: telegramSecret.

Тело: `object`.

- `200` — Обновление принято
- `401` — Секрет webhook не совпал

### `GET /api/v1/cards`

Открытые карточки и их копии

По умолчанию только ещё открытые (firing). status=resolved или status=all меняет выбор.
text_stored=false значит текст карточки ещё не сохранён и ссылку или заметку писать рано (будет 409).

Доступ: bearerToken.

- `200` — Карточки
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату

### `GET /api/v1/cards/item`

Одна карточка по ключу

Ключ берётся из списка карточек или из ответа POST /api/v1/messages.
В ключе бывают слэши, поэтому он передаётся параметром key, а не частью пути.
Возвращает и открытую, и уже закрытую карточку.

Доступ: bearerToken.

- `200` — Карточка и её копии
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату
- `404` — Карточка с таким ключом не отправлялась

### `POST /api/v1/cards/notes`

Дописать или заменить короткую заметку на карточке

Одна строка без HTML, не длиннее 240 символов. Попадает во все копии и сохраняется
при следующем обновлении алерта. Повтор той же строки ничего не дублирует.

Доступ: bearerToken.

Тело: `CardNote`.

- `200` — Заметка записана
- `400` — Нет ключа или заметка пустая либо слишком длинная
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату
- `404` — Карточка ещё не отправлялась
- `409` — У карточки ещё не сохранён текст

### `GET /api/v1/audience`

Получатели групп VPN, Alerts, LLM и Cluster

username без символа @. Его может не быть, тогда остаётся telegram_id.
Списки VPN, LLM и Cluster — по выбранным интересам; Alerts — выбранный интерес и одобрение владельца.

Доступ: bearerToken.

- `200` — Три списка по группам
- `401` — Bearer-токен отсутствует, недействителен, отозван или истёк
- `403` — Нет нужного разрешения или доступа к чату

### `GET /healthz`

Процесс жив

Доступ: без заголовка.

- `200` — Процесс отвечает

## Тела запросов

### Message

- `chat_id`: integer, обязательно — Чат для обычного сообщения. Для алерта получатели определяются incident generation и receive_level; fallback нет.
- `text`: string
- `alert`: Alert
- `alerts`: список Alert
- `card`: Card
- `actions`: список object
- `images`: список AlertImage
- `parse_mode`: string

### AlertImage

- `data_base64`: string, обязательно — Canonical strict base64; decoded limit 4 MiB, matching PNG/JPEG signature required.
- `mime_type`: string, обязательно
- `filename`: string

### Alert

- `status`: string
- `labels`: object
- `annotations`: object
- `fingerprint`: string
- `spi_episode`: string — Authoritative SPI episode digest. Creates an immutable episode-specific card key; firing/resolved share it, later firings use a new key.

### Card

- `key`: string, обязательно
- `title`: string, обязательно
- `where`: string
- `summary`: string
- `status`: string
- `labels`: object
- `links`: список string — Имена провайдеров ссылок, например alert, rancher, logs.
- `hrefs`: список object

### CardLink

- `url`: string, обязательно
- `label`: string
- `key`: string — Ключ карточки, если он уже известен.
- `alert`: Alert
- `revision`: integer — Monotonic desired-link revision; stale revisions return409. Requires expected_copies.
- `expected_copies`: список object — Optional atomic guard: all listed chat/message identities must still belong to this card, otherwise409.

### CardLinkResult

- `key`: string
- `updated`: список object
- `pending`: список object — Copies not acknowledged by Telegram; retry even on HTTP200.

### SentMessage

- `attachments`: список object — Present when retained images exist; text copies do not imply photo success. Slots are zero-based. Pending includes failed/disabled recipients; no bytes are exposed.
- `id`: integer
- `chat_id`: integer
- `replayed`: boolean
- `key`: string — Ключ карточки. Для обычного сообщения нет.
- `copies`: список object — Завершённые копии. Пусто, если нет допустимых получателей; незавершённая доставка сохраняется для durable retry.
- `episode`: string
- `immutable_key`: boolean

### CardNote

- `text`: string, обязательно
- `key`: string
- `alert`: Alert

### CardSummary

- `key`: string, обязательно
- `generation`: integer
- `level`: integer
- `escalation_history`: список object
- `status`: string, обязательно
- `text_stored`: boolean
- `spi_url`: string
- `spi_label`: string
- `note`: string
- `copies`: список object, обязательно

### CardList

- `cards`: список CardSummary

### Audience

- `cluster`: список Person
- `llm`: список Person
- `vpn`: список Person
- `alerts`: список Person

### Person

- `telegram_id`: integer, обязательно
- `username`: string
- `admin_level`: integer — Assigned authority tier; lower numbers have higher authority.
- `receive_level`: integer — New firing delivery threshold, greater than or equal to admin_level.
