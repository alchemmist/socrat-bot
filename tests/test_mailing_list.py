import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest
from test_mailing_edit import (
    store as store,  # noqa: PLC0414 - explicit pytest fixture re-export
)

from socrat_bot import db as database
from socrat_bot.logic import ensure_user
from socrat_bot.mailing_list import PANEL_LIMIT, on_list_callback, panel, units
from socrat_bot.models import Broadcast, BroadcastCopy


@pytest.fixture
def history(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    with database.session() as db:
        ensure_user(db, 1)
        ensure_user(db, 2)
    return store


def add_history(count, *, owner=1, status="sent", body="Message", copies=1):
    identities = []
    with database.session() as db:
        for i in range(count):
            row = Broadcast(chat_id=owner, status=status, text=f"{body} {i}")
            db.add(row)
            db.flush()
            identities.append(row.id)
            for j in range(copies):
                db.add(BroadcastCopy(broadcast_id=row.id, chat_id=100 + j, message_id=i * 10 + j + 1, applied_text=row.text, sent_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i)))
    return identities


def callback(data="ml:all:0", actor=1, chat=1):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=actor), message=SimpleNamespace(chat_id=chat), answer=AsyncMock(), edit_message_text=AsyncMock())


def test_recent_history_filters_deduplicates_and_orders_by_receipt(history):
    ids = add_history(5, body="<tag> & text", copies=2)
    hidden = add_history(1, owner=2) + add_history(1, status="sending")
    text, markup = panel(1)
    assert "Latest 3 of 5" in text
    assert text.count("<pre>") == text.count("</pre>") == 3
    assert "&lt;tag&gt; &amp; text" in text and "<tag>" not in text
    assert all(str(id_) not in text for id_ in ids[:2] + hidden + [history])
    positions = [text.index(str(id_)) for id_ in reversed(ids[2:])]
    assert positions == sorted(positions)
    assert markup.inline_keyboard[0][0].text == "Show all"
    all_text, all_markup = panel(1, show_all=True)
    assert "All 5 mailings" in all_text
    assert all(str(id_) in all_text for id_ in ids)
    assert all_markup.inline_keyboard[-1][0].text == "Latest 3"
    # Edits do not move an old mailing to the front of delivery history.
    with database.session() as db:
        db.get(Broadcast, ids[0]).text = "Revised older message"
    assert "Revised older message" not in panel(1)[0]


def test_empty_history_hides_uneditable_mailings(history):
    text, markup = panel(1)
    assert "No editable mailings yet" in text
    assert str(history) not in text and "unavailable" not in text
    assert markup is None


def test_show_all_callbacks_update_same_message_and_recheck_permissions(history):
    add_history(5)
    query = callback()
    assert asyncio.run(on_list_callback(query))
    assert "All 5 mailings" in query.edit_message_text.call_args.args[0]
    assert query.edit_message_text.call_args.kwargs["parse_mode"] == "HTML"
    query.data = "ml:recent"
    assert asyncio.run(on_list_callback(query))
    assert "Latest 3 of 5" in query.edit_message_text.call_args.args[0]
    for other in (callback(actor=2, chat=2), callback(chat=-99)):
        assert asyncio.run(on_list_callback(other))
        other.edit_message_text.assert_not_awaited()
        other.answer.assert_awaited_once()
    malformed = callback("ml:all:-1")
    assert asyncio.run(on_list_callback(malformed))
    malformed.edit_message_text.assert_not_awaited()
    assert not asyncio.run(on_list_callback(callback("m:n:other")))
    repeated = callback()
    repeated.edit_message_text.side_effect = BadRequest("Message is not modified")
    assert asyncio.run(on_list_callback(repeated))


def test_app_routes_list_callback_before_other_action_handlers(history):
    from socrat_bot.app import on_callback

    add_history(4)
    query = callback()
    asyncio.run(on_callback(SimpleNamespace(callback_query=query), SimpleNamespace()))
    assert "All 4 mailings" in query.edit_message_text.call_args.args[0]


def test_long_html_unicode_history_is_bounded_and_paged(history):
    add_history(20, body="😀<&>" * 1500)
    recent, markup = panel(1)
    assert units(recent) <= PANEL_LIMIT
    assert recent.count("<pre>") == recent.count("</pre>") == 3
    assert "…</pre>" in recent
    assert markup.inline_keyboard[0][0].text == "Show all"
    first, controls = panel(1, show_all=True)
    assert "Page 1/" in first and units(first) <= PANEL_LIMIT
    assert any(button.text == "Next ▶" for row in controls.inline_keyboard for button in row)
    last, controls = panel(1, show_all=True, page=99999999)
    assert units(last) <= PANEL_LIMIT
    assert not any(button.text == "Next ▶" for row in controls.inline_keyboard for button in row)
    assert "&lt;" in last and "&amp;" in last
