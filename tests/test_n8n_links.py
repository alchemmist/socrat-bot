"""Offline card rendering only; never sends a real Telegram message."""
import pytest
from socrat_bot.cards import N8N_URL, build_alert, n8n_link, render_card, select_links


def links(payload):
    return [link for link in build_alert(payload).links if link.label.startswith('n8n')]


def test_native_execution_generator_is_rendered_in_firing_and_resolved_cards():
    url = N8N_URL + '/workflow/abc/executions/123'
    for status in ('firing', 'resolved'):
        payload = {'status': status, 'generatorURL': url, 'labels': {
            'alertname': 'N8nWorkflowExecutionFailed', 'component': 'n8n',
            'namespace': 'automation', 'workflow_id': 'abc'}}
        card = build_alert(payload)
        assert links(payload)[0].url == url
        assert f'<a href="{url}">n8n execution</a>' in render_card(card)
        assert card.key == 'N8nWorkflowExecutionFailed/cluster'  # no historical identity migration


def test_most_precise_target_wins_and_annotations_are_supported():
    payload = {'generatorURL': N8N_URL, 'annotations': {'n8n_url': N8N_URL + '/workflow/abc/executions/123'}}
    assert links(payload)[0].label == 'n8n execution'
    assert links({'annotations': {'n8n_url': N8N_URL + '/workflow/abc'}})[0].label == 'n8n workflow'


def test_explicit_ids_and_generic_workflow_id_scope():
    result = select_links({'component': 'n8n', 'workflow_id': 'abc', 'execution_id': '123'})
    assert next(link for link in result if link.label == 'n8n execution').url.endswith('/workflow/abc/executions/123')
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
    assert n8n_link(url) is None
    assert not links({'generatorURL': url})


def test_specific_spi_and_calendar_links_without_namespace_wide_guessing():
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
    result = links({'labels': {'namespace': 'automation', 'deployment': 'n8n'}})
    assert result[0].url == N8N_URL


def test_link_survives_default_cap_and_explicit_provider_optout_is_preserved():
    labels = {'namespace': 'automation', 'deployment': 'n8n', 'node': 'socrat-1'}
    result = select_links(labels)
    assert len(result) <= 4 and any(link.label == 'n8n' for link in result)
    assert not any(link.label.startswith('n8n') for link in select_links(labels, ['alert', 'logs']))


def test_invalid_id_does_not_enter_link():
    assert select_links({'n8n_workflow_id': '../../secret'}, ['n8n']) == []
    result = select_links({'n8n_workflow_id': 'abc', 'n8n_execution_id': '../secret'}, ['n8n'])
    assert result[0].url == N8N_URL + '/workflow/abc'
