"""Alertmanager silence transport: never blindly replay an ambiguous POST."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

SERVICE = "http://kube-prometheus-stack-alertmanager.cattle-monitoring-system.svc:9093"
PEERS = tuple(
    f"http://alertmanager-kube-prometheus-stack-alertmanager-{n}.alertmanager-operated.cattle-monitoring-system.svc.cluster.local.:9093"
    for n in (0, 1)
)


def signature(matchers):
    return sorted(
        (m["name"], m["value"], m.get("isRegex", False), m.get("isEqual", True))
        for m in matchers
    )


def failure(reason):
    return {"status": "failed", "reason": reason}


async def execute(action, previous):
    base = os.environ.get("ALERTMANAGER_URL", SERVICE).rstrip("/")
    peers = PEERS if base == SERVICE else ()
    payload = action.payload
    matchers = payload.get("matchers")
    if not matchers and payload.get("alertname"):
        matchers = [
            {"name": "alertname", "value": payload["alertname"], "isRegex": False}
        ]
    if not matchers:
        return failure("invalid_matchers")
    minutes = max(1, min(int(payload.get("minutes") or 60), 1440))
    legacy = f"muted for {minutes} minutes from Telegram"
    marker = f"{legacy}; action={action.id}"
    timeout = httpx.Timeout(8, connect=3)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if previous is not None and previous.get("reason") != "not_connected":
            # Old builds did not record which stage failed. Treat them as ambiguous.
            # Never infer absence from one HA peer or from unavailable readback.
            unknown = {**previous, **failure("unconfirmed")}
            if not peers:
                return unknown

            async def read(peer):
                r = await client.get(peer + "/api/v2/silences")
                r.raise_for_status()
                rows = r.json()
                if not isinstance(rows, list):
                    raise ValueError("invalid readback")
                return [
                    x
                    for x in rows
                    if x["createdBy"] == "socrat-bot"
                    and x["comment"] in (marker, legacy)
                    and signature(x["matchers"]) == signature(matchers)
                    and x["status"]["state"] in ("active", "pending")
                ]

            try:
                copies = await asyncio.gather(*(read(p) for p in peers))
            except (httpx.HTTPError, KeyError, ValueError, TypeError):
                return unknown
            matches = [x for rows in copies for x in rows]
            for x in matches:
                if x["comment"] == marker:
                    return {
                        "status": "silenced",
                        "silence": {"silenceID": x["id"]},
                        "reconciled": True,
                    }
            if matches:
                # Legacy comments were shared. Do not claim another action's receipt.
                return failure("legacy_overlap")
            # Only an explicit new button press reaches here. Both peers saw no silence.
            stamp = previous.get("attempted_at")
            if stamp and datetime.now(timezone.utc).timestamp() - stamp < 30:
                return previous
        now = datetime.now(timezone.utc)
        body = {
            "matchers": matchers,
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(minutes=minutes)).isoformat(),
            "createdBy": "socrat-bot",
            "comment": marker,
        }
        for dest in (base, *peers):
            try:
                r = await client.post(dest + "/api/v2/silences", json=body)
                r.raise_for_status()
                sid = r.json()["silenceID"]
                if not isinstance(sid, str) or not sid:
                    raise ValueError("no receipt")
                return {"status": "silenced", "silence": {"silenceID": sid}}
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                continue  # no request was sent on this connection
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                return {
                    **failure("unconfirmed"),
                    "stage": type(exc).__name__,
                    "attempted_at": datetime.now(timezone.utc).timestamp(),
                }
        return failure("not_connected")
