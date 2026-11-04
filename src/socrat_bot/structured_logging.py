"""Bounded JSON application events. Never serialize arbitrary record extras or exceptions."""
import json
import logging
import math
import sys
from datetime import datetime, timezone

SERVICE = 'socrat-bot'
EVENTS = {'telegram start', 'card note failed', 'spi link failed', 'action webhook failed', 'telegram update failed', 'alert card failed', 'broadcast completed', 'telegram webhook completed', 'broadcast loop failed'}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Only reviewed static event names are emitted; args/exception text can contain secrets.
        message = record.msg if isinstance(record.msg, str) and record.msg in EVENTS else "application event"
        data = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname.lower(), "service": SERVICE, "msg": message}
        if record.exc_info:
            data["exception"] = "exception"  # no type/message/stack from untrusted exceptions
        try:
            from opentelemetry import trace
            context = trace.get_current_span().get_span_context()
            if context.is_valid:
                data.update(trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}")
        except ImportError:
            pass
        fields = {
            "http request completed": ("status", "duration_ms"),
            "telegram webhook completed": ("status",),
            "broadcast completed": ("sent", "failed"),
        }
        if message in fields:
            for key in fields[message]:
                value = getattr(record, key, None)
                if type(value) in (int, float) and math.isfinite(value):
                    data[key] = value
        return json.dumps(data, separators=(",", ":"), allow_nan=False)


def configure_logging():
    logger = logging.getLogger(SERVICE)
    if not any(getattr(handler, "_safe_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler._safe_json = True
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
