import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from socrat_bot import mute_transport as mute

MATCH = [{"name": "alertname", "value": "FixtureOnly", "isRegex": False}]
ACTION = SimpleNamespace(id="fixture", payload={"matchers": MATCH, "minutes": 15})


def run(handler, previous=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        patch.object(mute.httpx, "AsyncClient", return_value=client),
        patch.dict("os.environ", {"ALERTMANAGER_URL": mute.SERVICE}),
    ):
        return asyncio.run(mute.execute(ACTION, previous))


def test_connect_timeout_uses_another_route_but_never_widens_matchers():
    import json

    calls = []

    def handler(req):
        calls.append(req.url.host)
        assert json.loads(req.content)["matchers"] == MATCH
        if len(calls) == 1:
            raise httpx.ConnectTimeout("fixture")
        return httpx.Response(200, json={"silenceID": "ok"})

    assert run(handler)["status"] == "silenced"
    assert len(set(calls)) == 2


def test_lost_reply_checks_both_peers_without_posting_twice():
    import json

    received = []

    def handler(req):
        if req.method == "POST":
            received.append(json.loads(req.content))
            raise httpx.ReadTimeout("lost after acceptance")
        return httpx.Response(
            200, json=[{**received[0], "id": "original", "status": {"state": "active"}}]
        )

    previous = run(handler)
    assert previous["reason"] == "unconfirmed"
    assert run(handler, previous)["silence"] == {"silenceID": "original"}
    assert len(received) == 1


def test_old_failed_button_only_reposts_after_both_peers_confirm_absence():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.host))
        return httpx.Response(
            200, json=[] if req.method == "GET" else {"silenceID": "new"}
        )

    assert run(handler, {"status": "failed", "error": ""})["status"] == "silenced"
    assert [x[0] for x in calls] == ["GET", "GET", "POST"] and calls[0][1] != calls[1][
        1
    ]


def test_missing_peer_never_reposts_ambiguous_request():
    def handler(req):
        assert req.method == "GET"
        raise httpx.ConnectError("no peer")

    assert run(handler, {"status": "failed", "error": ""})["reason"] == "unconfirmed"


def test_shared_legacy_comment_is_not_an_action_receipt():
    def handler(req):
        assert req.method == "GET"
        return httpx.Response(
            200,
            json=[
                {
                    "createdBy": "socrat-bot",
                    "comment": "muted for 15 minutes from Telegram",
                    "matchers": MATCH,
                    "status": {"state": "active"},
                    "id": "different",
                }
            ],
        )

    assert run(handler, {"status": "failed", "error": ""}) == {
        "status": "failed",
        "reason": "legacy_overlap",
    }


def test_transient_readback_failure_recovers_before_first_post():
    calls = []

    def handler(req):
        calls.append(req.method)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("fixture")
        return httpx.Response(
            200, json=[] if req.method == "GET" else {"silenceID": "ok"}
        )

    assert run(handler, {"status": "failed", "error": ""})["status"] == "silenced"
    assert calls == ["GET"] * 4 + ["POST"]


def test_final_pre_send_failures_expose_safe_per_route_timing():
    def handler(req):
        raise httpx.ConnectTimeout("private fixture")

    result = run(handler)
    assert result["reason"] == "not_connected"
    assert [r["route"] for r in result["attempts"]] == [
        "service",
        "peer_0",
        "peer_1",
        "service",
    ]
    assert "private fixture" not in str(result)


def test_one_verified_receipt_suffices_but_no_new_write_on_partial_readback():
    def handler(req):
        assert req.method == "GET"
        if req.url.host.startswith(
            "alertmanager-kube-prometheus-stack-alertmanager-0."
        ):
            raise httpx.ConnectTimeout("fixture")
        return httpx.Response(
            200,
            json=[
                {
                    "createdBy": "socrat-bot",
                    "comment": "muted for 15 minutes from Telegram; action=fixture",
                    "matchers": MATCH,
                    "status": {"state": "active"},
                    "id": "existing",
                }
            ],
        )

    assert run(handler, {"status": "failed"})["silence"] == {"silenceID": "existing"}


def test_display_and_event_share_transport_duration_policy():
    from socrat_bot.mute_transport import mute_minutes

    assert [
        mute_minutes(p)
        for p in (
            {},
            {"minutes": 0},
            {"minutes": -2},
            {"minutes": 15},
            {"minutes": 5000},
        )
    ] == [60, 60, 1, 15, 1440]
