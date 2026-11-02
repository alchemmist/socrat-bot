import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test.db"

from fastapi.testclient import TestClient

from socrat_bot.app import api as bot_api
from socrat_bot.broker import api as broker_api
from socrat_bot.updater import api as updater_api


def _assert_health(api) -> None:
    with TestClient(api) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


def test_bot_healthz():
    _assert_health(bot_api)


def test_broker_healthz():
    _assert_health(broker_api)


def test_updater_healthz():
    _assert_health(updater_api)
