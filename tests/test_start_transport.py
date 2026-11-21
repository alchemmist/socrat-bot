import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import TimedOut

from socrat_bot.telegram_reply import reply_text


@pytest.fixture(autouse=True)
def capture_nonpropagating_logger(monkeypatch):
    import logging
    monkeypatch.setattr(logging.getLogger('socrat-bot'), 'propagate', True)


@pytest.mark.parametrize(
    "cause,retry", [(httpx.ConnectTimeout, True), (httpx.ReadTimeout, False)]
)
def test_start_retries_only_before_send(cause, retry):
    error = TimedOut()
    error.__cause__ = cause("timeout")
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=[error, "ok"]))
    if retry:
        assert asyncio.run(reply_text(message, "welcome")) == "ok"
        assert message.reply_text.await_count == 2
    else:
        with pytest.raises(TimedOut):
            asyncio.run(reply_text(message, "welcome"))
        assert message.reply_text.await_count == 1


@pytest.mark.parametrize(
    "args, reason",
    [([], "Не указано"), (["0"], "целым числом"), (["5", "vpn"], "только одно число")],
)
def test_resend_explains_invalid_input(monkeypatch, args, reason):
    from socrat_bot import resend

    monkeypatch.setattr(resend, "allowed", lambda *_: True)
    message = SimpleNamespace(chat_id=1, reply_text=AsyncMock())
    asyncio.run(
        resend.start(
            SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1)),
            SimpleNamespace(args=args),
        )
    )
    text = message.reply_text.await_args.args[0]
    assert reason in text and "/resend 5" in text and "ничего не отправлено" in text
    assert "reply_markup" not in message.reply_text.await_args.kwargs


@pytest.mark.parametrize('sequence, outcomes', [
    (['ok'], ['confirmed']),
    (['connect', 'ok'], ['connect_timeout', 'confirmed']),
    (['connect', 'connect'], ['connect_timeout', 'connect_timeout']),
    (['read'], ['unconfirmed']),
])
def test_redacted_attempt_history(sequence, outcomes, caplog):
    import json
    import logging
    from socrat_bot.structured_logging import JsonFormatter
    from socrat_bot.telegram_reply import accepted_start

    responses = []
    for item in sequence:
        if item == 'ok':
            responses.append('ok')
        else:
            error = TimedOut()
            error.__cause__ = (httpx.ReadTimeout if item == 'read' else httpx.ConnectTimeout)('PRIVATE_CANARY')
            responses.append(error)
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=responses))
    with caplog.at_level(logging.INFO, logger='socrat-bot'):
        operation = accepted_start()
        call = reply_text(message, 'PRIVATE_CANARY', operation=operation)
        if sequence[-1] == 'ok':
            assert asyncio.run(call) == 'ok'
        else:
            with pytest.raises(TimedOut):
                asyncio.run(call)
    records = [json.loads(JsonFormatter().format(r)) for r in caplog.records if getattr(r, 'operation', None)]
    assert all(r['operation'] == operation for r in records)
    assert records[0]['msg'] == 'telegram command accepted'
    results = [r for r in records if r['msg'] == 'telegram reply result']
    assert [r['outcome'] for r in results] == outcomes
    assert [r['attempt'] for r in results] == list(range(1, len(outcomes) + 1))
    assert all(r['duration_ms'] >= 0 for r in results)
    assert 'PRIVATE_CANARY' not in json.dumps(records)
    assert message.reply_text.await_count == len(outcomes)


def test_telegram_accepts_send_but_http_reply_is_lost(caplog):
    """Exercise PTB's actual httpx error mapping with a token-free, in-memory transport."""
    import logging
    from telegram import Bot
    from telegram.request import HTTPXRequest

    accepted = []

    async def telegram(request):
        assert request.url.path.endswith('/sendMessage')
        accepted.append('applied')
        raise httpx.ReadTimeout('response lost after receipt', request=request)

    async def run():
        req = HTTPXRequest(httpx_kwargs={'transport': httpx.MockTransport(telegram)})
        poll = HTTPXRequest(httpx_kwargs={'transport': httpx.MockTransport(telegram)})
        bot = Bot(token='123:TEST_ONLY', request=req, get_updates_request=poll)
        async def send(text, **kwargs):
            return await bot.send_message(1, text, **kwargs)
        try:
            with pytest.raises(TimedOut):
                await reply_text(SimpleNamespace(reply_text=send), 'test')
        finally:
            await req.shutdown()
            await poll.shutdown()

    with caplog.at_level(logging.WARNING, logger='socrat-bot'):
        asyncio.run(run())
    assert accepted == ['applied']
    assert [r.outcome for r in caplog.records if hasattr(r, 'outcome')] == ['unconfirmed']


def test_cancelled_attempt_is_unconfirmed_and_never_retried(caplog):
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(reply_text(message, 'test'))
    assert message.reply_text.await_count == 1
    assert [r.outcome for r in caplog.records if hasattr(r, 'outcome')] == ['unconfirmed']
