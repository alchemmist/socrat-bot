import asyncio
from types import SimpleNamespace
from contextlib import contextmanager
from socrat_bot import delivery


def test_dispatch_respects_requested_order_and_deduplicates(monkeypatch):
    called = []

    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda *_: SimpleNamespace(done=True))

    monkeypatch.setattr(delivery, "session", session)
    monkeypatch.setattr(delivery, "prepare_work", lambda job: called.append(job))
    asyncio.run(delivery.dispatch_jobs([3, 1, 3, 2]))
    assert called == [3, 1, 2]
