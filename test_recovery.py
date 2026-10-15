#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""本地假上游：流内错误、输出边界、断流和并发；不调用真实模型。"""
import concurrent.futures
import datetime
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from compression import zstd
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

for key in list(os.environ):
    if key.lower().endswith('_proxy'):
        del os.environ[key]
import witches


def event(kind, **fields):
    return ('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **fields}) + '\n\n').encode()


CREATED = event('response.created', response={'id': 'new', 'output': []})
DONE = event('response.completed', response={'output': []})
OUTPUT = event('response.output_text.delta', delta='hello')
BUSY = event('response.failed', response={'error': {'code': 'server_overloaded', 'message': 'PRIVATE_MESSAGE'}})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, address):
        self.errors.append('handler raised')


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        req = json.loads(zstd.decompress(raw) if self.headers.get('Content-Encoding') == 'zstd' else raw)
        model = req['model']
        with self.server.lock:
            self.server.seen.append((model, raw, dict(self.headers)))
            n = self.server.counts.get(model, 0)
            self.server.counts[model] = n + 1
            actions = self.server.scripts.get(model, [])
            action = actions[min(n, len(actions) - 1)] if actions else {}
        if self.server.block:
            self.server.block.wait(3)
        status = action.get('status', 200)
        body = action.get('body', CREATED + DONE)
        self.send_response(status)
        self.send_header('x-request-id', f'{model}-{n}')
        self.send_header('x-codex-turn-state', f'state-{n}')
        for k, v in action.get('headers', {}).items():
            self.send_header(k, v)
        if action.get('drop'):
            self.send_header('Transfer-Encoding', 'chunked')
        else:
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if action.get('drop'):
            if body:
                self.wfile.write(b'%x\r\n%s\r\n' % (len(body), body))
            self.wfile.flush()
            self.close_connection = True  # 故意不发 HTTP 结束块
        else:
            self.wfile.write(body)


