"""Mirror of spi-triage backend/graph_contract.py at d5ab410bf8e3fa60757e09065e553da5de0b4a1c. Digest mismatch fails closed; no network."""

import datetime as dt
import hashlib
import json
import math
import re
import urllib.parse

MAX_GRAPHS = 16
MAX_ANNOTATION_BYTES = 32768
MAX_PNG_BYTES = 2 * 1024 * 1024
MAX_WINDOW_SECONDS = 7 * 86400
VERSION = 1
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class GraphError(ValueError):
    def __init__(self, code="INVALID_SPEC"):
        self.code = code
        super().__init__(code)


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise GraphError()
        out[key] = value
    return out


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise GraphError()
    return value


def _text(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise GraphError()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise GraphError()
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def parse_annotation(raw):
    """Missing annotation is compatible. Malformed declarations fail explicitly."""
    if raw is None:
        return []
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ANNOTATION_BYTES:
        raise GraphError()
    try:
        obj = json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(GraphError()),
        )
    except (ValueError, TypeError, RecursionError):
        raise GraphError() from None
    if not isinstance(obj, dict) or set(obj) != {"version", "graphs"}:
        raise GraphError()
    if type(obj["version"]) is not int or obj["version"] != VERSION:
        raise GraphError()
    graphs = obj["graphs"]
    if not isinstance(graphs, list) or not 1 <= len(graphs) <= MAX_GRAPHS:
        raise GraphError()
    result, seen = [], set()
    for graph in graphs:
        normalized = validate_graph(graph)
        if normalized["id"] in seen:
            raise GraphError()
        seen.add(normalized["id"])
        result.append(normalized)
    return result


def validate_graph(graph):
    required = {"id", "title", "dashboard_uid", "panel_id", "variables"}
    optional = {"before_seconds", "after_seconds"}
    if (
        not isinstance(graph, dict)
        or not required <= graph.keys()
        or graph.keys() - required - optional
    ):
        raise GraphError()
    for key in ("id", "dashboard_uid"):
        if not isinstance(graph[key], str) or not _ID.fullmatch(graph[key]):
            raise GraphError()
    _text(graph["title"], 160)
    _int(graph["panel_id"], 1, 2**31 - 1)
    before = _int(graph.get("before_seconds", 1800), 1, MAX_WINDOW_SECONDS)
    after = _int(graph.get("after_seconds", 300), 0, MAX_WINDOW_SECONDS)
    if before + after > MAX_WINDOW_SECONDS:
        raise GraphError()
    variables = graph["variables"]
    if not isinstance(variables, dict) or len(variables) > 16:
        raise GraphError()
    normalized = {}
    for name, binding in variables.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise GraphError()
        if not isinstance(binding, dict) or set(binding) not in (
            {"label"},
            {"literal"},
        ):
            raise GraphError()
        kind = next(iter(binding))
        value = _text(binding[kind], 512)
        if kind == "label" and not _NAME.fullmatch(value):
            raise GraphError()
        # Never accidentally widen an instance to Grafana All/regex selectors.
        if kind == "literal" and value in ("All", "$__all", ".*"):
            raise GraphError()
        normalized[name] = {kind: value}
    return {
        **graph,
        "variables": normalized,
        "before_seconds": before,
        "after_seconds": after,
    }


def timestamp_ms(value):
    if not isinstance(value, str) or len(value) > 64:
        raise GraphError("INVALID_TIMESTAMP")
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        result = parsed.timestamp() * 1000
        if not math.isfinite(result) or result < 0:
            raise ValueError()
        return int(result)
    except (ValueError, OverflowError, OSError):
        raise GraphError("INVALID_TIMESTAMP") from None


def resolve(
    graph,
    labels,
    *,
    starts_at,
    observed_at,
    status="firing",
    ends_at=None,
    purpose="telegram",
):
    graph = validate_graph(graph)
    if (
        not isinstance(labels, dict)
        or status not in ("firing", "resolved")
        or purpose not in ("telegram", "spi")
    ):
        raise GraphError()
    start, observed = timestamp_ms(starts_at), timestamp_ms(observed_at)
    if observed < start:
        raise GraphError("INVALID_TIMESTAMP")
    mode, anchor = "onset", start
    if status == "resolved":
        mode, anchor = "resolved", timestamp_ms(ends_at)
        if not start <= anchor <= observed:
            raise GraphError("INVALID_TIMESTAMP")
    elif purpose == "telegram" and observed > start + graph["after_seconds"] * 1000:
        mode, anchor = "ongoing", observed
    variables = {}
    for name, binding in graph["variables"].items():
        value = (
            labels.get(binding["label"]) if "label" in binding else binding["literal"]
        )
        if (
            not isinstance(value, str)
            or not value.strip()
            or value in ("All", "$__all", ".*")
        ):
            raise GraphError("MISSING_LABEL")
        variables[name] = _text(value, 512)
    result = {
        "version": VERSION,
        "dashboard_uid": graph["dashboard_uid"],
        "panel_id": graph["panel_id"],
        "variables": variables,
        "from_ms": anchor - graph["before_seconds"] * 1000,
        "to_ms": min(observed, anchor + graph["after_seconds"] * 1000),
        "width": 1200,
        "height": 600,
        "theme": "light",
        "mode": mode,
    }
    if result["from_ms"] < 0 or result["to_ms"] <= result["from_ms"]:
        raise GraphError("INVALID_TIMESTAMP")
    result["digest"] = hashlib.sha256(canonical(result).encode("ascii")).hexdigest()
    return result


def render_path(request):
    """Build only a Grafana relative path; network origin is server-owned."""
    if not isinstance(request.get("dashboard_uid"), str) or not _ID.fullmatch(
        request["dashboard_uid"]
    ):
        raise GraphError()
    _int(request.get("panel_id"), 1, 2**31 - 1)
    _int(request.get("from_ms"), 0, 2**53 - 1)
    _int(
        request.get("to_ms"),
        request["from_ms"] + 1,
        request["from_ms"] + MAX_WINDOW_SECONDS * 1000,
    )
    if (
        request.get("width") != 1200
        or request.get("height") != 600
        or request.get("theme") != "light"
    ):
        raise GraphError()
    variables = request.get("variables")
    if not isinstance(variables, dict) or len(variables) > 16:
        raise GraphError()
    params = [
        ("panelId", request["panel_id"]),
        ("from", request["from_ms"]),
        ("to", request["to_ms"]),
        ("width", 1200),
        ("height", 600),
        ("theme", "light"),
        ("tz", "UTC"),
    ]
    for name, value in sorted(variables.items()):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise GraphError()
        _text(value, 512)
        if value in ("All", "$__all", ".*"):
            raise GraphError("MISSING_LABEL")
        params.append(("var-" + name, value))
    return (
        "/render/d-solo/"
        + request["dashboard_uid"]
        + "/_?"
        + urllib.parse.urlencode(params)
    )
