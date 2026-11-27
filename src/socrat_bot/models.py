import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    func,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


JSONType = JSON().with_variant(JSONB, "postgresql")


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str] = mapped_column(String(32))
    panel_client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    interests: Mapped[list["UserInterest"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True,
    )

    def interested_in(self, topic: str) -> bool:
        return any(row.topic == topic and row.enabled for row in self.interests)

    def set_interest(self, topic: str, enabled: bool) -> None:
        for row in self.interests:
            if row.topic == topic:
                row.enabled = enabled
                return
        self.interests.append(UserInterest(topic=topic, enabled=enabled))

    @hybrid_property
    def interested_in_vpn(self):
        return self.interested_in("vpn")

    @interested_in_vpn.setter
    def interested_in_vpn(self, enabled):
        self.set_interest("vpn", enabled)

    @interested_in_vpn.expression
    def interested_in_vpn(cls):
        return cls.interests.any((UserInterest.topic == "vpn") & UserInterest.enabled)

    @hybrid_property
    def interested_in_alerts(self):
        return self.interested_in("alerts")

    @interested_in_alerts.setter
    def interested_in_alerts(self, enabled):
        self.set_interest("alerts", enabled)

    @interested_in_alerts.expression
    def interested_in_alerts(cls):
        return cls.interests.any((UserInterest.topic == "alerts") & UserInterest.enabled)

    @hybrid_property
    def alerts_active(self):
        return self.interested_in_alerts and bool(self.alerts_approved)

    @alerts_active.expression
    def alerts_active(cls):
        return cls.interested_in_alerts & cls.alerts_approved

    alerts_approved: Mapped[bool] = mapped_column(default=False)
    admin_level: Mapped[int] = mapped_column(Integer, default=20)
    receive_level: Mapped[int] = mapped_column(Integer, default=20)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserInterest(Base):
    """Extensible opt-in flags; authority/approval never lives in this table."""

    __tablename__ = "user_interests"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)


class LinkCode(Base):
    __tablename__ = "link_codes"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    panel_client_id: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (UniqueConstraint("idempotency_key"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Broadcast(Base):
    photo_id: Mapped[str | None] = mapped_column(Text)
    audience_group: Mapped[str | None] = mapped_column(String(64).evaluates_none(), nullable=True, default="vpn", server_default="vpn")
    entities: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")
    __tablename__ = "broadcasts"
    edit_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    edit_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[dict] = mapped_column(JSONType, default=list)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GraphSnapshot(Base):
    __tablename__ = "graph_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    png: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    render_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chart_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class GraphJob(Base):
    """Bounded render intent; generation and lease fence all late publication."""
    __tablename__ = "graph_jobs"
    __table_args__ = (Index("ix_graph_jobs_due", "state", "available_at"),)
    id: Mapped[str] = mapped_column(ForeignKey("graph_snapshots.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONType)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TelegramCard(Base):
    __tablename__ = "telegram_cards"
    message_kind: Mapped[str] = mapped_column(String(8), default="text", server_default="text")
    graph_capture: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="firing")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    spi_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    spi_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    link_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    desired_link_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    desired_spi_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SudoRequest(Base):
    __tablename__ = "sudo_requests"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_name: Mapped[str] = mapped_column(String(128))
    node: Mapped[str] = mapped_column(String(128))
    argv: Mapped[list] = mapped_column(JSONType)
    argv_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Incident(Base):
    __tablename__ = "incidents"
    graph_capture: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graph_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    level: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="firing")
    history: Mapped[list] = mapped_column(JSONType, default=list)


class AlertOutbox(Base):
    __tablename__ = "alert_outbox"
    __table_args__ = (Index("ix_alert_outbox_due", "done", "available_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payload: Mapped[dict] = mapped_column(JSONType)
    done: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CardDelivery(Base):
    """Recipient intent, authoritative content revision and cross-process HTTP lease."""
    __tablename__ = "card_deliveries"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    message_generation: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    delivered_revision: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    spi_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    spi_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    buttons: Mapped[list] = mapped_column(JSONType, default=list)
    link_revision: Mapped[int] = mapped_column(Integer, default=0)
    pressed_operation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BroadcastCopy(Base):
    applied_photo_id: Mapped[str | None] = mapped_column(Text)
    applied_entities: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]")
    __tablename__ = "broadcast_copies"
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    broadcast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broadcasts.id"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    applied_text: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertImage(Base):
    __tablename__ = "alert_images"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    mime_type: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(100))


class ImageDelivery(Base):
    __tablename__ = "image_deliveries"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResendClaim(Base):
    """Committed before Telegram I/O; an ambiguous send must not be retried blindly."""
    __tablename__ = "resend_claims"
    broadcast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broadcasts.id"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class ApiGrant(Base):
    __tablename__ = 'api_grants'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    scopes: Mapped[list] = mapped_column(JSONType)
    chats: Mapped[list] = mapped_column(JSONType)
    expires: Mapped[int] = mapped_column(BigInteger)
