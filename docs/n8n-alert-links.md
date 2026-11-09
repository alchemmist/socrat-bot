# n8n links in alert cards

Alert cards now include a compact English HTML anchor (not a new callback action).
It appears in the ordinary message/caption alongside existing links. Firing and
Resolved use the same resolver. No payload mutation, existing card-key/mute/SPI
identity migration, notification replay or historical message rewrite is required.
Native `Workflow Failure Alerts` already supplies `generatorURL`; no n8n workflow
edit or credential access is needed for its link to become visible after release.

Priority: validated execution URL -> validated workflow URL -> n8n UI home.
`generatorURL`, `annotations.n8n_url` and `labels.n8n_url` are supported; the most
specific valid target wins. Only the fixed HTTPS n8n human origin and UI paths
`/workflow/<id>[/executions/<numeric-id>]` or home are accepted. No userinfo,
query/fragment, alternate port/host, insecure scheme, encoded traversal, webhook
endpoint or token-bearing URL is forwarded. Public UI links still require the
existing native SSO; no session/token is attached.

Explicit `n8n_workflow_id`/`n8n_execution_id` labels construct links safely.
Generic `workflow_id`/`execution_id` are interpreted ONLY for `component=n8n`,
not other workflow engines. Known platform mappings are intentionally bounded:

- SPI `namespace=automation,component=spi-triage`: original SPI workflow.
- Yandex calendar maintenance-heartbeat alert: original maintenance workflow.
- Combined Yandex session/speech failure: n8n home (no invented attribution).
- Explicit n8n component/workload/container/service in automation: n8n home.
- Actual blackbox probe `service_id=n8n` (no namespace label): n8n home.
- Unrelated infrastructure or namespace=automation alone: no n8n link.

The provider is `n8n` and defaults immediately after `alert`, so it survives the
existing four-link cap. Explicit provider lists remain opt-in and can omit it.
Old Alert/Nodes/Rancher/Logs/Argo/Traces providers and card identities are retained.
Existing photo transport preserves full text or falls back to text when caption
limits require; this feature does not truncate the link or trigger new graphs.

## Checks / release boundary

New checks are declared in `qg.yaml`, generated with pinned CLI/provider0.1.8 and
immutable runtime/setup/upload Actions, as in the official quickstart/migration.
`uv run qg generate` / `uv run qg validate`; do not edit generated YAML directly.
No qg.yaml existed here before this change. Keep legacy CI/release until bootstrap
and representative PR parity; privileged Harbor/deploy job remains outside the
secretless graph. Isolated PostgreSQL test-service trust is CI-only, never a
production authorization setting. No production secret or deployment in PR tests.

Run `uv run pytest -q tests/test_cards.py tests/test_n8n_links.py` locally. Full
regressions run against disposable PostgreSQL in Quality Graph. This source/PR
acceptance is not a published Harbor image, Argo reconciliation or natural
Telegram delivery receipt. The standard immutable-image GitOps release is still
required before claiming live bot behavior. Do not send synthetic production
alerts/messages or mutate real incidents merely to prove a hyperlink.
