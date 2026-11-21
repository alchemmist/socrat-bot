"""Ephemeral per-webhook correlation; never log Telegram IDs, bodies or errors."""

from contextvars import ContextVar
import logging
import uuid

operation = ContextVar("callback_operation", default=None)
log = logging.getLogger("socrat-bot")


def start(payload):
    query = payload.get("callback_query") if isinstance(payload, dict) else None
    if isinstance(query, dict) and str(query.get("data", "")).startswith("act:"):
        token = operation.set(uuid.uuid4().hex)
        stage("received")
        return token
    return None


def stage(value, *, exception=None):
    if operation.get():
        log.info(
            "action callback",
            extra={
                "operation": operation.get(),
                "stage": value,
                **(
                    {"failure_kind": type(exception).__name__}
                    if exception is not None
                    else {}
                ),
            },
        )
