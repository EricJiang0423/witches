#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""端到端自检：假 ChatGPT 后端 + 假 Chat 上游，跑一遍 witches 的各条路径。用法：uv run --script test_witches.py"""
import base64
import json
import os
import sys
import tempfile
import threading
import urllib.request
from compression import zstd
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ['WITCHES_HOME'] = str(tmp / 'w')
os.environ['CODEX_HOME'] = str(tmp / 'c')
for k in [k for k in os.environ if k.lower().endswith('_proxy')]:
    del os.environ[k]
sys.path.insert(0, str(Path(__file__).parent))
import witches  # noqa: E402

seen = []  # 假上游收到的 (path, headers, body)


class Fake(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def reply(self, status, body, ctype='application/json', headers=()):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if ctype:  # 真实的 ChatGPT 后端 SSE 不带 Content-Type
            self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.reply(200, {'models': [{'slug': 'gpt-x'}]}, headers=[('ETag', 'E1')])

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        seen.append((self.path, dict(self.headers), body))
        if self.path.startswith('/r/'):  # 原生 Responses 上游
            return self.reply(200, b'data: {"type":"response.output_item.done","item":{"type":"reasoning","id":"rs_1","encrypted_content":"X"}}\n\n', 'text/event-stream')
        if self.path.startswith('/busy'):
            return self.reply(429, {'error': {'message': 'slow down'}})
        if self.path.endswith('/chat/completions'):
            req = json.loads(body)
            if not req['stream']:
                return self.reply(200, {'choices': [{'message': {'content': 'SUMMARY'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 1}})
            chunks = [{'choices': [{'delta': {'reasoning_content': 'think'}}]},
                      {'choices': [{'delta': {'content': 'hi'}}]},
                      {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'x', 'function': {'name': 'mcp__gh__issue', 'arguments': '{"a"'}}]}}]},
                      {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': ':1}'}}]}, 'finish_reason': 'tool_calls'}]},
                      {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 4}}}]
            return self.reply(200, ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks).encode() + b'data: [DONE]\n\n', 'text/event-stream')
        self.reply(200, b'data: {"type":"response.completed"}\n\n', None, [('X-Models-Etag', 'E1')])  # 假 ChatGPT，和真的一样不带 Content-Type


fake = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()
up = f'http://127.0.0.1:{fake.server_port}'
witches.OPENAI['/backend-api/codex'] = up + '/backend-api/codex'
(tmp / 'w').mkdir()
(tmp / 'c').mkdir()
(tmp / 'c' / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'TOKEN'}}))
(tmp / 'w' / 'config.toml').write_text(f'''
vision = ["Busy/m2", "Fake/eye"]
[route]
collab_spawn = "Fake/m1"
[providers.Fake]
base_url = "{up}/v1"
api_key = "k"
[providers.Fake.models]
"m1" = {{ context = 1000 }}
"eye" = {{ images = true, hidden = true }}
[providers.R]
base_url = "{up}/r"
wire = "responses"
[providers.R.models]
"g" = {{ }}
[providers.Busy]
base_url = "{up}/busy"
[providers.Busy.models]
"m2" = {{ fallback = ["Fake/m1"] }}
''')
srv = ThreadingHTTPServer(('127.0.0.1', 0), witches.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{srv.server_port}/backend-api/codex'


def post(body, headers=None, raw=None):
    h = {'Authorization': 'Bearer TOKEN', 'Content-Type': 'application/json', **(headers or {})}
    req = urllib.request.Request(base + '/responses', data=raw or json.dumps(body).encode(), headers=h)
    try:
        r = urllib.request.urlopen(req)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def events(sse):
    return [json.loads(ln[6:]) for ln in sse.splitlines() if ln.startswith('data: ')]


# 1. /models：ChatGPT 的列表 + 我们的
ms = json.loads(urllib.request.urlopen(base + '/models?client_version=1').read())['models']
assert [m['slug'] for m in ms] == ['gpt-x', 'Fake/m1', 'Fake/eye', 'R/g', 'Busy/m2'], ms
assert ms[1]['input_modalities'] == ['text', 'image']  # 配了 vision，纯文本模型也声明收图
assert ms[1]['visibility'] == 'list' and ms[2]['visibility'] == 'hide'  # hidden 的看图模型不进选择器
assert '`m1` model' in ms[1]['base_instructions'] and ms[1]['base_instructions'] != ms[2]['base_instructions']

# 2. GPT 请求：zstd 压缩的原始字节原样转发
raw = zstd.compress(json.dumps({'model': 'gpt-x', 'input': [{'type': 'message', 'role': 'user', 'content': 'q'}]}).encode())
seen.clear()
assert post(None, {'Content-Encoding': 'zstd'}, raw)[0] == 200
assert seen[0][2] == raw and seen[0][1]['Content-Encoding'] == 'zstd'

# 3. GPT 请求里有我们的摘要和第三方推理：还原成明文、丢掉推理
cmp_item = {'type': 'compaction', 'encrypted_content': witches.CMP + base64.b64encode('OLD'.encode()).decode()}
seen.clear()
post({'model': 'gpt-x', 'input': [cmp_item, {'type': 'reasoning', 'summary': []},
                                  {'type': 'message', 'role': 'assistant', 'id': 'msg_wch123', 'content': 'a'}]})
sent = json.loads(seen[0][2])['input']
assert len(sent) == 2 and 'OLD' in sent[0]['content'][0]['text'] and 'id' not in sent[1], sent

# 4. 第三方模型：Responses -> Chat -> Responses 流
seen.clear()
status, sse = post({'model': 'Fake/m1', 'instructions': 'SYS', 'reasoning': {'effort': 'ultra'}, 'input': [
    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q'}]},
    {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'r0'}]},
    {'type': 'function_call', 'call_id': 'c1', 'name': 'exec', 'arguments': '{}'},
    {'type': 'function_call', 'call_id': 'c2', 'name': 'exec', 'arguments': '{}'},
    {'type': 'function_call_output', 'call_id': 'c1', 'output': 'out1'},
    {'type': 'message', 'role': 'user', 'content': 'next'}],
    'tools': [{'type': 'function', 'name': 'exec', 'parameters': {}},
              {'type': 'custom', 'name': 'apply_patch', 'format': {'syntax': 'lark', 'definition': 'G'}},
              {'type': 'namespace', 'name': 'mcp__gh', 'tools': [{'type': 'function', 'name': 'issue'}]}]})
