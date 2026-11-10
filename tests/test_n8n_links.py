"""Offline card rendering only; never sends a real Telegram message."""
import pytest
from socrat_bot.cards import N8N_URL, build_alert, n8n_link, render_card, select_links


def links(payload):
    """Select only n8n anchors from an otherwise ordinary alert card."""
    return [link for link in build_alert(payload).links if link.label.startswith('n8n')]


def test_native_execution_generator_is_rendered_in_firing_and_resolved_cards():
    """Keep execution navigation visible across status changes without migrating card keys."""
    url = N8N_URL + '/workflow/abc/executions/123'
    for status in ('firing', 'resolved'):
        payload = {'status': status, 'generatorURL': url, 'labels': {
            'alertname': 'N8nWorkflowExecutionFailed', 'component': 'n8n',
            'namespace': 'automation', 'workflow_id': 'abc'}}
        card = build_alert(payload)
        assert links(payload)[0].url == url
        assert f'<a href="{url}">n8n</a>' in render_card(card)
        assert card.key == 'N8nWorkflowExecutionFailed/cluster'  # no historical identity migration


def test_most_precise_target_wins_and_annotations_are_supported():
    """Prefer execution metadata over a generic home link irrespective of its source field."""
    payload = {'generatorURL': N8N_URL, 'annotations': {'n8n_url': N8N_URL + '/workflow/abc/executions/123'}}
    assert links(payload)[0].label == 'n8n'
    assert links({'annotations': {'n8n_url': N8N_URL + '/workflow/abc'}})[0].label == 'n8n'


def test_explicit_ids_and_generic_workflow_id_scope():
    """Interpret generic workflow IDs only when the producer explicitly identifies n8n."""
    result = select_links({'component': 'n8n', 'workflow_id': 'abc', 'execution_id': '123'})
    assert next(link for link in result if link.label == 'n8n').url.endswith('/workflow/abc/executions/123')
    assert select_links({'n8n_workflow_id': 'abc'}, ['n8n'])[0].url.endswith('/workflow/abc')
    assert not links({'labels': {'component': 'github', 'workflow_id': '123'}})


@pytest.mark.parametrize('url', [
    'http://n8n.socrat.alchemmist.xyz/workflow/abc',
    'https://evil.invalid/workflow/abc',
    'https://n8n.socrat.alchemmist.xyz.evil.invalid/workflow/abc',
    'https://user:secret@n8n.socrat.alchemmist.xyz/workflow/abc',
    N8N_URL + ':443/workflow/abc', N8N_URL + '/workflow/../secret',
    N8N_URL + '/workflow/a%2fb', N8N_URL + '/webhook/secret',
    N8N_URL + '/workflow/abc?token=secret', N8N_URL + '/workflow/abc#secret',
    N8N_URL + '/workflow/abc?', N8N_URL + '/workflow/abc#',
    N8N_URL + '/workflow/abc\n', 'javascript:alert(1)',
    N8N_URL + '/workflow/abc/executions/not-a-number',
])
def test_untrusted_or_secret_bearing_urls_are_rejected(url):
    """Reject unsafe authorities, paths and token-bearing metadata instead of forwarding it."""
    assert n8n_link(url) is None
    assert not links({'generatorURL': url})


def test_specific_spi_and_calendar_links_without_namespace_wide_guessing():
    """Map only known platform targets and keep unrelated automation alerts untouched."""
    spi = links({'labels': {'namespace': 'automation', 'component': 'spi-triage'}})
    assert spi[0].url.endswith('/workflow/J9akzlydrf9XZiID')
    maintenance = links({'labels': {'namespace': 'automation', 'component': 'yandex-calendar',
                                   'alertname': 'Yandex session maintenance heartbeat missing'}})
    assert maintenance[0].url.endswith('/workflow/BJczDRwqaikJBnyo')
    combined = links({'labels': {'namespace': 'automation', 'component': 'yandex-calendar'}})
    assert combined[0].url == N8N_URL
    assert not links({'labels': {'namespace': 'automation', 'component': 'unrelated'}})
    assert not links({'labels': {'namespace': 'llm', 'deployment': 'vllm'}})


def test_n8n_service_health_links_to_ui_and_never_to_a_fabricated_execution():
    """Link service health to its UI rather than inventing a failed execution."""
    result = links({'labels': {'namespace': 'automation', 'deployment': 'n8n'}})
    assert result[0].url == N8N_URL


def test_actual_service_probe_labels_need_no_namespace_and_other_probes_do_not_match():
    """Respect real blackbox labels while excluding unrelated probe targets."""
    result = links({'labels': {'alertname': 'SocratServiceN8nUnavailable', 'service_id': 'n8n'}})
    assert result[0].url == N8N_URL
    assert not links({'labels': {'alertname': 'SocratServiceHarborUnavailable', 'service_id': 'harbor'}})
    result = links({'labels': {'namespace': 'automation', 'container': 'n8n', 'pod': 'n8n-example'}})
    assert result[0].url == N8N_URL


def test_link_survives_default_cap_and_explicit_provider_optout_is_preserved():
    """Preserve the four-link budget and callers that intentionally omit this provider."""
    labels = {'namespace': 'automation', 'deployment': 'n8n', 'node': 'socrat-1'}
    result = select_links(labels)
    assert len(result) <= 4 and any(link.label == 'n8n' for link in result)
    assert not any(link.label.startswith('n8n') for link in select_links(labels, ['alert', 'logs']))


def test_invalid_id_does_not_enter_link():
    """Discard unsafe execution IDs and retain a safe workflow fallback."""
    assert select_links({'n8n_workflow_id': '../../secret'}, ['n8n']) == []
    result = select_links({'n8n_workflow_id': 'abc', 'n8n_execution_id': '../secret'}, ['n8n'])
    assert result[0].url == N8N_URL + '/workflow/abc'


@pytest.mark.parametrize('metadata', [
    {'n8n_workflow_id': 'J9akzlydrf9XZiID', 'n8n_execution_id': '16473'},
    {'component': 'n8n', 'workflow_id': 'J9akzlydrf9XZiID', 'execution_id': '16473'},
    {'namespace': 'automation', 'component': 'spi-triage', 'n8n_execution_id': '16473'},
])
def test_execution_ids_upgrade_a_workflow_url_without_long_anchor_text(metadata):
    """An available execution ID must not be hidden by a less precise URL."""
    payload = {'labels': metadata, 'annotations': {'n8n_url': N8N_URL + '/workflow/J9akzlydrf9XZiID'}}
    card = build_alert(payload)
    link = next(link for link in card.links if link.label == 'n8n')
    assert link.url == N8N_URL + '/workflow/J9akzlydrf9XZiID/executions/16473'
    assert f'<a href="{link.url}">n8n</a>' in render_card(card)
    assert 'n8n workflow' not in render_card(card)
    assert 'n8n execution' not in render_card(card)


def test_execution_url_still_wins_when_all_anchor_labels_are_identical():
    """URL specificity, not the display label, determines target precedence."""
    payload = {'generatorURL': N8N_URL + '/workflow/abc',
               'annotations': {'n8n_url': N8N_URL + '/workflow/abc/executions/16473'}}
    assert links(payload)[0].url.endswith('/executions/16473')
    assert links(payload)[0].label == 'n8n'
