import asyncio
import os
from datetime import UTC

import pytest
from sqlalchemy import create_engine, select

from socrat_bot import db as database
from socrat_bot.logic import deliver_broadcast
from socrat_bot.mailing import edit_mailing
from socrat_bot.models import Base, Broadcast, BroadcastCopy


@pytest.fixture
def store(tmp_path, monkeypatch):
    url = os.environ.get("TEST_POSTGRES_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    assert "test" in url, "Only disposable test databases are allowed"
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("BROADCAST_PAUSE_SECONDS", "0")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_Session", None)
    with database.session() as db:
        row = Broadcast(text="old", status="sent", chat_id=1)
        db.add(row)
        db.flush()
        identity = row.id
    yield identity
    database._engine.dispose()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_receipts_partial_and_retry(store, monkeypatch):
    calls = []
    failures = {2}

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            calls.append((url.rsplit('/', 1)[1], json))
            chat = json['chat_id']
            if url.endswith('sendMessage'):
                if chat == 3:
                    raise RuntimeError('secret transport URL')
                return Response({'ok': True, 'result': {'message_id': chat * 10}})
            if chat in failures:
                return Response({'ok': False, 'description': 'Bad Request: message to edit not found'})
            return Response({'ok': True})

    monkeypatch.setattr('httpx.AsyncClient', Client)
    result = asyncio.run(deliver_broadcast('old', [1, 2, 3], store))
    assert result['sent'] == 2 and result['failed'] == [3]
    with database.session() as db:
        assert len(list(db.scalars(select(BroadcastCopy)))) == 2
    calls.clear()
    result = asyncio.run(edit_mailing(store, 1, 'new 😀\nline'))
    assert 'Updated 1; failed 1; skipped 0' in result
    assert 'message to edit not found' in result
    assert all(method == 'editMessageText' for method, body in calls)
    assert {body['chat_id'] for method, body in calls} == {1, 2}
    failures.clear()
    calls.clear()
    assert 'Updated 1; failed 0; skipped 1' in asyncio.run(edit_mailing(store, 1, 'new 😀\nline'))
    assert len(calls) == 1
    assert 'unavailable' in asyncio.run(edit_mailing(store, 99, 'wrong'))


def test_historic_and_busy(store):
    assert 'no original message IDs' in asyncio.run(edit_mailing(store, 1, 'new'))
    with database.session() as db:
        assert db.get(Broadcast, store).text == 'old'
    assert '4096' in asyncio.run(edit_mailing(store, 1, '😀' * 2049))


def test_concurrent_edit_claim(store, monkeypatch):
    with database.session() as db:
        db.add(BroadcastCopy(broadcast_id=store, chat_id=7, message_id=70, applied_text='old'))

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json):
                calls.append(json)
                entered.set()
                await release.wait()

                class Response:
                    status_code = 200

                    def json(self):
                        return {'ok': False, 'description': 'Bad Request: message is not modified'}

                return Response()

        monkeypatch.setattr('httpx.AsyncClient', Client)
        first = asyncio.create_task(edit_mailing(store, 1, 'first'))
        await entered.wait()
        assert 'another edit is running' in await edit_mailing(store, 1, 'second')
        release.set()
        assert 'Updated 1' in await first
        assert len(calls) == 1 and calls[0]['text'] == 'first'

    asyncio.run(exercise())


