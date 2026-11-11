import asyncio
import json

import httpx
import pytest
from test_escalation import store  # noqa: F401

from socrat_bot.cards import Card
from socrat_bot.db import session
from socrat_bot.delivery import annotate_card, submit_card
from socrat_bot.escalation import _deliver_work, prepare_work
from socrat_bot.models import GraphSnapshot, TelegramCard, User
from socrat_bot.primary_graphs import capture

pytestmark = pytest.mark.usefixtures("store")
REAL_CLIENT = httpx.AsyncClient
PNG = b"\x89PNG\r\n\x1a\nsynthetic-transport-fixture"


def seed():
    with session() as db:
        db.add(
            User(
                telegram_id=1,
                role="owner",
                alerts_enabled=True,
                admin_level=10,
                receive_level=20,
            )
        )
        db.add(GraphSnapshot(id="a" * 64, png=PNG, render_digest="c" * 64))
    submit_card(
        Card(key="graph", title="Latency", summary="Existing text"),
        primary_graph="a" * 64,
    )


def mock_transport(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 100 + len(calls)}}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    return calls


def deliver():
    work = None
    for _ in range(4):
        work = prepare_work()
        if work is not None:
            break
    assert work is not None
    assert asyncio.run(_deliver_work(work))
    return work


def test_one_primary_photo_with_original_text_buttons_and_no_reply(monkeypatch):
    calls = mock_transport(monkeypatch)
    seed()
    work = deliver()
    assert work[1] == "sendPhoto"
    assert "Existing text" in work[2]["text"]
    assert work[2]["reply_markup"]["inline_keyboard"]
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/sendPhoto")
    assert b'name="photo"' in calls[0].content
    assert PNG in calls[0].content
    assert b"Existing text" in calls[0].content
    assert b"reply_to_message_id" not in calls[0].content
    with session() as db:
        row = db.get(TelegramCard, ("graph", 1))
        assert row.message_kind == "photo" and row.graph_capture == "a" * 64
        assert row.message_id == 101


def test_spi_link_and_mute_update_same_photo(monkeypatch):
    calls = mock_transport(monkeypatch)
    seed()
    deliver()
    annotate_card(
        "graph", url="https://github.com/alchemmist/homelab/issues/123", label="SPI"
    )
    assert deliver()[1] == "editMessageCaption"
    assert json.loads(calls[-1].content)["message_id"] == 101
    assert "issues/123" in json.loads(calls[-1].content)["caption"]
    annotate_card("graph", mute="<i>Muted 15m</i>")
    work = deliver()
    assert work[1] == "editMessageCaption"
    assert work[2]["reply_markup"]["inline_keyboard"] == []
    assert len(calls) == 3


def test_resolved_replaces_graph_and_removes_buttons(monkeypatch):
    calls = mock_transport(monkeypatch)
    seed()
    deliver()
    with session() as db:
        db.add(
            GraphSnapshot(id="b" * 64, png=PNG + b"resolved", render_digest="d" * 64)
        )
    submit_card(
        Card(key="graph", title="Latency", status="resolved"), primary_graph="b" * 64
    )
    work = deliver()
    assert work[1] == "editMessageMedia"
    assert work[2]["message_id"] == 101
    assert work[2]["reply_markup"]["inline_keyboard"] == []
    assert b"resolved" in calls[-1].content
    with session() as db:
        row = db.get(TelegramCard, ("graph", 1))
        assert row.status == "resolved" and row.graph_capture == "b" * 64


def test_long_initial_text_falls_back_without_truncating(monkeypatch):
    calls = mock_transport(monkeypatch)
    with session() as db:
        db.add(User(telegram_id=1, role="owner", alerts_enabled=True, receive_level=20))
        db.add(GraphSnapshot(id="a" * 64, png=PNG, render_digest="c" * 64))
    submit_card(
        Card(key="graph", title="Latency", summary="x" * 2000), primary_graph="a" * 64
    )
    work = deliver()
    assert work[1] == "sendMessage"
    assert "x" * 2000 in json.loads(calls[0].content)["text"]


