#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""witches：给 Codex 加第三方模型的单文件代理（零依赖，Python 3.14+）。

Codex 内置的 openai provider 经 `openai_base_url` 指向本机 witches：
  * GPT 模型：原样转发到 ChatGPT 后端（登录态、旧线程、请求目标都不变，不存状态）
  * `provider/model`：转发到 ~/.witches/config.toml 里配置的上游（Chat 或 Responses 接口）
  * [route]：按 Codex 的 x-openai-subagent 头，把子代理/审查请求改派给第三方模型
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
import uuid
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get('WITCHES_HOME') or Path.home() / '.witches')
CODEX_HOME = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
OPENAI = {'/backend-api/codex': 'https://chatgpt.com/backend-api/codex', '/v1': 'https://api.openai.com/v1'}
DEFAULT_PORT = 10777
BIN = Path.home() / '.local/bin/witches'
MARK = '# witches'
CMP = 'witches1:'  # 我们产出的 compaction：encrypted_content = CMP + base64(摘要)
WID = '_wch'  # 我们生成的条目 id 都带这个标记；OpenAI 不认陌生 id（会当成要查已存条目而 400），切回 GPT 时去掉
HOP = {'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'upgrade', 'te', 'trailer',
       'content-length', 'host'}
EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}
CAPACITY_CODES = {'server_overloaded', 'server_is_overloaded', 'model_overloaded', 'overloaded_error',
                  'rate_limit_exceeded', 'slow_down'}
QUOTA_CODES = {'insufficient_quota', 'quota_exhausted', 'billing_hard_limit_reached',
               'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'}
STREAM_ERRORS = (OSError, http.client.HTTPException)
PRELUDE_LIMIT = 262144
OPENAI_DEFAULTS = {'max_concurrency': 3, 'queue_timeout': 30, 'max_retries': 2, 'retry_window': 60}
PROMPT = Path(__file__).resolve().with_name('prompt.md').read_text()  # Codex 的基础提示词（openai/codex，Apache-2.0）

