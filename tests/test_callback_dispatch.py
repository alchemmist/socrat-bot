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


def test_preflight_error_reports_failure_without_dispatch(monkeypatch):
    from types import SimpleNamespace as NS

    def fail(*args):
        raise RuntimeError("private")

    monkeypatch.setattr(app, "_prepare_callback", fail)
    query = NS(
        data="act:00000000-0000-0000-0000-000000000001",
        from_user=NS(id=1, username=None),
        answer=AsyncMock(),
    )
    asyncio.run(app.on_callback(NS(callback_query=query), None))
    query.answer.assert_awaited_once_with(
        "Не удалось проверить действие. Попробуйте позже."
    )


def test_escalation_delivers_committed_pressed_copy_while_answer_is_in_flight(
    monkeypatch,
):
    from contextlib import contextmanager
    from threading import get_ident

    main_thread = get_ident()
    committed = []

    @contextmanager
    def session():
        assert get_ident() != main_thread
        yield object()
        committed.append(True)

    def escalate(db, *args, pressed_jobs):
        pressed_jobs.append("pressed")
        return "Escalated"

    async def run():
        delivered = asyncio.Event()

        async def dispatch(jobs):
            assert committed and jobs == ["pressed"]
            delivered.set()

        async def answer(result):
            assert result == "Escalated"
            await asyncio.wait_for(delivered.wait(), 1)

        monkeypatch.setattr(app, "dispatch_jobs", dispatch)
        q = SimpleNamespace(
            data="esc:digest:1:20",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat_id=1, message_id=2),
            answer=answer,
        )
        await app.on_callback(SimpleNamespace(callback_query=q), None)

    monkeypatch.setattr(app, "session", session)
    monkeypatch.setattr(app, "escalate", escalate)
    asyncio.run(run())


def test_escalation_worker_failure_answers_without_dispatch(monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(app, "session", nullcontext)

    def fail(*args, **kwargs):
        raise RuntimeError("private")

    monkeypatch.setattr(app, "escalate", fail)
    send = AsyncMock()
    monkeypatch.setattr(app, "dispatch_jobs", send)
    q = SimpleNamespace(
        data="esc:digest:1:20",
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat_id=1, message_id=2),
        answer=AsyncMock(),
    )
    asyncio.run(app.on_callback(SimpleNamespace(callback_query=q), None))
    q.answer.assert_awaited_once_with(
        "Не удалось подтвердить эскалацию. Проверьте уровень карточки."
    )
    send.assert_not_awaited()


def test_private_button_receipt_is_in_webhook_response(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "fixture")
    monkeypatch.setattr(app, "ptb", object())
    processed = []

    async def worker(payload):
        processed.append(app._webhook_receipt.get())

    monkeypatch.setattr(app, "_run_update", worker)

    async def run():
        for data in ["act:fixture", "esc:digest:1:20", "users:topic"]:
            payload = {"callback_query": {"id": "fixture", "data": data,
                       "message": {"chat": {"type": "private"}}}}
            response = await app.webhook(SimpleNamespace(json=AsyncMock(return_value=payload)), "fixture")
            if data.startswith("users:"):
                assert response == {"ok": True}
            else:
                assert response["method"] == "answerCallbackQuery"
                assert response["callback_query_id"] == "fixture"
            assert not app._webhook_receipt.get()
        assert not processed
        await asyncio.gather(*app._updates)

    asyncio.run(run())
    assert processed == [True, True, False]
