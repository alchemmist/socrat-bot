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


def test_actual_entrypoints_emit_owned_json(tmp_path):
    """Real uvicorn startup, isolated SQLite, no Telegram/network credentials."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    for module, component in [('socrat_bot', 'bot'), ('socrat_bot.broker', 'broker'),
                              ('socrat_bot.updater', 'updater')]:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('TELEGRAM_', 'INTERNAL_', 'DATABASE_'))}
        env.update(PORT=str(port), DATABASE_URL=f'sqlite+pysqlite:///{tmp_path / (component + ".db")}',
                   PYTHONPATH=os.path.abspath('src'))
        with (tmp_path / (component + '.out')).open('w+') as output:
            process = subprocess.Popen([sys.executable, '-m', module], env=env,
                                       stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise AssertionError(f'{component} exited during actual startup: {process.returncode}')
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz?private_canary=secret', timeout=.5) as response:
                            assert response.status == 200
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AssertionError(f'{component} actual startup timed out')
                        time.sleep(.05)
            finally:
                process.terminate()
                process.wait(timeout=10)
            output.seek(0)
            text = output.read()
        assert 'private_canary' not in text and 'secret' not in text
        events = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
        assert any(e['msg'] == 'application started' and e['component'] == component for e in events)
        assert any(e['msg'] == 'application stopped' for e in events)
        requests = [e for e in events if e['msg'] == 'http request completed']
        assert requests and all(e['status'] == 200 and e['duration_ms'] >= 0 for e in requests)
        assert all(set(e) <= {'ts', 'level', 'service', 'msg', 'component', 'status', 'duration_ms'} for e in events)


def test_request_failure_diagnostics_do_not_serialize_exception():
    import asyncio
    from unittest.mock import Mock
    from socrat_bot.structured_logging import OperationalEvents

    async def failing(scope, receive, send):
        raise ValueError('private request body')

    middleware = OperationalEvents(failing, 'broker')
    middleware.log = Mock()
    async def noop(*args):
        pass
    try:
        asyncio.run(middleware({'type': 'http'}, noop, noop))
    except ValueError:
        pass
    else:
        raise AssertionError('middleware must not swallow application failures')
    args, kwargs = middleware.log.info.call_args
    assert args == ('http request completed',)
    assert kwargs['extra']['status'] == 500
    assert kwargs['extra']['component'] == 'broker'
    assert 'private' not in str(kwargs)
