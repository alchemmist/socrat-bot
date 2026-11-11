import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from socrat_bot import db as database
from socrat_bot.escalation import (
    button,
    drain_one,
    escalate,
    marker,
    route,
    token,
)
from socrat_bot.logic import alert_levels, ensure_user
from socrat_bot.models import (
    Action,
    AlertOutbox,
    Audit,
    Base,
    CardDelivery,
    Incident,
    TelegramCard,
    User,
    utcnow,
)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, monkeypatch):
    if request.param == "postgres":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip(
                "Set TEST_POSTGRES_URL to an isolated disposable PostgreSQL database"
            )
        assert "test" in url, (
            "TEST_POSTGRES_URL must identify a disposable test database"
        )
        engine = create_engine(url)
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(
        database, "_Session", sessionmaker(engine, expire_on_commit=False)
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    monkeypatch.setenv("ALERT_LEVELS", "10,20")
    yield request.param
    Base.metadata.drop_all(engine)
    engine.dispose()


def seed():
    with database.session() as db:
        for id_, admin, receive in [(1, 10, 20), (2, 20, 20), (3, 10, 10), (4, 10, 20)]:
            db.add(
                User(
                    telegram_id=id_,
                    role="owner" if id_ == 1 else "user",
                    alerts_enabled=True,
                    admin_level=admin,
                    receive_level=receive,
                )
            )
        db.flush()
        assert route(db, "disk", True) == [1, 2, 4]
        for id_ in (1, 2, 4):
            db.add(
                TelegramCard(
                    key="disk",
                    chat_id=id_,
                    generation=1,
                    status="firing",
                    message_id=100 + id_,
                    text="<b>Disk</b>\nbody",
                    spi_url="https://example.org/spi",
                    spi_label="SPI",
                    note="investigating",
                )
            )
            db.add(
                Action(
                    type="alert.silence",
                    chat_id=id_,
                    idempotency_key=f"disk:g1:15:{id_}",
                    payload={"key": "disk", "minutes": 15, "card": "<b>Disk</b>"},
                    message_id=100 + id_,
                )
            )

        db.flush()
        # Seed represents already-successfully-delivered cards, not pending initial intents.
        for card in db.scalars(select(TelegramCard)):
            state = db.get(CardDelivery, (card.key, card.chat_id))
            state.text, state.spi_url, state.spi_label, state.note = (
                card.text,
                card.spi_url,
                card.spi_label,
                card.note,
            )
            state.message_generation = card.generation
            state.revision = state.delivered_revision = 1
        db.execute(delete(AlertOutbox))


def transition(actor=2, chat=2, generation=1, expected=20, message=102):
    with database.session() as db:
        return escalate(db, actor, chat, message, token("disk"), generation, expected)


def test_levels_defaults(store, monkeypatch):
    with database.session() as db:
        owner = ensure_user(db, 1)
        user = ensure_user(db, 9)
        assert (owner.admin_level, owner.receive_level) == (10, 20)
        assert (user.admin_level, user.receive_level, user.alerts_enabled) == (
            20,
            20,
            False,
        )
    for raw in ["", "0,20", "20,10", "10,10", "-1,20", "a"]:
        monkeypatch.setenv("ALERT_LEVELS", raw)
        with pytest.raises(ValueError):
            alert_levels()
    monkeypatch.setenv("ALERT_LEVELS", "5,10,20,40")
    assert alert_levels() == [5, 10, 20, 40]


def test_routing_receive_change_no_replay_and_disabled_edits(store):
    seed()
    with database.session() as db:
        db.scalar(select(User).where(User.telegram_id == 3)).receive_level = 20
        db.scalar(select(User).where(User.telegram_id == 2)).alerts_enabled = False
        assert set(route(db, "disk", True)) == {1, 2, 4}
        assert set(route(db, "disk", False)) == {1, 2, 4}
        assert set(route(db, "disk", True)) == {1, 3, 4}
        incident = db.get(Incident, "disk")
        assert (incident.generation, incident.level, incident.history) == (2, 20, [])


def test_transition_audit_outbox_and_stale(store):
    seed()
    assert transition() == "Escalated"
    assert "Stale" in transition()
    with database.session() as db:
        incident = db.get(Incident, "disk")
        assert incident.level == 10
        assert incident.history[0]["actor"] == 2
        assert incident.history[0]["at"]
        rows = list(db.scalars(select(AlertOutbox)))
        assert {r.payload["chat_id"] for r in rows if r.payload["kind"] == "card"} == {
            1,
            2,
            3,
            4,
        }
        # Existing destination admins receive a short notice; new arrival gets no duplicate notice.
        assert {
            r.payload["chat_id"] for r in rows if r.payload["kind"] == "notice"
        } == {1, 4}
        assert (
            len(list(db.scalars(select(Audit).where(Audit.event == "alert.escalated"))))
            == 1
        )
        assert button(db, incident, 2) is None
        assert button(db, incident, 1) is None  # last tier
        assert "Escalated: 20 → 10" in marker("<b>Disk</b>", incident)
        assert marker(marker("<b>Disk</b>", incident), incident) == marker(
            "<b>Disk</b>", incident
        )
        route(db, "disk", False)
    assert "Stale" in transition()
    with database.session() as db:
        route(db, "disk", True)
    assert "Stale" in transition()


@pytest.mark.parametrize(
    "mutation",
    [
        "disabled",
        "foreign_chat",
        "foreign_message",
        "missing_copy",
        "authority",
        "generation",
        "empty",
    ],
)
def test_rejections(store, mutation):
    seed()
    args = {}
    with database.session() as db:
        if mutation == "disabled":
            db.scalar(select(User).where(User.telegram_id == 2)).alerts_enabled = False
        elif mutation == "foreign_chat":
            args["chat"] = 1
        elif mutation == "foreign_message":
            args["message"] = 999
        elif mutation == "missing_copy":
            db.delete(db.get(TelegramCard, ("disk", 2)))
        elif mutation == "authority":
            db.scalar(select(User).where(User.telegram_id == 2)).admin_level = 40
        elif mutation == "generation":
            args["generation"] = 2
        elif mutation == "empty":
            for user in db.scalars(select(User).where(User.admin_level == 10)):
                user.alerts_enabled = False
    assert transition(**args) != "Escalated"
    with database.session() as db:
        assert db.get(Incident, "disk").level == 20
        assert not list(db.scalars(select(AlertOutbox)))


def test_multiple_tiers_no_skip_actor_exclusion(store, monkeypatch):
    monkeypatch.setenv("ALERT_LEVELS", "5,10,20")
    seed()
    with database.session() as db:
        db.add(
            User(
                telegram_id=5,
                role="user",
                alerts_enabled=True,
                admin_level=5,
                receive_level=5,
            )
        )
    assert transition(actor=1, chat=1, message=101) == "Escalated"
    with database.session() as db:
        assert db.get(Incident, "disk").level == 10
        assert button(db, db.get(Incident, "disk"), 2) is None
        assert button(db, db.get(Incident, "disk"), 1)[2] == 1
        assert not any(
            r.payload["kind"] == "notice" and r.payload["chat_id"] == 1
            for r in db.scalars(select(AlertOutbox))
        )
    assert transition(actor=1, chat=1, message=101) != "Escalated"
    assert transition(actor=1, chat=1, message=101, expected=10) == "Escalated"
    with database.session() as db:
        assert db.get(Incident, "disk").level == 5


def test_concurrent_cas(store):
    if store != "postgres":
        pytest.skip("Real concurrent writes tested on PostgreSQL")
    seed()
    barrier = Barrier(2)

    def click():
        barrier.wait()
        return transition()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: click(), range(2)))
    assert results.count("Escalated") == 1
    with database.session() as db:
        assert len(db.get(Incident, "disk").history) == 1
        assert len(list(db.scalars(select(AlertOutbox)))) == 6


