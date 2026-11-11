"""Digest-bound panel provenance; never accepts caller-provided URLs."""

from urllib.parse import urlencode

from socrat_bot.graph_contract import GraphError, parse_annotation, resolve

# Same trusted human Grafana origin as the renderer deployment; no auth widening.
GRAFANA_ORIGIN = "https://grafana.socrat.alchemmist.xyz"


def chart_url(payload, digest):
    try:
        graphs = parse_annotation(payload["annotation"])
        if not graphs:
            return None
        request = resolve(
            graphs[0],
            payload["labels"],
            starts_at=payload["starts_at"],
            observed_at=payload["observed_at"],
            ends_at=payload["ends_at"],
            status=payload["status"],
            purpose="telegram",
        )
        if request["digest"] != digest:
            return None
        params = [
            ("panelId", request["panel_id"]),
            ("from", request["from_ms"]),
            ("to", request["to_ms"]),
            ("tz", "UTC"),
            ("theme", "light"),
        ]
        params.extend(("var-" + k, v) for k, v in sorted(request["variables"].items()))
        return (
            GRAFANA_ORIGIN
            + "/d-solo/"
            + request["dashboard_uid"]
            + "/_?"
            + urlencode(params)
        )
    except (GraphError, KeyError, TypeError, ValueError, RecursionError):
        return None
