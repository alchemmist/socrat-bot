import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from socrat_bot import app, callback_trace
from socrat_bot.structured_logging import JsonFormatter
from telegram import Bot


def test_webhook_decodes_real_callback_and_reports_invalid_action_without_db(
    monkeypatch,
):
    records = []
    monkeypatch.setattr(
        callback_trace.log,
        "info",
        lambda message, *, extra: records.append((message, dict(extra))),
    )
    monkeypatch.setattr(Bot, "answer_callback_query", AsyncMock(return_value=True))
    bot = Bot("123:fixture")

    async def dispatch(update):
        await app.on_callback(update, SimpleNamespace(bot=bot))

    monkeypatch.setattr(app, "ptb", SimpleNamespace(bot=bot, process_update=dispatch))
    asyncio.run(
        app._run_update(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "secret-id",
                    "chat_instance": "secret-chat",
                    "from": {"id": 42, "is_bot": False, "first_name": "secret-name"},
                    "data": "act:secret-invalid",
                },
            }
        )
    )
    assert [fields["stage"] for _, fields in records] == [
        "received",
        "handler",
        "invalid",
        "acknowledged",
    ]
    assert len({fields["operation"] for _, fields in records}) == 1
    assert "secret" not in json.dumps(records)
    assert callback_trace.operation.get() is None


def test_trace_rejects_arbitrary_fields():
    import logging

    r = logging.LogRecord(
        "socrat-bot", logging.INFO, "", 0, "action callback", (), None
    )
    r.stage = r.operation = "secret"
    assert "secret" not in JsonFormatter().format(r)


def test_slow_lookup_acknowledged_before_db_returns(monkeypatch):
    import threading
    from types import SimpleNamespace as NS

    gate = threading.Event()

    def slow(*args):
        assert gate.wait(4)
        return ("rejected", ("missing", "not there"))

    monkeypatch.setattr(app, "_prepare_callback", slow)
    answers = []

    async def answer(text):
        answers.append(text)
        gate.set()

    query = NS(
        data="act:00000000-0000-0000-0000-000000000001",
        from_user=NS(id=1, username=None),
        answer=answer,
    )
    asyncio.run(app.on_callback(NS(callback_query=query), None))
    assert answers == ["Запрос принят. Проверяю…"]
