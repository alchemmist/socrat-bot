import hashlib
import json
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from socrat_bot.models import Action, Audit, Broadcast, LinkCode, SudoRequest, TelegramCard, User, utcnow

ACTION_TYPES = {
    "alert.silence",
    "broadcast.send",
    "broadcast.cancel",
    "xui.upgrade",
    "sudo.approve",
    "sudo.deny",
}


def owner_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    return {int(raw)} if raw else set()


def broadcast_zone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("BROADCAST_TZ", "Europe/Moscow"))


def ensure_user(db: Session, telegram_id: int, username: str | None = None) -> User:
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    configured = owner_ids()
    has_owner = db.scalar(select(User.id).where(User.role == "owner")) is not None
    promote = telegram_id in configured or (not configured and not has_owner)
    if user is None:
        user = User(
            telegram_id=telegram_id,
            role="owner" if promote else "user",
            uses_vpn=False,
            alerts_enabled=promote,
        )
        db.add(user)
    elif promote:
        user.role = "owner"
        user.alerts_enabled = True
    if username is not None:
        user.username = username.lstrip("@") or None
    db.flush()
    return user


def ensure_owner(db: Session, telegram_id: int) -> User | None:
    user = ensure_user(db, telegram_id)
    return user if user.role == "owner" else None


def is_owner(db: Session, telegram_id: int) -> bool:
    return ensure_owner(db, telegram_id) is not None


def mark_vpn(db: Session, telegram_id: int, username: str | None = None) -> User:
    user = ensure_user(db, telegram_id, username)
    user.uses_vpn = True
    if user.role == "user":
        user.role = "vpn"
    audit(db, str(telegram_id), "vpn.opt_in", {"role": user.role})
    return user


def begin_mailing(db: Session, chat_id: int, text: str) -> Broadcast:
    row = Broadcast(text=text, status="choosing", chat_id=chat_id)
    db.add(row)
    db.flush()
    return row


def mailing_dates(now: datetime, days: int = 7) -> list[date]:
    start = now.astimezone(broadcast_zone()).date()
    return [start + timedelta(days=offset) for offset in range(days)]


def mailing_hours(day: date, now: datetime) -> list[int]:
    """Hours 8 through 22 inclusive. Today skips hours that already started."""
    local = now.astimezone(broadcast_zone())
    first = 8
    if day == local.date():
        first = max(8, local.hour + 1)
    if first > 22:
        return []
    return list(range(first, 23))


def mailing_slot(day: date, hour: int) -> datetime:
    local = datetime.combine(day, time(hour, 0), tzinfo=broadcast_zone())
    return local.astimezone(timezone.utc)


def schedule_broadcast(db: Session, chat_id: int, send_at: datetime, text: str) -> Broadcast:
    row = Broadcast(text=text, status="scheduled", chat_id=chat_id, send_at=send_at)
    db.add(row)
    db.flush()
    audit(db, str(chat_id), "broadcast.schedule", {"id": str(row.id), "send_at": send_at.isoformat()})
    return row


def claim_due_broadcasts(db: Session, now: datetime | None = None) -> list[Broadcast]:
    moment = now or datetime.now(timezone.utc)
    rows = list(
        db.scalars(
            select(Broadcast).where(Broadcast.status == "scheduled", Broadcast.send_at.is_not(None), Broadcast.send_at <= moment)
        )
    )
    for row in rows:
        row.status = "sending"
    db.flush()
    return rows


def audit(db: Session, actor: str, event: str, detail: dict) -> None:
    db.add(Audit(actor=actor, event=event, detail=detail))


def create_action(
    db: Session,
    *,
    type_: str,
    payload: dict,
    chat_id: int,
    idempotency_key: str,
    message_id: int | None = None,
    ttl_seconds: int | None = None,
    refresh: bool = False,
) -> Action:
    existing = db.scalar(select(Action).where(Action.idempotency_key == idempotency_key))
    if existing:
        if refresh and existing.status == "pending":
            existing.payload = payload
            existing.chat_id = chat_id
        return existing
    expires = utcnow() + timedelta(seconds=ttl_seconds) if ttl_seconds else None
    row = Action(
        type=type_,
        payload=payload,
        chat_id=chat_id,
        message_id=message_id,
        idempotency_key=idempotency_key,
        expires_at=expires,
    )
    db.add(row)
    db.flush()
    return row


def open_card(db: Session, key: str, chat_id: int, firing: bool) -> TelegramCard:
    """Return the card row without changing its Telegram message.

    Status and message id are stored only after Telegram accepts the update.
    """
    del firing
    row = db.get(TelegramCard, (key, chat_id))
    if row is None:
        row = TelegramCard(key=key, chat_id=chat_id, generation=1, status="firing")
        db.add(row)
        db.flush()
        return row
    return row


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def argv_hash(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode()).hexdigest()


def issue_link_code(db: Session, panel_client_id: str, ttl_seconds: int = 900) -> str:
    code = uuid.uuid4().hex[:16]
    db.add(
        LinkCode(
            code=code,
            panel_client_id=panel_client_id,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
    )
    db.flush()
    return code


def consume_link(db: Session, telegram_id: int, code: str) -> str:
    row = db.get(LinkCode, code)
    now = datetime.now(timezone.utc)
    if row is None or row.used_by is not None or row.expires_at.replace(tzinfo=timezone.utc) < now:
        return "invalid"
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, role="vpn")
        db.add(user)
    if user.role != "owner":
        user.role = "vpn"
    user.panel_client_id = row.panel_client_id
    row.used_by = telegram_id
    audit(db, str(telegram_id), "vpn.link", {"panel_client_id": row.panel_client_id})
    return "linked"


