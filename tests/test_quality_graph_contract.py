"""Keep optional trusted publication off until its upstream contracts are verified."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_pr_and_main_retain_same_checks_without_optional_presentation():
    """Preserve all three quality checks and ordering without comment-command handling."""
    declaration = yaml.safe_load((ROOT / 'qg.yaml').read_text())
    expected = {'graph-freshness', 'n8n-card-links', 'full-regressions'}
    assert set(declaration['operations']) == expected
    for name in ('review', 'main'):
        flow = declaration['flows'][name]
        assert flow['presentation'] == 'none'
        assert set(flow['nodes']) == expected
        assert flow['nodes']['full-regressions']['needs'] == ['n8n-card-links']
    assert not declaration['provider']['configuration'].get('merge', {}).get('required')


def test_no_generated_publisher_or_comment_command_entrypoint_exists():
    """Make both reported upstream risks unreachable, not merely hidden in job conditions."""
    assert not (ROOT / '.github/workflows/quality-graph-publish.yml').exists()
    for filename in ('quality-graph.yml', 'quality-graph-push.yml'):
        workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
        events = workflow.get('on', workflow.get(True))
        assert 'workflow_run' not in events
        assert 'issue_comment' not in events
        assert workflow['permissions'] == {'contents': 'read'}
        for job in workflow['jobs'].values():
            assert set(job['permissions'].values()) <= {'read', 'none'}
            assert any(step.get('name', '').startswith('Upload ') for step in job['steps'])
            assert any(step.get('name', '').startswith('Enforce ') for step in job['steps'])


def test_released_compiler_and_runtime_are_upgraded_as_one_contract():
    """Prevent accidental mixing of old collectors with explicit-flow provenance."""
    config = yaml.safe_load((ROOT / 'qg.yaml').read_text())
    assert config['provider']['configuration']['runtime']['action'] == (
        'alchemmist/quality-graph@3b0cf7d8a33215d5a3bb5521b4e0e41c18e8cb7c'
    )
    manifest = json.loads((ROOT / '.quality-graph/manifest.json').read_text())
    assert manifest['manifestVersion'] == 1
    project = (ROOT / 'pyproject.toml').read_text()
    assert 'quality-graph-cli==0.2.0' in project
    assert 'quality-graph-github==0.2.0' in project
