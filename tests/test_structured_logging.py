import json
import logging

from socrat_bot.structured_logging import JsonFormatter, configure_logging, SERVICE


def test_safe_json():
    record = logging.LogRecord(SERVICE, logging.ERROR, '', 1, 'private\n%s', ('secret',),
                               (ValueError, ValueError('secret'), None))
    record.headers = {'Authorization': 'secret'}
    encoded = JsonFormatter().format(record)
    data = json.loads(encoded)
    assert data['msg'] == 'application event'
    assert data['level'] == 'error'
    assert data['service'] == SERVICE
    assert data['ts']
    assert 'secret' not in encoded and '\n' not in encoded
    assert 'trace_id' not in data and 'span_id' not in data


def test_initialization_idempotent():
    logger = configure_logging()
    configure_logging()
    assert len([h for h in logger.handlers if getattr(h, '_safe_json', False)]) == 1
    assert not logger.propagate


def test_reviewed_events_keep_names_and_only_safe_numeric_fields():
    from socrat_bot.structured_logging import EVENTS
    for event in EVENTS:
        record = logging.LogRecord(SERVICE, logging.INFO, '', 1, event, ('secret',), None)
        record.token = 'secret'
        data = json.loads(JsonFormatter().format(record))
        assert data['msg'] == event
        assert 'secret' not in json.dumps(data)


def test_nonfinite_metrics_are_omitted():
    record = logging.LogRecord(SERVICE, logging.INFO, '', 1,
                               'broadcast completed', (), None)
    record.status = 200
    record.duration_ms = float('nan')
    record.sent = 3
    record.failed = float('inf')
    data = json.loads(JsonFormatter().format(record))
    assert 'duration_ms' not in data and 'failed' not in data
    assert data.get('status', data.get('sent')) == 3


def test_existing_trace_context_only():
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
    context = SpanContext(1, 2, False, TraceFlags(1))
    with trace.use_span(NonRecordingSpan(context)):
        record = logging.LogRecord(SERVICE, logging.INFO, '', 1, 'http request completed', (), None)
        data = json.loads(JsonFormatter().format(record))
    assert data['trace_id'] == '00000000000000000000000000000001'
    assert data['span_id'] == '0000000000000002'
