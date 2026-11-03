"""Render openapi.yaml into the public wiki page. Run from the bot repo."""

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "openapi.yaml"
TARGET = pathlib.Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else "/Users/antonmoss/code/homelab/wiki/content/platform/socrat-bot.md"
)

INTRO = """---
title: "Socrat Bot"
weight: 35
---

Socrat Bot — это Telegram-бот кластера. Через него приходят карточки алертов,
список людей на VPN и служебные ответы владельцу. Снаружи у бота открыт только
вход от Telegram. Всё остальное — внутренний API внутри кластера. Страница
собрана из `openapi.yaml` в репозитории бота, чтобы у процессов было одно место,
где видно, как к нему обращаться.

Алерт приходит одним запросом `POST /v1/messages`. Бот рисует карточку и
отправляет копию каждому, кто подписан на алерты. Повтор того же алерта правит
ту же карточку, в том числе когда он закрылся. Кнопки Mute ставят одну тишину
в Alertmanager и убирают кнопки у всех копий. Отдельный процесс может позже
найти эту карточку и дописать ссылку SPI через `POST /v1/cards/links`.

## Куда ходить

Внутренний адрес: `http://socrat-bot.socrat-bot.svc:8080`. Заголовок
`X-Internal-Token`. Публичный адрес `https://bot.socrat.alchemmist.xyz` принимает
только `POST /telegram/webhook` и заголовок `X-Telegram-Bot-Api-Secret-Token`.

Команды `/start`, `/mailing` и `/clients` в этот API не входят: их нажимают люди
в Telegram. Sudo и обновление 3X-UI живут в отдельных процессах и сюда тоже не входят.

## Методы
"""


def schema_name(schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return f"список {schema_name(schema.get('items', {}))}"
    return schema.get("type", "object")


def main() -> None:
    spec = yaml.safe_load(SPEC.read_text())
    lines = [INTRO.rstrip(), ""]
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method.startswith("x-"):
                continue
            lines.append(f"### `{method.upper()} {path}`")
            lines.append("")
            lines.append(op.get("summary", ""))
            lines.append("")
            if op.get("description"):
                lines.append(op["description"].strip())
                lines.append("")
            security = op.get("security", spec.get("security", []))
            names = [next(iter(item)) for item in security]
            auth = "без заголовка" if not names else ", ".join(names)
            lines.append(f"Доступ: {auth}.")
            lines.append("")
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                lines.append(f"Тело: `{schema_name(body['schema'])}`.")
                lines.append("")
            for code, response in op.get("responses", {}).items():
                lines.append(f"- `{code}` — {response.get('description', '')}")
            lines.append("")
    lines.append("## Тела запросов")
    lines.append("")
    for name, schema in spec["components"]["schemas"].items():
        lines.append(f"### {name}")
        lines.append("")
        for field, meta in schema.get("properties", {}).items():
            required = field in schema.get("required", [])
            mark = ", обязательно" if required else ""
            detail = meta.get("description", "")
            extra = f" — {detail}" if detail else ""
            lines.append(f"- `{field}`: {schema_name(meta)}{mark}{extra}")
        lines.append("")
    TARGET.write_text("\n".join(lines).rstrip() + "\n")
    print(TARGET)


if __name__ == "__main__":
    main()