# Codex 自己的压缩提示词与摘要前缀（openai/codex，Apache-2.0）
COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""
SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"""


# ---------------------------------------------------------------- 配置

def config():
    try:
        return tomllib.loads((HOME / 'config.toml').read_text())
    except FileNotFoundError:
        return {}


def lookup(cfg, slug):
    """'gateway/glm-5.3' -> (provider, model)；不是我们配置的模型返回 None。"""
    name, _, mid = slug.partition('/')
    p = cfg.get('providers', {}).get(name)
    m = p and mid and p.get('models', {}).get(mid)
    if m is None or not p:
        return None
    return p, {'upstream': mid, **m}


def catalog(cfg):
    """我们的模型，按 Codex /models 的条目格式（参考 magpie 的最小字段集，不克隆 GPT 条目）。"""
    out = []
    for pname, p in cfg.get('providers', {}).items():
        for mid, m in p.get('models', {}).items():
            lv = levels({'upstream': mid, **m})
            top = next(reversed(lv), None)
            out.append({
                'slug': f'{pname}/{mid}', 'display_name': m.get('name', f'{pname}/{mid}'),
                'description': f'{pname}/{mid} via witches', 'base_instructions': identity(pname, mid) + PROMPT,
                'default_reasoning_level': m.get('default_effort', 'high' if 'high' in lv else top),
                # 每个模型都加 Codex 原生的 ultra：主代理按最高档推理，并由 harness 主动拉起子代理群（需要 multi_agent v2）
                'supported_reasoning_levels': [{'effort': k, 'description': f'{k} -> {mid} {v if isinstance(v, str) else json.dumps(v)}'}
                                               for k, v in lv.items()] + ([{'effort': 'ultra', 'description':
                                               f'Codex Ultra: {top} + proactive subagents'}] if top else []),
                'multi_agent_reasoning_effort': top, 'multi_agent_version': 'v2',
                'default_reasoning_summary': 'auto',  # 让 Codex 请求并显示推理摘要（GPT-6 的官方条目是 none）
                # hidden：只给看图/fallback 在后台用，不出现在选择器里（条目保留，用过它的旧线程不受影响）
                'shell_type': 'unified_exec', 'visibility': 'hide' if m.get('hidden') else 'list', 'supported_in_api': True,
                'priority': 100 + len(out), 'additional_speed_tiers': [], 'service_tiers': [],
                'availability_nux': None, 'upgrade': None, 'support_verbosity': False, 'default_verbosity': None,
                'apply_patch_tool_type': 'freeform', 'truncation_policy': {'mode': 'tokens', 'limit': 10000},
                'supports_parallel_tool_calls': True, 'context_window': m.get('context', 128000),
                'experimental_supported_tools': [], 'supports_search_tool': False,
                'input_modalities': ['text', 'image'] if m.get('images') or cfg.get('vision') else ['text'],
            })
    return out


_etag = {'upstream': None}  # 最近一次见到的 ChatGPT 模型列表 ETag


def with_tag(etag):
    """模型列表 ETag 拼上我们目录的哈希：Codex 每次收到回答都拿 X-Models-Etag 和缓存比，一致就只续期不重拉，
    不拼的话 witches 改了配置/模型，只要还在用 GPT 就永远传不到 Codex。"""
    if not etag:
        return None
    return f"{etag}-w{hashlib.sha256(json.dumps(catalog(config()), sort_keys=True).encode()).hexdigest()[:10]}"


def identity(pname, mid):
    """每个模型的系统提示开头写明身份。线程中途换模型时 Codex 会把新模型的系统提示作为 <model_switch> 插进历史，
    不写的话模型会顺着历史里 GPT 的说明自称 GPT；写了，witches 模型之间互切时提示也不同，Codex 才会插入切换说明。"""
    return (f'You are Codex, a coding agent powered by the `{mid}` model (provider `{pname}`, routed through witches). '
            'When asked which model you are, answer with this model name. Earlier messages in this conversation may '
            'describe a different model because the user switched models; this instruction takes precedence.\n\n')


# 各家官方文档里的思考档位（2026-10 核对，并经 PANTHEON/直连实测）：Codex 档位 -> 上游取值。
# 取值是字符串就作为 reasoning_effort 发出；是表就合并进请求体（比如关闭思考）。配置里的 efforts 可以覆盖。
PRESETS = [
    # Z.ai：GLM-5.3 永远在思考，只收 low/high/max，其他值报错  https://docs.z.ai/guides/capabilities/thinking
    (r'glm-5', {'low': 'low', 'high': 'high', 'max': 'max'}),
    # Kimi 平台：K3 永远在思考，low/high/max，默认 max  https://platform.kimi.ai/docs/guide/use-thinking-effort
    (r'kimi-k3', {'low': 'low', 'high': 'high', 'max': 'max'}),
    # DeepSeek：low/high/max，默认 high；thinking.type=disabled 关闭思考  https://api-docs.deepseek.com/guides/thinking_mode/
    (r'deepseek-v4', {'none': {'thinking': {'type': 'disabled'}}, 'low': 'low', 'high': 'high', 'max': 'max'}),
    # MiMo：直连只收 low/medium/high，xhigh/max 实测 400
    (r'mimo-v2', {'low': 'low', 'medium': 'medium', 'high': 'high'}),
]


def levels(m):
    """模型的推理档位：Codex 档位 -> 上游取值，按 Codex 的顺序排。配置 efforts（列表或表）优先，其次按模型名套预设。"""
    e = m.get('efforts')
    if e is None:
        e = next((v for pat, v in PRESETS if re.search(pat, m['upstream'])), ['low', 'medium', 'high'])
    e = {k: k for k in e} if isinstance(e, list) else e
    return dict(sorted(e.items(), key=lambda kv: EFFORTS.index(kv[0])))


def effort(m, want):
    """Codex 发来的推理强度 -> 上游取值（字符串或要合并进请求体的表）；None 表示不传。
    ultra 用最高档（Codex 新版会先自己换成 multi_agent_reasoning_effort）；不支持的档位取最近的一档，距离相同往高取。"""
    lv = levels(m)
    if not want or not lv:
        return None
    if want not in lv:
        rank = len(EFFORTS) if want == 'ultra' else EFFORTS.index(want) if want in EFFORTS else EFFORTS.index('medium')
        want = min(lv, key=lambda k: (abs(EFFORTS.index(k) - rank), -EFFORTS.index(k)))
    return lv[want] or None


# ---------------------------------------------------------------- 上游连接

_ctx = ssl.create_default_context()


class Response:
    """HTTPResponse 和创建它的连接一起关闭，避免 keep-alive socket 留给 GC。"""
    def __init__(self, response, connection):
        self.response, self.connection = response, connection

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __iter__(self):
        return iter(self.response)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            self.response.close()
        finally:
            self.connection.close()


def private_ip(host):
    try:
        ip = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')


def fetch(method, url, body=None, headers=None, timeout=900):
    """http.client 发请求；尊重 http(s)_proxy/no_proxy，私网 IP（含 Tailscale 100.64/10）总是直连。"""
    u = urllib.parse.urlsplit(url)
    https = u.scheme == 'https'
    port = u.port or (443 if https else 80)
    path = (u.path or '/') + (f'?{u.query}' if u.query else '')
    proxy = None
    if not private_ip(u.hostname) and not urllib.request.proxy_bypass_environment(u.hostname):
        proxy = urllib.request.getproxies_environment().get(u.scheme)
        if proxy and not proxy.startswith('http'):
            proxy = None  # ponytail: 只支持 http 代理，socks 需要时再加
    if proxy:
        pu = urllib.parse.urlsplit(proxy if '://' in proxy else 'http://' + proxy)
        auth = {}
        if pu.username:
            cred = base64.b64encode(f'{urllib.parse.unquote(pu.username)}:{urllib.parse.unquote(pu.password or "")}'.encode())
            auth = {'Proxy-Authorization': 'Basic ' + cred.decode()}
        if https:
            conn = http.client.HTTPSConnection(pu.hostname, pu.port or 80, timeout=timeout, context=_ctx)
            conn.set_tunnel(u.hostname, port, headers=auth)
        else:
            conn = http.client.HTTPConnection(pu.hostname, pu.port or 80, timeout=timeout)
            path, headers = url, {**(headers or {}), **auth}
    elif https:
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=_ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        return Response(conn.getresponse(), conn)
    except BaseException:
        conn.close()
        raise


_slots = {}
_slots_lock = threading.Lock()


class Gate:
    """可热更新的进程内并发闸门；缩小上限不影响已在运行的请求。"""
    def __init__(self):
        self.condition = threading.Condition()
        self.active, self.limit, self.waiting = 0, 0, 0

    def resize(self, limit):
        with self.condition:
            self.limit = max(0, int(limit))
            self.condition.notify_all()

    def acquire(self, timeout=None):
        with self.condition:
            self.waiting += 1
            try:
                if not self.condition.wait_for(lambda: not self.limit or self.active < self.limit, timeout):
                    return False
                self.active += 1
                return True
            finally:
                self.waiting -= 1

    def release(self):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {'active': self.active, 'waiting': self.waiting, 'runtime_limit': self.limit}


def slot(pname, p):
    """每个 provider 的并发闸门（max_concurrency），突发的子代理请求排队而不是一起打到上游。"""
    with _slots_lock:
        gate = _slots.setdefault(pname, Gate())
        gate.resize(p.get('max_concurrency', 0))
        return gate


def openai_options(cfg):
    return {**OPENAI_DEFAULTS, **cfg.get('openai', {})}


def health():
    opts = openai_options(config())
    with _slots_lock:
        gate = _slots.get('@openai')
    state = gate.snapshot() if gate else {'active': 0, 'waiting': 0, 'runtime_limit': opts['max_concurrency']}
    return {'ok': True, 'pid': os.getpid(), 'openai': {**{k: opts[k] for k in OPENAI_DEFAULTS}, **state}}


def retry_after(value):
    """Retry-After 支持秒数和 HTTP 日期；不会缩短服务端要求的等待时间。"""
    if value is None:
        return None
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def error_fields(event):
    response = event.get('response')
    err = event.get('error') or (response.get('error') if isinstance(response, dict) else None) or {}
    return err if isinstance(err, dict) else {'code': 'upstream_error'}


def transient_error(err):
    codes = {str(err.get('code', '')), str(err.get('type', ''))}
    return not codes & QUOTA_CODES and bool(codes & CAPACITY_CODES)


class StreamProbe:
    """旁路检查 SSE，原始字节不重编码。只缓冲已知的启动事件，未知事件保守地提交。"""
    def __init__(self):
        self.buffer = b''
        self.output = self.terminal = self.opaque = False
        self.failure = None
        self.terminal_kind = None

    def feed(self, data):
        self.buffer += data
        while match := re.search(rb'\r\n\r\n|\r\n\n|\n\r\n|\n\n|\r\r', self.buffer):
            frame, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            if self.opaque or len(frame) > PRELUDE_LIMIT:
                self.opaque = False
                self.output = True
                continue
            lines = re.split(rb'\r\n|\r|\n', frame)
            values = [ln[5:].lstrip(b' ') for ln in lines if ln.startswith(b'data:')]
            if not values:  # 注释/心跳
                continue
            try:
                event = json.loads(b'\n'.join(values))
                if not isinstance(event, dict):
                    raise ValueError('non-object SSE data')
            except ValueError:
                self.output = True
                continue
            kind = event.get('type') or next((ln[6:].strip().decode('utf-8', 'replace') for ln in lines
                                             if ln.startswith(b'event:')), '')
            if kind in ('response.failed', 'error') or event.get('error'):
                self.failure = error_fields(event)
                self.terminal = True
                self.terminal_kind = kind or 'error'
            elif kind in ('response.completed', 'response.incomplete'):
                self.terminal = self.output = True
                self.terminal_kind = kind
            elif kind not in ('response.created', 'response.in_progress', 'response.queued', 'ping') or \
                    (isinstance(event.get('response'), dict) and event['response'].get('output')):
                self.output = True
        if len(self.buffer) > PRELUDE_LIMIT:
            self.opaque = self.output = True
            self.buffer = b''


def encryption_shape(body, headers):
    """仅统计结构，不记录密文、明文、消息长度或内容。用于定位无效加密字段。"""
    try:
        encoding = next((v for k, v in headers.items() if k.lower() == 'content-encoding'), '')
        req = json.loads(zstd.decompress(body) if encoding == 'zstd' else body)
        items = req.get('input')
        if not isinstance(items, list):
            return {}
        counts = {'reasoning': 0, 'compaction': 0, 'witches_compaction': 0, 'content_capsules': 0}

        def visit(value):
            if isinstance(value, dict):
                if value.get('type') == 'encrypted_content':
                    counts['content_capsules'] += 1
                for child in value.values():
                    if isinstance(child, (list, dict)):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for it in items:
            if isinstance(it, dict):
                kind = it.get('type')
                if it.get('encrypted_content'):
                    if kind == 'reasoning':
                        counts['reasoning'] += 1
                    elif kind in ('compaction', 'compaction_summary', 'context_compaction'):
                        counts['compaction'] += 1
                        if str(it['encrypted_content']).startswith(CMP):
                            counts['witches_compaction'] += 1
                visit(it)
        return counts
    except (ValueError, TypeError, AttributeError, zstd.ZstdError):
        return {'unavailable': True}


# ---------------------------------------------------------------- 请求整理

def wid(prefix):
    return f'{prefix}{WID}{uuid.uuid4().hex}'


def text_of(content):
    if isinstance(content, str):
        return content
    return '\n'.join(c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('text'))


def user_msg(text):
    return {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}


def our_summary(it):
    enc = it.get('encrypted_content') or ''
    if it.get('type') in ('compaction', 'compaction_summary') and enc.startswith(CMP):
        return base64.b64decode(enc[len(CMP):]).decode()
    return None


def lift_tools(req):
    """Responses Lite（gpt-6 等）把工具放在 additional_tools 输入项、functions 命名空间里；提回 tools。"""
    items = req.get('input')
    if not isinstance(items, list) or not any(i.get('type') == 'additional_tools' for i in items):
        return req
    tools = list(req.get('tools') or [])
    for it in items:
        for t in it.get('tools', []) if it.get('type') == 'additional_tools' else []:
            tools += t.get('tools', []) if t.get('type') == 'namespace' and t.get('name') == 'functions' else [t]
    return {**req, 'tools': tools, 'input': [i for i in items if i.get('type') != 'additional_tools']}


NOTE = '✓ 已切换到 '  # 切换模型后第一次回答前插入的一句提示；只给人看，发给任何模型前都会删掉


def note_item(text):
    return {'type': 'message', 'id': wid('msg'), 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


def is_note(it):
    return it.get('type') == 'message' and it.get('role') == 'assistant' and text_of(it.get('content')).startswith(NOTE)


def just_switched(items):
    """刚切换模型后的第一次调用：Codex 插入的最近一条 <model_switch> 之后，还没有任何模型输出。"""
    idx = [i for i, it in enumerate(items) if it.get('type') == 'message' and it.get('role') == 'developer'
           and text_of(it.get('content')).lstrip().startswith('<model_switch>')]
    return bool(idx) and not any(it.get('type') in ('function_call', 'custom_tool_call', 'reasoning')
                                 or it.get('type') == 'message' and it.get('role') == 'assistant' for it in items[idx[-1] + 1:])


def for_openai(items):
    """切回 GPT 时清掉 witches 留下的东西：我们的摘要还原成明文，第三方推理丢弃，id 改成 OpenAI 认的前缀。"""
    out = []
    for it in items:
        s = our_summary(it)
        if s is not None:
            out.append(user_msg(SUMMARY_PREFIX + '\n' + s))
            continue
        t = it.get('type')
        if t == 'reasoning' and not it.get('encrypted_content'):
            continue
        if WID in str(it.get('id', '')):
            it = {k: v for k, v in it.items() if k != 'id'}
        out.append(it)
    return out


def for_third_party(items):
    """发给第三方 Responses 上游：还原我们的摘要；丢掉别家加密的推理/压缩条目（换个后端解不开，只会 400）。"""
    out = []
    for it in items:
        s = our_summary(it)
        if s is not None:
            out.append(user_msg(SUMMARY_PREFIX + '\n' + s))
        elif it.get('type') not in ('reasoning', 'compaction', 'compaction_summary', 'context_compaction',
                                    'configuration_update'):
            out.append(it)
    return out


def flat_name(ns, name):
    flat = f'{ns}__{name}' if ns else name
    return flat if len(flat) <= 64 else flat[:55] + '_' + hashlib.md5(flat.encode()).hexdigest()[:8]


def to_chat(req, p, m):
    """Responses 请求 -> Chat Completions 请求。返回 (body, names)，names: 扁平工具名 -> (kind, namespace, name)。"""
    system = [req['instructions']] if req.get('instructions') else []
    msgs, names, tools = [], {}, []
    pending = {'reasoning': '', 'calls': [], 'deferred': []}
    replied = False

    def flush():  # 关闭一轮工具调用：没结果的调用补占位（Chat 要求 tool_calls 必须成对），再补延后的图片
        for cid in pending['calls']:
            msgs.append({'role': 'tool', 'tool_call_id': cid, 'content': '[No result: the call was interrupted.]'})
        msgs.extend(pending['deferred'])
        pending['calls'], pending['deferred'] = [], []

    def assistant():
        if msgs and msgs[-1]['role'] == 'assistant':
            a = msgs[-1]
        else:
            flush()
            a = {'role': 'assistant', 'content': ''}
            msgs.append(a)
        if pending['reasoning']:  # kimi/deepseek 的思考模式要求把 reasoning_content 带回去
            a['reasoning_content'] = a.get('reasoning_content', '') + pending['reasoning']
            pending['reasoning'] = ''
        return a

    def call(cid, name, args):
        a = assistant()
        a.setdefault('tool_calls', []).append({'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': args}})
        pending['calls'].append(cid)

    def parts(content):
        out = []
        for c in [{'type': 'input_text', 'text': content}] if isinstance(content, str) else content or []:
            if c.get('type') in ('input_text', 'output_text', 'text') and c.get('text'):
                out.append({'type': 'text', 'text': c['text']})
            elif c.get('type') == 'input_image' and c.get('image_url'):
                out.append({'type': 'image_url', 'image_url': {'url': c['image_url']}})
        return out

    def user(content):
        ps = parts(content)
        if not ps:
            return
        flush()
        allt = all(x['type'] == 'text' for x in ps)
        msgs.append({'role': 'user', 'content': '\n'.join(x['text'] for x in ps) if allt else ps})

    for it in req.get('input') or []:
        t = it.get('type') or ('message' if 'role' in it else '')
        s = our_summary(it)
        if s is not None:
            user(SUMMARY_PREFIX + '\n' + s)
        elif t == 'message' and it.get('role') in ('system', 'developer'):
            txt = text_of(it.get('content'))
            if txt and not replied:
                system.append(txt)  # 首轮前的 developer 消息并进 system；之后的保持原位，避免改动前缀破坏缓存
            elif txt:
                user(f'<system-reminder>\n{txt}\n</system-reminder>')
        elif t == 'message' and it.get('role') == 'assistant':
            replied = True
            a = assistant()
            a['content'] += text_of(it.get('content'))
        elif t in ('message', 'agent_message'):
            user(it.get('content'))
        elif t == 'reasoning':
            replied = True
            pending['reasoning'] += ''.join(x.get('text', '') for x in it.get('content') or [] if
                                            x.get('type') == 'reasoning_text') or \
                ''.join(x.get('text', '') for x in it.get('summary') or [])
        elif t == 'function_call':
            replied = True
            call(it.get('call_id') or it.get('id'), flat_name(it.get('namespace'), it['name']), it.get('arguments') or '{}')
        elif t == 'custom_tool_call':
            replied = True
            call(it.get('call_id') or it.get('id'), flat_name(it.get('namespace'), it['name']),
                 json.dumps({'input': it.get('input', '')}))
        elif t in ('function_call_output', 'custom_tool_call_output'):
            out = it.get('output')
            cid = it.get('call_id')
            if isinstance(out, dict):  # 旧格式 {content, success}
                out = out.get('content')
            txt = text_of(out) if not isinstance(out, str) else out
            imgs = [x for x in parts(out) if x['type'] == 'image_url'] if isinstance(out, list) else []
            if cid in pending['calls']:
                pending['calls'].remove(cid)
                msgs.append({'role': 'tool', 'tool_call_id': cid, 'content': txt or '(empty)'})
                if imgs:
                    pending['deferred'].append({'role': 'user', 'content': imgs})
            else:  # 找不到对应调用的结果（跨线程通知等）当作用户消息
                user([{'type': 'input_text', 'text': txt or '(empty tool result)'}])
    flush()

    def add_tool(t, ns):
        flat = flat_name(ns, t['name'])
        names[flat] = (t['type'], ns, t['name'])
        desc = t.get('description', '')
        if t['type'] == 'function':
            params = t.get('parameters') or {'type': 'object', 'properties': {}}
        else:  # custom/freeform 工具（apply_patch 等）包成只有一个 input 字符串参数的函数
            fmt = t.get('format') or {}
            if fmt.get('definition'):
                desc += f"\n\nThe `input` must follow this {fmt.get('syntax', '')} grammar:\n{fmt['definition']}"
            params = {'type': 'object', 'properties': {'input': {'type': 'string'}}, 'required': ['input']}
        tools.append({'type': 'function', 'function': {'name': flat, 'description': desc, 'parameters': params}})

    for t in req.get('tools') or []:
        if t.get('type') == 'namespace':
            for sub in t.get('tools', []):
                if sub.get('type') in ('function', 'custom'):
                    add_tool(sub, None if t.get('name') == 'functions' else t['name'])
        elif t.get('type') in ('function', 'custom'):
            add_tool(t, None)

    body = {'model': m['upstream'], 'messages': ([{'role': 'system', 'content': '\n\n'.join(system)}] if system else []) + msgs,
            'stream': True, 'stream_options': {'include_usage': True}}
    if tools:
        body['tools'] = tools
        tc = req.get('tool_choice', 'auto')
        if isinstance(tc, dict):
            tc = {'type': 'function', 'function': {'name': flat_name(tc.get('namespace'), tc['name'])}} \
                if tc.get('type') == 'function' and tc.get('name') else 'auto'
        body['tool_choice'] = tc
        if 'parallel_tool_calls' in req:
            body['parallel_tool_calls'] = req['parallel_tool_calls']
    e = effort(m, (req.get('reasoning') or {}).get('effort'))
    if isinstance(e, dict):
        body.update(e)
    elif e:
        body['reasoning_effort'] = e
    if req.get('max_output_tokens'):
        body['max_tokens'] = req['max_output_tokens']
    for k in ('temperature', 'top_p'):
        if k in req:
            body[k] = req[k]
    fmt = (req.get('text') or {}).get('format') or {}
    if fmt.get('type') == 'json_schema':
        body['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': fmt.get('name', 'output'), 'schema': fmt.get('schema', {}), 'strict': bool(fmt.get('strict'))}}
    if p.get('prompt_cache_key') and req.get('prompt_cache_key'):
        body['prompt_cache_key'] = req['prompt_cache_key']
    body.update(m.get('extra', {}))
    return body, names


def to_responses(req, m):
    """发给第三方 Responses 上游的请求体。"""
    body = {k: v for k, v in req.items() if k not in ('service_tier', 'include')}
    body['model'] = m['upstream']
    body['input'] = for_third_party(req.get('input') or [])
    e = effort(m, (req.get('reasoning') or {}).get('effort'))
    if isinstance(e, str):
        body['reasoning'] = {**(req.get('reasoning') or {}), 'effort': e}
    else:
        body.pop('reasoning', None)
        body.update(e or {})
    body.update(m.get('extra', {}))
    return body


def flatten(items, limit):
    """把对话压成纯文本（只用于压缩）：不带 id/加密内容/工具结构，任何上游都收。"""
    lines = []
    for it in items:
        t = it.get('type') or 'message'
        s = our_summary(it)
        if s is not None:
            role, txt = 'summary', s
        elif t == 'message':
            role, txt = it.get('role', 'user'), text_of(it.get('content'))
        elif t in ('function_call', 'custom_tool_call'):
            role, txt = 'tool call', f"{it.get('name')} {it.get('arguments') or it.get('input') or ''}"
        elif t in ('function_call_output', 'custom_tool_call_output'):
            out = it.get('output')
            role, txt = 'tool output', out if isinstance(out, str) else text_of(out if isinstance(out, list) else [])
            if len(txt) > 4000:
                txt = txt[:2000] + '\n…\n' + txt[-2000:]
        elif t == 'reasoning':
            role, txt = 'reasoning', ''.join(x.get('text', '') for x in it.get('summary') or [])
        else:
            continue
        if txt.strip():
            lines.append(f'[{role}]\n{txt}')
    s = '\n\n'.join(lines)
    return s if len(s) <= limit else '…\n' + s[-limit:]


# ---------------------------------------------------------------- Chat 流 -> Responses 流

def plaintext_collaboration(item):
    """显式声明普通协作参数为明文，避免 Codex 缺省把代理消息当成密文。
    参见 openai/codex rust-v0.162.1 core/src/tools/router.rs ToolCall::direct_source。
    保留已有的密文参数标记，不检查或重写消息内容。
    """
    if item.get('type') == 'function_call' and item.get('namespace') == 'collaboration' and \
            item.get('name') in ('spawn_agent', 'send_message', 'followup_task'):
        item.setdefault('encrypted_function_args', [])
    return item


def chat_stream(resp, emit, model, names, note=None):
    """把上游 Chat SSE 翻译成 Codex 认的 Responses SSE 事件。"""
    rid = 'resp_' + uuid.uuid4().hex
    emit('response.created', {'response': {'id': rid, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})
    done, cur, calls = [], {}, {}  # cur: 正在输出的 reasoning/message
    if note:
        done.append(note_item(note))
        emit('response.output_item.added', {'output_index': 0, 'item': done[0]})
        emit('response.output_item.done', {'output_index': 0, 'item': done[0]})
    usage, finished = None, False

    def close(kind):
        c = cur.pop(kind, None)
        if not c:
            return
        if kind == 'reasoning':
            emit('response.reasoning_summary_text.done', {'item_id': c['id'], 'output_index': c['idx'], 'summary_index': 0, 'text': c['buf']})
            item = {'type': 'reasoning', 'id': c['id'], 'summary': [{'type': 'summary_text', 'text': c['buf']}]}
        else:
            emit('response.output_text.done', {'item_id': c['id'], 'output_index': c['idx'], 'content_index': 0, 'text': c['buf']})
            item = {'type': 'message', 'id': c['id'], 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': c['buf'], 'annotations': []}]}
        done.append(item)
        emit('response.output_item.done', {'output_index': c['idx'], 'item': item})

    def opened(kind, delta):
        if kind not in cur:
            idx = len(done) + len(cur)
            if kind == 'reasoning':
                c = cur[kind] = {'id': wid('rs'), 'idx': idx, 'buf': ''}
                emit('response.output_item.added', {'output_index': idx, 'item': {'type': 'reasoning', 'id': c['id'], 'summary': []}})
                emit('response.reasoning_summary_part.added', {'item_id': c['id'], 'output_index': idx, 'summary_index': 0,
                                                               'part': {'type': 'summary_text', 'text': ''}})
            else:
                c = cur[kind] = {'id': wid('msg'), 'idx': idx, 'buf': ''}
                emit('response.output_item.added', {'output_index': idx, 'item': {
                    'type': 'message', 'id': c['id'], 'role': 'assistant', 'status': 'in_progress', 'content': []}})
                emit('response.content_part.added', {'item_id': c['id'], 'output_index': idx, 'content_index': 0,
                                                     'part': {'type': 'output_text', 'text': '', 'annotations': []}})
        c = cur[kind]
        c['buf'] += delta
        if kind == 'reasoning':
            emit('response.reasoning_summary_text.delta', {'item_id': c['id'], 'output_index': c['idx'], 'summary_index': 0, 'delta': delta})
        else:
            emit('response.output_text.delta', {'item_id': c['id'], 'output_index': c['idx'], 'content_index': 0, 'delta': delta})

    for raw in resp:
        line = raw.decode('utf-8', 'replace').strip()
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            finished = True
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if chunk.get('error'):
            err = chunk['error']
            emit('response.failed', {'response': {'id': rid, 'object': 'response', 'status': 'failed', 'error': {
                'code': str(err.get('code') or 'upstream_error'), 'message': err.get('message') or json.dumps(err)}}})
            return
        usage = chunk.get('usage') or usage
        for ch in chunk.get('choices') or []:
            d = ch.get('delta') or {}
            r = d.get('reasoning_content') or d.get('reasoning')  # kimi/deepseek/mimo 用前者，glm 经 vLLM 用后者
            if r:
                opened('reasoning', r)
            if d.get('content'):
                close('reasoning')
                opened('message', d['content'])
            for i, tc in enumerate(d.get('tool_calls') or []):
                close('reasoning')
                close('message')
                c = calls.setdefault(tc.get('index', i), {'name': '', 'args': ''})
                f = tc.get('function') or {}
                c['name'] += f.get('name') or ''
                c['args'] += f.get('arguments') or ''
            if ch.get('finish_reason'):
                finished = True
    if not finished:  # 上游断流：不发 completed，让 Codex 自己重试
        raise http.client.IncompleteRead(b'')
    close('reasoning')
    close('message')
    for _, c in sorted(calls.items()):
        kind, ns, name = names.get(c['name'], ('function', None, c['name']))
        cid = 'call_' + uuid.uuid4().hex[:24]  # 自己生成 call_id：OpenAI 也认，切回 GPT 不会因 id 格式 400
        if kind == 'custom':
            try:
                inp = json.loads(c['args']).get('input', c['args'])
            except (ValueError, AttributeError):
                inp = c['args']
            item = {'type': 'custom_tool_call', 'id': wid('ctc'), 'call_id': cid, 'name': name,
                    'input': inp, 'status': 'completed'}
        else:
            item = {'type': 'function_call', 'id': wid('fc'), 'call_id': cid, 'name': name,
                    'arguments': c['args'] or '{}', 'status': 'completed'}
        if ns:
            item['namespace'] = ns
        plaintext_collaboration(item)
        idx = len(done)
        emit('response.output_item.added', {'output_index': idx, 'item': item})
        done.append(item)
        emit('response.output_item.done', {'output_index': idx, 'item': item})
    emit('response.completed', {'response': {'id': rid, 'object': 'response', 'status': 'completed', 'model': model,
                                             'output': done, 'usage': responses_usage(usage)}})


def responses_usage(u):
    u = u or {}
    inp, out = u.get('prompt_tokens', 0), u.get('completion_tokens', 0)
    cached = (u.get('prompt_tokens_details') or {}).get('cached_tokens') or u.get('prompt_cache_hit_tokens') or 0
    reasoning = (u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0
    return {'input_tokens': inp, 'input_tokens_details': {'cached_tokens': cached}, 'output_tokens': out,
            'output_tokens_details': {'reasoning_tokens': reasoning}, 'total_tokens': u.get('total_tokens') or inp + out}


# ---------------------------------------------------------------- 上游调用

class Upstream(Exception):
    def __init__(self, status, detail, retry_after=None):
        super().__init__(f'{status} {detail}')
        self.status, self.detail, self.retry_after = status, detail, retry_after


def call_upstream(p, body, path):
    h = {'Content-Type': 'application/json', 'Authorization': f"Bearer {p.get('api_key', '')}", **p.get('headers', {})}
    try:
        r = fetch('POST', p['base_url'].rstrip('/') + path, json.dumps(body, ensure_ascii=False).encode(), h)
    except STREAM_ERRORS as e:
        raise Upstream(502, f'cannot reach {p["base_url"]}: {e}')
    if r.status >= 400:
        try:
            raise Upstream(r.status, r.read(2000).decode('utf-8', 'replace'), r.getheader('Retry-After'))
        finally:
            r.close()
    return r


def oneshot(p, m, req):
    """非流式调用一次上游，返回 (文本, usage)。"""
    if (m.get('wire') or p.get('wire', 'chat')) == 'chat':
        body, _ = to_chat(req, p, m)
        body['stream'] = False
        body.pop('stream_options', None)
        with call_upstream(p, body, '/chat/completions') as r:
            res = json.loads(r.read())
        return (res['choices'][0]['message'].get('content') or '').strip(), responses_usage(res.get('usage'))
    with call_upstream(p, {**to_responses(req, m), 'stream': False}, '/responses') as r:
        res = json.loads(r.read())
    text = '\n'.join(text_of(o.get('content')) for o in res.get('output', []) if o.get('type') == 'message')
    return text.strip(), res.get('usage')


def mark(item_id):
    """Responses 上游返回的条目 id 加上我们的标记（确定性映射，added/done/delta 里保持一致）。"""
    if WID in item_id:
        return item_id
    head, _, rest = item_id.partition('_')
    return f'{head}{WID}_{rest}' if rest else f'x{WID}_{head}'


VISION_PROMPT = """You are the eyes of a coding assistant that cannot see images. Describe this image so it can act without seeing it:
1. Transcribe ALL visible text verbatim (code, terminal output, error messages, UI labels, numbers, URLs).
2. Then describe layout, UI state, diagrams or charts (axes, values, trends) and anything else notable.
Be precise and complete; do not guess beyond what is visible."""


def see(req, cfg):
    """纯文本模型：把输入里的图片先交给 vision 模型转成文字。
    vision 可以是列表（依次回退）；结果按图片哈希缓存在 ~/.witches/vision/，重启后历史里的旧图也不用重看。"""
    def parts_of(it):
        key = {'message': 'content', 'function_call_output': 'output', 'custom_tool_call_output': 'output'}.get(it.get('type'))
        return key, (it.get(key) if key and isinstance(it.get(key), list) else [])

    def cache(u):
        return HOME / 'vision' / hashlib.sha256(u.encode()).hexdigest()

    def describe(u):
        err = 'no vision model configured'
        for slug in models:
            t0, found = time.time(), lookup(cfg, slug)
            if not found:
                err = f'unknown vision model {slug}'
                continue
            try:  # 转写不需要推理：低推理强度快很多（kimi-k3 实测 50s -> 34s，推理 token 827 -> 6）
                text, _ = oneshot(*found, {'reasoning': {'effort': 'low'}, 'input': [{'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': VISION_PROMPT}, {'type': 'input_image', 'image_url': u}]}]})
                (HOME / 'vision').mkdir(mode=0o700, parents=True, exist_ok=True)
                cache(u).write_text(text or '(empty description)')
                log(f'vision {slug} {time.time() - t0:.1f}s ok ({len(text)} chars)')
                return
            except (Upstream, OSError, http.client.HTTPException, ValueError, KeyError) as e:
                err = str(e)[:200]
                log(f'vision {slug} {time.time() - t0:.1f}s failed: {err}')
        failed[u] = f'(image could not be described: {err})'

    models = cfg['vision'] if isinstance(cfg['vision'], list) else [cfg['vision']]
    urls = {c['image_url'] for it in req.get('input') or [] for c in parts_of(it)[1]
            if c.get('type') == 'input_image' and c.get('image_url')}
    failed = {}
    todo = [u for u in urls if not cache(u).exists()]
    if todo:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(describe, todo))

    def swap(c):
        if c.get('type') != 'input_image':
            return c
        u = c.get('image_url', '')
        text = cache(u).read_text() if cache(u).exists() else failed.get(u, '(image unavailable)')
        return {'type': 'input_text', 'text': '[Image, transcribed for a text-only model]\n' + text}

    items = []
    for it in req.get('input') or []:
        k, ps = parts_of(it)
        items.append({**it, k: [swap(c) for c in ps]} if any(c.get('type') == 'input_image' for c in ps) else it)
    return {**req, 'input': items}


def log(msg):
    print(time.strftime('%m-%d %H:%M:%S'), msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):  # Codex 关掉空闲的 keep-alive 连接
            pass

    def log(self, msg):
        print(time.strftime('%m-%d %H:%M:%S'), self.command, self.path.split('?')[0], msg, file=sys.stderr, flush=True)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = lambda self: self.route()

    # -- 输出

    def end_headers(self):
        self.response_started = True
        super().end_headers()

    def send_json(self, status, obj, headers=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def error(self, status, message, code='witches_error'):
        self.log(f'{status} {message}')
        self.send_json(status, {'error': {'message': f'witches: {message}', 'type': code, 'code': code}})

    def begin(self, status=200, headers=(('Content-Type', 'text/event-stream'), ('Cache-Control', 'no-cache'))):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if with_tag(_etag['upstream']):
            self.send_header('X-Models-Etag', with_tag(_etag['upstream']))
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()

    def chunk(self, data):
        if data:
            self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
            self.wfile.flush()

    def end(self):
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    def emit(self, kind, obj):
        self.chunk(f'event: {kind}\ndata: {json.dumps({"type": kind, **obj}, ensure_ascii=False)}\n\n'.encode())

    def emit_note(self, note):
        if note:  # 在上游的流之前插一条完整的 assistant 消息（Codex 按 item 收，不看 output_index）
            item = note_item(note)
            self.emit('response.output_item.added', {'output_index': 0, 'item': item})
            self.emit('response.output_item.done', {'output_index': 0, 'item': item})

    # -- 路由

    def route(self):
        self.response_started = False
        t0 = time.time()
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''  # 先读完请求体，keep-alive 连接上的下一个请求才不会错位
        prefix = next((p for p in OPENAI if self.path.startswith(p + '/') or self.path == p), None)
        if self.path == '/health':
            return self.send_json(200, health())
        if self.headers.get('Upgrade', '').lower() == 'websocket':  # 426 让 Codex 立刻退回 HTTP
            return self.send_json(426, {'error': {'message': 'witches: HTTP only', 'type': 'witches_error'}})
        if prefix is None:
            return self.error(404, 'unknown path')
        rest = self.path[len(prefix):].split('?')[0]
        try:
            if self.command == 'GET' and rest == '/models' and prefix == '/backend-api/codex':
                return self.models(prefix)
            if self.command == 'POST' and rest in ('/responses', '/responses/compact'):
                return self.responses_api(prefix, rest, raw, t0)
            return self.passthrough(prefix, raw)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except STREAM_ERRORS as e:
            self.log(f'upstream_disconnect error={type(e).__name__} committed={self.response_started}')
            if self.response_started:
                self.close_connection = True  # 不发正常结束块，Codex 才能识别截断；不再写第二组 HTTP 头
            else:
                self.error(502, 'upstream disconnected before response headers', 'upstream_disconnect')

    def passthrough(self, prefix, body, headers=None, label='', note=None, recovery=None):
        """GPT 首次输出前有限重试；其他路径只重试连接异常。所有响应保留原始字节。"""
        h = headers or {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        # 需要检查 SSE 的 GPT 请求使用明文响应，原始请求（包括 zstd）仍原样转发。
        if recovery is not None:
            h = {k: v for k, v in h.items() if k.lower() != 'accept-encoding'}
            h['Accept-Encoding'] = 'identity'
        url = OPENAI[prefix] + self.path[len(prefix):]
        started = time.monotonic()
        chain_id = uuid.uuid4().hex[:16]
        retries = max(0, int((recovery or {}).get('max_retries', 2)))
        window = max(0, float((recovery or {}).get('retry_window', 60)))

        def delay(attempt, advice=None):
            wait = retry_after(advice)
            if wait is None:
                wait = random.uniform(0.5, 1) * min(2 ** (attempt + 1), 15)
            if attempt >= retries or time.monotonic() - started + wait >= window:
                return None
            return wait

        for attempt in range(retries + 1):
            r, pending, committed, wait = None, bytearray(), False, None
            probe, outcome = StreamProbe(), 'ok'
            request_id = '-'
            stage, error_class, delivered = 'connect', None, 0
            truncated_after_terminal = False

            def begin():
                nonlocal committed, stage, delivered
                stage = 'write_headers'
                self.send_response(r.status, r.reason)
                for k, v in r.getheaders():
                    if k.lower() == 'x-models-etag':
                        _etag['upstream'] = v
                        v = with_tag(v)
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                no_body = r.status in (204, 304) or self.command == 'HEAD'
                if not no_body:
                    self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                committed = True
                if note and r.status < 300 and not no_body:
                    stage = 'write_stream'
                    self.emit_note(note)
                if pending:
                    stage = 'write_stream'
                    self.chunk(bytes(pending))
                    delivered += len(pending)
                    pending.clear()

            try:
                r = fetch(self.command, url, body or None, h)
                request_id = r.getheader('x-request-id') or r.getheader('x-oai-request-id') or '-'
                inspect = recovery is not None and self.path.split('?')[0].endswith('/responses') and \
                    r.status == 200 and (r.getheader('Content-Encoding') or 'identity').lower() == 'identity' and \
                    'json' not in (r.getheader('Content-Type') or '').lower()
                if r.status >= 400:
                    outcome = 'http_error'
                if recovery is not None and r.status in RETRYABLE:
                    stage = 'read_http_error'
                    pending.extend(r.read(65536))
                    try:
                        err = error_fields(json.loads(pending))
                    except (ValueError, AttributeError):
                        err = {}
                    outcome = 'http_error'
                    if not {str(err.get('code', '')), str(err.get('type', ''))} & QUOTA_CODES:
                        wait = delay(attempt, r.getheader('Retry-After'))
                    if wait is not None:
                        continue
                if r.status in (204, 304) or self.command == 'HEAD':
                    begin()
                    return
                if not inspect:
                    begin()
                while True:
                    stage = 'read_stream'
                    try:
                        data = r.read1(65536)
                    except STREAM_ERRORS:
                        if probe.terminal:  # 已有完整终态事件，仅缺 HTTP 的结束块
                            truncated_after_terminal = True
                            break
                        raise
                    if not data:
                        if inspect and not probe.terminal:
                            raise http.client.IncompleteRead(b'')
                        break
                    if inspect:
                        probe.feed(data)
                    if not committed:
                        pending.extend(data)
                        if probe.failure is not None and not probe.output:
                            outcome = 'stream_error'
                            if transient_error(probe.failure):
                                wait = delay(attempt, r.getheader('Retry-After'))
                            if wait is not None:
                                break
                        if probe.output or probe.failure is not None or len(pending) > PRELUDE_LIMIT:
                            begin()
                    else:
                        stage = 'write_stream'
                        self.chunk(data)
                        delivered += len(data)
                if wait is not None:
                    continue
                if not committed:
                    begin()
                stage = 'write_end'
                self.end()
                return
            except STREAM_ERRORS as e:
                error_class = type(e).__name__
                outcome = 'client_disconnect' if stage.startswith('write_') else 'disconnect'
                # 客户端取消不重放；上游断流只允许在首次交付前恢复。
                if stage.startswith('write_') or committed or self.response_started:
                    self.close_connection = True
                    return
                wait = delay(attempt) if recovery is not None or r is None else None
                if wait is None:
                    return self.error(502, f'upstream disconnected ({type(e).__name__})', 'upstream_disconnect')
            finally:
                if probe.failure is not None and outcome not in ('disconnect', 'client_disconnect'):
                    outcome = 'stream_error'
                elif probe.terminal_kind == 'response.incomplete' and outcome == 'ok':
                    outcome = 'incomplete'
                fields = {'http': r.status if r else None, 'outcome': outcome, 'attempt': attempt + 1,
                          'committed': committed, 'request_id': request_id,
                          'chain_id': chain_id, 'stage': stage, 'error_class': error_class,
                          'terminal': probe.terminal_kind, 'delivered_bytes': delivered,
                          'elapsed': round(time.monotonic() - started, 3)}
                if truncated_after_terminal:
                    fields['truncated_after_terminal'] = True
                if probe.failure is not None:
                    fields.update(code=probe.failure.get('code'), error_type=probe.failure.get('type'))
                    if probe.failure.get('code') == 'invalid_encrypted_content':
                        fields['encryption_shape'] = encryption_shape(body, h)
                        param = probe.failure.get('param')
                        if isinstance(param, str) and param.startswith('input') and \
                                re.fullmatch(r'[A-Za-z0-9_.\[\]-]{1,160}', param):
                            fields['error_param'] = param
                if wait is not None:
                    fields['retry_in'] = round(wait, 3)
                self.log(f'{label} -> openai ' + json.dumps(fields, ensure_ascii=False))
                if r is not None:
                    r.close()
                if wait is not None:
                    time.sleep(wait)

    def gpt(self, prefix, body, cfg, headers=None, label='', note=None):
        opts = openai_options(cfg)
        gate = slot('@openai', opts)
        t0 = time.monotonic()
        if not gate.acquire(timeout=max(0, float(opts['queue_timeout']))):
            self.log(f'{label} openai_queue_timeout waited={time.monotonic() - t0:.3f}s')
            return self.send_json(503, {'error': {'code': 'witches_queue_timeout', 'type': 'witches_error',
                                  'message': 'witches: GPT concurrency queue timed out'}}, {'Retry-After': '2'})
        try:
            return self.passthrough(prefix, body, headers, label, note, recovery=opts)
        finally:
            gate.release()

    def models(self, prefix):
        """ChatGPT 的模型列表 + 我们的模型。上游不可用时用 Codex 自己缓存的列表兜底。"""
        own, extra = None, {}
        h = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {'accept-encoding'}}
        r = None
        try:
            r = fetch('GET', OPENAI[prefix] + self.path[len(prefix):], None, h, timeout=4)
            b = r.read()
            if r.status in (401, 403):  # 登录过期这种事要让 Codex 自己看到
                return self.send_json(r.status, json.loads(b or b'{}'))
            if r.status < 300:
                own = json.loads(b)['models']
                _etag['upstream'] = r.getheader('ETag') or _etag['upstream']
                extra = {'ETag': with_tag(r.getheader('ETag'))} if r.getheader('ETag') else {}
        except (OSError, http.client.HTTPException, ValueError, KeyError):
            pass
        finally:
            if r is not None:
                r.close()
        if own is None:
            try:
                own = json.loads((CODEX_HOME / 'models_cache.json').read_text())['models']
            except (OSError, ValueError, KeyError):
                own = []
        own = [x for x in own if '/' not in x.get('slug', '')]
        ours = catalog(config())
        self.log(f'{len(own)} openai + {len(ours)} witches models')
        self.send_json(200, {'models': own + ours}, extra)

    def authorized(self):
        """只有拿着本机 Codex 登录凭据的请求才能用第三方模型（共享服务器上别人摸不到你的 key）。"""
        try:
            a = json.loads((CODEX_HOME / 'auth.json').read_text())
        except (OSError, ValueError):
            return True
        keys = {a.get('OPENAI_API_KEY'), (a.get('tokens') or {}).get('access_token')} - {None, ''}
        got = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        return not keys or got in keys

    def responses_api(self, prefix, rest, raw, t0):
        body = raw
        if self.headers.get('Content-Encoding', '').lower() == 'zstd':
            body = zstd.decompress(raw)
        try:
            req = json.loads(body)
        except ValueError:
            return self.passthrough(prefix, raw)
        cfg = config()
        model = req.get('model', '')
        sub = self.headers.get('x-openai-subagent', '')
        if os.environ.get('WITCHES_DUMP'):  # 调试：把请求体存下来看 Codex 实际发了什么（含对话内容，用完删掉）
            Path(os.environ['WITCHES_DUMP']).mkdir(mode=0o700, parents=True, exist_ok=True)
            (Path(os.environ['WITCHES_DUMP']) / f'{time.time():.3f}-{model.replace("/", "_")}.json').write_bytes(body)
        if '/' not in model and cfg.get('route', {}).get(sub):
            model = cfg['route'][sub]
            req = lift_tools(req)
        items = req.get('input') if isinstance(req.get('input'), list) else []
        note = NOTE + model if cfg.get('switch_note', True) and req.get('stream') and just_switched(items) and not any(
            it.get('type') == 'compaction_trigger' for it in items) else None
        items = [it for it in items if not is_note(it)]  # 之前插的提示只给人看，不发给模型
        if '/' not in model:  # GPT：只在线程里有 witches 痕迹时改写，否则连压缩过的原始字节一起原样转发
            if isinstance(req.get('input'), list) and (clean := for_openai(items)) != req['input']:
                h = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {'content-encoding'}}
                return self.gpt(prefix, json.dumps({**req, 'input': clean}).encode(), cfg, h, model + ' (cleaned)', note)
            return self.gpt(prefix, raw, cfg, label=model, note=note)
        if isinstance(req.get('input'), list):
            req = {**req, 'input': items}
        if not self.authorized():
            return self.error(403, 'token does not match this machine\'s Codex login')
        if rest == '/responses/compact':
            return self.error(400, 'use compaction_trigger on /responses for witches models')
        chain = [model] + [x for x in (lookup(cfg, model) or ({}, {}))[1].get('fallback', []) if x != model]
        tries = [(rnd, s) for rnd in range(3) for s in chain]  # 整条备选链最多走 3 轮，轮间退避
        self.wait = None
        for n, (rnd, slug) in enumerate(tries):
            if n and tries[n - 1][0] != rnd:
                time.sleep(min(self.wait or 2 ** rnd, 10))  # ponytail: 固定 3 轮，上限 10s
            found = lookup(cfg, slug)
            if not found:
                return self.error(404, f'unknown model {slug} (add it to {HOME / "config.toml"})')
            p, m = found
            last = n == len(tries) - 1
            i = chain.index(slug)
            gate = slot(slug.partition('/')[0], p)
            if gate:
                gate.acquire()
            try:
                self.stream_outcome = 'ok'
                if any(it.get('type') == 'compaction_trigger' for it in req.get('input') or []):
                    ok = self.compact(req, p, m, slug, last)
                else:
                    ok = self.serve(see(req, cfg) if cfg.get('vision') and not m.get('images') else req, p, m, slug, last,
                                    NOTE + slug if note else None)
            finally:
                if gate:
                    gate.release()
            if ok:
                self.log(f'{slug} {self.stream_outcome} {time.time() - t0:.1f}s' + (f' (fallback from {model})' if i else '')
                         + (f' [{sub}]' if sub else ''))
                return

    def failed(self, slug, e, last):
        """上游失败：可重试的错误且还有备选/下一轮就返回 False 继续，否则把错误交给 Codex。"""
        self.wait = int(e.retry_after) if str(e.retry_after).isdigit() else None
        self.log(f'{slug} upstream {e.status} {e.detail[:300]}')
        if e.status in RETRYABLE and not last:
            return False
        try:
            msg = json.loads(e.detail).get('error', {}).get('message') or e.detail
        except (ValueError, AttributeError):
            msg = e.detail
        self.send_json(e.status, {'error': {'message': f'{slug}: {msg}', 'type': 'upstream_error', 'code': str(e.status)}})
        return True

    def serve(self, req, p, m, slug, last, note=None):
        chat = (m.get('wire') or p.get('wire', 'chat')) == 'chat'
        try:
            if chat:
                body, names = to_chat(req, p, m)
                r = call_upstream(p, body, '/chat/completions')
            else:
                r = call_upstream(p, {**to_responses(req, m), 'stream': True}, '/responses')
        except Upstream as e:
            return self.failed(slug, e, last)
        def stream_error(err):
            self.stream_outcome = 'stream_error'
            self.log(f'{slug} ' + json.dumps({'outcome': 'stream_error', 'code': err.get('code'),
                     'error_type': err.get('type'), 'request_id': r.getheader('x-request-id') or
                     r.getheader('x-oai-request-id'), 'committed': True}, ensure_ascii=False))

        def emit(kind, obj):
            if kind == 'response.failed':
                stream_error(error_fields(obj))
            self.emit(kind, obj)

        with r:
            self.begin()
            if chat:
                chat_stream(r, emit, slug, names, note)
            else:  # Responses 上游：只给条目 id 打标记、去掉加密推理
                probe = StreamProbe()
                self.emit_note(note)
                for line in r:
                    probe.feed(line)
                    if line.startswith(b'data:'):
                        try:
                            ev = json.loads(line[5:])
                        except ValueError:
                            ev = None
                        if isinstance(ev, dict):
                            for it in [ev.get('item')] + list((ev.get('response') or {}).get('output') or []):
                                if isinstance(it, dict):
                                    if it.get('id'):
                                        it['id'] = mark(it['id'])
                                    if it.get('type') == 'reasoning':
                                        it.pop('encrypted_content', None)
                            if ev.get('item_id'):
                                ev['item_id'] = mark(ev['item_id'])
                            line = b'data: ' + json.dumps(ev, ensure_ascii=False).encode() + b'\n'
                    self.chunk(line)
                if probe.failure is not None:
                    stream_error(probe.failure)
                if not probe.terminal:
                    raise http.client.IncompleteRead(b'')
            self.end()
        return True

    def compact(self, req, p, m, slug, last):
        """Codex 的远程压缩（compaction_trigger）：让模型对纯文本化的对话写摘要，包成 compaction 条目还给 Codex。"""
        items = [it for it in req['input'] if it.get('type') != 'compaction_trigger']
        convo = flatten(items, int(m.get('context', 128000) * 0.8))
        sub = {k: v for k, v in req.items() if k not in ('tools', 'tool_choice', 'parallel_tool_calls', 'input')}
        sub['input'] = [user_msg('This is the conversation so far:\n\n' + convo), user_msg(COMPACT_PROMPT)]
        try:
            summary, usage = oneshot(p, m, sub)
            if not summary:
                raise Upstream(502, 'the model wrote an empty summary')
        except Upstream as e:
            return self.failed(slug, e, last)
        rid = 'resp_' + uuid.uuid4().hex
        item = {'type': 'compaction', 'id': wid('cmp'),
                'encrypted_content': CMP + base64.b64encode(summary.encode()).decode()}
        self.begin()
        self.emit('response.created', {'response': {'id': rid, 'object': 'response', 'status': 'in_progress', 'output': []}})
        self.emit('response.output_item.added', {'output_index': 0, 'item': item})
        self.emit('response.output_item.done', {'output_index': 0, 'item': item})
        done = {'id': rid, 'object': 'response', 'status': 'completed', 'output': [item]}
        if usage:
            done['usage'] = usage
        self.emit('response.completed', {'response': done})
        self.end()
        return True


def serve(port):
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'witches listening on http://127.0.0.1:{port}  (config {HOME / "config.toml"})', file=sys.stderr, flush=True)
    srv.serve_forever()


# ---------------------------------------------------------------- 安装 / 卸载

def base_url(port):
    return f'http://127.0.0.1:{port}/backend-api/codex'


def edit_codex(port=None):
    """往 config.toml 顶部加（port=None 时删除）唯一一行 openai_base_url；改之前先备份。"""
    path = CODEX_HOME / 'config.toml'
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    kept = [ln for ln in lines if MARK not in ln]
    if port is not None:
        top = []
        for ln in kept:
            if ln.lstrip().startswith('['):
                break
            top.append(ln)
        if any(re.match(r'\s*openai_base_url\s*=', ln) for ln in top):
            sys.exit(f'{path} 里已经有别人写的 openai_base_url（opencodex？），先处理掉再装 witches')
        kept.insert(0, f'openai_base_url = "{base_url(port)}"  {MARK}\n')
    if kept != lines:
        if path.exists():
            shutil.copy2(path, path.with_name('config.toml.witches-bak'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(kept))


def interpreter():
    """常驻服务用的解释器：优先 PATH 里的 python3.14 稳定链接，别写死带补丁版本号的路径（brew upgrade 后会失效）。"""
    venv = (Path(sys.prefix) / 'bin').resolve()  # uv run --script 把临时环境放在 PATH 最前，用完就删，必须跳过
    path = os.pathsep.join(d for d in os.environ.get('PATH', '').split(os.pathsep) if d and Path(d).resolve() != venv)
    return stable(shutil.which(f'python3.{sys.version_info.minor}', path=path) or getattr(sys, '_base_executable', sys.executable))


def stable(py):
    """uv 管理的 Python 换成只带小版本号的链接（cpython-3.14.7-… -> cpython-3.14-…，uv 升级补丁版本时会更新它）。"""
    alt = re.sub(r'cpython-(3\.\d+)\.\d+-', r'cpython-\1-', py, count=1)
    return alt if alt != py and Path(alt).exists() else py


def service(action, port):
    """后台常驻：Linux 用 systemd --user，macOS 用 launchd；自动带上安装时 shell 里的代理环境变量。"""
    py = interpreter()
    script = HOME / 'witches.py'
    env = {k: v for k, v in os.environ.items() if k.lower() in ('http_proxy', 'https_proxy', 'no_proxy', 'all_proxy')}
    env['CODEX_HOME'] = str(CODEX_HOME)
    log = HOME / 'witches.log'
    if sys.platform == 'darwin':
        plist = Path.home() / 'Library/LaunchAgents/dev.witches.plist'
        doms = [f'gui/{os.getuid()}', f'user/{os.getuid()}']  # SSH 进来、没人登录桌面时只有 user 域可用
        for dom in doms:
            subprocess.run(['launchctl', 'bootout', f'{dom}/dev.witches'], capture_output=True)
        if action == 'remove':
            plist.unlink(missing_ok=True)
            return
        envxml = ''.join(f'<key>{k}</key><string>{v}</string>' for k, v in env.items())
        plist.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.witches</string>
<key>ProgramArguments</key><array><string>{py}</string><string>{script}</string><string>serve</string><string>--port</string><string>{port}</string></array>
<key>EnvironmentVariables</key><dict>{envxml}</dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>{log}</string><key>StandardOutPath</key><string>{log}</string>
</dict></plist>
''')
        for dom in doms:
            if subprocess.run(['launchctl', 'bootstrap', dom, str(plist)], capture_output=True).returncode == 0:
                return
        sys.exit(f'launchctl bootstrap 失败：{plist}')
    unit = Path.home() / '.config/systemd/user/witches.service'
    if action == 'remove':
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'witches'], capture_output=True)
        unit.unlink(missing_ok=True)
        return
    unit.parent.mkdir(parents=True, exist_ok=True)
    envlines = ''.join(f'Environment="{k}={v}"\n' for k, v in env.items())
    unit.write_text(f'''[Unit]
Description=witches (extra models for Codex)

[Service]
ExecStart={py} {script} serve --port {port}
{envlines}StandardOutput=append:{log}
StandardError=append:{log}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
''')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'witches'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'witches'], check=True)


def wait_up(port):
    for _ in range(50):
        try:
            with fetch('GET', f'http://127.0.0.1:{port}/health', timeout=1) as r:
                r.read()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def install(port):
    HOME.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    if here != HOME.resolve():
        for f in ('witches.py', 'prompt.md'):
            shutil.copy2(here / f, HOME / f)
    if not (HOME / 'config.toml').exists():
        shutil.copy2(here / 'config.example.toml', HOME / 'config.toml')
    os.chmod(HOME / 'config.toml', 0o600)
    os.chmod(HOME / 'witches.py', 0o755)
    BIN.parent.mkdir(parents=True, exist_ok=True)
    BIN.unlink(missing_ok=True)  # 包装脚本直接调装好的解释器，运行时不依赖 PATH 里有 uv
    BIN.write_text(f'#!/bin/sh\nexec "{interpreter()}" "{HOME / "witches.py"}" "$@"\n')
    BIN.chmod(0o755)
    service('install', port)
    if not wait_up(port):
        sys.exit(f'witches 没起来，看日志：{HOME / "witches.log"}')
    edit_codex(port)
    print(f'✓ witches 已在 127.0.0.1:{port} 常驻；{CODEX_HOME / "config.toml"} 只加了一行 openai_base_url')
    print(f'  配置：{HOME / "config.toml"}    命令：{BIN} status')
    print('  已加载的 Codex 线程要重启 app-server 才会生效：codex app-server daemon restart（会打断正在跑的任务）')


def uninstall():
    edit_codex(None)
    port = config().get('port', DEFAULT_PORT)
    service('remove', port)
    if BIN.is_symlink() or BIN.exists() and str(HOME / 'witches.py') in BIN.read_text():
        BIN.unlink()
    print(f'✓ 已从 {CODEX_HOME / "config.toml"} 删掉 witches 那一行并停掉服务（{HOME} 保留）')
    print('  用 witches 模型压缩过的线程，卸载前先在 GPT 上 /compact 一次，否则那段摘要 GPT 读不到')


def status():
    cfg = config()
    port = cfg.get('port', DEFAULT_PORT)
    line = next((ln for ln in (CODEX_HOME / 'config.toml').read_text().splitlines() if MARK in ln), None) \
        if (CODEX_HOME / 'config.toml').exists() else None
    print('codex 配置 :', line or '未接入')
    print('服务       :', '运行中' if wait_up(port) else '未运行', f'127.0.0.1:{port}   日志 {HOME / "witches.log"}')
    for pname, p in cfg.get('providers', {}).items():
        try:
            with fetch('GET', p['base_url'].rstrip('/') + '/models', None,
                       {'Authorization': f"Bearer {p.get('api_key', '')}"}, timeout=8) as r:
                r.read()
                ok = r.status
        except OSError as e:
            ok = e
        print(f'{pname:10} :', ok, ', '.join(p.get('models', {})))
    for k, v in cfg.get('route', {}).items():
        print(f'route      : {k} -> {v}')


def deploy(host):
    """把脚本、提示词和配置同步到远端 ~/.witches，然后在远端用户的登录 shell 里（拿到代理变量）执行 install。
    每台远端可以有自己的模型：有 ~/.witches/hosts/<host>.toml 就用它，否则用 ~/.witches/config.toml。"""
    here = Path(__file__).resolve().parent
    subprocess.run(['ssh', host, 'mkdir -p ~/.witches && chmod 700 ~/.witches'], check=True)
    subprocess.run(['scp', '-q', *map(str, [here / 'witches.py', here / 'prompt.md', here / 'config.example.toml']),
                    f'{host}:.witches/'], check=True)
    cfg = next((f for f in (HOME / 'hosts' / f'{host}.toml', HOME / 'config.toml') if f.exists()), None)
    if cfg:
        print(f'配置：{cfg} -> {host}:~/.witches/config.toml')
        subprocess.run(['scp', '-q', str(cfg), f'{host}:.witches/config.toml'], check=True)
    # 用远端用户自己的登录 shell（macOS 是 zsh，读 .zprofile），才能拿到代理变量和 Homebrew 里的 uv
    subprocess.run(['ssh', host, '"$SHELL" -lc "uv run --script ~/.witches/witches.py install"'], check=True)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else 'help'
    port = int(args[args.index('--port') + 1]) if '--port' in args else config().get('port', DEFAULT_PORT)
    if cmd == 'serve':
        serve(port)
    elif cmd == 'install':
        install(port)
    elif cmd == 'uninstall':
        uninstall()
    elif cmd == 'status':
        status()
    elif cmd == 'deploy' and len(args) > 1:
        deploy(args[1])
    else:
        print(__doc__)
        print('用法: witches serve|install|uninstall|status [--port N] | deploy <ssh-host>')


if __name__ == '__main__':
    main()
