import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import TimedOut

from socrat_bot.telegram_reply import reply_text


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