def mock_telegram(monkeypatch, fail=False):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            calls.append((url.rsplit("/", 1)[-1], json))
            if hasattr(database._engine.pool, "checkedout"):
                assert database._engine.pool.checkedout() == 0
            # Prove no checked-out DB connection is retained during Telegram calls.
            if fail:
                raise httpx.ReadTimeout("ambiguous timeout")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})

    monkeypatch.setattr("socrat_bot.escalation.httpx.AsyncClient", Client)
    return calls


def test_outbox_retry_restart_new_card_metadata_mute(store, monkeypatch):
    seed()
    assert transition() == "Escalated"
    calls = mock_telegram(monkeypatch, fail=True)
    assert asyncio.run(drain_one()) is False
    with database.session() as db:
        leased = db.scalar(select(AlertOutbox).where(AlertOutbox.attempts == 1))
        assert not leased.done
        leased.available_at = utcnow() - timedelta(seconds=1)
        for state in db.scalars(select(CardDelivery)):
            state.lease_until = utcnow() - timedelta(seconds=1)
    calls = mock_telegram(monkeypatch)
    for _ in range(12):
        asyncio.run(drain_one())
    with database.session() as db:
        assert all(r.done for r in db.scalars(select(AlertOutbox)))
        new = db.get(TelegramCard, ("disk", 3))
        assert new.message_id == 999
        assert new.generation == 1
        assert new.spi_url == "https://example.org/spi"
        assert new.note == "investigating"
        assert "Escalated: 20 → 10" in new.text
        mute = db.scalar(select(Action).where(Action.chat_id == 3))
        assert mute.message_id == 999
    mains = [
        body
        for method, body in calls
        if method == "sendMessage" and body["chat_id"] == 3
    ]
    assert len(mains) == 1
    assert mains[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Mute 15m"
    assert len([body for method, body in calls if "Open alert" in body["text"]]) == 2


def test_outbox_disables_new_deliveries_and_invalidates_generation(store, monkeypatch):
    seed()
    transition()
    with database.session() as db:
        db.scalar(select(User).where(User.telegram_id == 3)).alerts_enabled = False
    calls = mock_telegram(monkeypatch)
    for _ in range(12):
        asyncio.run(drain_one())
    assert not any(body["chat_id"] == 3 for _, body in calls)
    with database.session() as db:
        db.add(
            AlertOutbox(
                payload={"kind": "card", "key": "disk", "generation": 1, "chat_id": 1}
            )
        )
        route(db, "disk", False)
        route(db, "disk", True)
    before = len(calls)
    asyncio.run(drain_one())
    assert len(calls) == before


def test_muted_card_outbox_has_no_buttons(store, monkeypatch):
    seed()
    with database.session() as db:
        db.get(TelegramCard, ("disk", 1)).text += "\n\n<i>Muted for 15 minutes</i>"
        db.get(CardDelivery, ("disk", 1)).text += "\n\n<i>Muted for 15 minutes</i>"
    transition()
    calls = mock_telegram(monkeypatch)
    for _ in range(12):
        asyncio.run(drain_one())
    edited = next(
        body
        for method, body in calls
        if method == "editMessageText" and body["chat_id"] == 1
    )
    assert edited["reply_markup"] == {"inline_keyboard": []}
    assert "Muted for 15 minutes" in edited["text"]


def test_api_card_keyboard_generation_and_no_replay(store, monkeypatch):
    from socrat_bot.app import OutMessage, post_message
    from socrat_bot.cards import build_alert

    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    calls = mock_telegram(monkeypatch)
    with database.session() as db:
        for id_, admin, receive in [(1, 10, 20), (2, 20, 20), (3, 10, 10)]:
            db.add(
                User(
                    telegram_id=id_,
                    role="owner" if id_ == 1 else "user",
                    alerts_enabled=True,
                    admin_level=admin,
                    receive_level=receive,
                )
            )
    payload = {"labels": {"alertname": "Disk"}, "status": "firing"}
    key = build_alert(payload).key

    def post(status="firing"):
        return asyncio.run(
            post_message(
                OutMessage(chat_id=9999, alert={**payload, "status": status}), "test"
            )
        )

    first = post()
    assert {c["chat_id"] for c in first["copies"]} == {1, 2}
    for method, body in calls:
        rows = body["reply_markup"]["inline_keyboard"]
        assert len(rows[0]) == 3
        assert len(rows[1]) == 1
        assert rows[1][0]["text"] == "Escalate"
        assert len(rows[1][0]["callback_data"].encode()) <= 64
        assert "Level 20" in body["text"]
    with database.session() as db:
        db.scalar(select(User).where(User.telegram_id == 3)).receive_level = 20
        db.scalar(select(User).where(User.telegram_id == 2)).alerts_enabled = False
    calls.clear()
    assert {c["chat_id"] for c in post()["copies"]} == {1, 2}
    assert all(method == "editMessageText" for method, body in calls)
    calls.clear()
    post("resolved")
    assert all(body["reply_markup"] == {"inline_keyboard": []} for _, body in calls)
    calls.clear()
    assert {c["chat_id"] for c in post()["copies"]} == {1, 3}
    assert all(method == "sendMessage" for method, body in calls)
    with database.session() as db:
        incident = db.get(Incident, key)
        assert incident.generation == 2
        assert incident.level == 20
        assert db.get(TelegramCard, (key, 3)).generation == 2
        assert button(db, incident, 2) is None


def test_no_audience_never_falls_back_to_body_chat(store, monkeypatch):
    from socrat_bot.app import OutMessage, post_message

    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    calls = mock_telegram(monkeypatch)
    result = asyncio.run(
        post_message(
            OutMessage(chat_id=9999, alert={"labels": {"alertname": "Disk"}}), "test"
        )
    )
    assert result["copies"] == []
    assert calls == []


def test_commands_private_settings_owner_assignment(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socrat_bot.app import on_alertlevel, on_alerts

    seed()
    message = SimpleNamespace(reply_text=AsyncMock())

    def update(id_, chat=None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=id_, username=None),
            effective_chat=SimpleNamespace(id=id_ if chat is None else chat),
            effective_message=message,
        )

    asyncio.run(on_alerts(update(1), SimpleNamespace(args=["level", "10"])))
    with database.session() as db:
        assert db.scalar(select(User).where(User.telegram_id == 1)).receive_level == 10
    asyncio.run(on_alerts(update(1), SimpleNamespace(args=["off"])))
    asyncio.run(on_alerts(update(1), SimpleNamespace(args=[])))
    assert "Alerts: disabled" in message.reply_text.call_args.args[0]
    asyncio.run(on_alerts(update(1), SimpleNamespace(args=["on"])))
    asyncio.run(on_alertlevel(update(1), SimpleNamespace(args=["3", "20"])))
    with database.session() as db:
        user = db.scalar(select(User).where(User.telegram_id == 3))
        assert (user.admin_level, user.receive_level) == (20, 20)
    asyncio.run(on_alertlevel(update(2), SimpleNamespace(args=["3", "10"])))
    assert "Owner only" in message.reply_text.call_args.args[0]
    asyncio.run(on_alerts(update(3, chat=-9), SimpleNamespace(args=["off"])))
    assert "private chat" in message.reply_text.call_args.args[0]
    with database.session() as db:
        assert db.scalar(select(User).where(User.telegram_id == 3)).alerts_enabled
    asyncio.run(on_alerts(update(2), SimpleNamespace(args=["level", "10"])))
    assert "Invalid" in message.reply_text.call_args.args[0]


def test_transition_and_outbox_are_one_transaction(store):
    seed()
    with pytest.raises(RuntimeError), database.session() as db:
        assert escalate(db, 2, 2, 102, token("disk"), 1, 20) == "Escalated"
        raise RuntimeError("simulate transaction failure")
    with database.session() as db:
        assert db.get(Incident, "disk").level == 20
        assert db.get(Incident, "disk").history == []
        assert not list(db.scalars(select(AlertOutbox)))
        assert not list(db.scalars(select(Audit)))


def test_pg_outbox_workers_claim_distinct_work(store):
    from socrat_bot.escalation import prepare_work

    if store != "postgres":
        pytest.skip("Concurrent leases require PostgreSQL")
    seed()
    transition()
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        for _ in range(10):
            work = prepare_work()
            if work:
                return work[0], work[3]["chat_id"]
        raise AssertionError("no independent work claimed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: claim(), range(2)))
    assert len({id_ for id_, _chat in ids}) == 2
    assert len({chat for _id, chat in ids}) == 2


def test_receive_callback_private_and_approved(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socrat_bot.app import on_callback

    seed()
    query = SimpleNamespace(
        data="rl:10",
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat_id=1),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    asyncio.run(on_callback(SimpleNamespace(callback_query=query), None))
    assert "Receive level: 10" in query.edit_message_text.call_args.args[0]
    query.message.chat_id = -100
    query.data = "rl:20"
    asyncio.run(on_callback(SimpleNamespace(callback_query=query), None))
    assert query.answer.call_args.args[0] == "Not allowed"
    with database.session() as db:
        assert db.scalar(select(User).where(User.telegram_id == 1)).receive_level == 10


def test_deterministic_marker_preserves_title_spi_note_mute():
    from socrat_bot.cards import render_escalation

    text = '<b>Level warning</b>\nbody\n<a href="https://example.org">SPI</a>\nnote\n\n<i>Muted</i>'
    history = [{"from": 20, "level": 10, "actor": 1, "at": "2026-01-01T00:00:00Z"}]
    marked = render_escalation(text, 10, history)
    assert marked.splitlines()[0] == "<b>Level warning</b>"
    assert marked.replace("<b>Level 10</b> · Escalated: 20 → 10\n", "") == text
    assert render_escalation(marked, 10, history) == marked


@pytest.mark.parametrize("next_generation", [False, True])
def test_inflight_resolve_or_generation_retired_durably(
    store, monkeypatch, next_generation
):
    from socrat_bot.escalation import _deliver_work, prepare_work

    seed()
    transition()
    work = prepare_work()
    assert work[3]["kind"] == "card"
    calls = mock_telegram(monkeypatch)
    with database.session() as db:
        route(db, "disk", False)
        for card in db.scalars(select(TelegramCard)):
            card.status = "resolved"
            card.text = "[Resolved] " + card.text
        if next_generation:
            route(db, "disk", True)
    # Simulate a Telegram request already prepared on another replica.
    assert asyncio.run(_deliver_work(work))
    with database.session() as db:
        jobs = list(db.scalars(select(AlertOutbox)))
        assert any(
            j.payload["kind"] == ("retire" if next_generation else "card")
            and not j.done
            for j in jobs
        )
    for _ in range(20):
        asyncio.run(drain_one())
    repairs = [
        body for method, body in calls[1:] if body["chat_id"] == work[3]["chat_id"]
    ]
    assert any(
        body["text"].startswith("[Resolved] ")
        and body["reply_markup"] == {"inline_keyboard": []}
        for body in repairs
    )


def initial_users():
    with database.session() as db:
        for id_, receive in [(1, 20), (2, 20), (3, 10)]:
            db.add(
                User(
                    telegram_id=id_,
                    role="owner" if id_ == 1 else "user",
                    alerts_enabled=True,
                    admin_level=10 if id_ != 2 else 20,
                    receive_level=receive,
                )
            )


def response_client(
    monkeypatch, *, rejected=None, http_barrier=None, http_release=None, block_chat=None
):
    from threading import Lock

    calls, lock = [], Lock()
    rejected = rejected if rejected is not None else set()

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            method = url.rsplit("/", 1)[-1]
            with lock:
                calls.append((method, dict(json)))
            if http_barrier and (block_chat is None or json["chat_id"] == block_chat):
                http_barrier.set()
                assert http_release.wait(15), "test HTTP release timed out"
            return httpx.Response(
                200,
                json={
                    "ok": json["chat_id"] not in rejected,
                    "result": {"message_id": 1000 + json["chat_id"]},
                },
            )

    monkeypatch.setattr("socrat_bot.escalation.httpx.AsyncClient", Client)
    return calls


@pytest.mark.parametrize("partial", [False, True])
def test_initial_failure_retry_keeps_original_audience(store, monkeypatch, partial):
    from fastapi import HTTPException

    from socrat_bot.app import OutMessage, post_message

    initial_users()
    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    rejected = {2} if partial else {1, 2}
    calls = response_client(monkeypatch, rejected=rejected)
    payload = {"labels": {"alertname": "InitialRetry"}}

    def post():
        return asyncio.run(post_message(OutMessage(chat_id=999, alert=payload), "test"))

    if partial:
        assert {c["chat_id"] for c in post()["copies"]} == {1}
    else:
        with pytest.raises(HTTPException) as error:
            post()
        assert error.value.status_code == 502
    with database.session() as db:
        assert {s.chat_id for s in db.scalars(select(CardDelivery))} == {1, 2}
        assert list(db.scalars(select(AlertOutbox).where(AlertOutbox.done.is_(False))))
        # Neither approval nor receive changes join the frozen initial audience.
        db.scalar(select(User).where(User.telegram_id == 3)).receive_level = 20
        db.add(
            User(
                telegram_id=4,
                role="user",
                alerts_enabled=True,
                admin_level=20,
                receive_level=20,
            )
        )
    rejected.clear()
    result = post()
    assert {c["chat_id"] for c in result["copies"]} == {1, 2}
    assert not any(body["chat_id"] in {3, 4, 999} for _, body in calls)
    if partial:
        assert (
            len(
                [
                    1
                    for method, body in calls
                    if method == "sendMessage" and body["chat_id"] == 1
                ]
            )
            == 1
        )


def test_initial_crash_before_http_recovers_without_webhook(store, monkeypatch):
    from socrat_bot.cards import build_alert
    from socrat_bot.delivery import submit_card

    initial_users()
    card = build_alert({"labels": {"alertname": "CrashBeforeHTTP"}})
    recipients, _jobs = submit_card(card)
    assert recipients == [1, 2]
    # Process exits here; another stateless worker consumes durable intent + latest content.
    with database.session() as db:
        assert not list(db.scalars(select(TelegramCard)))
        db.scalar(select(User).where(User.telegram_id == 3)).receive_level = 20
    calls = response_client(monkeypatch)
    for _ in range(10):
        asyncio.run(drain_one())
    assert {body["chat_id"] for method, body in calls if method == "sendMessage"} == {
        1,
        2,
    }
    with database.session() as db:
        assert all(
            s.delivered_revision == s.revision for s in db.scalars(select(CardDelivery))
        )


def test_initial_intents_rollback_with_incident(store):
    initial_users()
    with pytest.raises(RuntimeError), database.session() as db:
        route(db, "rollback-initial", True)
        raise RuntimeError("crash before commit")
    with database.session() as db:
        assert not list(db.scalars(select(Incident)))
        assert not list(db.scalars(select(CardDelivery)))
        assert not list(db.scalars(select(AlertOutbox)))


def test_pg_first_webhooks_upsert_one_initial_snapshot_and_main(store, monkeypatch):
    if store != "postgres":
        pytest.skip("Independent PostgreSQL sessions required")
    from threading import Event

    from socrat_bot import escalation
    from socrat_bot.app import OutMessage, post_message
    from socrat_bot.cards import build_alert

    initial_users()
    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    original_insert = escalation.insert_once
    insert_barrier, http_started, release = Barrier(2), Event(), Event()

    def concurrent_insert(db, model, values):
        if model is Incident:
            insert_barrier.wait(timeout=10)
        return original_insert(db, model, values)

    monkeypatch.setattr(escalation, "insert_once", concurrent_insert)
    calls = response_client(
        monkeypatch, http_barrier=http_started, http_release=release, block_chat=1
    )
    payload = {"labels": {"alertname": "ConcurrentFirst"}}
    key = build_alert(payload).key

    def post():
        return asyncio.run(post_message(OutMessage(chat_id=999, alert=payload), "test"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post) for _ in range(2)]
        try:
            assert http_started.wait(10)
            with database.session() as db:
                assert len(list(db.scalars(select(Incident)))) == 1
                assert {
                    s.chat_id
                    for s in db.scalars(
                        select(CardDelivery).where(CardDelivery.key == key)
                    )
                } == {1, 2}
        finally:
            release.set()
        results = [f.result(timeout=30) for f in futures]
    assert all({c["chat_id"] for c in r["copies"]} == {1, 2} for r in results)
    assert (
        len(
            [
                1
                for method, body in calls
                if method == "sendMessage" and body["chat_id"] == 1
            ]
        )
        == 1
    )
    assert (
        len(
            [
                1
                for method, body in calls
                if method == "sendMessage" and body["chat_id"] == 2
            ]
        )
        == 1
    )
    assert results[0]["id"] == results[1]["id"]


@pytest.mark.parametrize("pause_at", ["http", "persistence"])
def test_pg_same_recipient_cross_worker_exclusion_no_success_duplicates(
    store, monkeypatch, pause_at
):
    if store != "postgres":
        pytest.skip("Independent PostgreSQL sessions required")
    from threading import Event

    from socrat_bot import escalation
    from socrat_bot.escalation import _deliver_work, enqueue_card, prepare_work

    seed()
    transition()
    with database.session() as db:
        first_id = next(
            r.id
            for r in db.scalars(select(AlertOutbox))
            if r.payload["kind"] == "card" and r.payload["chat_id"] == 3
        )
    paused, release = Event(), Event()
    calls = response_client(
        monkeypatch,
        http_barrier=paused if pause_at == "http" else None,
        http_release=release,
        block_chat=3,
    )
    original_finish = escalation.finish_work

    def blocked_finish(work, data):
        if work[3]["chat_id"] == 3:
            paused.set()
            assert release.wait(15)
        return original_finish(work, data)

    if pause_at == "persistence":
        monkeypatch.setattr(escalation, "finish_work", blocked_finish)

    def worker_a():
        work = prepare_work(first_id)
        assert work and work[1] == "sendMessage"
        return asyncio.run(_deliver_work(work))

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(worker_a)
        try:
            assert paused.wait(10)
            with database.session() as db:
                second_id = enqueue_card(db, db.get(Incident, "disk"), 3)
            # Different job, connection, worker, same recipient: excluded even after HTTP success before commit.
            assert pool.submit(prepare_work, second_id).result(timeout=10) is None
            assert (
                len(
                    [
                        1
                        for method, body in calls
                        if method == "sendMessage" and body["chat_id"] == 3
                    ]
                )
                == 1
            )
        finally:
            release.set()
        assert future.result(timeout=15)
        second = pool.submit(prepare_work, second_id).result(timeout=10)
        assert second and second[1] == "editMessageText"
        assert pool.submit(lambda: asyncio.run(_deliver_work(second))).result(
            timeout=15
        )
    assert (
        len(
            [
                1
                for method, body in calls
                if method == "sendMessage" and body["chat_id"] == 3
            ]
        )
        == 1
    )


@pytest.mark.parametrize("mutation", ["text", "note", "spi", "mute"])
def test_pg_same_stage_newer_revision_survives_old_http_finish(
    store, monkeypatch, mutation
):
    if store != "postgres":
        pytest.skip("Independent PostgreSQL sessions required")
    from threading import Event

    from socrat_bot.cards import Card
    from socrat_bot.delivery import annotate_card, submit_card
    from socrat_bot.escalation import _deliver_work, enqueue_card, prepare_work

    seed()
    with database.session() as db:
        first_id = enqueue_card(db, db.get(Incident, "disk"), 1)
    paused, release = Event(), Event()
    calls = response_client(
        monkeypatch, http_barrier=paused, http_release=release, block_chat=1
    )

    def old_worker():
        work = prepare_work(first_id)
        assert work
        return asyncio.run(_deliver_work(work))

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(old_worker)
        try:
            assert paused.wait(10)
            if mutation == "text":
                submit_card(Card(key="disk", title="Disk", summary="NEW CONTENT"))
                expected = "NEW CONTENT"
            elif mutation == "note":
                annotate_card("disk", note="NEW NOTE")
                expected = "NEW NOTE"
            elif mutation == "spi":
                annotate_card("disk", url="https://example.org/NEW-SPI", label="SPI")
                expected = "NEW-SPI"
            else:
                annotate_card("disk", mute="<i>NEW MUTE</i>")
                expected = "NEW MUTE"
            with database.session() as db:
                desired = db.get(CardDelivery, ("disk", 1))
                assert expected in desired.text
                target_revision = desired.revision
            # Another worker cannot overtake the older successful HTTP operation.
            for job in list_pending_for_chat("disk", 1):
                assert pool.submit(prepare_work, job).result(timeout=10) is None
        finally:
            release.set()
        assert future.result(timeout=15)
    for _ in range(20):
        asyncio.run(drain_one())
    with database.session() as db:
        state, card = (
            db.get(CardDelivery, ("disk", 1)),
            db.get(TelegramCard, ("disk", 1)),
        )
        assert state.revision == target_revision
        assert state.delivered_revision == target_revision
        assert expected in state.text and expected in card.text
    final = [body for method, body in calls if body["chat_id"] == 1][-1]
    assert expected in final["text"]
    if mutation == "mute":
        assert final["reply_markup"] == {"inline_keyboard": []}


def list_pending_for_chat(key, chat_id):
    with database.session() as db:
        return [
            r.id
            for r in db.scalars(select(AlertOutbox).where(AlertOutbox.done.is_(False)))
            if r.payload["key"] == key and r.payload["chat_id"] == chat_id
        ]


def test_pg_simultaneous_bot_broker_schema_initialization(store, tmp_path, monkeypatch):
    if store != "postgres":
        pytest.skip("PostgreSQL advisory migration ownership required")
    import subprocess
    import sys
    import time
    from pathlib import Path

    from sqlalchemy import text

    Base.metadata.drop_all(database._engine)
    with database._engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    env = dict(os.environ)
    env["DATABASE_URL"] = database._engine.url.render_as_string(hide_password=False)
    env["PYTHONPATH"] = str(Path("src").resolve())
    script = """
import pathlib,sys,time
from socrat_bot.db import init_schema
root=pathlib.Path(sys.argv[1]); (root/sys.argv[2]).touch()
while not (root/'go').exists(): time.sleep(0.01)
init_schema()
"""

    def simultaneous_start():
        for filename in ("bot", "broker", "go"):
            (tmp_path / filename).unlink(missing_ok=True)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), name],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in ("bot", "broker")
        ]
        try:
            deadline = time.monotonic() + 15
            while not all((tmp_path / name).exists() for name in ("bot", "broker")):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            (tmp_path / "go").touch()
            for process in processes:
                _stdout, stderr = process.communicate(timeout=30)
                assert process.returncode == 0, stderr
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    simultaneous_start()  # first version-table creation also races without advisory lock
    with database._engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0019_graph_source"
        )
    command = [sys.executable, "-m", "alembic"]
    subprocess.run(
        command + ["downgrade", "0007_card_note"],
        env=env,
        capture_output=True,
        check=True,
    )
    with database._engine.begin() as connection:
        import uuid

        for tid, role in [(1, "owner"), (2, "user")]:
            connection.execute(
                text(
                    "INSERT INTO users (id, telegram_id, role, uses_vpn, alerts_enabled, created_at) VALUES (:id, :tid, :role, false, true, CURRENT_TIMESTAMP)"
                ),
                {"id": uuid.uuid4(), "tid": tid, "role": role},
            )
        connection.execute(
            text(
                "INSERT INTO telegram_cards (key, chat_id, message_id, generation, status, text) VALUES ('upgrade-card',1,99,3,'firing','Existing')"
            )
        )
    simultaneous_start()  # populated 0007→0011, two independent startup processes
    with database.session() as db:
        assert [
            (u.admin_level, u.receive_level)
            for u in db.scalars(select(User).order_by(User.telegram_id))
        ] == [(10, 20), (20, 20)]
        state = db.get(CardDelivery, ("upgrade-card", 1))
        assert (state.generation, state.message_generation, state.text) == (
            3,
            3,
            "Existing",
        )
    # Upstream 0009 may already be deployed with a newer accepted SPI fence whose
    # Telegram edit failed. Upgrade must preserve applied vs desired and recover it.
    subprocess.run(
        command + ["downgrade", "0009_spi_desired_revision"],
        env=env,
        capture_output=True,
        check=True,
    )
    old_url, new_url = "https://example.org/old", "https://example.org/new"
    with database._engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE telegram_cards SET spi_url=:old, spi_label='SPI', link_revision=1, desired_link_revision=2, desired_spi_url=:new, text=:text WHERE key='upgrade-card'"
            ),
            {
                "old": old_url,
                "new": new_url,
                "text": f'Existing\n<a href="{old_url}">SPI</a>',
            },
        )
        connection.execute(
            text(
                "INSERT INTO telegram_cards (key,chat_id,message_id,generation,status,text,spi_url,spi_label,link_revision,desired_link_revision) VALUES ('historical-card',2,202,1,'firing','History',:url,'SPI',7,0)"
            ),
            {"url": old_url},
        )
    simultaneous_start()
    with database.session() as db:
        card = db.get(TelegramCard, ("upgrade-card", 1))
        state = db.get(CardDelivery, ("upgrade-card", 1))
        assert (card.link_revision, card.spi_url) == (1, old_url)
        assert (card.desired_link_revision, card.desired_spi_url) == (2, new_url)
        assert (state.link_revision, state.spi_url) == (2, new_url)
        assert db.get(CardDelivery, ("historical-card", 2)).link_revision == 7
        assert any(
            row.payload["kind"] == "spi" for row in db.scalars(select(AlertOutbox))
        )
    calls = response_client(monkeypatch)
    for _ in range(5):
        asyncio.run(drain_one())
    with database.session() as db:
        card = db.get(TelegramCard, ("upgrade-card", 1))
        assert card.link_revision == card.desired_link_revision == 2
        assert card.spi_url == card.desired_spi_url == new_url
        assert old_url not in card.text and card.text.count(">SPI</a>") == 1
    assert calls and new_url in calls[0][1]["text"]
    from socrat_bot.cards import Card
    from socrat_bot.delivery import dispatch_jobs, submit_card

    _people, jobs = submit_card(Card(key="historical-card", title="History update"))
    asyncio.run(dispatch_jobs(jobs))
    with database.session() as db:
        assert db.get(TelegramCard, ("historical-card", 2)).link_revision == 7
    subprocess.run(
        command + ["downgrade", "0007_card_note"],
        env=env,
        capture_output=True,
        check=True,
    )
    simultaneous_start()


