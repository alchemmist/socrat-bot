"""Opt-in durable graph capture; alert delivery never waits for rendering capacity."""

import asyncio
import hashlib
import os
import re
import stat

import httpx
from sqlalchemy.exc import SQLAlchemyError

GATEWAY = "http://socrat-alert-graphs.cattle-monitoring-system.svc.cluster.local:8080/v1/primary"
MAX_BYTES = 2 * 1024 * 1024


def _token():
    with open("/graph-client/telegram", "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o027
            or info.st_size > 4096
        ):
            raise ValueError()
        value = stream.read(4097).decode("ascii").strip()
    if not 16 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError()
    return value


async def capture(key, alert):
    """Persist a render intent without blocking alert delivery on Chromium capacity."""
    if os.environ.get("ALERT_GRAPHS_ENABLED", "false") != "true" or not isinstance(
        alert, dict
    ):
        return None
    annotations = alert.get("annotations")
    annotation = (
        annotations.get("spi_graphs") if isinstance(annotations, dict) else None
    )
    if not isinstance(annotation, str) or not annotation or len(annotation) > 16000:
        return None
    payload = {
        "annotation": annotation,
        "labels": alert.get("labels", {}),
        "starts_at": alert.get("startsAt"),
        "ends_at": alert.get("endsAt"),
        "status": "resolved" if alert.get("status") == "resolved" else "firing",
    }
    if payload["ends_at"] and str(payload["ends_at"]).startswith("0001-"):
        payload["ends_at"] = None
    try:
        from socrat_bot.graph_jobs import enqueue

        async with asyncio.timeout(5):
            return await asyncio.to_thread(enqueue, key, payload)
    except (OSError, ValueError, TypeError, TimeoutError, SQLAlchemyError):
        return None


async def fetch_png(payload):
    """Fetch only the fixed gateway, returning safe bounded result codes, never upstream text."""
    try:
        token = await asyncio.to_thread(_token)
        async with asyncio.timeout(7):
            async with (
                httpx.AsyncClient(
                    timeout=6, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    "POST",
                    GATEWAY,
                    json=payload,
                    headers={"Authorization": "Bearer " + token},
                ) as response,
            ):
                if response.status_code != 200:
                    retry = response.status_code == 429 or response.status_code >= 500
                    return (
                        None,
                        None,
                        retry,
                        "HTTP_TRANSIENT" if retry else "HTTP_REJECTED",
                    )
                if (
                    response.headers.get("content-type", "").split(";")[0]
                    != "image/png"
                    or response.headers.get("content-encoding", "identity")
                    != "identity"
                ):
                    return None, None, False, "INVALID_IMAGE"
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_BYTES:
                        return None, None, False, "IMAGE_LIMIT"
                    data.extend(chunk)
                checksum = response.headers.get("x-content-sha256", "")
                digest = response.headers.get("x-render-digest", "")
                if (
                    not re.fullmatch("[a-f0-9]{64}", checksum)
                    or not re.fullmatch("[a-f0-9]{64}", digest)
                    or hashlib.sha256(data).hexdigest() != checksum
                    or not data.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    return None, None, False, "INVALID_IMAGE"
                return bytes(data), digest, False, "READY"
    except (TimeoutError, httpx.HTTPError, OSError):
        return None, None, True, "TRANSPORT"
    except (ValueError, TypeError, KeyError):
        return None, None, False, "CLIENT_CONFIG"
