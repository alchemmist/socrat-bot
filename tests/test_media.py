import asyncio
import base64
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from test_escalation import store  # noqa: F401

from socrat_bot import app, media
from socrat_bot.cards import Card
from socrat_bot.db import session
from socrat_bot.delivery import submit_card
from socrat_bot.models import (
    AlertImage,
    CardDelivery,
    ImageDelivery,
    Incident,
    TelegramCard,
    User,
    utcnow,
)

pytestmark = pytest.mark.usefixtures("store")

REAL_CLIENT = httpx.AsyncClient

PNG = b"\x89PNG\r\n\x1a\nexample"


def image(raw=PNG):
    return {"data_base64": base64.b64encode(raw).decode(), "mime_type": "image/png"}


def prepare(raw=PNG):
    with session() as db:
        db.add(User(telegram_id=1, role="user", alerts_enabled=True, receive_level=20))
    submit_card(
        Card(key="disk", title="Disk", status="firing"),
        images=[media.ImageIn(**image(raw))],
    )
    with session() as db:
        state = db.get(CardDelivery, ("disk", 1))
        state.message_generation = 1
        db.add(TelegramCard(key="disk", chat_id=1, message_id=71, generation=1))


def transport(monkeypatch, *, fail=False):
    calls = []
    real = REAL_CLIENT

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"ok": False, "description": "failed"}
            if fail
            else {"ok": True, "result": {"message_id": 90}},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handle), **kw),
    )
    return calls


def test_multipart_dedup_update(monkeypatch):
    prepare()
    calls = transport(monkeypatch)
    asyncio.run(media.drain())
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/sendPhoto")
    assert PNG in calls[0].content
    assert b'"message_id": 71' in calls[0].content
    assert b'"allow_sending_without_reply": false' in calls[0].content
    assert media.status("disk", [1])[0]["status"] == "sent"
    submit_card(
        Card(key="disk", title="Disk", status="firing"),
        images=[media.ImageIn(**image())],
    )
    asyncio.run(media.drain())
    assert len(calls) == 1
    submit_card(
        Card(key="disk", title="Disk", status="firing"),
        images=[media.ImageIn(**image(PNG + b"new"))],
    )
    asyncio.run(media.drain())
    assert calls[-1].url.path.endswith("/editMessageMedia")
    assert b"90" in calls[-1].content
    assert len(calls) == 2
    with session() as db:
        assert db.scalar(select(ImageDelivery)).message_id == 90


def test_failure_restart_omission_and_unsubscribe(monkeypatch):
    prepare()
    calls = transport(monkeypatch, fail=True)
    asyncio.run(media.drain())
    assert len(calls) == 1
    assert media.status("disk", [1])[0]["status"] == "pending"
    submit_card(Card(key="disk", title="Disk", status="firing"), images=[])
    with session() as db:
        assert db.scalar(select(AlertImage)).data == PNG
        db.scalar(select(ImageDelivery)).available_at = utcnow() - timedelta(seconds=1)
        db.scalar(select(User)).alerts_enabled = False
    asyncio.run(media.drain())
    assert len(calls) == 1
    with session() as db:
        db.scalar(select(User)).alerts_enabled = True
    asyncio.run(media.drain())
    assert len(calls) == 2


def test_generation_and_ownership_fence():
    prepare()
    assert media._claim("unrelated") is None
    work = media._claim()
    assert work
    assert media._claim() is None
    with session() as db:
        db.get(Incident, "disk").generation = 2
        db.get(CardDelivery, ("disk", 1)).generation = 2
    assert not media._eligible(work)
    with session() as db:
        db.get(CardDelivery, ("disk", 1)).lease_owner = None
    media._finish(work, 123)
    with session() as db:
        assert db.scalar(select(ImageDelivery)).message_id is None