def test_big_telegram_id_durable_claim(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import submit_card

    with database.session() as db:
        db.add(
            User(
                telegram_id=8_000_000_001,
                role="user",
                alerts_enabled=True,
                admin_level=20,
                receive_level=20,
            )
        )
    submit_card(Card(key="big-id", title="Big ID"))
    calls = response_client(monkeypatch)
    for _ in range(4):
        asyncio.run(drain_one())
    assert (
        len(
            [
                1
                for method, body in calls
                if method == "sendMessage" and body["chat_id"] == 8_000_000_001
            ]
        )
        == 1
    )


def test_initial_timeout_restart_waits_for_shared_lease(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import submit_card
    from socrat_bot.escalation import prepare_work

    initial_users()
    _recipients, jobs = submit_card(Card(key="initial-timeout", title="Timeout"))
    mock_telegram(monkeypatch, fail=True)
    work = prepare_work(jobs[0])
    from socrat_bot.escalation import _deliver_work

    assert not asyncio.run(_deliver_work(work))
    with database.session() as db:
        second_id = db.scalar(select(AlertOutbox.id).where(AlertOutbox.id != work[0]))
        state = db.get(CardDelivery, ("initial-timeout", work[3]["chat_id"]))
        assert state.lease_owner is not None
        state.lease_until = utcnow() - timedelta(seconds=1)
        db.get(AlertOutbox, work[0]).available_at = utcnow() - timedelta(seconds=1)
    calls = response_client(monkeypatch)
    assert second_id is not None
    for _ in range(8):
        asyncio.run(drain_one())
    assert {body["chat_id"] for method, body in calls if method == "sendMessage"} == {
        1,
        2,
    }


def test_custom_card_actions_keep_keyboard_and_message_identity(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import dispatch_jobs, submit_card

    initial_users()
    _people, jobs = submit_card(
        Card(key="custom", title="Custom"),
        [{"type": "xui.upgrade", "label": "Upgrade", "payload": {}, "row": 0}],
    )
    calls = response_client(monkeypatch)
    asyncio.run(dispatch_jobs(jobs))
    for _method, body in calls:
        assert body["reply_markup"]["inline_keyboard"][0][0]["text"] == "Upgrade"
    with database.session() as db:
        for action in db.scalars(select(Action).where(Action.type == "xui.upgrade")):
            assert action.message_id == 1000 + action.chat_id


def test_patch_known_card_uses_shared_revision_pipeline(store, monkeypatch):
    from socrat_bot.app import PatchMessage, patch_message

    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    seed()
    calls = response_client(monkeypatch)
    result = asyncio.run(
        patch_message(101, PatchMessage(text="Replaced <plain>"), 1, "test")
    )
    assert result == {"id": 101}
    assert calls[0][0] == "editMessageText"
    assert "Replaced &lt;plain&gt;" in calls[0][1]["text"]
    with database.session() as db:
        state = db.get(CardDelivery, ("disk", 1))
        assert state.delivered_revision == state.revision
        assert state.spi_url == "https://example.org/spi"
        assert state.note == "investigating"


def test_expired_owner_is_fenced_before_http_and_persistence(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import submit_card
    from socrat_bot.escalation import _deliver_work, finish_work, prepare_work

    initial_users()
    _people, jobs = submit_card(Card(key="fenced", title="Fence"))
    old = prepare_work(jobs[0])
    assert old
    with database.session() as db:
        state = db.get(CardDelivery, ("fenced", old[3]["chat_id"]))
        state.lease_until = utcnow() - timedelta(seconds=1)
        db.get(AlertOutbox, old[0]).available_at = utcnow() - timedelta(seconds=1)
    new = prepare_work(jobs[0])
    assert new and new[3]["_owner"] != old[3]["_owner"]
    calls = response_client(monkeypatch)
    assert not asyncio.run(_deliver_work(old))
    assert calls == []
    assert not finish_work(old, {"ok": True, "result": {"message_id": 999999}})
    assert asyncio.run(_deliver_work(new))
    assert len(calls) == 1
    with database.session() as db:
        card = db.get(TelegramCard, ("fenced", new[3]["chat_id"]))
        assert card.message_id == 1000 + new[3]["chat_id"]


def test_escalation_readmits_old_generation_copy_with_new_main(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import dispatch_jobs, submit_card

    seed()
    calls = response_client(monkeypatch)
    assert transition() == "Escalated"
    for _ in range(15):
        asyncio.run(drain_one())
    for status in ("resolved", "firing"):
        _people, jobs = submit_card(Card(key="disk", title="Disk", status=status))
        asyncio.run(dispatch_jobs(jobs))
    with database.session() as db:
        old = db.get(CardDelivery, ("disk", 3))
        assert old.generation == 1
        assert db.get(Incident, "disk").generation == 2
    assert transition(generation=2, message=1002) == "Escalated"
    for _ in range(20):
        asyncio.run(drain_one())
    assert (
        len(
            [
                1
                for method, body in calls
                if method == "sendMessage" and body["chat_id"] == 3
            ]
        )
        == 2
    )
    with database.session() as db:
        state = db.get(CardDelivery, ("disk", 3))
        card = db.get(TelegramCard, ("disk", 3))
        assert state.generation == state.message_generation == card.generation == 2
        assert state.delivered_revision == state.revision
        assert "Escalated: 20 → 10" in card.text


def test_mute_completion_cannot_annotate_refired_incident(store, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from socrat_bot import app

    seed()
    entered, released = Event(), Event()
    with database.session() as db:
        action_id = db.scalar(select(Action.id).where(Action.chat_id == 2))

    async def apply(_db, _action, _actor):
        entered.set()
        assert await asyncio.to_thread(released.wait, 10)
        return {"status": "silenced", "minutes": 15}

    async def answer(*args, **kwargs):
        pass

    async def forbidden_edit(*args, **kwargs):
        pytest.fail("Old mute must not edit the new generation")

    monkeypatch.setattr(app, "apply_action", apply)
    query = SimpleNamespace(
        data=f"act:{action_id}",
        from_user=SimpleNamespace(id=2, username=None),
        message=SimpleNamespace(chat_id=2, message_id=102),
        answer=answer,
        edit_message_text=forbidden_edit,
    )
    context = SimpleNamespace(bot=SimpleNamespace(edit_message_text=forbidden_edit))
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            asyncio.run, app.on_callback(SimpleNamespace(callback_query=query), context)
        )
        assert entered.wait(10)
        try:
            with database.session() as db:
                route(db, "disk", False)
                route(db, "disk", True)
        finally:
            released.set()
        result.result(timeout=15)
    with database.session() as db:
        incident = db.get(Incident, "disk")
        assert incident.generation == 2
        assert incident.level == 20
        for state in db.scalars(
            select(CardDelivery).where(CardDelivery.generation == 2)
        ):
            assert not state.text or "Muted" not in state.text


def spi_pairs():
    return [{"chat_id": i, "message_id": 100 + i} for i in (1, 2, 4)]


def test_spi_desired_fence_before_http_rejects_prepared_old_snapshot(
    store, monkeypatch
):
    from fastapi import HTTPException

    from socrat_bot.delivery import publish_spi, spi_acknowledgements
    from socrat_bot.escalation import _deliver_work, prepare_work

    seed()
    old_url, new_url = "https://example.org/spi#old", "https://example.org/spi#new"
    pairs, old_jobs = publish_spi("disk", old_url, "SPI", 1, spi_pairs())
    old_work = prepare_work(old_jobs[0])
    pairs, new_jobs = publish_spi("disk", new_url, "SPI", 2, spi_pairs())
    calls = response_client(monkeypatch)
    assert not asyncio.run(_deliver_work(old_work))
    assert calls == []
    with database.session() as db:
        for card in db.scalars(select(TelegramCard)):
            assert card.desired_link_revision == 2 and card.desired_spi_url == new_url
            assert card.link_revision == 0 and card.spi_url != new_url
    with pytest.raises(HTTPException) as error:
        publish_spi("disk", old_url, "SPI", 1, spi_pairs())
    assert error.value.status_code == 409
    for _ in range(20):
        asyncio.run(drain_one())
    assert not spi_acknowledgements("disk", pairs, old_jobs, old_url, 1)["updated"]
    assert (
        len(spi_acknowledgements("disk", pairs, new_jobs, new_url, 2)["updated"]) == 3
    )
    assert all(
        new_url in body["text"] and body["text"].count(">SPI</a>") == 1
        for _, body in calls
    )


def test_pg_spi_inflight_newer_failed_fence_keeps_ack_pending(store, monkeypatch):
    if store != "postgres":
        pytest.skip("Independent PostgreSQL sessions required")
    from threading import Event

    from socrat_bot.app import CardLinkIn, add_card_link
    from socrat_bot.delivery import publish_spi

    seed()
    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    started, release = Event(), Event()
    calls, reject_new = [], [True]
    old_url, new_url = "https://example.org/spi#old", "https://example.org/spi#new"

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            calls.append(dict(json))
            if old_url in json["text"] and json["chat_id"] == 1:
                started.set()
                assert release.wait(15)
            return httpx.Response(
                200, json={"ok": not (reject_new[0] and new_url in json["text"])}
            )

    monkeypatch.setattr("socrat_bot.escalation.httpx.AsyncClient", Client)
    old = CardLinkIn(key="disk", url=old_url, revision=1, expected_copies=spi_pairs())
    new = old.model_copy(update={"url": new_url, "revision": 2})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: asyncio.run(add_card_link(old, "test")))
        try:
            assert started.wait(10)
            with database.session() as db:
                assert all(
                    c.desired_link_revision == 1
                    for c in db.scalars(select(TelegramCard))
                )
            publish_spi("disk", new_url, "SPI", 2, spi_pairs())
        finally:
            release.set()
        response = future.result(timeout=30)
    assert response["updated"] == [] and len(response["pending"]) == 3
    with database.session() as db:
        for card in db.scalars(select(TelegramCard)):
            assert card.desired_link_revision == 2 and card.desired_spi_url == new_url
            assert card.link_revision < 2 and card.spi_url != new_url
    previous_calls = len(calls)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        asyncio.run(add_card_link(old, "test"))
    assert error.value.status_code == 409 and len(calls) == previous_calls
    reject_new[0] = False
    response = asyncio.run(add_card_link(new, "test"))
    assert len(response["updated"]) == 3 and response["pending"] == []
    with database.session() as db:
        for card in db.scalars(select(TelegramCard)):
            assert card.link_revision == card.desired_link_revision == 2
            assert card.spi_url == card.desired_spi_url == new_url
            assert card.text.count(">SPI</a>") == 1


@pytest.mark.parametrize("writer", ["webhook", "note", "mute"])
def test_pg_spi_fence_survives_concurrent_other_card_writers(
    store, monkeypatch, writer
):
    if store != "postgres":
        pytest.skip("Independent PostgreSQL sessions required")
    from threading import Event

    from socrat_bot.cards import Card
    from socrat_bot.delivery import annotate_card, publish_spi, submit_card
    from socrat_bot.escalation import _deliver_work, prepare_work

    seed()
    old_url, new_url = "https://example.org/spi#old", "https://example.org/spi#new"
    _pairs, jobs = publish_spi("disk", old_url, "SPI", 1, spi_pairs())
    work = prepare_work(jobs[0])
    started, release = Event(), Event()
    calls = response_client(
        monkeypatch, http_barrier=started, http_release=release, block_chat=1
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: asyncio.run(_deliver_work(work)))
        try:
            assert started.wait(10)
            publish_spi("disk", new_url, "SPI", 2, spi_pairs())
            if writer == "webhook":
                submit_card(Card(key="disk", title="NEW WEBHOOK"))
                expected = "NEW WEBHOOK"
            elif writer == "note":
                annotate_card("disk", note="NEW NOTE")
                expected = "NEW NOTE"
            else:
                annotate_card("disk", mute="<i>NEW MUTE</i>")
                expected = "NEW MUTE"
        finally:
            release.set()
        assert future.result(timeout=15)
    for _ in range(30):
        asyncio.run(drain_one())
    with database.session() as db:
        for card in db.scalars(select(TelegramCard)):
            assert card.link_revision == card.desired_link_revision == 2
            assert card.spi_url == card.desired_spi_url == new_url
            assert expected in card.text and card.text.count(">SPI</a>") == 1
    assert expected in [body for _, body in calls if body["chat_id"] == 1][-1]["text"]


def test_spi_episode_without_approved_audience_never_falls_back(store, monkeypatch):
    from socrat_bot.app import OutMessage, post_message

    monkeypatch.setenv("INTERNAL_TOKEN", "test")
    calls = response_client(monkeypatch)
    result = asyncio.run(
        post_message(
            OutMessage(
                chat_id=999,
                alert={"spi_episode": "d" * 64, "labels": {"alertname": "NoAudience"}},
            ),
            "test",
        )
    )
    assert (
        result["copies"] == []
        and result["immutable_key"]
        and result["episode"] == "d" * 64
    )
    assert calls == []


def test_legacy_unversioned_spi_can_replace_target_until_versioned_fence(
    store, monkeypatch
):
    from fastapi import HTTPException

    from socrat_bot.delivery import dispatch_jobs, publish_spi, spi_acknowledgements

    seed()
    calls = response_client(monkeypatch)
    for suffix in ("first", "second"):
        url = "https://example.org/" + suffix
        pairs, jobs = publish_spi("disk", url, "SPI", None, None)
        asyncio.run(dispatch_jobs(jobs))
        result = spi_acknowledgements("disk", pairs, jobs, url, None)
        assert len(result["updated"]) == 3 and not result["pending"]
    assert all(body["text"].count(">SPI</a>") == 1 for _, body in calls)
    pairs, jobs = publish_spi(
        "disk", "https://example.org/versioned", "SPI", 1, spi_pairs()
    )
    asyncio.run(dispatch_jobs(jobs))
    with pytest.raises(HTTPException) as error:
        publish_spi("disk", "https://example.org/third", "SPI", None, None)
    assert error.value.status_code == 409


def test_spi_association_reaches_original_pending_initial_recipient(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import (
        dispatch_jobs,
        publish_spi,
        spi_acknowledgements,
        submit_card,
    )

    initial_users()
    _people, jobs = submit_card(Card(key="initial-spi", title="Initial SPI"))
    calls = response_client(monkeypatch)
    asyncio.run(dispatch_jobs([jobs[0]]))
    with database.session() as db:
        assert db.get(TelegramCard, ("initial-spi", 1)).message_id == 1001
        assert db.get(TelegramCard, ("initial-spi", 2)) is None
    url = "https://example.org/initial-spi"
    pairs, link_jobs = publish_spi(
        "initial-spi", url, "SPI", 1, [{"chat_id": 1, "message_id": 1001}]
    )
    asyncio.run(dispatch_jobs(link_jobs))
    assert (
        len(spi_acknowledgements("initial-spi", pairs, link_jobs, url, 1)["updated"])
        == 1
    )
    for _ in range(15):
        asyncio.run(drain_one())
    second_main = [
        body
        for method, body in calls
        if method == "sendMessage" and body["chat_id"] == 2
    ]
    assert len(second_main) == 1 and url in second_main[0]["text"]
    with database.session() as db:
        card = db.get(TelegramCard, ("initial-spi", 2))
        assert card.link_revision == card.desired_link_revision == 1
        assert card.spi_url == card.desired_spi_url == url


def test_pg_spi_acceptance_refreshes_copy_materialized_during_lock_wait(
    store, monkeypatch
):
    if store != "postgres":
        pytest.skip("Independent PostgreSQL sessions required")
    from threading import Event

    from socrat_bot import delivery
    from socrat_bot.cards import Card
    from socrat_bot.delivery import (
        dispatch_jobs,
        publish_spi,
        spi_acknowledgements,
        submit_card,
    )
    from socrat_bot.escalation import _deliver_work, prepare_work

    initial_users()
    _people, jobs = submit_card(Card(key="spi-materialize", title="Materialize"))
    response_client(monkeypatch)
    asyncio.run(dispatch_jobs([jobs[0]]))
    second = prepare_work(jobs[1])
    assert second and second[1] == "sendMessage"
    waiting, release = Event(), Event()
    original = delivery.delivery_state

    def paused_state(db, key, chat, generation):
        state = original(db, key, chat, generation)
        if key == "spi-materialize" and chat == 1:
            waiting.set()
            assert release.wait(15)
        return state

    monkeypatch.setattr(delivery, "delivery_state", paused_state)
    url = "https://example.org/materialized"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            publish_spi,
            "spi-materialize",
            url,
            "SPI",
            1,
            [{"chat_id": 1, "message_id": 1001}],
        )
        try:
            assert waiting.wait(10)
            assert asyncio.run(_deliver_work(second))
        finally:
            release.set()
        pairs, link_jobs = future.result(timeout=15)
    assert {pair["chat_id"] for pair in pairs} == {1, 2}
    asyncio.run(dispatch_jobs(link_jobs))
    result = spi_acknowledgements("spi-materialize", pairs, link_jobs, url, 1)
    assert len(result["updated"]) == 2 and result["pending"] == []


def test_unsubscribe_preserves_approval_and_resubscribe_needs_no_owner(store, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socrat_bot import app

    with database.session() as db:
        ensure_user(db, 1)
        person = ensure_user(db, 2)
        person.uses_vpn = True
    bot = SimpleNamespace(send_message=AsyncMock())

    def query(actor=2, data="al:yes", chat=2):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=actor, username=None),
            message=SimpleNamespace(chat_id=chat, reply_text=AsyncMock()),
            data=data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            get_bot=lambda: bot,
        )

    asyncio.run(app._request_alerts(query()))
    assert bot.send_message.await_count == 1
    assert bot.send_message.call_args.args[0] == 1  # first request reaches owner
    with database.session() as db:
        person = db.scalar(select(User).where(User.telegram_id == 2))
        assert not person.alerts_enabled and not person.alerts_approved
    asyncio.run(app._approve_alerts(query(actor=1, data="al:ok:2", chat=1)))
    bot.send_message.reset_mock()
    for method in ("command", "button", "alerts off"):
        user = SimpleNamespace(id=2, username=None)
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=2), effective_message=msg)
        if method == "command":
            asyncio.run(app.on_unsubscribe(update, SimpleNamespace()))
        elif method == "button":
            asyncio.run(app.on_callback(SimpleNamespace(callback_query=query(data="al:no")), SimpleNamespace()))
        else:
            asyncio.run(app.on_alerts(update, SimpleNamespace(args=["off"])))
        with database.session() as db:
            person = db.scalar(select(User).where(User.telegram_id == 2))
            assert not person.alerts_enabled
            assert person.alerts_approved and person.uses_vpn
            assert person.admin_level == person.receive_level == 20
            assert 2 not in route(db, "new-after-unsubscribe-" + method, True)
        # Merely opening /start (ensure_user) must not resubscribe.
        with database.session() as db:
            assert not ensure_user(db, 2).alerts_enabled
        asyncio.run(app._request_alerts(query()))
        assert bot.send_message.await_count == 0
        with database.session() as db:
            person = db.scalar(select(User).where(User.telegram_id == 2))
            assert person.alerts_enabled and person.alerts_approved
            assert list(db.scalars(select(TelegramCard))) == []  # no backlog replay
    assert app._start_keyboard(True, True).inline_keyboard[0][0].callback_data == "al:no"
    assert app._start_keyboard(True, False).inline_keyboard[0][0].callback_data == "al:yes"


def test_unsubscribe_private_and_unapproved_cannot_enable(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socrat_bot import app

    with database.session() as db:
        ensure_user(db, 1)
        ensure_user(db, 2)
    reply = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=2, username=None), effective_chat=SimpleNamespace(id=2), effective_message=SimpleNamespace(reply_text=reply))
    asyncio.run(app.on_alerts(update, SimpleNamespace(args=["on"])))
    assert "Approval required" in reply.call_args.args[0]
    with database.session() as db:
        assert not ensure_user(db, 2).alerts_enabled
        owner = ensure_user(db, 1)
        owner.alerts_enabled = True
    update.effective_user.id = 1
    update.effective_chat.id = -123
    asyncio.run(app.on_unsubscribe(update, SimpleNamespace()))
    with database.session() as db:
        assert ensure_user(db, 1).alerts_enabled
    update.effective_chat.id = 1
    asyncio.run(app.on_unsubscribe(update, SimpleNamespace()))
    with database.session() as db:
        owner = ensure_user(db, 1)
        assert owner.alerts_approved and not owner.alerts_enabled


