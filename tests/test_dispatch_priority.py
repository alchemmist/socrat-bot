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


def test_busy_preferred_copy_does_not_block_ready_copy(monkeypatch):
    attempts, delivered = [], []

    def prepare(job):
        attempts.append(job)
        return None if job == 3 and attempts.count(3) == 1 else job

    async def deliver(job):
        delivered.append(job)
        return True

    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda _, job: SimpleNamespace(done=job in delivered))

    monkeypatch.setattr(delivery, "prepare_work", prepare)
    monkeypatch.setattr(delivery, "_deliver_work", deliver)
    monkeypatch.setattr(delivery, "session", session)
    asyncio.run(delivery.dispatch_jobs([3, 1, 3]))
    assert attempts == [3, 1, 3]
    assert delivered == [1, 3]
