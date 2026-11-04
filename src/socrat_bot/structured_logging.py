"""Bounded JSON application events. Never serialize arbitrary record extras or exceptions."""
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone

SERVICE = 'socrat-bot'
EVENTS = {'application started', 'application stopped', 'http request completed', 'telegram start', 'card note failed', 'spi link failed', 'action webhook failed', 'telegram update failed', 'alert card failed', 'broadcast completed', 'telegram webhook completed', 'broadcast loop failed'}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Only reviewed static event names are emitted; args/exception text can contain secrets.
        message = record.msg if isinstance(record.msg, str) and record.msg in EVENTS else "application event"
        data = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname.lower(), "service": SERVICE, "msg": message}
        component = getattr(record, "component", None)
        if component in {"bot", "broker", "updater"}:
            data["component"] = component
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


class OperationalEvents:
    """ASGI diagnostics without recording request or exception content."""

    def __init__(self, app, component):
        self.app = app
        self.component = component
        self.log = configure_logging()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def lifecycle_send(message):
                events = {"lifespan.startup.complete": "application started",
                          "lifespan.shutdown.complete": "application stopped"}
                if message["type"] in events:
                    self.log.info(events[message["type"]], extra={"component": self.component})
                await send(message)
            await self.app(scope, receive, lifecycle_send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500
        async def response_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, response_send)
        finally:
            self.log.info("http request completed", extra={
                "component": self.component, "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 3)})
