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
from urllib.parse import quote, urlsplit

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

DEFAULT_ALERT_LINKS = ("alert", "n8n", "nodes", "rancher", "logs", "argo")
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


def _node_exporter(labels: dict[str, str]) -> bool:
    pod = labels.get("pod", "")
    return (
        labels.get("job") == "node-exporter"
        or labels.get("container") == "node-exporter"
        or "node-exporter" in pod
    )


def _exporter_pod(labels: dict[str, str]) -> bool:
    """The scrape pod is not the object when the metric is about something else."""
    pod = labels.get("pod", "")
    if _node_exporter(labels):
        return True
    if "kube-state-metrics" in pod or labels.get("container") == "kube-state-metrics":
        return True
    return bool(labels.get("prometheus_replica") and pod.startswith("prometheus-"))


def systemd_unit(labels: dict[str, str]) -> str:
    if not _node_exporter(labels):
        return ""
    return labels.get("name", "")


def exporter_daemonset(labels: dict[str, str]) -> tuple[str, str]:
    service = labels.get("service", "")
    namespace = labels.get("namespace", "")
    pod = labels.get("pod", "")
    if namespace and service and pod.startswith(service + "-"):
        return namespace, service
    return "", ""


def subject(labels: dict[str, str]) -> tuple[str, str]:
    """What the card is about: a workload, or a pod when that pod is the object."""
    unit = systemd_unit(labels)
    if unit:
        return "systemd", unit
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
    if kind == "systemd" and name:
        host = node or labels.get("instance", "").split(":", 1)[0] or "host"
        ident = f"systemd/{host}/{name}"
    elif kind and name and namespace:
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
    elif kind == "systemd" and name:
        pairs.append(("name", name))
        instance = labels.get("instance", "")
        if instance:
            pairs.append(("instance", instance))
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
    return f"{GRAFANA_URL}/explore?orgId=1&left={encoded}&forceLogin=true"


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
        if kind == "systemd":
            namespace, daemonset = exporter_daemonset(labels)
            if namespace and daemonset:
                return Link(
                    "Rancher",
                    f"{base}/apps.daemonset/{quote(namespace, safe='')}/{quote(daemonset, safe='')}#pods",
                )
        if rancher_kind and name and namespace:
            url = f"{base}/{rancher_kind}/{quote(namespace, safe='')}/{quote(name, safe='')}"
            if kind != "pod":
                url += "#pods"
            return Link("Rancher", url)
        node = node_name(labels)
        if node and kind != "pod":
            return Link("Rancher", f"{base}/node/{quote(node, safe='')}#pods")
        return None


def _loki_explore(expr: str, start: str = "now-1h") -> str:
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
            "range": {"from": start, "to": "now"},
        }
    }
    encoded = quote(json.dumps(panes, separators=(",", ":")), safe="")
    return f"{GRAFANA_URL}/explore?schemaVersion=1&orgId=1&panes={encoded}&forceLogin=true"


class LogsLink(LinkProvider):
    name = "logs"

    def match(self, labels: dict[str, str]) -> Link | None:
        namespace = labels.get("namespace", "")
        kind, name = subject(labels)
        if kind == "systemd":
            expr = '{service_name="%s"}' % _logql(name)
            return Link("Logs", _loki_explore(expr, "now-24h"))
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


N8N_URL = "https://n8n.socrat.alchemmist.xyz"
_N8N_WORKFLOW = re.compile(r"^/workflow/[A-Za-z0-9_-]{1,80}/?$")
_N8N_EXECUTION = re.compile(r"^/workflow/[A-Za-z0-9_-]{1,80}/executions/[0-9]{1,24}/?$")
_N8N_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def n8n_link(url: str) -> Link | None:
    """Allow only token-free HTTPS links to the existing human n8n UI."""
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) > 126 for c in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "n8n.socrat.alchemmist.xyz"
                or parsed.query or parsed.fragment or "?" in url or "#" in url):
            return None
    except ValueError:
        return None
    if _N8N_EXECUTION.fullmatch(parsed.path):
        return Link("n8n execution", N8N_URL + parsed.path.rstrip("/"))
    if _N8N_WORKFLOW.fullmatch(parsed.path):
        return Link("n8n workflow", N8N_URL + parsed.path.rstrip("/"))
    if parsed.path in ("", "/"):
        return Link("n8n", N8N_URL)
    return None