def test_long_photo_update_preserves_full_text_and_retires_old_buttons(monkeypatch):
    calls = mock_transport(monkeypatch)
    seed()
    deliver()
    annotate_card("graph", replacement="x" * 2000)
    work = deliver()
    assert work[1] == "sendMessage"
    assert work[3]["_supersedes_photo"] == 101
    assert "x" * 2000 in json.loads(calls[-1].content)["text"]
    assert deliver()[1] == "editMessageReplyMarkup"
    assert json.loads(calls[-1].content)["reply_markup"]["inline_keyboard"] == []


def test_disabled_capture_does_not_call_gateway(monkeypatch):
    monkeypatch.delenv("ALERT_GRAPHS_ENABLED", raising=False)
    assert asyncio.run(capture("graph", {"annotations": {"spi_graphs": "{}"}})) is None


def test_enabled_capture_is_queued_and_reuses_persisted_timestamp(monkeypatch):
    import hashlib

    from socrat_bot import primary_graphs

    monkeypatch.setenv("ALERT_GRAPHS_ENABLED", "true")
    monkeypatch.setattr(primary_graphs, "_token", lambda: "synthetic-token-only")
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == primary_graphs.GATEWAY
        assert "observed_at" in json.loads(request.content)
        return httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "image/png",
                "x-content-sha256": hashlib.sha256(PNG).hexdigest(),
                "x-render-digest": "d" * 64,
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    raw = {
        "startsAt": "2020-01-01T00:00:00Z",
        "status": "firing",
        "labels": {"node": "synthetic"},
        "annotations": {"spi_graphs": '{"version":1,"graphs":[]}'},
    }
    first = asyncio.run(capture("graph", raw))
    assert first
    assert asyncio.run(capture("graph", raw)) == first
    assert len(calls) == 0
    submit_card(Card(key="graph", title="Latency"), primary_graph=first)
    from socrat_bot.graph_jobs import drain_one

    assert asyncio.run(drain_one())
    assert len(calls) == 1
    assert asyncio.run(capture("graph", raw)) == first
    assert not asyncio.run(drain_one())


