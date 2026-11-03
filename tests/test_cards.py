import os

os.environ["GRAFANA_RULES_DISABLE"] = "1"

from socrat_bot.cards import build_alert, render_card, select_links


def _pod():
    return build_alert(
        {
            "status": "firing",
            "labels": {
                "alertname": "NodeSystemdServiceFailed",
                "namespace": "cattle-monitoring-system",
                "pod": "node-exporter-dt68v",
                "node": "socrat-2",
            },
            "annotations": {"summary": "Systemd service has entered failed state."},
        }
    )


def test_firing_card_has_no_count_header():
    text = render_card(_pod())
    assert text.startswith("<b>NodeSystemdServiceFailed</b>")
    assert "Firing" not in text
    assert "Alert" in text and "Rancher" in text and "Logs" in text
    assert "Nodes" not in text
    assert "Argo CD" not in text


def test_node_card_uses_dashboard_and_rancher_node():
    card = build_alert(
        {
            "status": "firing",
            "labels": {"alertname": "KubeNodeNotReady", "node": "socrat-1"},
            "annotations": {"summary": "Node is not ready."},
        }
    )
    labels = [link.label for link in card.links]
    assert labels == ["Alert", "Nodes", "Rancher"]
    assert "var-node=socrat-1" in card.links[1].url
    assert card.links[2].url.endswith("/node/socrat-1#pods")
    assert card.matchers == [
        {"name": "alertname", "value": "KubeNodeNotReady", "isRegex": False, "isEqual": True},
        {"name": "node", "value": "socrat-1", "isRegex": False, "isEqual": True},
    ]


def test_cluster_alert_is_only_the_grafana_list():
    card = build_alert({"labels": {"alertname": "KubeMemoryOvercommit"}, "annotations": {"summary": "Memory"}})
    assert [link.label for link in card.links] == ["Alert"]
    assert card.where == ""
    assert card.key == "KubeMemoryOvercommit/cluster"


def test_resolved_prefix_and_single_namespace_argo():
    card = build_alert(
        {
            "status": "resolved",
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "socrat-bot", "pod": "socrat-bot-0"},
            "annotations": {"summary": "restarting"},
        }
    )
    text = render_card(card)
    assert text.startswith("<b>Resolved</b>")
    assert "Argo CD" in text
    assert "/applications/argocd/socrat-bot" in text


def test_explicit_providers_include_traces_without_the_default_cap():
    labels = {"alertname": "Latency", "trace_id": "abc123"}
    links = select_links(labels, ["traces", "alert"])
    assert [link.label for link in links] == ["Traces", "Alert"]


def test_same_pod_keeps_one_key():
    first = _pod()
    second = _pod()
    assert first.key == second.key == "NodeSystemdServiceFailed/pod/cattle-monitoring-system/node-exporter-dt68v"