class N8nLink(LinkProvider):
    name = "n8n"

    def match(self, labels: dict[str, str]) -> Link | None:
        explicit = n8n_link(labels.get("n8n_url", ""))
        if explicit:
            return explicit
        # Prefixed IDs are explicit intent. Generic workflow_id is n8n-specific
        # only when the component says so; never map GitHub/Temporal IDs to n8n.
        workflow = labels.get("n8n_workflow_id", "")
        if not workflow and labels.get("component") == "n8n":
            workflow = labels.get("workflow_id", "")
        if workflow and _N8N_ID.fullmatch(workflow):
            execution = labels.get("n8n_execution_id", "")
            if not execution and labels.get("component") == "n8n":
                execution = labels.get("execution_id", "")
            base = N8N_URL + "/workflow/" + workflow
            if execution and re.fullmatch(r"[0-9]{1,24}", execution):
                return Link("n8n execution", base + "/executions/" + execution)
            return Link("n8n workflow", base)
        if labels.get("namespace") != "automation":
            return None
        if labels.get("component") == "spi-triage":
            # Existing stable platform workflow identity, not a namespace guess.
            return Link("n8n workflow", N8N_URL + "/workflow/J9akzlydrf9XZiID")
        if labels.get("component") == "yandex-calendar":
            if labels.get("alertname") == "Yandex session maintenance heartbeat missing":
                return Link("n8n workflow", N8N_URL + "/workflow/BJczDRwqaikJBnyo")
            # Combined voice/session rules cannot identify one execution/workflow.
            return Link("n8n", N8N_URL)
        if (labels.get("component") == "n8n" or any(
                labels.get(key) == "n8n" for key in ("deployment", "service", "service_name", "app"))):
            return Link("n8n", N8N_URL)
        return None


class TracesLink(LinkProvider):
    name = "traces"

    def match(self, labels: dict[str, str]) -> Link | None:
        trace_id = labels.get("trace_id") or labels.get("traceID") or ""
        if not trace_id:
            return None
        return Link("Traces", _explore(TEMPO_DATASOURCE, trace_id, "query"))


PROVIDERS: dict[str, LinkProvider] = {
    item.name: item
    for item in (AlertLink(), N8nLink(), NodesLink(), RancherLink(), LogsLink(), ArgoLink(), TracesLink())
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



def add_link(text: str, url: str, label: str = "SPI") -> str:
    """Add one HTML link to a rendered card without dropping a mute footer."""
    link = f'<a href="{_esc(url)}">{_esc(label)}</a>'
    if url in text or link in text:
        return text
    lines = text.split("\n")
    footer = ""
    if lines and lines[-1].startswith("<i>") and lines[-1].endswith("</i>"):
        footer = lines.pop()
        if lines and lines[-1] == "":
            lines.pop()
    for index, line in enumerate(lines):
        if "<a href=" in line:
            lines[index] = line + " · " + link
            break
    else:
        lines.append(link)
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)




def add_note(text: str, note: str) -> str:
    """Append one short plain-text line, keeping a mute footer below it."""
    cleaned = " ".join(note.split())
    if not cleaned:
        return text
    if len(cleaned) > 240:
        raise ValueError("note is too long")
    line = _esc(cleaned)
    lines = text.splitlines()
    # splitlines drops the separator; keep blank lines by splitting explicitly
    lines = text.split(chr(10))
    footer = ""
    if lines and lines[-1].startswith("<i>") and lines[-1].endswith("</i>"):
        footer = lines.pop()
        if lines and lines[-1] == "":
            lines.pop()
    if line not in lines:
        lines.append(line)
    if footer:
        lines.extend(["", footer])
    return chr(10).join(lines)


