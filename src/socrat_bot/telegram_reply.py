import httpx
from telegram.error import TimedOut


async def reply_text(message, text, **kwargs):
    # Only connection setup is safe to retry: a read timeout may mean delivered.
    try:
        return await message.reply_text(text, connect_timeout=15, **kwargs)
    except TimedOut as exc:
        if not isinstance(exc.__cause__, httpx.ConnectTimeout):
            raise
    return await message.reply_text(text, connect_timeout=15, **kwargs)
