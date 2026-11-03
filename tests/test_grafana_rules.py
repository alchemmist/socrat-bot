import json

from socrat_bot.grafana_rules import PromRule, rule_hash, rule_view_url

QUERY = "(max by (namespace, pod, container, uid) (increase(kube_pod_container_status_restarts_total{job=\"kube-state-metrics\"}[30m]) or increase(kube_pod_init_container_status_restarts_total{job=\"kube-state-metrics\"}[30m])) >= 3) * on (namespace, pod, uid) group_left (node) max by (namespace, pod, uid, node) (kube_pod_info{job=\"kube-state-metrics\"})"
ANNOTATIONS = {"description": "Container {{ $labels.container }} in pod {{ $labels.namespace }}/{{ $labels.pod }} on node {{ $labels.node }} has an estimated increase of at least 3 restarts in the trailing 30m for 1m. This includes regular and init containers and does not require continuous unready or CrashLoopBackOff states. Check termination reasons and previous container logs; Prometheus increase extrapolates between samples.", "rancher_url": "https://rancher.socrat.alchemmist.xyz/dashboard/c/local/explorer/pod/{{ $labels.namespace }}/{{ $labels.pod }}", "summary": "Container is restarting frequently."}
LABELS = {"prometheus": "cattle-monitoring-system/kube-prometheus-stack-prometheus", "prometheus_replica": "prometheus-kube-prometheus-stack-prometheus-0", "severity": "warning"}
FILE = "/etc/prometheus/rules/prometheus-kube-prometheus-stack-prometheus-rulefiles-0/cattle-monitoring-system-monitoring-k3s-shared-registry-bdbc2ae6-606d-4769-afad-4f3cfff40496.yaml"
GROUP = "socrat.containers.health"


def test_restart_rule_hash_matches_the_live_grafana_link():
    rule = PromRule(
        file=FILE,
        group=GROUP,
        name="SocratContainerRestartsFrequent",
        query=QUERY,
        labels=LABELS,
        annotations=ANNOTATIONS,
    )
    assert rule_hash(rule) == "1251941481"
    url = rule_view_url(rule)
    assert "/alerting/Prometheus/pri%24Prometheus%24" in url
    assert "1251941481" in url
    assert "alertname%3A" not in url