assert status == 200, sse
chat = json.loads(seen[0][2])
roles = [m['role'] for m in chat['messages']]
assert roles == ['system', 'user', 'assistant', 'tool', 'tool', 'user'], roles  # c2 没结果，补了占位
assert chat['messages'][2]['reasoning_content'] == 'r0' and chat['reasoning_effort'] == 'high'
assert [t['function']['name'] for t in chat['tools']] == ['exec', 'apply_patch', 'mcp__gh__issue']
assert 'G' in chat['tools'][1]['function']['description']
ev = events(sse)
kinds = [e['type'] for e in ev]
assert kinds[0] == 'response.created' and kinds[-1] == 'response.completed', kinds
done = ev[-1]['response']
assert [o['type'] for o in done['output']] == ['reasoning', 'message', 'function_call'], done
call = done['output'][2]
assert call['name'] == 'issue' and call['namespace'] == 'mcp__gh' and call['arguments'] == '{"a":1}'
assert done['usage']['input_tokens_details']['cached_tokens'] == 4

# 5. 别人的 token 用不了第三方模型
assert post({'model': 'Fake/m1', 'input': []}, {'Authorization': 'Bearer nope'})[0] == 403

# 6. 压缩：compaction_trigger -> 我们的 compaction 条目
status, sse = post({'model': 'Fake/m1', 'input': [{'type': 'message', 'role': 'user', 'content': 'q'}, {'type': 'compaction_trigger'}]})
item = events(sse)[-1]['response']['output'][0]
assert item['type'] == 'compaction' and witches.our_summary(item) == 'SUMMARY', sse

# 7. 429 自动换备选模型
seen.clear()
status, sse = post({'model': 'Busy/m2', 'input': [{'type': 'message', 'role': 'user', 'content': 'q'}]})
assert status == 200 and [s[0] for s in seen] == ['/busy/chat/completions', '/v1/chat/completions'], seen

# 8. 子代理改派：GPT 模型 + x-openai-subagent: collab_spawn -> Fake/m1（Responses Lite 的工具被提出来）
seen.clear()
post({'model': 'gpt-x', 'input': [{'type': 'additional_tools', 'tools': [
    {'type': 'namespace', 'name': 'functions', 'tools': [{'type': 'function', 'name': 'exec'}]}]},
    {'type': 'message', 'role': 'user', 'content': 'q'}]}, {'x-openai-subagent': 'collab_spawn'})
chat = json.loads(seen[0][2])
assert seen[0][0] == '/v1/chat/completions' and chat['tools'][0]['function']['name'] == 'exec', chat

# 9. 看图代理：纯文本模型收到图片 -> vision 列表里 Busy 429，回退到 Fake/eye 描述；结果落盘，同一张图第二次不再看
img = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA'}]}
for _ in range(2):
    seen.clear()
    post({'model': 'Fake/m1', 'input': [img]})
paths = [(s[0], json.loads(s[2])['stream']) for s in seen]
assert paths == [('/v1/chat/completions', True)], paths  # 第二次只有正式请求，没有看图请求
assert 'SUMMARY' in json.loads(seen[0][2])['messages'][0]['content'], seen[0][2]
assert len(list((tmp / 'w' / 'vision').iterdir())) == 1