@pytest.fixture
def application(store, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from socrat_bot import app
    from socrat_bot.models import User

    monkeypatch.setattr(app, 'is_owner', lambda db, identity: identity == 1)
    with database.session() as db:
        db.add(User(telegram_id=10, role='user', uses_vpn=True))
        db.add(User(telegram_id=11, role='user', uses_vpn=True))
    state = SimpleNamespace(depth=0, calls=[])

    @contextmanager
    def checked_session():
        state.depth += 1
        try:
            with database.session() as db:
                yield db
        finally:
            state.depth -= 1

    monkeypatch.setattr(app, 'session', checked_session)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            assert state.depth == 0, 'DB session held during broadcast HTTP'
            state.calls.append((url.rsplit('/', 1)[1], json))
            await asyncio.sleep(0)

            class Response:
                status_code = 200

                def json(self):
                    if json['chat_id'] == 11:
                        return {'ok': False, 'description': 'blocked'}
                    return {'ok': True, 'result': {'message_id': 90}}

            return Response()

    monkeypatch.setattr('httpx.AsyncClient', Client)
    state.query = SimpleNamespace(
        from_user=SimpleNamespace(id=1, username=None),
        message=SimpleNamespace(chat_id=1),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    state.app = app
    return state


def test_immediate_callback_receipts_and_repeat(store, application):
    with database.session() as db:
        db.get(Broadcast, store).status = 'choosing'
    query = application.query
    async def simultaneous():
        await asyncio.gather(*(application.app._on_mailing_callback(query, f'm:n:{store}') for _ in range(2)))

    asyncio.run(simultaneous())
    assert len(application.calls) == 2
    with database.session() as db:
        row = db.get(Broadcast, store)
        assert row.status == 'sent' and row.sent == 1 and row.failed == [11]
        assert db.get(BroadcastCopy, (store, 10)).message_id == 90
    assert str(store) in query.edit_message_text.call_args.args[0]


def test_scheduled_loop_receipts(store, application, monkeypatch):
    from datetime import datetime, timedelta

    with database.session() as db:
        row = db.get(Broadcast, store)
        row.status = 'scheduled'
        row.send_at = datetime.now(UTC) - timedelta(minutes=1)
    real_sleep = asyncio.sleep
    ticks = 0

    async def sleep(delay):
        nonlocal ticks
        if delay == 15:
            ticks += 1
            if ticks > 1:
                raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(application.app._broadcast_loop())
    with database.session() as db:
        assert db.get(Broadcast, store).sent == 1
        assert db.get(BroadcastCopy, (store, 10)).message_id == 90


def test_legacy_callback_receipts(store, application):
    from socrat_bot.logic import create_action

    with database.session() as db:
        db.get(Broadcast, store).status = 'preview'
        action = create_action(db, type_='broadcast.send', payload={'broadcast_id': str(store)},
                               chat_id=1, idempotency_key='test-send')
        action_id = action.id
    application.query.data = f'act:{action_id}'
    from types import SimpleNamespace
    asyncio.run(application.app.on_callback(SimpleNamespace(callback_query=application.query), None))
    with database.session() as db:
        assert db.get(Broadcast, store).sent == 1
        assert db.get(BroadcastCopy, (store, 10)).message_id == 90
    assert len(application.calls) == 2


def test_api_preview_releases_session(store, application, monkeypatch):
    monkeypatch.setattr(application.app, 'require_internal', lambda token: None)
    result = asyncio.run(application.app.broadcasts(
        application.app.BroadcastIn(chat_id=1, text='preview'), None,
    ))
    import uuid
    with database.session() as db:
        assert db.get(Broadcast, uuid.UUID(result['id'])).message_id == 90


def test_command_permissions_parsing_and_listing(store, application, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    edit = AsyncMock(return_value='updated')
    monkeypatch.setattr('socrat_bot.mailing.edit_mailing', edit)
    message = SimpleNamespace(chat_id=-100, text='/mailing_edit new', reply_to_message=None,
                              reply_text=AsyncMock(return_value=SimpleNamespace(message_id=80)))
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(args=['new'])
    asyncio.run(application.app.on_mailing(update, context))
    asyncio.run(application.app.on_mailing_edit(update, context))
    assert not edit.called
    message.chat_id = 2
    update.effective_user.id = 2
    asyncio.run(application.app.on_mailing_edit(update, context))
    assert not edit.called
    update.effective_user.id = message.chat_id = 1
    message.text = f'/mailing_edit {store} New 😀\nsecond line'
    asyncio.run(application.app.on_mailing_edit(update, context))
    edit.assert_awaited_with(store, 1, 'New 😀\nsecond line')
    with database.session() as db:
        db.get(Broadcast, store).message_id = 77
        db.add(Broadcast(text='PRIVATE OTHER OWNER', status='sent', chat_id=2))
    message.reply_to_message = SimpleNamespace(message_id=77)
    message.text = '/mailing_edit Reply 😀\nsecond line'
    asyncio.run(application.app.on_mailing_edit(update, context))
    edit.assert_awaited_with(store, 1, 'Reply 😀\nsecond line')
    message.reply_to_message = None
    message.text = '/mailing_edit'
    asyncio.run(application.app.on_mailing_edit(update, context))
    listing = message.reply_text.call_args.args[0]
    assert str(store) in listing and 'unavailable (no saved IDs)' in listing
    assert 'PRIVATE OTHER OWNER' not in listing
    message.text = '/mailing hello'
    asyncio.run(application.app.on_mailing(update, context))
    with database.session() as db:
        choosing = db.scalar(select(Broadcast).where(Broadcast.status == 'choosing'))
        assert choosing.message_id == 80


def test_receipt_write_failure_is_not_rejection(store, application, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def broken():
        raise RuntimeError('database secret must not be exposed')
        yield

    monkeypatch.setattr(database, 'session', broken)
    result = asyncio.run(deliver_broadcast('old', [10], store))
    assert result['sent'] == 1 and result['failed'] == []
    assert result['untracked'] == [{'chat_id': 10, 'message_id': 90}]
    assert 'Do not resend' in application.app._delivery_notice('Delivery', result)
