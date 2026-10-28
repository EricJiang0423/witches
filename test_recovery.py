#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""本地假上游：流内错误、输出边界、断流和并发；不调用真实模型。"""
import base64
import concurrent.futures
import datetime
import http.client
import io
import json
import os
import select
import socket
import socketserver
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

    def await_cancel(self, action):
        action['started'].set()
        try:
            if not select.select([self.connection], [], [], 3)[0]:
                self.server.errors.append('abandoned upstream still open')
            elif self.connection.recv(1, socket.MSG_PEEK):
                self.server.errors.append('unexpected upstream bytes')
        except ConnectionResetError:
            pass
        finally:
            self.close_connection = True
            action['closed'].set()

    def do_GET(self):
        if not self.path.endswith('/wham/usage'):
            return self.send_error(404)
        with self.server.lock:
            self.server.seen.append(('usage', b'', dict(self.headers)))
        hit = self.headers['Authorization'].endswith('.b')
        body = json.dumps({'plan_type': 'plus', 'rate_limit': {'limit_reached': hit, 'primary_window': {
            'used_percent': 100 if hit else 30, 'limit_window_seconds': 18000, 'reset_after_seconds': 60},
            'secondary_window': {'used_percent': 5, 'limit_window_seconds': 604800, 'reset_after_seconds': 99}}}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if action.get('stall') == 'headers':
            return self.await_cancel(action)
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
        if action.get('stall') == 'stream':
            self.wfile.write(CREATED)
            self.wfile.flush()
            return self.await_cancel(action)
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

    def cfg(self, extra='', limit=3):
        (witches.HOME / 'config.toml').write_text(f'[openai]\nmax_concurrency = {limit}\n' + extra + f'''
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

    def wait_idle(self):
        gate = witches._slots.get('@openai')
        if gate:
            with gate.condition:
                self.assertTrue(gate.condition.wait_for(lambda: gate.active == 0, timeout=2))

    def attempt_logs(self):
        self.wait_idle()
        return [json.loads(row.split(' -> openai ', 1)[1]) for row in self.proxy.logs if ' -> openai {' in row]

    def assert_cancel_release(self, chain_id):
        # gate.release() 先唤醒等待者，随后才写日志，不能仅以 active=0 判断日志已落盘。
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self.proxy.lock:
                rows = [json.loads(row.split(' -> openai_release ', 1)[1]) for row in self.proxy.logs
                        if ' -> openai_release ' in row]
            if any(row['chain_id'] == chain_id for row in rows):
                break
            time.sleep(.01)
        released = [row for row in rows if row['chain_id'] == chain_id]
        self.assertEqual(len(released), 1)
        row = released[0]
        self.assertEqual(row['reason'], 'client_cancelled')
        self.assertGreaterEqual(row['cancel_to_release_ms'], 0)
        self.assertLess(row['cancel_to_release_ms'], 1000)
        self.assertGreaterEqual(row['active'], 0)
        self.assertGreaterEqual(row['waiting'], 0)
        self.assertEqual(row['runtime_limit'], witches._slots['@openai'].limit)

    def accounts(self, *names):
        """额外账户：Codex 格式的 auth.json，access token 是没过期的假 JWT。"""
        paths = []
        for name in names:
            payload = base64.urlsafe_b64encode(json.dumps({'exp': time.time() + 3600}).encode()).decode().rstrip('=')
            path = witches.HOME / f'{name}.json'
            path.write_text(json.dumps({'tokens': {'access_token': f'x.{payload}.{name}', 'refresh_token': 'r',
                                                   'account_id': f'id-{name}'}}))
            paths.append(str(path))
            witches._down.pop(path, None)
        witches._down.pop(None, None)
        self.cfg(f'accounts = {json.dumps(paths)}\n')

    def test_quota_fails_over_to_next_account_and_sticks(self):
        self.accounts('b', 'c')
        quota = json.dumps({'error': {'type': 'usage_limit_reached', 'resets_in_seconds': 3600}}).encode()
        self.up.scripts['gpt-x'] = [{'status': 429, 'body': quota}, {}]
        status, data, _, _ = self.post(headers={'x-codex-turn-state': 'sticky', 'chatgpt-account-id': 'id-a'})
        self.assertEqual((status, data), (200, CREATED + DONE))
        first, second = (h for _, _, h in self.up.seen)
        self.assertEqual((first['Authorization'], first['chatgpt-account-id']), ('Bearer TOKEN', 'id-a'))
        self.assertTrue(second['Authorization'].endswith('.b'))
        self.assertEqual(second['chatgpt-account-id'], 'id-b')
        self.assertNotIn('x-codex-turn-state', second)
        self.assertEqual([r['account'] for r in self.attempt_logs()], ['primary', 'b'])
        self.up.seen.clear()  # 之后的请求直接走还有额度的账号，不再先撞一次已知用完的主账号
        self.assertEqual(self.post()[1], CREATED + DONE)
        self.assertTrue(self.up.seen[0][2]['Authorization'].endswith('.b'))
        self.assertEqual([a['down_s'] > 0 for a in self.health()['openai']['accounts']], [True, False, False])

    def test_primary_position_and_panel(self):
        self.accounts('b')
        self.cfg(f'accounts = [{json.dumps(str(witches.HOME / "b.json"))}, "primary"]\n')
        self.assertTrue(self.post()[1] == CREATED + DONE and self.up.seen[0][2]['Authorization'].endswith('.b'))
        self.assertEqual([a['account'] for a in self.health()['openai']['accounts']], ['b', 'primary'])
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('GET', '/panel')
        r = conn.getresponse()
        self.assertEqual((r.status, r.getheader('Content-Type').split(';')[0]), (200, 'text/html'))
        conn.close()

    def test_usage_api_reports_remaining_quota_and_caches(self):
        self.accounts('b')
        witches._usage_cache.clear()

        def get(path):
            conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
            conn.request('GET', path)
            try:
                return json.loads(conn.getresponse().read())
            finally:
                conn.close()
        rows = get('/api/usage')['accounts']
        self.assertEqual([r['account'] for r in rows], ['primary', 'b'])
        self.assertEqual(rows[0]['windows'], [
            {'name': '5h', 'used_percent': 30, 'remaining_percent': 70, 'resets_in_s': 60},
            {'name': '7d', 'used_percent': 5, 'remaining_percent': 95, 'resets_in_s': 99}])
        self.assertTrue(rows[1]['limit_reached'])
        self.assertEqual(rows[1]['windows'][0]['remaining_percent'], 0)
        self.assertNotIn('TOKEN', json.dumps(rows))
        get('/api/usage')
        self.assertEqual(sum(1 for m, _, _ in self.up.seen if m == 'usage'), 2)  # 第二次命中缓存
        get('/api/usage?refresh=1')
        self.assertEqual(sum(1 for m, _, _ in self.up.seen if m == 'usage'), 4)

    def test_stream_quota_fails_over_and_all_exhausted_returns_original_error(self):
        self.accounts('b')
        err = event('response.failed', response={'error': {'code': 'usage_limit_reached', 'resets_in_seconds': 60}})
        self.up.scripts['gpt-x'] = [{'body': CREATED + err}, {}]
        self.assertEqual(self.post()[1], CREATED + DONE)
        self.assertEqual(len(self.up.seen), 2)
        quota = json.dumps({'error': {'type': 'usage_limit_reached'}}).encode()
        self.up.scripts['gpt-x'] = [{'status': 429, 'body': quota}]
        self.up.counts.clear()
        self.assertEqual(self.post()[:2], (429, quota))  # 主账号和 b 都已标记用完：用主账号，上游错误原样给 Codex
        self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_broken_account_file_is_skipped(self):
        self.accounts('b')
        (witches.HOME / 'b.json').write_text('not json')
        quota = json.dumps({'error': {'type': 'usage_limit_reached'}}).encode()
        self.up.scripts['gpt-x'] = [{'status': 429, 'body': quota}]
        self.assertEqual(self.post()[:2], (429, quota))
        self.assertEqual(len(self.up.seen), 2)  # 失败切换后 b 读不出来：回到主账号拿真实错误

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

    def test_empty_semantic_prelude_retries_without_leaking_failed_attempt(self):
        placeholders = [
            event('response.output_item.added', item={'type': 'reasoning', 'id': 'r1', 'summary': []}),
            event('response.output_item.added', item={'type': 'message', 'id': 'm1',
                  'role': 'assistant', 'status': 'in_progress', 'content': []}),
            event('response.output_item.added', item={'type': 'message', 'id': 'm2',
                  'role': 'assistant', 'status': 'in_progress', 'content': [], 'phase': 'commentary'}),
            event('response.output_item.added', item={'type': 'message', 'id': 'm3',
                  'role': 'assistant', 'status': 'in_progress', 'content': [], 'phase': 'final_answer'}),
            event('response.content_part.added', part={'type': 'output_text', 'text': '', 'annotations': []}),
            event('response.reasoning_summary_part.added', part={'type': 'summary_text', 'text': ''}),
        ]
        for placeholder in placeholders:
            with self.subTest(placeholder=placeholder):
                self.up.counts.clear()
                self.up.scripts['gpt-x'] = [{'body': CREATED + placeholder + BUSY},
                                          {'body': CREATED + OUTPUT + DONE}]
                self.assertEqual(self.post()[1], CREATED + OUTPUT + DONE)
                self.assertEqual(self.up.counts['gpt-x'], 2)

    def test_semantic_prelude_does_not_hide_content_tools_or_unknown_fields(self):
        items = [
            {'type': 'reasoning', 'summary': [], 'encrypted_content': 'opaque'},
            {'type': 'reasoning', 'summary': [{'text': 'thinking'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'text': 'hello'}]},
            {'type': 'message', 'role': 'assistant', 'content': [], 'phase': 'future_phase'},
            {'type': 'function_call', 'name': 'exec', 'arguments': ''},
            {'type': 'custom_tool_call', 'name': 'exec', 'input': ''},
            {'type': 'web_search_call', 'status': 'in_progress'},
            {'type': 'reasoning', 'summary': [], 'future_field': 'unknown'},
        ]
        for item in items:
            with self.subTest(item=item):
                self.up.counts.clear()
                body = CREATED + event('response.output_item.added', item=item) + BUSY
                self.up.scripts['gpt-x'] = [{'body': body}]
                self.assertEqual(self.post()[1], body)
                self.assertEqual(self.up.counts['gpt-x'], 1)

    def test_phase_prelude_commits_on_first_actual_delta(self):
        probe = witches.StreamProbe()
        start = CREATED + event('response.output_item.added', item={
            'type': 'message', 'role': 'assistant', 'status': 'in_progress',
            'content': [], 'phase': 'commentary'})
        for b in start:
            probe.feed(bytes([b]))
        self.assertFalse(probe.output)
        probe.feed(OUTPUT)
        self.assertTrue(probe.output)
        self.assertEqual(probe.first_output_event, 'response.output_text.delta')

    def test_exhausted_capacity_returns_last_original_error(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + BUSY}]
        self.assertEqual(self.post()[1], CREATED + BUSY)
        self.assertEqual(self.up.counts['gpt-x'], 3)
        self.assertEqual(self.attempt_logs()[-1]['retry_stop'], 'retry_limit')

    def test_never_replays_output_or_tool_events(self):
        for output in (OUTPUT, event('response.output_item.added', item={'type': 'function_call', 'name': 'exec'}),
                       event('response.reasoning_summary_text.delta', delta='thinking')):
            self.up.counts.clear()
            self.up.scripts['gpt-x'] = [{'body': CREATED + output + BUSY}]
            self.assertEqual(self.post()[1], CREATED + output + BUSY)
            self.assertEqual(self.up.counts['gpt-x'], 1)
            self.assertEqual(self.attempt_logs()[-1]['retry_stop'], 'output_started')

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
        self.assertEqual(self.attempt_logs()[-1]['retry_stop'], 'retry_window')
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
        with patch('witches.ClientWatch.wait') as sleep:
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

    def test_retry_chain_is_correlated_and_client_disconnect_is_not_replayed(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + BUSY}, {}]
        self.post()
        rows = self.attempt_logs()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['chain_id'], rows[1]['chain_id'])
        self.assertNotEqual(rows[0]['request_id'], rows[1]['request_id'])
        self.up.counts.clear()
        self.proxy.logs.clear()
        self.up.scripts['gpt-x'] = [{}]
        with patch('witches.Handler.chunk', side_effect=BrokenPipeError):
            self.assertTrue(self.post()[3])
        rows = self.attempt_logs()
        self.assertEqual(self.up.counts['gpt-x'], 1)
        self.assertEqual(rows[0]['outcome'], 'client_disconnect')
        self.assertEqual(rows[0]['stage'], 'write_stream')
        self.assertEqual(rows[0]['error_class'], 'BrokenPipeError')

    def test_header_write_failure_is_never_replayed(self):
        with patch('witches.Handler.end_headers', side_effect=BrokenPipeError):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.post()
        row = self.attempt_logs()[0]
        self.assertEqual(self.up.counts['gpt-x'], 1)
        self.assertEqual((row['outcome'], row['stage']), ('client_disconnect', 'write_headers'))

    def open_request(self, model='gpt-x'):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('POST', '/backend-api/codex/responses', json.dumps({'model': model, 'stream': True}),
                     {'Content-Type': 'application/json'})
        return conn

    def test_close_after_response_preserves_outcome_and_retry_reason(self):
        original_enter, original_end = witches.ClientWatch.__enter__, witches.Handler.end
        watches = []

        def enter(watch):
            watches.append(watch)
            return original_enter(watch)

        def end(handler):
            original_end(handler)
            # 确定性模拟：客户端已收到完整响应，在尝试日志落盘前正常关闭连接。
            watches[-1].cancelled_at = time.monotonic()
            watches[-1].cancelled.set()

        cases = [(CREATED + DONE, 'ok', None),
                 (event('response.incomplete', response={}), 'incomplete', None),
                 (BUSY, 'stream_error', 'retry_limit')]
        self.cfg('max_retries = 0\n')
        with patch.object(witches.ClientWatch, '__enter__', enter), patch.object(witches.Handler, 'end', end):
            for body, outcome, stop in cases:
                with self.subTest(outcome=outcome):
                    self.proxy.logs.clear()
                    self.up.counts.clear()
                    self.up.scripts['gpt-x'] = [{'body': body}]
                    self.assertEqual(self.post()[:2], (200, body))
                    rows = self.attempt_logs()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual((rows[0]['outcome'], rows[0].get('retry_stop')), (outcome, stop))
                    self.assertFalse(any(' -> openai_release ' in row for row in self.proxy.logs))

    def test_cancelled_capacity_retry_frees_slot_for_waiting_client(self):
        self.cfg(limit=1)
        action = {'stall': 'headers', 'started': threading.Event(), 'closed': threading.Event()}
        self.up.scripts['gpt-x'] = [{'body': BUSY}, action]
        conn = self.open_request()
        try:
            self.assertTrue(action['started'].wait(2))
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                waiting = pool.submit(self.post, 'gpt-waiting')
                gate = witches._slots['@openai']
                deadline = time.monotonic() + 2
                while gate.waiting != 1 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(gate.waiting, 1)
                conn.close()
                self.assertTrue(action['closed'].wait(1), 'upstream must close before it produces headers')
                self.assertEqual(waiting.result(timeout=1)[:2], (200, CREATED + DONE))
            rows = self.attempt_logs()
            cancelled = next(r for r in rows if r['outcome'] == 'client_disconnect')
            self.assertEqual((cancelled['stage'], cancelled['retry_stop']), ('connect', 'client_cancelled'))
            self.assertEqual(self.up.counts['gpt-x'], 2)
            self.assert_cancel_release(cancelled['chain_id'])
        finally:
            conn.close()

    def test_cancel_during_tls_handshake_releases_slot(self):
        for via_proxy in (False, True):
            with self.subTest(via_proxy=via_proxy):
                self.proxy.logs.clear()
                started, closed = threading.Event(), threading.Event()

                class StalledTLS(socketserver.BaseRequestHandler):
                    def handle(inner):
                        inner.request.settimeout(2)
                        try:
                            if via_proxy:
                                headers = bytearray()
                                while not headers.endswith(b'\r\n\r\n'):
                                    data = inner.request.recv(1)
                                    if not data:
                                        return
                                    headers.extend(data)
                                inner.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                            if inner.request.recv(65536):  # 收到 ClientHello，但不回复 TLS 握手
                                started.set()
                            while inner.request.recv(65536):
                                pass
                            closed.set()
                        except ConnectionResetError:
                            closed.set()
                        except TimeoutError:
                            pass

                server = Server(('127.0.0.1', 0), StalledTLS)
                server.errors = []
                threading.Thread(target=server.serve_forever, daemon=True).start()
                url = 'https://upstream.invalid' if via_proxy else f'https://127.0.0.1:{server.server_port}'
                env = {'https_proxy': f'http://127.0.0.1:{server.server_port}', 'no_proxy': ''} if via_proxy else {}
                try:
                    with patch.dict(witches.OPENAI, {'/backend-api/codex': url}), patch.dict(os.environ, env):
                        conn = self.open_request()
                        try:
                            self.assertTrue(started.wait(1))
                            conn.close()
                            self.assertTrue(closed.wait(.8), 'TLS handshake must abort without waiting for peer timeout')
                            row = self.attempt_logs()[0]
                            self.assertEqual(row['retry_stop'], 'client_cancelled')
                            self.assertEqual(row['connect_phase'], 'tls_handshake')
                            self.assertEqual(row['connection_route'], 'proxy' if via_proxy else 'direct')
                            self.assert_cancel_release(row['chain_id'])
                        finally:
                            conn.close()
                            self.wait_idle()
                finally:
                    server.shutdown()
                    server.server_close()

    def test_cancel_during_buffered_stream_closes_upstream(self):
        for headers in ({}, {'Connection': 'close'}):
            with self.subTest(headers=headers):
                self.up.counts.clear()
                self.proxy.logs.clear()
                action = {'stall': 'stream', 'headers': headers,
                          'started': threading.Event(), 'closed': threading.Event()}
                self.up.scripts['gpt-x'] = [action]
                conn = self.open_request()
                try:
                    self.assertTrue(action['started'].wait(2))
                    conn.close()
                    self.assertTrue(action['closed'].wait(1))
                    row = self.attempt_logs()[0]
                    self.assertEqual((row['outcome'], row['stage'], row['retry_stop']),
                                     ('client_disconnect', 'read_stream', 'client_cancelled'))
                    self.assertEqual((row['delivered_bytes'], self.up.counts['gpt-x']), (0, 1))
                    self.assert_cancel_release(row['chain_id'])
                finally:
                    conn.close()

    def test_cancel_during_retry_after_stops_before_next_attempt(self):
        self.up.scripts['gpt-x'] = [{'body': BUSY, 'headers': {'Retry-After': '5'}}, {}]
        conn = self.open_request()
        try:
            deadline = time.monotonic() + 2
            while not self.proxy.logs and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn('retry_in', self.proxy.logs[0])
            conn.close()
            self.wait_idle()
            rows = self.attempt_logs()
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.up.counts['gpt-x'], 1)
            cancelled = json.loads(next(x.split(' -> openai_cancel ', 1)[1]
                                        for x in self.proxy.logs if ' -> openai_cancel ' in x))
            self.assertEqual(cancelled['stage'], 'retry_wait')
            self.assertEqual(cancelled['chain_id'], rows[0]['chain_id'])
            self.assert_cancel_release(cancelled['chain_id'])
        finally:
            conn.close()

    def test_cancel_queued_request_does_not_call_upstream(self):
        self.cfg(limit=1)
        gate = witches.slot('@openai', {'max_concurrency': 1})
        gate.acquire()
        conn = self.open_request()
        try:
            deadline = time.monotonic() + 2
            while gate.waiting != 1 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(gate.waiting, 1)
            conn.close()
            with gate.condition:
                self.assertTrue(gate.condition.wait_for(lambda: gate.waiting == 0, timeout=1))
            self.assertEqual(gate.active, 1)
            self.assertEqual(self.up.seen, [])
        finally:
            conn.close()
            gate.release()
        self.assertFalse(any(' -> openai_release ' in row for row in self.proxy.logs))

    def test_live_client_survives_capacity_backoff_and_still_gets_original_stream(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + BUSY, 'headers': {'Retry-After': '0.3'}}, {}]
        self.assertEqual(self.post()[:2], (200, CREATED + DONE))
        rows = self.attempt_logs()
        self.assertEqual([r['outcome'] for r in rows], ['stream_error', 'ok'])
        self.assertGreaterEqual(rows[-1]['elapsed'], .3)
        self.assertEqual(self.up.counts['gpt-x'], 2)

    def test_client_watch_does_not_consume_pipelined_data(self):
        server, client = socket.socketpair()
        try:
            client.sendall(b'next request')
            with witches.ClientWatch(server) as watch:
                watch.wait(.15)
                self.assertFalse(watch.cancelled.is_set())
                self.assertEqual(server.recv(12), b'next request')
        finally:
            server.close()
            client.close()

    def test_upstream_disconnect_reports_read_stage_and_missing_terminal(self):
        self.up.scripts['gpt-x'] = [{'body': CREATED + OUTPUT, 'drop': True}]
        self.post()
        row = self.attempt_logs()[0]
        self.assertEqual((row['outcome'], row['stage'], row['terminal']), ('disconnect', 'read_stream', None))
        self.assertEqual(row['error_class'], 'IncompleteRead')
        self.assertGreater(row['delivered_bytes'], 0)

    def test_incomplete_is_distinguished_from_success(self):
        body = event('response.incomplete', response={'incomplete_details': {'reason': 'max_output_tokens'}})
        self.up.scripts['gpt-x'] = [{'body': body}]
        self.assertEqual(self.post()[1], body)
        row = self.attempt_logs()[0]
        self.assertEqual((row['outcome'], row['terminal']), ('incomplete', 'response.incomplete'))

    def test_invalid_encrypted_content_logs_shape_and_preserves_capsules(self):
        req = {'model': 'gpt-x', 'input': [
            {'type': 'reasoning', 'encrypted_content': 'PRIVATE_REASONING'},
            {'type': 'message', 'role': 'developer', 'content': [
                {'type': 'encrypted_content', 'encrypted_content': 'PRIVATE_CAPSULE'}]}]}
        raw = zstd.compress(json.dumps(req).encode())
        body = event('response.failed', response={'error': {'code': 'invalid_encrypted_content',
                     'param': 'input[1].content[0].encrypted_content', 'message': 'PRIVATE_MESSAGE'}})
        self.up.scripts['gpt-x'] = [{'body': body}]
        self.assertEqual(self.post(raw=raw, headers={'Content-Encoding': 'zstd'})[1], body)
        row = self.attempt_logs()[0]
        self.assertEqual(self.up.seen[0][1], raw)
        self.assertEqual(self.up.counts['gpt-x'], 1)
        self.assertEqual(row['encryption_shape']['content_capsules'], 1)
        self.assertEqual(row['encryption_shape']['reasoning'], 1)
        self.assertEqual(row['error_param'], 'input[1].content[0].encrypted_content')
        self.assertNotIn('PRIVATE_', json.dumps(row))

    def test_chat_collaboration_calls_explicitly_mark_plaintext_arguments(self):
        for name in ('spawn_agent', 'send_message', 'followup_task'):
            args = json.dumps({'message': '普通代理消息：保留原文。'}, ensure_ascii=False)
            chunk = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {
                'name': 'collab', 'arguments': args}}]}, 'finish_reason': 'tool_calls'}]}
            body = b'data: ' + json.dumps(chunk).encode() + b'\n\ndata: [DONE]\n\n'
            emitted = []
            witches.chat_stream(io.BytesIO(body), lambda kind, obj: emitted.append((kind, obj)),
                                'Fake/chat', {'collab': ('function', 'collaboration', name)})
            items = [obj['item'] for kind, obj in emitted if kind in (
                'response.output_item.added', 'response.output_item.done')]
            items += emitted[-1][1]['response']['output']
            self.assertEqual(len(items), 3)
            for item in items:
                self.assertEqual(item['encrypted_function_args'], [])
                self.assertEqual((item['namespace'], item['name'], item['arguments']), ('collaboration', name, args))

    def test_plaintext_marker_is_limited_to_collaboration_message_tools(self):
        for kind, ns, name in [('function_call', 'functions', 'exec_command'),
                               ('function_call', 'mcp__gh', 'send_message'),
                               ('function_call', 'collaboration', 'list_agents'),
                               ('custom_tool_call', 'collaboration', 'send_message')]:
            original = {'type': kind, 'namespace': ns, 'name': name, 'arguments': '{}'}
            self.assertEqual(witches.plaintext_collaboration(dict(original)), original)
        protected = {'type': 'function_call', 'namespace': 'collaboration', 'name': 'send_message',
                     'encrypted_function_args': ['message'], 'arguments': '{"message":"OPAQUE"}'}
        self.assertEqual(witches.plaintext_collaboration(dict(protected)), protected)

    def test_native_responses_and_gpt_encryption_markers_remain_unchanged(self):
        for marker in ('absent', None, [], ['message']):
            item = {'type': 'function_call', 'namespace': 'collaboration', 'name': 'send_message',
                    'call_id': 'call_1', 'arguments': '{"message":"OPAQUE"}'}
            if marker != 'absent':
                item['encrypted_function_args'] = marker
            body = event('response.output_item.done', item=item) + event('response.completed', response={'output': [item]})
            self.up.scripts['responses'] = [{'body': body}]
            response = self.post('Fake/responses')[1]
            parsed = [json.loads(line[6:]) for line in response.splitlines() if line.startswith(b'data: ')]
            self.assertEqual(parsed[0]['item'], item)
            self.assertEqual(parsed[-1]['response']['output'][0], item)
            self.up.scripts['gpt-x'] = [{'body': body}]
            self.assertEqual(self.post()[1], body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