def replace_note(text: str, previous: str | None, note: str) -> str:
    """Swap a previously stored note line, then add the new one."""
    if previous:
        old = _esc(" ".join(previous.split()))
        kept = [line for line in text.split(chr(10)) if line != old]
        text = chr(10).join(kept).strip(chr(10))
    return add_note(text, note)


def episode_card_key(base: str, payload: dict) -> str:
    """SPI supplies its authoritative episode digest; keep old episodes addressable."""
    import re
    episode = payload.get("spi_episode")
    if episode is None:
        return base  # Backward-compatible callers without SPI association.
    if not isinstance(episode, str) or not re.fullmatch(r"[a-f0-9]{64}", episode):
        raise ValueError("invalid SPI episode")
    # Reserve room for mute idempotency suffixes within their 128-character cap.
    return base[:20] + "/spi/" + episode


def build_alert(payload: dict, link_names: list[str] | None = None) -> Card:
    labels = labels_of(payload.get("labels"))
    annotations = labels_of(payload.get("annotations"))
    name = labels.get("alertname") or str(payload.get("name") or "alert")
    labels["alertname"] = name
    namespace = labels.get("namespace", "")
    kind, work = subject(labels)
    node = node_name(labels)
    if kind == "systemd" and work:
        host = node or labels.get("instance", "").split(":", 1)[0]
        where = f"{host} / {work}" if host else work
    elif work and namespace:
        where = f"{namespace} / {work}"
    elif node and kind != "pod":
        where = node
    else:
        where = ""
    summary = annotations.get("summary") or annotations.get("description") or str(payload.get("summary") or "")
    status = "resolved" if str(payload.get("status") or "").lower() == "resolved" else "firing"
    link_labels = dict(labels)
    # generatorURL is already supplied by native Error Trigger notifications.
    # Do not turn an arbitrary generator URL into a Telegram hyperlink.
    candidates = [n8n_link(value) for value in (
        payload.get("generatorURL", ""), annotations.get("n8n_url", ""), labels.get("n8n_url", ""))]
    valid = [link for link in candidates if link]
    if valid:
        # Most precise target wins, independent of metadata field ordering.
        chosen = min(valid, key=lambda link: {"n8n execution": 0, "n8n workflow": 1, "n8n": 2}[link.label])
        link_labels["n8n_url"] = chosen.url
    return Card(
        key=episode_card_key(card_key(name, labels), payload),
        title=name,
        where=where,
        summary=summary,
        status=status,
        links=select_links(link_labels, link_names),
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
                "payload": {"matchers": card.matchers, "minutes": minutes, "card": render_card(card), "key": card.key},
                "idempotency_key": f"{card.key}:g{generation}:{minutes}:{chat_id}"[:128],
            }
        )
    return actions


def render_escalation(text: str | None, level: int, history: list[dict]) -> str:
    """Replace only the bot-owned stage line; preserve links, notes and mute footer."""
    lines = [line for index, line in enumerate((text or "").split("\n")) if index == 0 or not line.startswith("<b>Level ")]
    stages = ([history[0]["from"]] if history and "from" in history[0] else []) + [item["level"] for item in history]
    trail = " → ".join(str(stage) for stage in stages)
    lines.insert(1, f"<b>Level {level}</b>" + (f" · Escalated: {trail}" if trail else ""))
    return "\n".join(lines)


def replace_spi_link(text, old_url, old_label, url, label):
    """Replace the applied/previous desired SPI anchor without duplicating it."""
    if old_url and old_url != url:
        anchor = f'<a href="{_esc(old_url)}">{_esc(old_label or "SPI")}</a>'
        text = text.replace(" · " + anchor, "").replace(anchor + " · ", "").replace(anchor, "")
    return add_link(text, url, label)