def test_render_failure_does_not_prevent_text_delivery(monkeypatch):
    from socrat_bot import primary_graphs

    monkeypatch.setenv("ALERT_GRAPHS_ENABLED", "true")
    monkeypatch.setattr(primary_graphs, "_token", lambda: "synthetic-token-only")

    def handler(request):
        return httpx.Response(503, json={"code": "BUSY"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    identity = asyncio.run(capture("graph", {"annotations": {"spi_graphs": "{}"}}))
    assert identity
    submit_card(Card(key="graph", title="Latency"), primary_graph=identity)
    from socrat_bot.graph_jobs import drain_one

    assert asyncio.run(drain_one())
    with session() as db:
        assert db.get(GraphSnapshot, identity).png is None


def test_caption_budget_counts_astral_characters_without_truncation():
    from socrat_bot.primary_delivery import caption_units

    assert caption_units("ordinary caption") == 16
    assert caption_units("\U0001f534" * 600) == 1200


def queue_graph(monkeypatch, *, key="graph", status="firing"):
    """Create a synthetic newly received intent, bound through the ordinary card writer."""
    from socrat_bot import primary_graphs

    monkeypatch.setenv("ALERT_GRAPHS_ENABLED", "true")
    monkeypatch.setattr(primary_graphs, "_token", lambda: "synthetic-token-only")
    raw = {
        "status": status,
        "startsAt": "2020-01-01T00:00:00Z",
        "endsAt": "2020-01-01T00:05:00Z" if status == "resolved" else None,
        "annotations": {"spi_graphs": '{"version":1,"graphs":[]}'},
    }
    identity = asyncio.run(capture(key, raw))
    submit_card(
        Card(key=key, title="KubePodNotReady", status=status), primary_graph=identity
    )
    return identity


def gateway_response(status=200):
    """Return a bounded transport-only fixture, not a production render or message."""
    import hashlib

    return httpx.Response(
        status,
        content=PNG,
        headers={
            "content-type": "image/png",
            "x-content-sha256": hashlib.sha256(PNG).hexdigest(),
            "x-render-digest": "d" * 64,
        },
    )


def test_transient_render_retries_then_upgrades_same_text_message(monkeypatch):
    """Recover a busy renderer without a second send or lost caption/buttons."""
    from socrat_bot import graph_jobs
    from socrat_bot.models import GraphJob, utcnow

    calls = mock_transport(monkeypatch)
    with session() as db:
        db.add(User(telegram_id=1, role="owner", alerts_enabled=True, receive_level=20))
    identity = queue_graph(monkeypatch)
    assert deliver()[1] == "sendMessage"
    text_id = 101
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(lambda request: gateway_response(503)), **kw
        ),
    )
    assert asyncio.run(graph_jobs.drain_one())
    with session() as db:
        job = db.get(GraphJob, identity)
        assert job.state == "pending" and job.attempts == 1
        assert job.last_code == "HTTP_TRANSIENT"
        observed = job.payload["observed_at"]
        job.available_at = utcnow()
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(lambda request: gateway_response()), **kw
        ),
    )
    assert asyncio.run(graph_jobs.drain_one())

    # Restore Telegram fixture, which must edit the same accepted message.
    def telegram(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": text_id}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(telegram), **kw),
    )
    work = deliver()
    assert work[1] == "editMessageMedia" and work[2]["message_id"] == text_id
    assert "KubePodNotReady" in work[2]["text"]
    assert work[2]["reply_markup"]["inline_keyboard"]
    assert [r.url.path.rsplit("/", 1)[-1] for r in calls] == [
        "sendMessage",
        "editMessageMedia",
    ]
    with session() as db:
        card = db.get(TelegramCard, ("graph", 1))
        job = db.get(GraphJob, identity)
        assert card.message_kind == "photo" and card.message_id == text_id
        assert (
            job.state == "ready"
            and job.attempts == 2
            and job.payload["observed_at"] == observed
        )


def test_late_firing_graph_cannot_replace_new_resolved_intent(monkeypatch):
    """Fence a stale render even within the same incident generation."""
    from socrat_bot.graph_jobs import claim, finish
    from socrat_bot.models import GraphJob

    mock_transport(monkeypatch)
    with session() as db:
        db.add(User(telegram_id=1, role="owner", alerts_enabled=True, receive_level=20))
    old = queue_graph(monkeypatch)
    deliver()
    old_work = claim()
    new = queue_graph(monkeypatch, status="resolved")
    assert old != new
    assert not finish(old_work, (PNG, "c" * 64, False, "READY"))
    new_work = claim()
    assert new_work[0] == new
    assert finish(new_work, (PNG, "d" * 64, False, "READY"))
    work = deliver()
    assert work[1] == "editMessageMedia" and work[2]["message_id"] == 101
    assert work[2]["text"].startswith("[Resolved]")
    assert work[2]["reply_markup"]["inline_keyboard"] == []
    with session() as db:
        assert db.get(GraphJob, old).state == "superseded"
        assert db.get(GraphSnapshot, old).png is None


def test_expired_owner_cannot_publish_after_lease_recovery(monkeypatch):
    """A restarted worker can recover an expired lease without accepting its old owner."""
    from datetime import timedelta

    from socrat_bot.graph_jobs import claim, finish
    from socrat_bot.models import GraphJob, utcnow

    identity = queue_graph(monkeypatch)
    first = claim()
    assert claim() is None
    with session() as db:
        db.get(GraphJob, identity).lease_until = utcnow() - timedelta(seconds=1)
    second = claim()
    assert second[2] != first[2]
    assert not finish(first, (PNG, "c" * 64, False, "READY"))
    assert finish(second, (PNG, "d" * 64, False, "READY"))


