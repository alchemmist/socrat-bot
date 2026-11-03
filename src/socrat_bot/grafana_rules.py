"""Grafana Prometheus rule view URLs.

Grafana identifies a Prometheus rule as pri$datasource$file$group$name$hash.
The hash is Grafana 13's Java string hash of the rule fingerprint. It only
matches when the query text is the one Grafana's datasource proxy returns.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

GRAFANA_URL = os.environ.get("GRAFANA_URL", "https://grafana.socrat.alchemmist.xyz").rstrip("/")
GRAFANA_INTERNAL_URL = os.environ.get(
    "GRAFANA_INTERNAL_URL",
    "http://kube-prometheus-stack-grafana.cattle-monitoring-system.svc",
).rstrip("/")
PROMETHEUS_DATASOURCE_UID = os.environ.get("PROMETHEUS_DATASOURCE_UID", "prometheus")
PROMETHEUS_DATASOURCE_NAME = os.environ.get("PROMETHEUS_DATASOURCE_NAME", "Prometheus")

_lock = threading.Lock()
_cached_at = 0.0
_cached: dict[str, PromRule] = {}


@dataclass(frozen=True)
class PromRule:
    file: str
    group: str
    name: str
    query: str
    labels: dict[str, str]
    annotations: dict[str, str]


def js_hash(value: str) -> str:
    hashed = 0
    for char in value:
        hashed = ((hashed << 5) - hashed + ord(char)) & 0xFFFFFFFF
        if hashed >= 2**31:
            hashed -= 2**32
    return str(hashed)


def hash_query(query: str) -> str:
    query = re.sub(r"#[^\n]*", "", query)
    query = "\n".join(line.strip() for line in query.split("\n") if line.strip())
    if len(query) > 1 and query.startswith("(") and query.endswith(")"):
        query = query[1:-1]
    query = re.sub(r"\s", "", query)
    query = query.replace('\\"', '"')
    query = re.sub(r"`([^`]*)`", lambda match: '"' + match.group(1) + '"', query)
    query = re.sub(r",(?=\})", "", query)
    query = query.replace("{}", "")
    query = re.sub(r"['\"()\[\]\\`]", "", query)
    return "".join(sorted(query))


def hash_labels(item: dict[str, str] | None) -> str:
    pairs = sorted((item or {}).items())
    return json.dumps(pairs, separators=(",", ":"), ensure_ascii=False)


def rule_hash(rule: PromRule) -> str:
    fingerprint = [
        rule.name,
        "alerting",
        hash_query(rule.query),
        hash_labels(rule.annotations),
        hash_labels(rule.labels),
    ]
    return js_hash(json.dumps(fingerprint, separators=(",", ":"), ensure_ascii=False))


def rule_identifier(rule: PromRule) -> str:
    def dollar(value: str) -> str:
        return value.replace("$", "_DOLLAR_")

    namespace = dollar(rule.file).replace("/", "\x1f").replace("\\", "\x1e")
    return "$".join(
        [
            "pri",
            dollar(PROMETHEUS_DATASOURCE_NAME),
            namespace,
            dollar(rule.group),
            dollar(rule.name),
            rule_hash(rule),
        ]
    )


def rule_view_url(rule: PromRule) -> str:
    ident = quote(rule_identifier(rule), safe="")
    source = quote(PROMETHEUS_DATASOURCE_NAME, safe="")
    return (
        f"{GRAFANA_URL}/alerting/{source}/{ident}/view"
        "?returnTo=%2Falerting%2Flist%3Fsearch%3Dstate%3Afiring"
    )


def alert_url(alertname: str) -> str:
    rule = rules_by_name().get(alertname)
    if rule is None:
        return f"{GRAFANA_URL}/alerting/list?search={quote(alertname, safe='')}"
    return rule_view_url(rule)


def rules_by_name() -> dict[str, PromRule]:
    global _cached_at, _cached
    if os.environ.get("GRAFANA_RULES_DISABLE") == "1":
        return {}
    now = time.monotonic()
    with _lock:
        if now - _cached_at < 60:
            return _cached
        loaded = _load()
        _cached = loaded or _cached
        _cached_at = now
        return _cached


def _load() -> dict[str, PromRule]:
    url = (
        f"{GRAFANA_INTERNAL_URL}/api/datasources/proxy/uid/"
        f"{PROMETHEUS_DATASOURCE_UID}/api/v1/rules"
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.load(response)
    except Exception:
        return {}
    found: dict[str, PromRule] = {}
    for group in payload.get("data", {}).get("groups", []):
        for item in group.get("rules", []):
            if item.get("type") not in (None, "alerting"):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            found[name] = PromRule(
                file=str(group.get("file") or ""),
                group=str(group.get("name") or ""),
                name=name,
                query=str(item.get("query") or ""),
                labels={str(key): str(value) for key, value in (item.get("labels") or {}).items()},
                annotations={str(key): str(value) for key, value in (item.get("annotations") or {}).items()},
            )
    return found