class Proxy(witches.Handler):
    def log(self, msg):
        with self.server.lock:
            self.server.logs.append(msg)


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        witches.HOME = Path(cls.tmp.name) / 'w'
        witches.CODEX_HOME = Path(cls.tmp.name) / 'c'
        witches.HOME.mkdir()
        witches.CODEX_HOME.mkdir()
        (witches.CODEX_HOME / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'TOKEN'}}))
        cls.up = Server(('127.0.0.1', 0), Upstream)
        cls.proxy = Server(('127.0.0.1', 0), Proxy)
        for server in (cls.up, cls.proxy):
            server.lock = threading.Lock()
            server.errors = []
            threading.Thread(target=server.serve_forever, daemon=True).start()
        witches.OPENAI['/backend-api/codex'] = f'http://127.0.0.1:{cls.up.server_port}'

    @classmethod
    def tearDownClass(cls):
        for server in (cls.proxy, cls.up):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.up.scripts, self.up.seen, self.up.counts, self.up.block = {}, [], {}, None
        self.proxy.logs = []
        self.cfg()
        self.jitter = patch('witches.random.uniform', return_value=0)
        self.jitter.start()

    def tearDown(self):
        self.jitter.stop()
        self.assertEqual(self.proxy.errors + self.up.errors, [])

    def cfg(self, extra=''):
        (witches.HOME / 'config.toml').write_text('[openai]\nmax_concurrency = 3\n' + extra + f'''
[providers.Fake]
base_url = "http://127.0.0.1:{self.up.server_port}"
[providers.Fake.models]
"chat" = {{}}
"responses" = {{wire = "responses"}}
''')

    def post(self, model='gpt-x', path='/responses', raw=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        body = raw or json.dumps({'model': model, 'stream': True, 'input': []}).encode()
        conn.request('POST', '/backend-api/codex' + path, body, {
            'Content-Type': 'application/json', 'Authorization': 'Bearer TOKEN', **(headers or {})})
        try:
            r = conn.getresponse()
            status, response_headers = r.status, dict(r.getheaders())
            try:
                data = r.read()
                truncated = False
            except http.client.IncompleteRead as e:
                data, truncated = e.partial, True
            return status, data, response_headers, truncated
        finally:
            conn.close()

    def health(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        try:
            conn.request('GET', '/health')
            return json.loads(conn.getresponse().read())
        finally:
            conn.close()

    def test_capacity_before_output_retries_and_keeps_wire_bytes(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + BUSY}, {'body': CREATED + DONE}]
        raw = zstd.compress(json.dumps({'model': 'gpt-x', 'stream': True, 'input': []}).encode())
        status, data, headers, truncated = self.post(raw=raw, headers={
            'Content-Encoding': 'zstd', 'x-codex-turn-state': 'sticky', 'Accept-Encoding': 'gzip'})
        self.assertEqual((status, data, truncated), (200, CREATED + DONE, False))
        self.assertEqual(len(self.up.seen), 2)
        for _, sent, h in self.up.seen:
            self.assertEqual(sent, raw)
            self.assertEqual(h['x-codex-turn-state'], 'sticky')
            self.assertEqual(h['Accept-Encoding'], 'identity')
        self.assertEqual(headers['x-request-id'], 'gpt-x-1')
        self.assertEqual(headers['x-codex-turn-state'], 'state-1')
        logs = '\n'.join(self.proxy.logs)
        self.assertIn('server_overloaded', logs)
        self.assertIn('gpt-x-0', logs)
        self.assertNotIn('PRIVATE_MESSAGE', logs)

    def test_exhausted_capacity_returns_last_original_error(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + BUSY}]
        self.assertEqual(self.post()[1], CREATED + BUSY)
        self.assertEqual(self.up.counts['gpt-x'], 3)

    def test_never_replays_output_or_tool_events(self):
        for output in (OUTPUT, event('response.output_item.added', item={'type': 'function_call', 'name': 'exec'}),
                       event('response.reasoning_summary_text.delta', delta='thinking')):
            self.up.counts.clear()
            self.up.scripts['gpt-x'] = [{'body': CREATED + output + BUSY}]
            self.assertEqual(self.post()[1], CREATED + output + BUSY)
            self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_non_transient_stream_error_is_not_retried(self):
        body = event('response.failed', response={'error': {'code': 'invalid_request_error'}})
        self.up.scripts['gpt-x'] = [{'body': body}]
        self.assertEqual(self.post()[1], body)
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_http_retry_and_quota_exclusion(self):
        self.up.scripts['gpt-x'] = [{'status': 503, 'body': b'busy', 'headers': {'Retry-After': '0'}}, {}]
        self.assertEqual(self.post()[1], CREATED + DONE)
        self.assertEqual(self.up.counts['gpt-x'], 2)
        for code, status in [('insufficient_quota', 429), ('unauthorized', 401)]:
            self.up.counts.clear()
            body = json.dumps({'error': {'code': code}}).encode()
            self.up.scripts['gpt-x'] = [{'status': status, 'body': body}]
            self.assertEqual(self.post()[:2], (status, body))
            self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_retry_after_beyond_window_and_zero_retries(self):
        self.up.scripts['gpt-x'] = [{'status': 503, 'body': b'busy', 'headers': {'Retry-After': '120'}}]
        self.assertEqual(self.post()[:2], (503, b'busy'))
        self.assertEqual(self.up.counts['gpt-x'], 1)
        self.cfg('max_retries = 0\n')
        self.up.counts.clear()
        self.up.scripts['gpt-x'] = [{'body': BUSY}]
        self.assertEqual(self.post()[1], BUSY)
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_connection_failure_is_bounded_and_releases_slot(self):
        with patch('witches.fetch', side_effect=ConnectionResetError('PRIVATE_MESSAGE')) as fetch:
            status, data, _, truncated = self.post()
        self.assertEqual(status, 502)
        self.assertFalse(truncated)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(witches._slots['@openai'].active, 0)
        self.assertNotIn(b'PRIVATE_MESSAGE', data)
        self.assertNotIn('PRIVATE_MESSAGE', '\n'.join(self.proxy.logs))

    def test_delay_respects_advice_and_retry_window(self):
        self.up.scripts['gpt-x'] = [{'status': 503, 'body': b'busy', 'headers': {'Retry-After': '7'}}, {}]
        with patch('witches.time.sleep') as sleep:
            self.assertEqual(self.post()[0], 200)
        sleep.assert_called_once_with(7)
        self.up.counts.clear()
        self.cfg('retry_window = 0\n')
        self.assertEqual(self.post()[:2], (503, b'busy'))
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_oversized_startup_commits_and_parser_resumes(self):
        body = b':' + b'x' * (witches.PRELUDE_LIMIT + 1) + b'\n\n' + BUSY
        self.up.scripts['gpt-x'] = [{'body': body}]
        self.assertEqual(self.post()[1], body)
        self.assertEqual(self.up.counts['gpt-x'], 1)
        self.assertIn('stream_error', '\n'.join(self.proxy.logs))

    def test_empty_stream_failure_still_logged(self):
        body = event('response.failed', response={'error': {}})
        self.up.scripts['gpt-x'] = [{'body': body}]
        self.assertEqual(self.post()[1], body)
        self.assertIn('stream_error', '\n'.join(self.proxy.logs))

    def test_disconnect_before_and_after_output(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED, 'drop': True}, {}]
        self.assertEqual(self.post()[1], CREATED + DONE)
        self.assertEqual(self.up.counts['gpt-x'], 2)
        self.up.counts.clear()
        self.up.scripts['gpt-x'] = [{'body': CREATED + OUTPUT, 'drop': True}]
        status, data, _, truncated = self.post()
        self.assertEqual((status, data, truncated), (200, CREATED + OUTPUT, True))
        self.assertEqual(self.up.counts['gpt-x'], 1)
        self.assertIn('disconnect', '\n'.join(self.proxy.logs))

    def test_clean_eof_without_terminal_is_still_truncated(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + OUTPUT}]
        self.assertTrue(self.post()[3])
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_complete_event_without_http_end_is_not_replayed(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + DONE, 'drop': True}]
        self.assertEqual(self.post()[1::2], (CREATED + DONE, False))
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_compact_json_and_other_paths_remain_passthrough(self):
        body = b'{"output": []}'
        self.up.scripts['gpt-x'] = [{'body': body, 'headers': {'Content-Type': 'application/json'}}]
        self.assertEqual(self.post(path='/responses/compact')[1], body)
        self.assertEqual(self.post()[1], body)
        self.up.counts.clear()
        self.up.scripts['gpt-x'] = [{'status': 503, 'body': b'busy'}]
        self.assertEqual(self.post(path='/alpha/search')[:2], (503, b'busy'))
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_third_party_disconnect_does_not_claim_completion(self):
        for model, body in [('chat', b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'),
                            ('responses', CREATED + OUTPUT)]:
            self.up.scripts[model] = [{'body': body}]
            status, data, _, truncated = self.post('Fake/' + model)
            self.assertEqual(status, 200)
            self.assertTrue(truncated)
            self.assertNotIn(b'response.completed', data)
            self.assertEqual(self.up.counts[model], 1)

    def test_gate_limits_parallel_requests_and_recovers_after_errors(self):
        self.cfg('queue_timeout = 3\n')
        self.up.block = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            jobs = [pool.submit(self.post) for _ in range(6)]
            deadline = time.monotonic() + 2
            while self.up.counts.get('gpt-x', 0) < 3 and time.monotonic() < deadline:
                time.sleep(.01)
            try:
                self.assertEqual(self.up.counts.get('gpt-x'), 3)
                self.assertEqual(witches._slots['@openai'].active, 3)
                deadline = time.monotonic() + 2
                while self.health()['openai']['waiting'] < 3 and time.monotonic() < deadline:
                    time.sleep(.01)
                state = self.health()['openai']
                self.assertEqual((state['active'], state['waiting'], state['runtime_limit']), (3, 3, 3))
            finally:
                self.up.block.set()
            self.assertTrue(all(job.result()[0] == 200 for job in jobs))
        self.assertEqual(witches._slots['@openai'].active, 0)
        self.assertEqual(self.health()['openai']['waiting'], 0)
        self.up.scripts['gpt-x'] = [{'body': CREATED + OUTPUT, 'drop': True}]
        self.post()
        self.assertEqual(witches._slots['@openai'].active, 0)

    def test_queue_timeout_is_distinct_from_upstream_capacity(self):
        self.cfg('queue_timeout = 0\n')
        gate = witches.slot('@openai', {'max_concurrency': 3})
        for _ in range(3):
            gate.acquire()
        try:
            status, body, headers, _ = self.post()
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)['error']['code'], 'witches_queue_timeout')
            self.assertEqual(headers['Retry-After'], '2')
            self.assertEqual(self.up.seen, [])
        finally:
            for _ in range(3):
                gate.release()

    def test_gate_hot_reload_preserves_active_count(self):
        gate = witches.slot('hot', {'max_concurrency': 2})
        gate.acquire()
        gate.acquire()
        self.assertIs(gate, witches.slot('hot', {'max_concurrency': 1}))
        self.assertFalse(gate.acquire(timeout=0))
        gate.release()
        self.assertFalse(gate.acquire(timeout=0))
        gate.release()
        self.assertTrue(gate.acquire(timeout=0))
        gate.release()

    def test_fragmented_multiline_sse_and_bounded_buffer(self):
        body = b': ping\r\n\r\nevent: response.failed\r\ndata: {"type":"response.failed",\r\ndata: "response":{"error":{"code":"server_is_overloaded"}}}\r\n\r\n'
        probe = witches.StreamProbe()
        for byte in body:
            probe.feed(bytes([byte]))
        self.assertTrue(witches.transient_error(probe.failure))
        self.assertFalse(probe.output)
        self.assertTrue(probe.terminal)
        probe = witches.StreamProbe()
        probe.feed(b'x' * (witches.PRELUDE_LIMIT + 1))
        self.assertTrue(probe.output)
        self.assertEqual(probe.buffer, b'')

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(witches.retry_after('0'), 0)
        self.assertEqual(witches.retry_after('120'), 120)
        future = format_datetime(datetime.datetime.fromtimestamp(time.time() + 60, datetime.timezone.utc), usegmt=True)
        self.assertGreater(witches.retry_after(future), 58)
        self.assertIsNone(witches.retry_after('invalid'))

    def test_health_reports_only_allowlisted_settings(self):
        self.cfg('unrelated_private_setting = "PRIVATE_MESSAGE"\n')
        result = self.health()
        self.assertEqual(result['pid'], os.getpid())
        self.assertEqual(result['openai']['max_retries'], 2)
        self.assertEqual(result['openai']['retry_window'], 60)
        self.assertNotIn('PRIVATE_MESSAGE', json.dumps(result))


if __name__ == '__main__':
    unittest.main(verbosity=2)