def test_old_approval_button_cannot_undo_unsubscribe(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socrat_bot import app
    from socrat_bot.logic import approve_alerts

    with database.session() as db:
        ensure_user(db, 1)
        person = approve_alerts(db, 2)
        person.alerts_enabled = False
    bot = SimpleNamespace(send_message=AsyncMock())
    query = SimpleNamespace(data="al:ok:2", from_user=SimpleNamespace(id=1), answer=AsyncMock(), edit_message_text=AsyncMock(), get_bot=lambda: bot)
    asyncio.run(app._approve_alerts(query))
    with database.session() as db:
        person = ensure_user(db, 2)
        assert person.alerts_approved and not person.alerts_enabled
    assert bot.send_message.await_count == 0


def test_unsubscribe_fences_prepared_new_delivery(store, monkeypatch):
    from socrat_bot.cards import Card
    from socrat_bot.delivery import submit_card
    from socrat_bot.escalation import _deliver_work, prepare_work

    with database.session() as db:
        ensure_user(db, 1)
        person = ensure_user(db, 2)
        person.alerts_enabled = True
        person.alerts_approved = True
    _recipients, jobs = submit_card(Card(key="prepared-unsubscribe", title="Test", where="", summary="", status="firing"))
    work = next(w for id_ in jobs if (w := prepare_work(id_)) and w[2]["chat_id"] == 2)
    with database.session() as db:
        db.scalar(select(User).where(User.telegram_id == 2)).alerts_enabled = False
    calls = response_client(monkeypatch)
    assert not asyncio.run(_deliver_work(work))
    assert calls == []
