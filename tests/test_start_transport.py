import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import TimedOut

from socrat_bot.app import _reply_start


@pytest.mark.parametrize('cause,retry', [(httpx.ConnectTimeout, True), (httpx.ReadTimeout, False)])
def test_start_retries_only_before_send(cause, retry):
    error = TimedOut()
    error.__cause__ = cause('timeout')
    message = SimpleNamespace(reply_text=AsyncMock(side_effect=[error, 'ok']))
    if retry:
        assert asyncio.run(_reply_start(message, 'welcome')) == 'ok'
        assert message.reply_text.await_count == 2
    else:
        with pytest.raises(TimedOut):
            asyncio.run(_reply_start(message, 'welcome'))
        assert message.reply_text.await_count == 1