def test_duplicate_input_never_resets_attempt_budget(monkeypatch):
    """Finite retries stay terminal even if the same native alert is received again."""
    from socrat_bot import graph_jobs
    from socrat_bot.models import GraphJob, utcnow

    monkeypatch.setattr(graph_jobs, "MAX_ATTEMPTS", 2)
    identity = queue_graph(monkeypatch)
    for _ in range(2):
        work = graph_jobs.claim()
        assert work
        assert not graph_jobs.finish(work, (None, None, True, "HTTP_TRANSIENT"))
        with session() as db:
            db.get(GraphJob, identity).available_at = utcnow()
    assert queue_graph(monkeypatch) == identity
    with session() as db:
        job = db.get(GraphJob, identity)
        assert job.state == "failed" and job.attempts == 2
    assert graph_jobs.claim() is None


def test_expired_job_does_not_render_or_backfill_history(monkeypatch):
    """A newly migrated queue has no historical rows, and aged intents expire locally."""
    from datetime import timedelta

    from socrat_bot.graph_jobs import MAX_AGE, claim
    from socrat_bot.models import GraphJob, utcnow

    with session() as db:
        db.add(GraphSnapshot(id="e" * 64, png=None))
    assert claim() is None  # Bare historical snapshot is not an intent.
    identity = queue_graph(monkeypatch)
    with session() as db:
        db.get(GraphJob, identity).created_at = utcnow() - timedelta(
            seconds=MAX_AGE + 1
        )
    assert claim() is None
    with session() as db:
        assert db.get(GraphJob, identity).state == "expired"
        assert db.get(GraphSnapshot, identity).png is None


def test_queue_and_storage_limits_preserve_text_fallback(monkeypatch):
    """Queue and PNG storage have explicit finite bounds, never silent unlimited growth."""
    from sqlalchemy import func, select

    from socrat_bot import graph_jobs
    from socrat_bot.models import GraphJob

    monkeypatch.setattr(graph_jobs, "MAX_PENDING", 1)
    identity = queue_graph(monkeypatch)
    assert queue_graph(monkeypatch, key="second") is None
    with session() as db:
        assert db.scalar(select(func.count()).select_from(GraphJob)) == 1
    monkeypatch.setattr(graph_jobs, "MAX_STORED_BYTES", 1)
    assert not graph_jobs.finish(graph_jobs.claim(), (PNG, "d" * 64, False, "READY"))
    with session() as db:
        assert db.get(GraphJob, identity).last_code == "STORAGE_LIMIT"
        assert db.get(GraphSnapshot, identity).png is None


def test_permanent_gateway_rejection_has_no_retry(monkeypatch):
    """Invalid declarations/authorization must not be retried like temporary capacity errors."""
    from socrat_bot.graph_jobs import drain_one
    from socrat_bot.models import GraphJob

    identity = queue_graph(monkeypatch)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(lambda request: gateway_response(422)), **kw
        ),
    )
    assert asyncio.run(drain_one())
    with session() as db:
        job = db.get(GraphJob, identity)
        assert job.state == "failed" and job.attempts == 1
        assert job.last_code == "HTTP_REJECTED"
    assert not asyncio.run(drain_one())