def test_api_validation_and_auth(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.api), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/messages", json={"chat_id": 1, "images": [image()]}
            )
            assert response.status_code == 401
            response = await client.post(
                "/v1/messages",
                headers={"x-internal-token": "secret"},
                json={"chat_id": 1, "images": [image()]},
            )
            assert response.status_code == 400
            for value in [
                {"data_base64": "SECRET!", "mime_type": "image/png"},
                image(b"invalid"),
                image() | {"filename": "../bad"},
                image() | {"url": "http://internal"},
            ]:
                response = await client.post(
                    "/v1/messages",
                    headers={"x-internal-token": "secret"},
                    json={"chat_id": 1, "images": [value]},
                )
                assert response.status_code == 422
                assert "SECRET" not in response.text
            for field in ["card", "alert", "alerts"]:
                content = {
                    "card": {"key": "disk", "title": "Disk"},
                    "alert": {"labels": {"alertname": "Disk"}},
                    "alerts": [{"labels": {"alertname": "Disk"}}],
                }[field]
                response = await client.post(
                    "/v1/messages",
                    headers={"x-internal-token": "secret"},
                    json={"chat_id": 1, field: content, "images": [image()]},
                )
                assert response.status_code == 200, response.text

    asyncio.run(run())


def test_api_text_success_photo_failure(monkeypatch):
    prepare()
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")
    real = REAL_CLIENT
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/sendPhoto"):
            return httpx.Response(
                200, json={"ok": False, "description": "photo rejected"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 71}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handle), **kw),
    )

    async def run():
        async with real(
            transport=httpx.ASGITransport(app=app.api), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/messages",
                headers={"x-internal-token": "secret"},
                json={
                    "chat_id": 1,
                    "card": {"key": "disk", "title": "Disk"},
                    "images": [image()],
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["id"] == 71
            assert data["attachments"][0]["status"] == "pending"
            assert "data_base64" not in response.text

    asyncio.run(run())
    assert any(c.url.path.endswith("/sendPhoto") for c in calls)


def test_escalation_retained_images_only_new_intents(monkeypatch):
    from socrat_bot.escalation import escalate, token

    prepare()
    with session() as db:
        owner = db.scalar(select(User))
        owner.admin_level = 10
        db.add(User(telegram_id=2, role="user", alerts_enabled=True, receive_level=10))
        db.add(User(telegram_id=3, role="user", alerts_enabled=True, receive_level=20))
    calls = transport(monkeypatch)
    asyncio.run(media.drain())
    with session() as db:
        assert escalate(db, 1, 1, 71, token("disk"), 1, 20) == "Escalated"
    with session() as db:
        # Simulate text receipts after escalation; unrelated newly subscribed 3
        # is admitted by explicit escalation too (not by media/settings itself).
        intents = db.scalars(select(CardDelivery)).all()
        admitted = {s.chat_id for s in intents}
        assert 2 in admitted
        for state in intents:
            state.message_generation = 1
            if state.chat_id != 1:
                db.add(
                    TelegramCard(
                        key="disk",
                        chat_id=state.chat_id,
                        message_id=70 + state.chat_id,
                        generation=1,
                    )
                )
    asyncio.run(media.drain())
    assert len(calls) == len(admitted)
    with session() as db:
        db.add(User(telegram_id=4, role="user", alerts_enabled=True, receive_level=20))
    asyncio.run(media.drain())
    assert len(calls) == len(admitted)


def test_expired_lease_restart_and_edit_failure(monkeypatch):
    prepare()
    abandoned = media._claim()
    assert abandoned
    with session() as db:
        db.get(CardDelivery, ("disk", 1)).lease_until = utcnow() - timedelta(seconds=1)
    calls = transport(monkeypatch)
    asyncio.run(media.drain())
    assert len(calls) == 1
    media._finish(abandoned, 999)
    with session() as db:
        assert db.scalar(select(ImageDelivery)).message_id == 90
    submit_card(
        Card(key="disk", title="Disk", status="firing"),
        images=[media.ImageIn(**image(PNG + b"changed"))],
    )
    # Same photo ID is retained even when Telegram rejects media edits.
    calls = transport(monkeypatch, fail=True)
    asyncio.run(media.drain())
    with session() as db:
        row = db.scalar(select(ImageDelivery))
        assert row.message_id == 90
        row.available_at = utcnow() - timedelta(seconds=1)
    asyncio.run(media.drain())
    assert len(calls) == 2
    assert all(c.url.path.endswith("/editMessageMedia") for c in calls)


def test_parallel_claim_single_owner(request):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if request.getfixturevalue("store") != "postgres":
        pytest.skip("parallel database connections use disposable PostgreSQL")
    prepare()
    gate = Barrier(2)

    def claim():
        gate.wait(timeout=5)
        return media._claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(r is not None for r in results) == 1
    work = next(r for r in results if r)
    with session() as db:
        db.scalar(select(User)).alerts_enabled = False
    assert not media._eligible(work)


def test_validation_bounds_and_optional_compatibility():
    from pydantic import ValidationError

    for value in [
        image(PNG + b"x" * media.MAX_BYTES),
        image() | {"data_base64": image()["data_base64"] + "="},
        image() | {"mime_type": "image/gif"},
        image() | {"filename": "a" * 101},
    ]:
        with pytest.raises(ValidationError):
            media.ImageIn(**value)
    with pytest.raises(ValidationError):
        app.OutMessage(chat_id=1, images=[image()] * 4)
    for optional in [{}, {"images": None}, {"images": []}]:
        message = app.OutMessage(chat_id=1, text="legacy", **optional)
        assert not message.images
        assert app.card_from_request(message) is None


def test_postlease_refresh_barriers(request, monkeypatch):
    """Two connections: pause claimant before its lease UPDATE, publish/finish."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from sqlalchemy import event

    from socrat_bot import db as database

    if request.getfixturevalue("store") != "postgres":
        pytest.skip("requires PostgreSQL READ COMMITTED")
    prepare()
    # Seed pending receipt before installing the barrier.
    old = media._claim()
    media._finish(old, None)
    with session() as db:
        db.scalar(select(ImageDelivery)).available_at = utcnow() - timedelta(seconds=1)
    reached, resume = Event(), Event()
    armed = [True]

    def pause(conn, cursor, statement, parameters, context, executemany):
        if (armed[0] and current_thread().name.startswith("claimant")
                and statement.startswith("UPDATE card_deliveries")):
            armed[0] = False
            reached.set()
            assert resume.wait(10)

    event.listen(database._engine, "before_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="claimant") as pool:
            future = pool.submit(media._claim)
            assert reached.wait(10)
            submit_card(Card(key="disk", title="Disk"),
                        images=[media.ImageIn(**image(PNG + b"B"))])
            resume.set()
            work = future.result(10)
        assert work["data"] == PNG + b"B"
        assert work["revision"] == hashlib.sha256(work["data"]).hexdigest()
        media._finish(work, 90)
        # A -> B -> A must require an edit, not suppress A as already delivered.
        submit_card(Card(key="disk", title="Disk"), images=[media.ImageIn(**image())])
        a = media._claim()
        assert a and a["data"] == PNG and a["photo"] == 90
        media._finish(a, None)
        with session() as db:
            db.scalar(select(ImageDelivery)).available_at = utcnow() - timedelta(seconds=1)
        # Claimant read pending, then another connection durably finishes A.
        reached.clear()
        resume.clear()
        armed[0] = True
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="claimant") as pool:
            future = pool.submit(media._claim)
            assert reached.wait(10)
            winner = media._claim()
            assert winner
            media._finish(winner, 90)
            resume.set()
            assert future.result(10) is None
        with session() as db:
            assert db.get(CardDelivery, ("disk", 1)).lease_owner is None
    finally:
        resume.set()
        event.remove(database._engine, "before_cursor_execute", pause)


@pytest.fixture
def heartbeat_database(request, tmp_path, monkeypatch):
    """Use independent SQLite connections for concurrent claim/heartbeat transactions.

    StaticPool's single in-memory connection lets one session roll back another
    thread's renewal. A file database exercises SQLite's real transaction locks;
    PostgreSQL keeps the original disposable backend and all assertions remain.
    """
    if request.getfixturevalue("store") != "sqlite":
        yield
        return
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from socrat_bot import db as database
    from socrat_bot.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'heartbeat_test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_Session", sessionmaker(engine, expire_on_commit=False))
    try:
        yield
    finally:
        engine.dispose()


def test_progress_transport_deadline_and_heartbeat(monkeypatch, heartbeat_database):
    prepare()
    # Allow real PostgreSQL round trips on loaded CI runners; 120 ms could
    # expire before two renewals despite a healthy heartbeat. Keep the actual
    # wall-clock deadline and all ownership/cleanup assertions exercised.
    monkeypatch.setattr(media, "HTTP_DEADLINE", 1.0)
    monkeypatch.setattr(media, "HEARTBEAT_INTERVAL", .02)
    renewals = []
    renew = media._renew

    def counted(work):
        renewals.append(work["owner"])
        return renew(work)

    monkeypatch.setattr(media, "_renew", counted)

    async def handle(request):
        while True:  # progressing transport: inactivity timeout cannot bound this
            await asyncio.sleep(.01)
            assert await asyncio.to_thread(media._claim) is None

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(handle), **kw))
    asyncio.run(media.drain_one())
    assert len(renewals) >= 2
    count = len(renewals)
    with session() as db:
        state = db.get(CardDelivery, ("disk", 1))
        assert state.lease_owner
        assert utcnow() < state.lease_until.replace(tzinfo=utcnow().tzinfo) < utcnow() + timedelta(seconds=61)
    assert media._claim() is None
    assert len(renewals) == count


def test_submission_snapshot_superseded():
    prepare()
    recipients, _jobs, snapshot = submit_card(
        Card(key="disk", title="Disk"), images=[media.ImageIn(**image())], metadata=True)
    submit_card(Card(key="disk", title="Disk"), images=[media.ImageIn(**image(PNG + b"B"))])
    assert media.status("disk", recipients, snapshot)[0]["status"] == "superseded"
    with session() as db:
        db.get(Incident, "disk").generation = 2
    assert media.status("disk", recipients, snapshot)[0]["status"] == "superseded"
    assert media._claim("disk", snapshot["generation"]) is None


def test_text_only_never_drains_media(monkeypatch):
    prepare()
    monkeypatch.setenv("INTERNAL_TOKEN", "secret")
    transport(monkeypatch)

    async def forbidden(**kwargs):
        pytest.fail("text-only request drained retained media")

    monkeypatch.setattr(media, "drain", forbidden)

    async def run():
        async with REAL_CLIENT(transport=httpx.ASGITransport(app=app.api),
                               base_url="http://test") as client:
            for optional in [{}, {"images": None}, {"images": []}]:
                response = await client.post("/v1/messages",
                    headers={"x-internal-token": "secret"}, json={
                        "chat_id": 1, "card": {"key": "disk", "title": "Disk"},
                        **optional})
                assert response.status_code == 200
                assert "attachments" not in response.json()

    asyncio.run(run())


def test_heartbeat_failure_does_not_discard_confirmed_photo(monkeypatch):
    prepare()
    monkeypatch.setattr(media, "HEARTBEAT_INTERVAL", .001)

    def broken_renew(work):
        raise RuntimeError("transient heartbeat failure")

    async def handle(request):
        await asyncio.sleep(.02)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 90}})

    monkeypatch.setattr(media, "_renew", broken_renew)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(handle), **kw))
    assert asyncio.run(media.drain_one())
    with session() as db:
        assert db.get(ImageDelivery, ("disk", 1, 1, 0)).message_id == 90
        assert db.get(CardDelivery, ("disk", 1)).lease_owner is None
