"""Opt-in primary graph capture. Existing alert delivery never depends on rendering."""

import asyncio
import hashlib
import json
import os
import re
import stat

import httpx
from sqlalchemy import func, select

from socrat_bot.db import session
from socrat_bot.models import GraphSnapshot, utcnow

GATEWAY = "http://socrat-alert-graphs.cattle-monitoring-system.svc.cluster.local:8080/v1/primary"
MAX_BYTES = 2 * 1024 * 1024


def _reserve(identity):
    from socrat_bot.escalation import insert_once

    with session() as db:
        insert_once(db, GraphSnapshot, {"id": identity, "observed_at": utcnow()})
        row = db.get(GraphSnapshot, identity)
        return (
            row.observed_at.isoformat()
            if row.observed_at.tzinfo
            else row.observed_at.isoformat() + "+00:00",
            row.png is not None,
        )


def _save(identity, data, digest):
    with session() as db:
        row = db.scalar(
            select(GraphSnapshot).where(GraphSnapshot.id == identity).with_for_update()
        )
        if row.png is not None:
            return identity
        total = db.scalar(select(func.sum(func.length(GraphSnapshot.png)))) or 0
        if total + len(data) > 256 * 1024 * 1024:
            return None
        row.png, row.render_digest = data, digest
        return identity


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
    if os.environ.get("ALERT_GRAPHS_ENABLED", "false") != "true" or not isinstance(
        alert, dict
    ):
        return None
    annotation = alert.get("annotations", {}).get("spi_graphs")
    if not isinstance(annotation, str) or not annotation or len(annotation) > 16000:
        return None
    payload = {
        "annotation": annotation,
        "labels": alert.get("labels", {}),
        "starts_at": alert.get("startsAt"),
        "ends_at": alert.get("endsAt"),
        "status": alert.get("status"),
    }
    if payload["ends_at"] and str(payload["ends_at"]).startswith("0001-"):
        payload["ends_at"] = None
    try:
        async with asyncio.timeout(5):
            identity = hashlib.sha256(
                json.dumps([key, payload], sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()
            # Persist timestamp before transport; retries request the same exact image.
            observed, ready = await asyncio.to_thread(_reserve, identity)
            if ready:
                return identity
            payload["observed_at"] = observed
            token = await asyncio.to_thread(_token)
            async with (
                httpx.AsyncClient(
                    timeout=4, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    "POST",
                    GATEWAY,
                    json=payload,
                    headers={"Authorization": "Bearer " + token},
                ) as response,
            ):
                if (
                    response.status_code != 200
                    or response.headers.get("content-type", "").split(";")[0]
                    != "image/png"
                ):
                    return None
                if response.headers.get("content-encoding", "identity") != "identity":
                    return None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_BYTES:
                        return None
                    data.extend(chunk)
                checksum = response.headers.get("x-content-sha256", "")
                digest = response.headers.get("x-render-digest", "")
                if (
                    not re.fullmatch("[a-f0-9]{64}", checksum)
                    or not re.fullmatch("[a-f0-9]{64}", digest)
                    or hashlib.sha256(data).hexdigest() != checksum
                    or not data.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    return None
            return await asyncio.to_thread(_save, identity, bytes(data), digest)
    except (OSError, ValueError, TypeError, KeyError, TimeoutError, httpx.HTTPError):
        return None  # No upstream exception/request text in logs or delivery responses.