def test_burst_of_six_resolved_cards_all_gain_graphs_in_place(monkeypatch):
    """Replay only synthetic unit fixtures for the reported six-card burst shape."""
    from socrat_bot.graph_jobs import claim, finish

    calls = mock_transport(monkeypatch)
    with session() as db:
        db.add(User(telegram_id=1, role="owner", alerts_enabled=True, receive_level=20))
    identities = []
    for i in range(6):
        submit_card(Card(key=f"synthetic-pod-{i}", title="KubePodNotReady"))
        assert deliver()[1] == "sendMessage"
        identities.append(
            queue_graph(monkeypatch, key=f"synthetic-pod-{i}", status="resolved")
        )
        assert deliver()[1] == "editMessageText"
    for identity in identities:
        work = claim()
        assert work[0] == identity
        assert finish(work, (PNG, "d" * 64, False, "READY"))
        edit = deliver()
        assert edit[1] == "editMessageMedia"
        assert edit[2]["text"].startswith("[Resolved]")
        assert edit[2]["reply_markup"]["inline_keyboard"] == []
    assert len(calls) == 18
    assert sum(r.url.path.endswith("/sendMessage") for r in calls) == 6
    assert sum(r.url.path.endswith("/editMessageMedia") for r in calls) == 6
    with session() as db:
        for i in range(6):
            card = db.get(TelegramCard, (f"synthetic-pod-{i}", 1))
            assert card.message_kind == "photo" and card.message_id == 101 + 2 * i


def test_caption_budget_uses_visible_text_not_href_or_entity_source():
    """Telegram's 1024-unit budget applies after formatting, not to private long URLs."""
    from socrat_bot.primary_delivery import caption_units

    assert (
        caption_units("<b>Ready</b> &amp; &#x1F534;")
        == len("Ready & 🔴".encode("utf-16-le")) // 2
    )
    assert (
        caption_units('<a href="https://example.org/' + "x" * 2000 + '">Logs</a>') == 4
    )
    assert caption_units("<b>broken") == len("<b>broken")
    assert caption_units("<unknown>body</unknown>") == len("<unknown>body</unknown>")
    assert caption_units("<b><i>bad</b></i>") == len("<b><i>bad</b></i>")


def test_long_link_markup_does_not_exclude_initial_or_late_photo(monkeypatch):
    """Preserve the full HTML/links on both sendPhoto and same-ID media upgrades."""
    from socrat_bot.cards import Link

    mock_transport(monkeypatch)
    seed()
    url = "https://example.org/" + "x" * 2000
    card = Card(key="graph", title="Pod", links=[Link("Logs", url)])
    submit_card(card)
    first = deliver()
    assert first[1] == "sendPhoto" and url in first[2]["text"]
    late_card = Card(key="late", title="Pod", links=[Link("Logs", url)])
    submit_card(late_card)
    assert deliver()[1] == "sendMessage"
    submit_card(late_card, primary_graph="a" * 64)
    late = deliver()
    assert late[1] == "editMessageMedia" and url in late[2]["text"]


def test_new_incident_generation_rejects_old_graph_owner(monkeypatch):
    """No renderer result from a previous refiring generation may publish a photo."""
    from socrat_bot.graph_jobs import claim, finish
    from socrat_bot.models import GraphJob

    identity = queue_graph(monkeypatch)
    work = claim()
    submit_card(Card(key="graph", title="Pod", status="resolved"))
    submit_card(Card(key="graph", title="Pod", status="firing"))
    assert not finish(work, (PNG, "d" * 64, False, "READY"))
    with session() as db:
        assert db.get(GraphJob, identity).state == "superseded"
        assert db.get(GraphSnapshot, identity).png is None


