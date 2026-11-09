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


def test_enabled_capture_is_bounded_and_reuses_persisted_timestamp(monkeypatch):
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
    assert len(calls) == 1


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
    assert asyncio.run(capture("graph", {"annotations": {"spi_graphs": "{}"}})) is None


def test_caption_budget_counts_astral_characters_without_truncation():
    from socrat_bot.primary_delivery import caption_units

    assert caption_units("ordinary caption") == 16
    assert caption_units("\U0001f534" * 600) == 1200