def vpn_audience(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.uses_vpn.is_(True))))


def card_messages(db: Session, key: str) -> list[tuple[int, int]]:
    rows = db.scalars(
        select(TelegramCard)
        .where(TelegramCard.key == key, TelegramCard.message_id.is_not(None))
        .order_by(TelegramCard.chat_id)
    )
    return [(row.chat_id, int(row.message_id)) for row in rows if row.message_id]


def alert_recipients(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.alerts_enabled.is_(True)).order_by(User.telegram_id)))


def owners(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.role == "owner").order_by(User.telegram_id)))


def approve_alerts(db: Session, telegram_id: int, username: str | None = None) -> User:
    user = ensure_user(db, telegram_id, username)
    user.alerts_enabled = True
    audit(db, str(telegram_id), "alerts.approve", {"role": user.role})
    return user


def format_vpn_clients(users: list[User]) -> str:
    ordered = sorted(users, key=lambda user: user.telegram_id)
    if not ordered:
        return "VPN clients: 0"
    lines = [f"VPN clients: {len(ordered)}"]
    lines.extend(
        f"\u2022 @{user.username}" if user.username else f"\u2022 {user.telegram_id}" for user in ordered
    )
    return "\n".join(lines)


async def apply_action(db: Session, action: Action, actor: str) -> dict:
    if action.status != "pending":
        stored = dict(action.result or {})
        stored["replayed"] = True
        stored.setdefault("status", action.status)
        return stored
    if action.expires_at and as_utc(action.expires_at) < datetime.now(timezone.utc):
        action.status = "expired"
        action.result = {"status": "expired"}
        return action.result
    action.status = "running"
    db.flush()
    try:
        result = await _dispatch(db, action)
    except Exception as exc:
        action.status = "failed"
        action.result = {"status": "failed", "error": str(exc)}
        audit(db, actor, action.type, action.result)
        return action.result
    action.status = "done"
    action.result = result
    audit(db, actor, action.type, result)
    return result


async def _dispatch(db: Session, action: Action) -> dict:
    if action.type == "alert.silence":
        return await _silence(action.payload)
    if action.type == "broadcast.send":
        row = db.get(Broadcast, uuid.UUID(action.payload["broadcast_id"]))
        return await deliver_broadcast(db, row)
    if action.type == "broadcast.cancel":
        row = db.get(Broadcast, uuid.UUID(action.payload["broadcast_id"]))
        if row:
            row.status = "cancelled"
        return {"status": "cancelled"}
    if action.type == "xui.upgrade":
        return await _upgrade()
    if action.type == "sudo.approve":
        return _sudo_decision(db, action, "approved")
    if action.type == "sudo.deny":
        return _sudo_decision(db, action, "denied")
    raise ValueError(f"unknown action {action.type}")


async def _silence(payload: dict) -> dict:
    base = os.environ.get("ALERTMANAGER_URL", "http://kube-prometheus-stack-alertmanager.cattle-monitoring-system.svc:9093")
    minutes = int(payload.get("minutes") or 60)
    minutes = max(1, min(minutes, 24 * 60))
    ends = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    body = {
        "matchers": payload.get("matchers") or [{"name": "alertname", "value": payload.get("alertname", ".*"), "isRegex": True}],
        "startsAt": datetime.now(timezone.utc).isoformat(),
        "endsAt": ends,
        "createdBy": "socrat-bot",
        "comment": f"muted for {minutes} minutes from Telegram",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"{base}/api/v2/silences", json=body)
        response.raise_for_status()
        return {"status": "silenced", "silence": response.json()}


async def deliver_broadcast(text: str, recipients: list[int]) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    pause = float(os.environ.get("BROADCAST_PAUSE_SECONDS", "0.4"))
    sent = 0
    failed: list[int] = []
    import asyncio

    async with httpx.AsyncClient(timeout=20) as client:
        for telegram_id in recipients:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": telegram_id, "text": text},
            )
            if response.status_code == 200 and response.json().get("ok"):
                sent += 1
            else:
                failed.append(telegram_id)
            await asyncio.sleep(pause)
    return {"status": "sent", "sent": sent, "failed": failed}


async def _upgrade() -> dict:
    base = os.environ.get("XUI_UPDATER_URL", "http://socrat-bot-xui-updater.socrat-bot.svc:8080")
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(f"{base}/upgrade")
        response.raise_for_status()
        return response.json()


def _sudo_decision(db: Session, action: Action, status: str) -> dict:
    row = db.get(SudoRequest, uuid.UUID(action.payload["request_id"]))
    if row is None:
        return {"status": "missing"}
    # Approved and consumed stay closed: a second click must not reopen the request.
    if row.status != "pending":
        return {"status": row.status, "replayed": True, "request_id": str(row.id)}
    if as_utc(row.expires_at) <= datetime.now(timezone.utc):
        row.status = "expired"
        return {"status": "expired", "request_id": str(row.id)}
    row.status = status
    return {"status": status, "request_id": str(row.id), "argv": row.argv, "node": row.node}