def test_render_deadline_and_lease_cover_gateway_budget(monkeypatch):
    """The client and owner lease must outlive the gateway's bounded 15-second render."""
    from contextlib import asynccontextmanager

    from socrat_bot import graph_jobs, primary_graphs

    seen = []
    real_timeout = asyncio.timeout

    @asynccontextmanager
    async def timeout(seconds):
        seen.append(seconds)
        async with real_timeout(seconds):
            yield

    monkeypatch.setattr(primary_graphs.asyncio, "timeout", timeout)
    monkeypatch.setattr(primary_graphs, "_token", lambda: "synthetic-token-only")

    def client(**kw):
        assert kw["timeout"] == 18
        return REAL_CLIENT(
            transport=httpx.MockTransport(lambda request: gateway_response()), **kw
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(primary_graphs.fetch_png({}))
    assert result[0] == PNG and seen == [20]
    assert graph_jobs.LEASE_SECONDS >= 20 + 2 * 5 + 10


def test_gateway_reason_survives_expiration_without_raw_error_storage(monkeypatch):
    """Keep the actual bounded error instead of overwriting it with a generic age label."""
    from datetime import timedelta

    from socrat_bot import graph_jobs, primary_graphs
    from socrat_bot.models import GraphJob, utcnow

    identity = queue_graph(monkeypatch)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503, json={"code": "RENDER_TIMEOUT", "private": "never stored"}
                )
            ),
            **kw,
        ),
    )
    result = asyncio.run(primary_graphs.fetch_png({}))
    assert result == (None, None, True, "RENDER_TIMEOUT")
    work = graph_jobs.claim()
    assert not graph_jobs.finish(work, result)
    with session() as db:
        row = db.get(GraphJob, identity)
        row.created_at = utcnow() - timedelta(seconds=graph_jobs.MAX_AGE + 1)
        row.available_at = utcnow()
    assert graph_jobs.claim() is None
    with session() as db:
        row = db.get(GraphJob, identity)
        assert row.state == "expired" and row.last_code == "RENDER_TIMEOUT"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "untrusted-error-URL-or-secret"},
        {"code": ["BUSY"]},
        {"code": "BUSY", "overflow": "x" * 300},
        [],
    ],
)
def test_gateway_error_projection_is_bounded_and_allowlisted(monkeypatch, body):
    """Untrusted response text cannot enter the durable diagnostic field or logs."""
    from socrat_bot import primary_graphs

    monkeypatch.setattr(primary_graphs, "_token", lambda: "synthetic-token-only")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, json=body)
            ),
            **kw,
        ),
    )
    assert asyncio.run(primary_graphs.fetch_png({})) == (
        None,
        None,
        True,
        "HTTP_TRANSIENT",
    )


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("terminal", [None, "mute", "resolved", "old_generation"])
def test_versioned_spi_retries_rebuild_current_keyboard(monkeypatch, photo, terminal):
    from socrat_bot.delivery import publish_spi
    from socrat_bot.models import Incident

    mock_transport(monkeypatch)
    seed()
    if not photo:
        with session() as db:
            db.get(Incident, "graph").graph_capture = None
    initial = deliver()
    expected = initial[2]["reply_markup"]
    assert expected["inline_keyboard"]
    if terminal == "mute":
        annotate_card("graph", mute="<i>Muted 15m</i>")
        deliver()
    elif terminal == "resolved":
        submit_card(Card(key="graph", title="Latency", status="resolved"))
        deliver()
    elif terminal == "old_generation":
        with session() as db:
            db.get(Incident, "graph").generation += 1
    with session() as db:
        message_id = db.get(TelegramCard, ("graph", 1)).message_id
    for revision in (1, 1, 2):
        _, jobs = publish_spi(
            "graph", f"https://github.com/alchemmist/homelab/issues/{revision}",
            "SPI", revision, [{"chat_id": 1, "message_id": message_id}],
        )
        work = prepare_work(jobs[0])
        assert work is not None
        assert work[1] == ("editMessageCaption" if photo else "editMessageText")
        assert work[2]["message_id"] == message_id
        assert work[2]["reply_markup"] == (
            {"inline_keyboard": []} if terminal else expected
        )
        assert asyncio.run(_deliver_work(work))


