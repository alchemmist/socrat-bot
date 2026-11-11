import json
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlsplit

from socrat_bot.graph_contract import parse_annotation, resolve
from socrat_bot.graph_source import chart_url
from socrat_bot.primary_delivery import adapt


def payload(status="firing"):
    return {
        "annotation": json.dumps(
            {
                "version": 1,
                "graphs": [
                    {
                        "id": "primary",
                        "title": "CPU",
                        "dashboard_uid": "node",
                        "panel_id": 17,
                        "variables": {"instance": {"label": "instance"}},
                    },
                    {
                        "id": "other",
                        "title": "Other",
                        "dashboard_uid": "wrong",
                        "panel_id": 99,
                        "variables": {},
                    },
                ],
            }
        ),
        "labels": {"instance": "host:9100"},
        "starts_at": "2026-01-01T00:00:00Z",
        "ends_at": "2026-01-01T01:00:00Z",
        "observed_at": "2026-01-01T02:00:00Z",
        "status": status,
    }


def test_digest_bound_exact_primary_and_resolved():
    for status in ("firing", "resolved"):
        p = payload(status)
        r = resolve(
            parse_annotation(p["annotation"])[0],
            p["labels"],
            starts_at=p["starts_at"],
            ends_at=p["ends_at"],
            observed_at=p["observed_at"],
            status=status,
        )
        # Golden values executed against read-only upstream graph_contract.py at
        # d5ab410bf8e3fa60757e09065e553da5de0b4a1c (not this mirror).
        assert (
            r["digest"]
            == {
                "firing": "1fc8cc44164944676ddce6bcfe1c6d00f5eb184506eede46ca5a1d327556d9e2",
                "resolved": "d156106ed85c4526c45c88b99ff43cc4fe4fedc23289b9e616ddb41e36896a5d",
            }[status]
        )
        url = chart_url(p, r["digest"])
        assert urlsplit(url).path == "/d-solo/node/_"
        q = parse_qs(urlsplit(url).query)
        assert q["panelId"] == ["17"]
        assert q["from"] == [str(r["from_ms"])]
        assert q["to"] == [str(r["to_ms"])]
        assert q["var-instance"] == ["host:9100"]
        assert chart_url(p, "0" * 64) is None
        assert chart_url({}, r["digest"]) is None


def test_transport_applied_provenance_and_overflow():
    a = NS(
        id="a",
        png=b"a",
        chart_url="https://grafana.socrat.alchemmist.xyz/d-solo/a/_?panelId=1",
    )
    b = NS(id="b", png=None, chart_url=None)

    class DB:
        def get(self, model, key, **kwargs):
            return {"a": a, "b": b}.get(key)

    incident = NS(graph_capture="b", graph_generation=1, generation=1)
    card = NS(graph_capture="a", message_kind="photo", message_id=10)

    def run(text, existing=True):
        body = {
            "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": "button"}]]},
            "message_id": 10,
        }
        snap = {}
        method, body = adapt(
            DB(), incident, card, existing, "editMessageText", body, snap
        )
        assert body["reply_markup"]["inline_keyboard"]
        return method, body, snap

    for _ in range(3):
        method, body, snap = run('Description\n<a href="https://example.org">Alert</a>')
        assert method == "editMessageCaption"
        assert body["text"].count(">Chart</a>") == 1
        assert "d-solo/a/" in body["text"]
        assert snap["_graph_capture"] == "a"
    # A later update may start from the actual accepted caption.
    method, repeated, snap = run(body["text"])
    assert repeated["text"].count(">Chart</a>") == 1
    b.png, b.chart_url = b"b", a.chart_url.replace("/a/", "/b/")
    method, body, snap = run("Description")
    assert method == "editMessageMedia" and snap["_graph_capture"] == "b"
    assert "d-solo/b/" in body["text"]
    method, body, snap = run("x" * 1024)
    assert method == "sendMessage" and "Chart" not in body["text"]
    assert snap["_graph_capture"] is None
    card.message_kind = "text"
    method, body, snap = run("Short")
    assert method == "editMessageMedia" and ">Chart</a>" in body["text"]
    method, body, snap = run("Short", False)
    assert method == "sendPhoto" and ">Chart</a>" in body["text"]
    b.chart_url = None
    method, body, snap = run("Short", False)
    assert method == "sendPhoto" and "Chart" not in body["text"]


def test_gateway_header_drift_omits_source_but_keeps_png(monkeypatch):
    import asyncio
    import hashlib

    import httpx

    from socrat_bot import primary_graphs

    p = payload()
    r = resolve(
        parse_annotation(p["annotation"])[0],
        p["labels"],
        starts_at=p["starts_at"],
        ends_at=p["ends_at"],
        observed_at=p["observed_at"],
        status=p["status"],
    )
    png = b"\x89PNG\r\n\x1a\nfixture"
    headers = {
        "content-type": "image/png",
        "x-content-sha256": hashlib.sha256(png).hexdigest(),
        "x-render-digest": r["digest"],
        "x-graph-from": str(r["from_ms"]),
        "x-graph-to": str(r["to_ms"]),
        "x-graph-mode": r["mode"],
    }
    real_client = httpx.AsyncClient
    monkeypatch.setattr(primary_graphs, "_token", lambda: "synthetic-test-token")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=png, headers=headers)
            ),
            **kwargs,
        ),
    )
    assert asyncio.run(primary_graphs.fetch_png(p)) == (
        png,
        r["digest"],
        False,
        "READY",
    )
    for header in ("x-graph-from", "x-graph-to", "x-graph-mode"):
        old = headers.pop(header)
        result = asyncio.run(primary_graphs.fetch_png(p))
        assert result == (png, None, False, "READY")
        headers[header] = old
    headers["x-render-digest"] = "0" * 64
    result = asyncio.run(primary_graphs.fetch_png(p))
    assert result[0] == png and chart_url(p, result[1]) is None
