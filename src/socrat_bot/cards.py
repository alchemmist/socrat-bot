"""One Telegram card and a registry of link providers.

Callers name the providers they want (`alert`, `logs`, `traces`, …).
Alert cards use the default set and skip a provider that does not match.
`traces` is not in that default: a message opts in by listing it, and the
Tempo URL is built only when a trace id is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

GRAFANA_URL = os.environ.get("GRAFANA_URL", "https://grafana.socrat.alchemmist.xyz").rstrip("/")
RANCHER_URL = os.environ.get("RANCHER_URL", "https://rancher.socrat.alchemmist.xyz").rstrip("/")
ARGOCD_URL = os.environ.get("ARGOCD_URL", "https://argocd.socrat.alchemmist.xyz").rstrip("/")
LOKI_DATASOURCE = os.environ.get("LOKI_DATASOURCE", "loki")
TEMPO_DATASOURCE = os.environ.get("TEMPO_DATASOURCE", "tempo")

# Namespace -> Argo CD Application names from homelab deploy/argocd/apps.
# Several apps in one namespace are not guessed; a label must name the app.
ARGO_APPS: dict[str, tuple[str, ...]] = {
    "argocd": ("argo-cd",),
    "automation": ("n8n",),
    "blog": ("blog",),
    "cattle-monitoring-system": (
        "grafana-mcp",
        "kube-prometheus-stack",
        "monitoring-k3s",
        "otel-backends",
        "thanos",
    ),
    "cattle-system": ("rancher",),
    "cats": ("cats",),
    "cert-manager": ("cert-manager",),
    "format-quorum": ("format-quorum",),
    "harbor": ("harbor",),
    "identity": ("authentik",),
    "img-proxy": ("img-proxy",),
    "kube-system": ("nvidia-device-plugin",),
    "llm": (
        "litellm",
        "litellm-infra",
        "monitoring-llm",
        "open-terminal",
        "open-webui",
        "searxng",
        "vllm",
    ),
    "lms": ("lms",),
    "monori": ("monori",),
    "pr-agent": ("pr-agent",),
    "remark42": ("remark42",),
    "socrat-bot": ("socrat-bot",),
    "socrat-home": ("socrat-home",),
    "syncthing": ("private-gateway", "syncthing"),
    "traefik-edge": ("traefik-edge", "xui-auth-gateway"),
    "umami": ("umami",),
    "uptime-kuma": ("uptime-kuma"),
    "voice": ("yandex-voice",),
    "wiki": ("wiki-runtime",),
    "x-ui": ("x-ui",),
}

DEFAULT_ALERT_LINKS = ("alert", "nodes", "rancher", "logs", "argo")
ALERT_LINK_LIMIT = 4
_HOST = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,126}$")


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass
class Card:
    key: str
    title: str
    where: str = ""
    summary: str = ""
    status: str = "firing"
    links: list[Link] = field(default_factory=list)
    matchers: list[dict] = field(default_factory=list)

    @property
    def firing(self) -> bool:
        return self.status != "resolved"


class LinkProvider:
    """One kind of URL. `match` returns nothing when this card has no such target."""

    name: str

    def match(self, labels: dict[str, str]) -> Link | None:
        raise NotImplementedError


def _esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _logql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _one_line(value: str) -> str:
    return " ".join(value.split())


def labels_of(raw: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[str(key)] = text
    return out


def node_name(labels: dict[str, str]) -> str:
    for key in ("node", "nodename"):
        value = labels.get(key, "")
        if value and _HOST.match(value):
            return value
    return ""


def workload(labels: dict[str, str]) -> tuple[str, str]:
    job_name = labels.get("job_name", "")
    if job_name:
        return "job", job_name
    for kind in ("deployment", "statefulset", "daemonset"):
        name = labels.get(kind, "")
        if name:
            return kind, name
    kind = labels.get("created_by_kind", "").lower()
    name = labels.get("created_by_name", "")
    if kind in {"job", "deployment", "statefulset", "daemonset"} and name:
        return kind, name
    return "", ""


def _exporter_pod(labels: dict[str, str]) -> bool:
    """kube-state-metrics stamps its own pod onto the series it exposes."""
    pod = labels.get("pod", "")
    if "kube-state-metrics" in pod or labels.get("container") == "kube-state-metrics":
        return True
    return bool(labels.get("prometheus_replica") and pod.startswith("prometheus-"))


def subject(labels: dict[str, str]) -> tuple[str, str]:
    """What the card is about: a workload, or a pod when that pod is the object."""
    kind, work = workload(labels)
    pod = "" if _exporter_pod(labels) else labels.get("pod", "")
    if kind and (not pod or not pod.startswith(work + "-")):
        return kind, work
    if pod:
        return "pod", pod
    return kind, work


def argo_app(labels: dict[str, str]) -> str:
    apps = ARGO_APPS.get(labels.get("namespace", ""), ())
    if not apps:
        return ""
    named = [app for app in apps if app in set(labels.values())]
    if len(named) == 1:
        return named[0]
    if len(apps) == 1:
        return apps[0]
    return ""


def card_key(alertname: str, labels: dict[str, str]) -> str:
    namespace = labels.get("namespace", "")
    kind, name = subject(labels)
    node = node_name(labels)
    if kind and name and namespace:
        ident = f"{kind}/{namespace}/{name}"
    elif node and kind != "pod":
        ident = f"node/{node}"
    else:
        ident = "cluster"
    raw = f"{alertname}/{ident}"
    if len(raw) <= 120:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{alertname[:80]}/{digest}"[:120]


def object_matchers(alertname: str, labels: dict[str, str]) -> list[dict]:
    pairs = [("alertname", alertname)]
    namespace = labels.get("namespace", "")
    kind, name = subject(labels)
    node = node_name(labels)
    if kind == "pod" and namespace:
        pairs.extend((("namespace", namespace), ("pod", name)))
    elif kind and name and namespace:
        label = "job_name" if kind == "job" else kind
        pairs.extend((("namespace", namespace), (label, name)))
    elif node:
        pairs.append(("node", node))
    return [{"name": name, "value": value, "isRegex": False, "isEqual": True} for name, value in pairs]


def _explore(datasource: str, query: str, field_name: str) -> str:
    left = {
        "datasource": datasource,
        "queries": [{field_name: query, "refId": "A"}],
        "range": {"from": "now-1h", "to": "now"},
    }
    encoded = quote(json.dumps(left, separators=(",", ":")), safe="")
    return f"{GRAFANA_URL}/explore?orgId=1&left={encoded}"


class AlertLink(LinkProvider):
    name = "alert"

    def match(self, labels: dict[str, str]) -> Link | None:
        name = labels.get("alertname", "")
        if not name:
            return None
        from socrat_bot.grafana_rules import alert_url

        return Link("Alert", alert_url(name))


class NodesLink(LinkProvider):
    name = "nodes"

    def match(self, labels: dict[str, str]) -> Link | None:
        node = node_name(labels)
        if not node or labels.get("pod"):
            return None
        query = (
            f"{GRAFANA_URL}/d/socrat-nodes/nodes?from=now-3h&to=now&timezone=browser"
            f"&var-node={quote(node, safe='')}&refresh=5s"
        )
        return Link("Nodes", query)


class RancherLink(LinkProvider):
    name = "rancher"

    def match(self, labels: dict[str, str]) -> Link | None:
        base = f"{RANCHER_URL}/dashboard/c/local/explorer"
        namespace = labels.get("namespace", "")
        kind, name = subject(labels)
        rancher_kind = {
            "deployment": "apps.deployment",
            "statefulset": "apps.statefulset",
            "daemonset": "apps.daemonset",
            "job": "batch.job",
            "pod": "pod",
        }.get(kind, "")
        if rancher_kind and name and namespace:
            url = f"{base}/{rancher_kind}/{quote(namespace, safe='')}/{quote(name, safe='')}"
            if kind != "pod":
                url += "#pods"
            return Link("Rancher", url)
        node = node_name(labels)
        if node and kind != "pod":
            return Link("Rancher", f"{base}/node/{quote(node, safe='')}#pods")
        return None


def _loki_explore(expr: str) -> str:
    panes = {
        "logs": {
            "datasource": LOKI_DATASOURCE,
            "queries": [
                {
                    "refId": "A",
                    "expr": expr,
                    "queryType": "range",
                    "datasource": {"type": "loki", "uid": LOKI_DATASOURCE},
                }
            ],
            "range": {"from": "now-1h", "to": "now"},
        }
    }
    encoded = quote(json.dumps(panes, separators=(",", ":")), safe="")
    return f"{GRAFANA_URL}/explore?schemaVersion=1&orgId=1&panes={encoded}"


class LogsLink(LinkProvider):
    name = "logs"

    def match(self, labels: dict[str, str]) -> Link | None:
        namespace = labels.get("namespace", "")
        kind, name = subject(labels)
        if not namespace or not name:
            return None
        # k8s_pod_name is Loki structured metadata, not an indexed stream label.
        if kind == "pod":
            expr = '{k8s_namespace_name="%s"} | k8s_pod_name="%s"' % (_logql(namespace), _logql(name))
        else:
            expr = '{k8s_namespace_name="%s"} | k8s_pod_name=~"%s-.+"' % (_logql(namespace), _logql(name))
        return Link("Logs", _loki_explore(expr))


class ArgoLink(LinkProvider):
    name = "argo"

    def match(self, labels: dict[str, str]) -> Link | None:
        app = argo_app(labels)
        if not app:
            return None
        return Link("Argo CD", f"{ARGOCD_URL}/applications/argocd/{quote(app, safe='')}")


class TracesLink(LinkProvider):
    name = "traces"

    def match(self, labels: dict[str, str]) -> Link | None:
        trace_id = labels.get("trace_id") or labels.get("traceID") or ""
        if not trace_id:
            return None
        return Link("Traces", _explore(TEMPO_DATASOURCE, trace_id, "query"))


PROVIDERS: dict[str, LinkProvider] = {
    item.name: item
    for item in (AlertLink(), NodesLink(), RancherLink(), LogsLink(), ArgoLink(), TracesLink())
}


def select_links(labels: dict[str, str], names: list[str] | None = None, *, limit: int | None = ALERT_LINK_LIMIT) -> list[Link]:
    """Resolve provider names to links. `names=None` uses the alert default and the cap.

    An explicit list is not capped: a caller can ask for traces plus anything added later.
    Unknown provider names raise ValueError so a typo is not a silent empty card.
    """
    chosen = list(DEFAULT_ALERT_LINKS) if names is None else names
    found: list[Link] = []
    for name in chosen:
        provider = PROVIDERS.get(name)
        if provider is None:
            raise ValueError(f"unknown link provider {name}")
        hit = provider.match(labels)
        if hit is not None:
            found.append(hit)
    if names is None and limit is not None:
        return found[:limit]
    return found


def render_card(card: Card) -> str:
    lines: list[str] = []
    title = f"<b>{_esc(card.title)}</b>"
    if card.status == "resolved":
        title = "[Resolved] " + title
    lines.append(title)
    if card.where:
        lines.append(f"<code>{_esc(card.where)}</code>")
    if card.summary:
        lines.append(_esc(_one_line(card.summary)))
    if card.links:
        lines.append(" · ".join(f'<a href="{_esc(link.url)}">{_esc(link.label)}</a>' for link in card.links))
    return "\n".join(lines)


def build_alert(payload: dict, link_names: list[str] | None = None) -> Card:
    labels = labels_of(payload.get("labels"))
    annotations = labels_of(payload.get("annotations"))
    name = labels.get("alertname") or str(payload.get("name") or "alert")
    labels["alertname"] = name
    namespace = labels.get("namespace", "")
    kind, work = subject(labels)
    node = node_name(labels)
    if work and namespace:
        where = f"{namespace} / {work}"
    elif node and kind != "pod":
        where = node
    else:
        where = ""
    summary = annotations.get("summary") or annotations.get("description") or str(payload.get("summary") or "")
    status = "resolved" if str(payload.get("status") or "").lower() == "resolved" else "firing"
    return Card(
        key=card_key(name, labels),
        title=name,
        where=where,
        summary=summary,
        status=status,
        links=select_links(labels, link_names),
        matchers=object_matchers(name, labels),
    )


def mute_actions(card: Card, generation: int, chat_id: int = 0) -> list[dict]:
    if not card.firing:
        return []
    actions = []
    for minutes, label in ((15, "Mute 15m"), (30, "Mute 30m"), (60, "Mute 1h")):
        actions.append(
            {
                "type": "alert.silence",
                "label": label,
                "row": 0,
                "payload": {"matchers": card.matchers, "minutes": minutes, "card": render_card(card)},
                "idempotency_key": f"{card.key}:g{generation}:{minutes}:{chat_id}"[:128],
            }
        )
    return actions