# 10. 原生 Responses 上游：条目 id 打标记、加密推理去掉
status, sse = post({'model': 'R/g', 'input': []})
item = events(sse)[0]['item']
assert item['id'] == 'rs_wch_1' and 'encrypted_content' not in item, item

# 11. 常驻服务的解释器不能是 uv 的临时环境（本测试本身就跑在 uv run --script 的临时环境里）
py = witches.interpreter()
assert sys.prefix == sys.base_prefix or not Path(py).resolve().is_relative_to(Path(sys.prefix).resolve()), py

# 12. 切换模型后第一次回答前插一句提示；下一轮不再插，历史里的提示也不发给模型；切回 GPT 时插在原始流前面
switch = {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': '<model_switch>\nYou are ...'}]}
q = {'type': 'message', 'role': 'user', 'content': 'q'}
status, sse = post({'model': 'Fake/m1', 'stream': True, 'input': [q, switch, q]})
out = events(sse)[-1]['response']['output']
assert out[0]['content'][0]['text'] == '✓ 已切换到 Fake/m1' and out[1]['type'] == 'reasoning', out
seen.clear()
status, sse = post({'model': 'Fake/m1', 'input': [q, switch, q, out[0], {'type': 'message', 'role': 'assistant', 'content': 'a'}, q]})
assert '已切换到' not in sse and '已切换到' not in seen[0][2].decode(), seen[0][2]
status, sse = post({'model': 'gpt-x', 'stream': True, 'input': [q, switch, q]})
assert sse.index('✓ 已切换到 gpt-x') < sse.index('response.completed'), sse

# 13. 推理档位：按模型名套官方预设，每个模型都有 ultra；不支持的档位取最近一档、距离相同往高取
assert [e['effort'] for e in ms[1]['supported_reasoning_levels']] == ['low', 'medium', 'high', 'ultra']
assert ms[1]['multi_agent_reasoning_effort'] == 'high' and ms[1]['multi_agent_version'] == 'v2'
glm, ds, mimo = ({'upstream': u} for u in ('glm-5.3', 'deepseek-v4.1-flash', 'mimo-v2.6-pro'))
assert [witches.effort(glm, e) for e in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'none')] == \
    ['low', 'high', 'high', 'max', 'max', 'max', 'low']
assert witches.effort(mimo, 'max') == 'high' and witches.effort(mimo, 'ultra') == 'high'
assert witches.effort(ds, 'none') == {'thinking': {'type': 'disabled'}}
assert witches.effort({'upstream': 'glm-5.3', 'efforts': ['high']}, 'low') == 'high'  # 配置覆盖预设

# 14. 模型列表 ETag 拼上我们目录的哈希，三处一致；改了配置哈希跟着变，Codex 才会重拉列表
r = urllib.request.urlopen(base + '/models?client_version=1')
tag = r.headers['ETag']
assert tag.startswith('E1-w'), tag
req = urllib.request.Request(base + '/responses', data=json.dumps({'model': 'gpt-x', 'input': []}).encode(),
                             headers={'Authorization': 'Bearer TOKEN', 'Content-Type': 'application/json'})
assert urllib.request.urlopen(req).headers['X-Models-Etag'] == tag
req = urllib.request.Request(base + '/responses', data=json.dumps({'model': 'Fake/m1', 'input': [q]}).encode(),
                             headers={'Authorization': 'Bearer TOKEN', 'Content-Type': 'application/json'})
assert urllib.request.urlopen(req).headers['X-Models-Etag'] == tag
cfgfile = tmp / 'w' / 'config.toml'
cfgfile.write_text(cfgfile.read_text().replace('"m1" = {{ context = 1000 }}'.replace('{{', '{').replace('}}', '}'), '"m1" = { context = 2000 }'))
assert urllib.request.urlopen(base + '/models?client_version=1').headers['ETag'] != tag

# 15. uv 的 Python 用只带小版本号的稳定链接
d = tmp / 'uvpy'
(d / 'cpython-3.14.7-linux-x86_64-gnu' / 'bin').mkdir(parents=True)
(d / 'cpython-3.14.7-linux-x86_64-gnu' / 'bin' / 'python3.14').touch()
(d / 'cpython-3.14-linux-x86_64-gnu').symlink_to(d / 'cpython-3.14.7-linux-x86_64-gnu')
assert witches.stable(f'{d}/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14') == f'{d}/cpython-3.14-linux-x86_64-gnu/bin/python3.14'
assert witches.stable('/opt/homebrew/bin/python3.14') == '/opt/homebrew/bin/python3.14'

print('all checks passed')
