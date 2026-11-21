"""Correlate a reply without recording Telegram payloads or recipients."""
import logging
import time
import uuid

import httpx
from telegram.error import TimedOut

log = logging.getLogger("socrat-bot")


def accepted_start():
    operation = uuid.uuid4().hex
    log.info("telegram command accepted", extra={"operation": operation})
    return operation


async def reply_text(message, text, *, operation=None, **kwargs):
    operation = operation or uuid.uuid4().hex
    for attempt in (1, 2):
        fields = {"operation": operation, "attempt": attempt}
        log.info("telegram reply attempt", extra=fields)
        start = time.monotonic()
        try:
            result = await message.reply_text(text, connect_timeout=15, **kwargs)
        except Exception as exc:
            before_send = isinstance(exc, TimedOut) and isinstance(exc.__cause__, httpx.ConnectTimeout)
            outcome = "connect_timeout" if before_send else "unconfirmed"
            log.warning("telegram reply result", extra={**fields, "outcome": outcome, "duration_ms": round(1000 * (time.monotonic() - start))})
            if before_send and attempt == 1:
                continue
            raise
        log.info("telegram reply result", extra={**fields, "outcome": "confirmed", "duration_ms": round(1000 * (time.monotonic() - start))})
        return result
