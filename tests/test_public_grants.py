from fastapi.testclient import TestClient

from socrat_bot import db
from socrat_bot.app import api
from socrat_bot.models import Base


def test_grant_lifecycle_and_boundaries(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/grants.db")
    monkeypatch.setenv("BOT_API_ADMIN_SECRET", "a" * 32)
    monkeypatch.setenv("INTERNAL_TOKEN", "legacy")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)
    Base.metadata.create_all(db.engine())
    c = TestClient(api)
    admin = ("admin", "a" * 32)
    body = {"name": "phone", "scopes": ["messages:send"], "chats": [12]}
    h = {"X-Bot-Admin": "1"}
    from socrat_bot.api_access import admin as gate

    monkeypatch.setenv("BOT_OIDC_SECRET", "x" * 48)
    assert c.get("/admin/tokens", auth=admin).status_code == 401
    monkeypatch.setitem(api.dependency_overrides, gate, lambda: None)
    assert c.post("/admin/tokens", auth=admin, json=body).status_code == 403
    r = c.post("/admin/tokens", auth=admin, headers=h, json=body)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    auth = {"Authorization": "Bearer " + token}
    assert token not in c.get("/admin/tokens", auth=admin).text
    assert (
        c.get("/api/v1/cards", headers={"X-Internal-Token": "legacy"}).status_code
        == 401
    )
    assert c.get("/api/v1/cards", headers=auth).status_code == 403
    import httpx

    async def fake_post(self, url, *, json):
        assert url.endswith('/sendMessage')
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    unrestricted = c.post("/admin/tokens", auth=admin, headers=h,
                          json={"name": "alerts", "scopes": ["messages:send"], "days": None})
    assert unrestricted.status_code == 201
    assert next(row for row in c.get("/admin/tokens", auth=admin).json() if row["name"] == "alerts")["expires"] is None
    unrestricted_auth = {"Authorization": "Bearer " + unrestricted.json()["token"]}
    for chat in (12, 13):
        assert c.post("/api/v1/messages", headers=unrestricted_auth,
                      json={"chat_id": chat, "text": "test"}).status_code == 200
    assert c.post("/api/v1/messages", headers=unrestricted_auth,
                  json={"chat_id": 13, "text": "test", "actions": [{}]}).status_code == 403
    for payload in [
        {"chat_id": 13, "text": "test"},
        {"chat_id": 12, "text": "test", "actions": [{}]},
    ]:
        assert c.post("/api/v1/messages", headers=auth, json=payload).status_code == 403
    assert (
        c.delete("/admin/tokens/" + r.json()["id"], auth=admin, headers=h).status_code
        == 204
    )
    assert (
        c.post(
            "/api/v1/messages", headers=auth, json={"chat_id": 12, "text": "test"}
        ).status_code
        == 401
    )


def test_console_language():
    import re

    from socrat_bot.grant_ui import page

    html = page(["messages:send"])
    assert '<html lang="en">' in html
    assert not re.search(r"[\u0400-\u04ff]", html)
