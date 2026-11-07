---
title: "Socrat Bot"
weight: 35
---

Команда `/unsubscribe` и кнопка **Unsubscribe** отключают алерты, сохраняя одобрение владельца.
Через `/start` → **Subscribe to alerts** одобренный пользователь подписывается снова без нового запроса
и без рассылки старых активных алертов. VPN-подписка и уровни при этом не меняются.

Socrat Bot — это Telegram-бот кластера. Через него приходят карточки алертов,
список людей на VPN и служебные ответы владельцу. Снаружи у бота открыт только
вход от Telegram. Всё остальное — внутренний API внутри кластера. Страница
собрана из `openapi.yaml` в репозитории бота, чтобы у процессов было одно место,
где видно, как к нему обращаться.

Алерт приходит одним запросом `POST /v1/messages`. Бот рисует карточку и
отправляет копию одобренным подписчикам с receive_level >= level инцидента. Повтор того же алерта правит
ту же карточку, в том числе когда он закрылся. Кнопки Mute ставят одну тишину
в Alertmanager и убирают кнопки у всех копий. Отдельный процесс может позже
найти эту карточку и дописать ссылку SPI через `POST /v1/cards/links`.

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

## Куда ходить

Внутренний адрес: `http://socrat-bot.socrat-bot.svc:8080`. Заголовок
`X-Internal-Token`. Публичный адрес `https://bot.socrat.alchemmist.xyz` принимает
только `POST /telegram/webhook` и заголовок `X-Telegram-Bot-Api-Secret-Token`.

Команды `/start`, `/mailing`, `/clients`, `/alerts` и `/alertlevel` в этот API не входят: их нажимают люди
в Telegram. Sudo и обновление 3X-UI живут в отдельных процессах и сюда тоже не входят.

## Методы

### `POST /v1/messages`

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
перед HTTP проверяется alerts_enabled. Изменение настроек не создаёт backlog.
Lease finite, ownership fenced; Telegram accepted-but-untracked может дать повтор после timeout,
exactly-once не гарантируется. Payload images не возвращается в list API.
Повтор того же алерта правит уже отправленную карточку. Resolved дописывается
в ту же строку, что и название. Следующее срабатывание после закрытия создаёт новое сообщение.

Доступ: internalToken.

Тело: `Message`.

- `200` — Сообщение отправлено или обновлено
- `400` — В запросе больше одного алерта или нет текста
- `401` — Нет или неверный X-Internal-Token

### `POST /v1/cards/links`

Дописать ссылку SPI к уже отправленной карточке

Для workflow, который разбирает алерт позже и заводит GitHub issue.
Бот находит все копии карточки и добавляет ссылку в строку ссылок, не убирая кнопки Mute.
Повторный вызов с тем же URL ничего не дублирует. Следующее обновление алерта сохраняет ссылку.
Если карточка ещё не отправлялась после появления этого метода, ответ 409:
нужно дождаться следующего обновления этого алерта.

Доступ: internalToken.

Тело: `CardLink`.

- `200` — Ссылка записана и проставлена в те копии, где это удалось
- `400` — Не переданы ни key, ни alert
- `401` — Нет или неверный X-Internal-Token
- `404` — Карточка с таким ключом ещё не отправлялась
- `409` — У карточки ещё не сохранён текст, повторите после следующего обновления алерта

### `PATCH /v1/messages/{message_id}`

Заменить текст одного сообщения

Доступ: internalToken.

Тело: `object`.

- `200` — Сообщение изменено
- `401` — Нет или неверный X-Internal-Token

### `POST /v1/link-codes`

Выдать одноразовый код привязки клиента панели

Код не добавляет человека в список VPN сам по себе. Список рассылки — это те, кто нажал «I use VPN».

Доступ: internalToken.

Тело: `object`.

- `200` — Код и команда /start, которой его нужно передать
- `401` — Нет или неверный X-Internal-Token

### `POST /v1/broadcasts`

Создать черновик рассылки

Обычная рассылка запускается командой /mailing в Telegram, не этим методом.

Доступ: internalToken.

Тело: `object`.

- `200` — Черновик создан и показан владельцу
- `401` — Нет или неверный X-Internal-Token

### `GET /v1/actions/{action_id}`

Статус кнопки в сообщении

Доступ: internalToken.

- `200` — Текущий статус действия
- `401` — Нет или неверный X-Internal-Token
- `404` — Действие не найдено

### `POST /telegram/webhook`

Принять обновление Telegram

Единственный публичный метод. Его вызывает Telegram, не внутренние workflow.

Доступ: telegramSecret.

Тело: `object`.

- `200` — Обновление принято
- `401` — Секрет webhook не совпал

### `GET /v1/cards`

Открытые карточки и их копии

По умолчанию только ещё открытые (firing). status=resolved или status=all меняет выбор.
text_stored=false значит текст карточки ещё не сохранён и ссылку или заметку писать рано (будет 409).

Доступ: internalToken.

- `200` — Карточки
- `401` — Нет или неверный X-Internal-Token

### `GET /v1/cards/item`

Одна карточка по ключу

Ключ берётся из списка карточек или из ответа POST /v1/messages.
В ключе бывают слэши, поэтому он передаётся параметром key, а не частью пути.
Возвращает и открытую, и уже закрытую карточку.

Доступ: internalToken.

- `200` — Карточка и её копии
- `401` — Нет или неверный X-Internal-Token
- `404` — Карточка с таким ключом не отправлялась

### `POST /v1/cards/notes`

Дописать или заменить короткую заметку на карточке

Одна строка без HTML, не длиннее 240 символов. Попадает во все копии и сохраняется
при следующем обновлении алерта. Повтор той же строки ничего не дублирует.

Доступ: internalToken.

Тело: `CardNote`.

- `200` — Заметка записана
- `400` — Нет ключа или заметка пустая либо слишком длинная
- `401` — Нет или неверный X-Internal-Token
- `404` — Карточка ещё не отправлялась
- `409` — У карточки ещё не сохранён текст

### `GET /v1/audience`

Кто на VPN и кто подписан на алерты

username без символа @. Его может не быть, тогда остаётся telegram_id.
Список VPN — только те, кто нажал «I use VPN», не все, кто нажал /start.

Доступ: internalToken.

- `200` — Два списка
- `401` — Нет или неверный X-Internal-Token

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

- `vpn`: список Person
- `alerts`: список Person

### Person

- `telegram_id`: integer, обязательно
- `username`: string
- `admin_level`: integer — Assigned authority tier; lower numbers have higher authority.
- `receive_level`: integer — New firing delivery threshold, greater than or equal to admin_level.