def test_spi_photo_overflow_replacement_keeps_keyboard_and_retires_photo(monkeypatch):
    from socrat_bot.delivery import publish_spi
    from socrat_bot.models import CardDelivery

    mock_transport(monkeypatch)
    seed()
    first = deliver()
    expected = first[2]["reply_markup"]
    with session() as db:
        # Simulate an accepted long link label / note in the desired SPI snapshot.
        state = db.get(CardDelivery, ("graph", 1))
        state.text += "\n" + "x" * 1100
        message_id = db.get(TelegramCard, ("graph", 1)).message_id
    _, jobs = publish_spi(
        "graph", "https://github.com/alchemmist/homelab/issues/9", "SPI", 1,
        [{"chat_id": 1, "message_id": message_id}],
    )
    work = prepare_work(jobs[0])
    assert work[1] == "sendMessage"
    assert work[2]["reply_markup"] == expected
    assert "x" * 1100 in work[2]["text"]
    assert asyncio.run(_deliver_work(work))
    retirement = deliver()
    assert retirement[1] == "editMessageReplyMarkup"
    assert retirement[2]["reply_markup"] == {"inline_keyboard": []}
    assert retirement[2]["message_id"] == message_id


def test_spi_and_note_graph_replacement_keep_custom_keyboard(monkeypatch):
    from socrat_bot.delivery import publish_spi

    mock_transport(monkeypatch)
    seed()
    initial = deliver()
    expected = initial[2]["reply_markup"]
    annotate_card("graph", note="Reviewed safely")
    assert deliver()[2]["reply_markup"] == expected
    with session() as db:
        db.add(GraphSnapshot(id="b" * 64, png=PNG + b"new", render_digest="d" * 64))
        from socrat_bot.models import Incident
        db.get(Incident, "graph").graph_capture = "b" * 64
        message_id = db.get(TelegramCard, ("graph", 1)).message_id
    _, jobs = publish_spi(
        "graph", "https://github.com/alchemmist/homelab/issues/9", "SPI", 1,
        [{"chat_id": 1, "message_id": message_id}],
    )
    work = prepare_work(jobs[0])
    assert work[1] == "editMessageMedia"
    assert work[2]["reply_markup"] == expected
    assert "Reviewed safely" in work[2]["text"]
    assert asyncio.run(_deliver_work(work))


@pytest.mark.parametrize("policy", ["eligible", "final", "disabled", "insufficient"])
def test_spi_text_photo_upgrade_rebuilds_escalation_policy(monkeypatch, policy):
    from socrat_bot.delivery import publish_spi
    from socrat_bot.models import Incident

    mock_transport(monkeypatch)
    seed()
    with session() as db:
        db.get(Incident, "graph").graph_capture = None
    deliver()
    with session() as db:
        incident = db.get(Incident, "graph")
        incident.graph_capture = "a" * 64
        if policy == "final":
            incident.level = 10
        elif policy in {"disabled", "insufficient"}:
            from sqlalchemy import select
            user = db.scalar(select(User).where(User.telegram_id == 1))
            if policy == "disabled":
                user.alerts_enabled = False
            else:
                user.admin_level = 30
        message_id = db.get(TelegramCard, ("graph", 1)).message_id
    _, jobs = publish_spi(
        "graph", "https://github.com/alchemmist/homelab/issues/9", "SPI", 1,
        [{"chat_id": 1, "message_id": message_id}],
    )
    work = prepare_work(jobs[0])
    assert work[1] == "editMessageMedia"
    assert work[2]["message_id"] == message_id
    buttons = [b for row in work[2]["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b["text"].startswith("Mute") for b in buttons)
    assert any(b["text"] == "Escalate" for b in buttons) == (policy == "eligible")
    assert asyncio.run(_deliver_work(work))


def test_delivery_diagnostic_failure_preserves_result_and_cleans_heartbeat(monkeypatch):
    import logging

    mock_transport(monkeypatch)
    seed()
    work = prepare_work()
    assert work is not None

    def broken_log(*args, **kwargs):
        raise RuntimeError("diagnostic handler failed")

    monkeypatch.setattr(logging.getLogger("socrat-bot"), "info", broken_log)

    async def run():
        before = asyncio.all_tasks()
        assert await _deliver_work(work)
        assert not (asyncio.all_tasks() - before)

    asyncio.run(run())
    with session() as db:
        assert db.get(TelegramCard, ("graph", 1)).message_id is not None
