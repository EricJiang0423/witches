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
import datetime
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import random
import re
import secrets
import select
import shutil
import socket
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
QUOTA_CODES = {'usage_limit_reached', 'insufficient_quota', 'quota_exhausted', 'billing_hard_limit_reached',
               'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'}
STREAM_ERRORS = (OSError, http.client.HTTPException)
PRELUDE_LIMIT = 262144
OPENAI_DEFAULTS = {'max_concurrency': 3, 'queue_timeout': 30, 'max_retries': 2, 'retry_window': 60}
PROMPT = Path(__file__).resolve().with_name('prompt.md').read_text()  # Codex 的基础提示词（openai/codex，Apache-2.0）
PANEL = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>witches</title>
<style>
:root{--bg:#fff;--fg:#1c1c1e;--mute:#6b6b70;--line:#e3e3e6;--ok:#1a7f4b;--warn:#b26a00;--down:#b3261e;--bar:#e9e9ec;--card:#f6f6f8;--acc:#2f5bea}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--mute:#8e8e96;--line:#2c2c30;--ok:#4cc38a;--warn:#e0a030;--down:#ff8a80;--bar:#2c2c30;--card:#1f1f22;--acc:#7c9cff}}
*{box-sizing:border-box}
body{font:15px/1.55 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:820px;margin:0 auto;padding:20px 16px 56px}
@media(min-width:1100px){body{max-width:1180px;font-size:16px}}
@media(min-width:1600px){body{max-width:1560px;font-size:18px;padding-top:32px}}
@media(min-width:2200px){body{max-width:2000px;font-size:21px}}
h1{font-size:1.2em;margin:0}h2{font-size:.9em;margin:0 0 8px;color:var(--mute);font-weight:500}.m{color:var(--mute);font-size:.85em}
nav{display:flex;gap:4px;margin:14px 0 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{background:none;border:0;border-bottom:2px solid transparent;color:var(--mute);padding:8px 14px;font:inherit;cursor:pointer}
nav button.on{color:var(--fg);border-color:var(--acc)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:500;font-size:.85em}.ok{color:var(--ok)}.warn{color:var(--warn)}.down{color:var(--down)}
.bar{height:6px;background:var(--bar);border-radius:3px;margin-top:4px;min-width:80px}.bar i{display:block;height:100%;border-radius:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.card.wide{grid-column:1/-1}
.card table td,.card table th{border-color:var(--line)}
button.b{font:inherit;font-size:.85em;padding:4px 12px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
button.b:hover:not(:disabled){border-color:var(--acc)}button.b:disabled{opacity:.4;cursor:default}button.p{background:var(--acc);border-color:var(--acc);color:#fff}button.d{color:var(--down)}
input,textarea,select{font:inherit;font-size:.9em;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);min-width:0}
input[type=checkbox],.f input[type=checkbox]{width:auto;accent-color:var(--acc)}
.f{display:grid;grid-template-columns:140px 1fr;gap:6px 12px;align-items:center;margin:6px 0}.f input,.f select{width:100%}.f label{color:var(--mute);font-size:.85em}
textarea.src{width:100%;font:.8em/1.55 ui-monospace,Menlo,monospace;min-height:62vh}
.row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}.grow{flex:1}.right{margin-left:auto}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 16px;border-radius:8px;font-size:.85em;display:none;max-width:90%;z-index:9}
section{display:none}section.on{display:block}
.sw{display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:.85em}
.pill{display:inline-block;padding:0 8px;border-radius:10px;font-size:.78em;border:1px solid var(--line);color:var(--mute)}
.off td{opacity:.5}
.log td{padding:4px 8px;font:.8em/1.5 ui-monospace,Menlo,monospace;border-bottom:1px solid var(--line)}.log tr{cursor:pointer}.log tr:hover{background:var(--card)}
.raw{white-space:pre-wrap;word-break:break-all;color:var(--mute);padding:4px 0}
.sub-tabs{display:flex;gap:6px;margin-bottom:12px}.sub-tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.mt td{padding:4px 4px}.mt input{width:100%}
</style>
<h1>witches</h1><div class=m id=sub>连接中…</div>
<div id=need class="m warn" style=display:none>没有面板令牌，只能看额度。在终端运行 <b>witches panel</b> 打开带令牌的地址，才能修改。</div>
<nav id=tabs></nav>
<section id=s-status></section><section id=s-accounts></section><section id=s-models></section><section id=s-config></section><section id=s-log></section>
<div id=toast></div>
<script>
const $=s=>document.querySelector(s);
const h=(tag,a,...k)=>{const e=document.createElement(tag);for(const[x,v]of Object.entries(a||{})){if(v==null||v===false)continue;if(x=='class')e.className=v;else if(x.startsWith('on'))e.addEventListener(x.slice(2),v);else e.setAttribute(x,v===true?'':v)}for(const c of k.flat())if(c!=null)e.append(c);return e};
let T='';try{const m=location.hash.match(/token=([A-Za-z0-9_-]+)/);if(m){T=m[1];try{localStorage.setItem('wt',T)}catch(e){}history.replaceState(null,'',location.pathname)}else{try{T=localStorage.getItem('wt')||''}catch(e){}}}catch(e){}
const t=s=>s==null?'':s>=86400?Math.floor(s/86400)+' 天 '+Math.floor(s%86400/3600)+' 小时':s>=3600?Math.floor(s/3600)+' 小时 '+Math.floor(s%3600/60)+' 分':s>=60?Math.floor(s/60)+' 分':s+' 秒';
const col=p=>p>=50?'ok':p>=20?'warn':'down';
let tt;const toast=(x,bad)=>{const e=$('#toast');e.textContent=x;e.style.display='block';e.style.background=bad?'var(--down)':'var(--fg)';clearTimeout(tt);tt=setTimeout(()=>e.style.display='none',3500)};
async function api(method,path,body){const r=await fetch(path,{method,headers:{'X-Witches-Token':T,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
const usage=()=>fetch('/api/usage',{headers:{'X-Witches-Token':T}}).then(r=>r.json());
const act=async(f,ok)=>{try{await f();if(ok)toast(ok)}catch(e){toast(e.message,1)}};
const bar=w=>w?[h('span',{class:col(w.remaining_percent)},w.remaining_percent+'%'),' ',h('span',{class:'m'},t(w.resets_in_s)+'后重置'),h('div',{class:'bar'},h('i',{style:'width:'+w.remaining_percent+'%;background:var(--'+col(w.remaining_percent)+')'}))]:h('span',{class:'m'},'—');
const tabs={status:'状态',accounts:'账号',models:'模型',config:'配置',log:'日志'};let cur='status',timer;
for(const[k,v]of Object.entries(tabs))$('#tabs').append(h('button',{'data-t':k,onclick:()=>show(k)},v));
function show(k){cur=k;clearInterval(timer);document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==k));document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id=='s-'+k));({status:rStatus,accounts:rAccounts,models:rModels,config:rConfig,log:rLog})[k]();if(k=='status')timer=setInterval(rStatus,5000);if(k=='accounts')timer=setInterval(rAccounts,20000)}
const put=(id,...k)=>{if(cur==id)$('#s-'+id).replaceChildren(...k.flat())};
const sw=(on,label,fn)=>{const c=h('input',{type:'checkbox',checked:on?true:null,onchange:()=>fn(c.checked)});return h('label',{class:'sw'},c,label)};
const w5=(r,n)=>(r.windows||[]).find(v=>v.name==n);

async function rStatus(){try{const[st,u]=await Promise.all([api('GET','/api/state'),usage()]);$('#need').style.display='none';
$('#sub').textContent='运行中 · pid '+st.pid+' · 已运行 '+t(st.uptime_s)+' · 端口 '+st.port;
const up=h('div',{class:'m'},'点“检测”测试各上游');
put('status',h('div',{class:'grid'},
h('div',{class:'card wide'},h('h2',null,'GPT 账号额度（按使用顺序）'),usageTable(st.accounts,u.accounts)),
h('div',{class:'card'},h('h2',null,'Codex 接入'),h('div',{class:st.codex.wired?'ok':'warn'},st.codex.wired?'已启用：Codex 的请求经过 witches':'已停用：Codex 直连 OpenAI，witches 不参与（第三方模型不可用）'),h('div',{class:'m'},st.codex.config+' 里的 openai_base_url 一行'),h('div',{class:'row'},h('button',{class:'b '+(st.codex.wired?'d':'p'),onclick:()=>(!st.codex.wired||confirm('停用 witches 接入？Codex 会直连 OpenAI，第三方模型和多账号轮换都不会生效'))&&act(async()=>{await api('POST','/api/codex',{on:!st.codex.wired});rStatus()},st.codex.wired?'已停用，已打开的线程需重启 app-server':'已启用，已打开的线程需重启 app-server')},st.codex.wired?'停用接入':'启用接入'))),
h('div',{class:'card'},h('h2',null,'服务'),h('div',{class:'m'},st.managed?'由系统服务托管，退出后自动拉起':'前台运行，不能从这里重启'),h('div',{class:'m'},'GPT 并发 '+st.openai.max_concurrency+'，排队 '+st.openai.queue_timeout+' 秒，失败重试 '+st.openai.max_retries+' 次'),h('div',{class:'row'},h('button',{class:'b',onclick:()=>confirm('重启 witches？进行中的请求会中断')&&act(async()=>{await api('POST','/api/restart');toast('重启中…');setTimeout(()=>location.reload(),2500)})},'重启 witches'))),
h('div',{class:'card'},h('h2',null,'第三方余额'),u.providers.length?u.providers.map(p=>h('div',null,p.provider+'：'+(p.error?'查询失败 '+p.error:p.balances.map(b=>b.total+' '+b.currency).join(' / ')))):h('div',{class:'m'},'没有支持余额查询的 provider（目前认 DeepSeek）')),
h('div',{class:'card'},h('h2',null,'上游连通性'),h('div',{class:'row'},h('button',{class:'b',onclick:()=>act(async()=>{up.replaceChildren('检测中…');const d=await api('GET','/api/upstreams');up.replaceChildren(...d.upstreams.map(x=>h('div',{class:x.error||x.status>=400?'down':'ok'},x.provider+'：'+(x.error||('HTTP '+x.status+' · '+x.ms+' ms')))))})},'检测')),up)))}
catch(e){$('#sub').textContent=T?'无法连接 witches':'只读模式';$('#need').style.display=T?'none':'block';if(!T){try{const u=await usage();put('status',h('div',{class:'card'},h('h2',null,'GPT 账号额度'),usageTable(u.accounts.map(a=>({label:a.account})),u.accounts)))}catch(x){}}else put('status',h('div',{class:'down'},e.message))}}
function usageTable(accs,rows){return h('table',null,h('thead',null,h('tr',null,['#','账号','5 小时剩余','7 天剩余','状态'].map(x=>h('th',null,x)))),h('tbody',null,rows.map((r,i)=>{const a=accs[i]||{};return h('tr',{class:r.disabled?'off':null},h('td',null,i+1),h('td',null,a.label||r.account,h('div',{class:'m'},r.plan||a.plan||'')),h('td',null,bar(w5(r,'5h'))),h('td',null,bar(w5(r,'7d'))),h('td',{class:r.disabled?'m':r.down_s||r.limit_reached?'down':r.error?'warn':'ok'},r.disabled?'已停用':r.down_s?'额度用完，'+t(r.down_s)+'后恢复':r.limit_reached?'额度用完':r.error?'查询失败：'+r.error:'可用'))})))}

async function rAccounts(){try{const[st,u]=await Promise.all([api('GET','/api/state'),usage()]);const refs=st.accounts.map(a=>a.ref);
const move=(i,d)=>act(async()=>{const o=[...refs];[o[i],o[i+d]]=[o[i+d],o[i]];await api('PUT','/api/accounts',{order:o});rAccounts()});
const name=h('input',{placeholder:'名称，例如 plus-3'}),auth=h('textarea',{placeholder:'粘贴这个账号的 auth.json 全部内容',style:'width:100%;min-height:120px;font:.8em ui-monospace,Menlo,monospace'});
put('accounts',h('div',{class:'grid'},h('div',{class:'card wide'},h('div',{class:'m'},'按顺序使用：前一个的额度用完（5 小时窗口）才换下一个，恢复后自动回到靠前的账号。停用的账号保留在列表里，但不参与轮换。'),
h('table',null,h('tbody',null,st.accounts.map((a,i)=>{const r=u.accounts[i]||{};return h('tr',{class:a.disabled?'off':null},h('td',null,i+1),h('td',null,a.label,' ',a.disabled?h('span',{class:'pill'},'已停用'):null,h('div',{class:'m'},(a.plan||'')+(a.codex_login?' · Codex 登录的账号':'')+' · '+a.ref)),h('td',{style:'min-width:150px'},bar(w5(r,'5h'))),h('td',{style:'white-space:nowrap'},sw(!a.disabled,'启用',on=>act(async()=>{await api('POST','/api/accounts/toggle',{ref:a.ref,disabled:!on});rAccounts()},on?'已启用':'已停用，不再参与轮换')),' ',h('button',{class:'b',onclick:()=>move(i,-1),disabled:i==0},'↑'),' ',h('button',{class:'b',onclick:()=>move(i,1),disabled:i==refs.length-1},'↓'),' ',a.codex_login?null:h('button',{class:'b d',onclick:()=>confirm('删除 '+a.label+'？会同时删掉它的 auth.json 文件')&&act(async()=>{await api('DELETE','/api/accounts',{ref:a.ref});rAccounts()},'已删除')},'删除')))})))),
h('div',{class:'card wide'},h('h2',null,'添加账号'),h('div',{class:'m'},'先单独登录：CODEX_HOME=某个新目录 codex login，再把该目录下的 auth.json 粘贴到这里。不要复用别的工具正在用的登录，refresh token 会互相失效。'),h('div',{class:'row'},name),auth,h('div',{class:'row'},h('button',{class:'b p',onclick:()=>act(async()=>{await api('POST','/api/accounts',{name:name.value.trim(),auth:auth.value});rAccounts()},'已添加')},'添加')))))}catch(e){put('accounts',h('div',{class:'down'},e.message))}}

async function rModels(){try{const st=await api('GET','/api/state');
put('models',h('div',{class:'grid'},h('div',{class:'card wide'},h('table',null,h('thead',null,h('tr',null,['模型','上下文','看图','推理档位','备选',''].map(x=>h('th',null,x)))),h('tbody',null,st.models.map(m=>{const r=h('span',{class:'m'});return h('tr',null,h('td',null,m.slug),h('td',null,m.context?Math.round(m.context/1000)+'k':''),h('td',null,m.images?'是':''),h('td',{class:'m'},m.efforts.join(' ')),h('td',{class:'m'},m.fallback.join(' ')),h('td',null,h('button',{class:'b',onclick:()=>act(async()=>{r.className='m';r.textContent='测试中…';const d=await api('POST','/api/test',{model:m.slug});r.className=d.ok?'ok':'down';r.textContent=d.ok?'正常 '+d.ms+' ms':'失败 '+(d.error||d.status)})},'测试'),' ',r))})))),
h('div',{class:'card'},h('h2',null,'看图代理'),h('div',{class:'m'},st.vision.length?[].concat(st.vision).join(' → '):'未配置')),h('div',{class:'card'},h('h2',null,'子代理改派'),h('div',{class:'m'},Object.keys(st.route).length?Object.entries(st.route).map(([k,v])=>k+' → '+v).join('，'):'未配置')),h('div',{class:'card'},h('h2',null,'切换提示'),h('div',{class:'m'},st.switch_note?'开':'关')),h('div',{class:'card wide m'},'这些都在“配置”页的表单里修改。')))}catch(e){put('models',h('div',{class:'down'},e.message))}}

/* ---- 配置：表单 + TOML 源码 ---- */
let cfgMode='form';
async function rConfig(){try{const d=await api('GET','/api/config');const data=d.data;data.providers=data.providers||{};data.openai=data.openai||{};data.route=data.route||{};
const bind=(o,k,opt={})=>{const e=h('input',{type:opt.num?'number':'text',placeholder:opt.ph||''});e.value=o[k]==null?'':(opt.list&&Array.isArray(o[k])?o[k].join(', '):o[k]);e.oninput=()=>{const v=e.value.trim();if(v==='')delete o[k];else o[k]=opt.num?Number(v):opt.list?v.split(',').map(x=>x.trim()).filter(Boolean):v};return e};
const fld=(label,el,hint)=>[h('label',null,label),h('div',null,el,hint?h('div',{class:'m'},hint):null)];
const slugs=[];for(const[pn,p]of Object.entries(data.providers))for(const mid of Object.keys(p.models||{}))slugs.push(pn+'/'+mid);
const dl=h('datalist',{id:'slugs'},slugs.map(s=>h('option',{value:s})));
const provCard=(pn,p)=>{p.models=p.models||{};return h('div',{class:'card wide'},h('div',{class:'row'},h('b',null,pn),h('button',{class:'b d right',onclick:()=>confirm('删除 provider '+pn+' 及其所有模型？（保存后才生效）')&&(delete data.providers[pn],draw())},'删除')),
h('div',{class:'f'},fld('base_url',bind(p,'base_url',{ph:'https://…/v1'})),fld('api_key',bind(p,'api_key'),'显示成 ••••xxxx#hash 表示已保存；换 key 直接整段替换'),fld('max_concurrency',bind(p,'max_concurrency',{num:1}),'同时请求上限，0 不限')),
h('table',{class:'mt'},h('thead',null,h('tr',null,['模型 id','上下文','看图','推理档位（逗号分隔，空=默认）','备选模型（逗号分隔）',''].map(x=>h('th',null,x)))),h('tbody',null,Object.entries(p.models).map(([mid,m])=>{const eff=m.efforts&&!Array.isArray(m.efforts);return h('tr',null,h('td',null,h('b',{class:'m'},mid)),h('td',null,bind(m,'context',{num:1})),h('td',null,sw(m.images,'',v=>{if(v)m.images=true;else delete m.images})),h('td',null,eff?h('span',{class:'m'},'自定义映射表，用 TOML 源码修改'):bind(m,'efforts',{list:1,ph:'low, high, max'})),h('td',null,bind(m,'fallback',{list:1,ph:'Provider/model'})),h('td',null,h('button',{class:'b d',onclick:()=>(delete p.models[mid],draw())},'×')))}))),
(()=>{const n=h('input',{placeholder:'新模型 id（上游模型名）'});return h('div',{class:'row'},n,h('button',{class:'b',onclick:()=>{const v=n.value.trim();if(!v)return;p.models[v]={};draw()}},'+ 添加模型'))})())};
const routeRows=()=>[...Object.entries(data.route).map(([k,v])=>h('div',{class:'row'},h('b',{style:'min-width:170px'},k),h('input',{list:'slugs',value:v,oninput:e=>data.route[k]=e.target.value,class:'grow'}),h('button',{class:'b d',onclick:()=>(delete data.route[k],draw())},'×'))),(()=>{const s=h('select',null,['collab_spawn','guardian','review','memory_consolidation'].filter(x=>!(x in data.route)).map(x=>h('option',null,x)));return h('div',{class:'row'},s,h('button',{class:'b',onclick:()=>{if(s.value){data.route[s.value]='';draw()}}},'+ 添加改派'))})()];
const visionIn=bind(data,'vision',{list:1,ph:'Provider/model, …'});visionIn.setAttribute('list','slugs');
const save=()=>act(async()=>{if(!Object.keys(data.route).length)delete data.route;await api('PUT','/api/config',cfgMode=='form'?{data}:{text:src.value});rConfig()},'已保存，对新请求立即生效');
const src=h('textarea',{class:'src',spellcheck:'false'});src.value=d.text;
const addProv=(()=>{const n=h('input',{placeholder:'新 provider 名称，例如 Zhipu'});return h('div',{class:'row'},n,h('button',{class:'b',onclick:()=>{const v=n.value.trim();if(!v||data.providers[v])return;data.providers[v]={base_url:'',api_key:'',models:{}};draw()}},'+ 添加 provider'))})();
function draw(){put('config',dl,h('div',{class:'row'},h('div',{class:'sub-tabs'},h('button',{class:'b '+(cfgMode=='form'?'on':''),onclick:()=>{cfgMode='form';draw()}},'表单'),h('button',{class:'b '+(cfgMode=='src'?'on':''),onclick:()=>{cfgMode='src';draw()}},'TOML 源码')),h('span',{class:'m'},'即 ~/.witches/config.toml；保存前校验并留一份 config.toml.bak；对新请求立即生效（改端口需重启）'),h('span',{class:'right row'},h('button',{class:'b',onclick:rConfig},'放弃修改'),h('button',{class:'b p',onclick:save},'保存'))),
cfgMode=='src'?[h('div',{class:'m'},'源码模式保留注释；表单模式保存时会重新生成文件，注释不保留。'),src]:
h('div',{class:'grid'},
h('div',{class:'card'},h('h2',null,'基础'),h('div',{class:'f'},fld('端口',bind(data,'port',{num:1}),'改了要重启'),fld('看图代理 vision',visionIn,'纯文本模型收到图片时，先交给它转写'),fld('切换提示',sw(data.switch_note!==false,'线程里换模型后提示一行',v=>{if(v)delete data.switch_note;else data.switch_note=false})))),
h('div',{class:'card'},h('h2',null,'GPT 通道'),h('div',{class:'f'},fld('max_concurrency',bind(data.openai,'max_concurrency',{num:1}),'同时请求数，0 不限'),fld('queue_timeout',bind(data.openai,'queue_timeout',{num:1}),'排队最多等几秒'),fld('max_retries',bind(data.openai,'max_retries',{num:1}),'首次输出前最多重试几次'),fld('retry_window',bind(data.openai,'retry_window',{num:1}),'安排重试的时间窗口（秒）')),h('div',{class:'m'},'账号顺序和启停在“账号”页。')),
h('div',{class:'card wide'},h('h2',null,'子代理 / 审查改派（原本发给 GPT 的请求改给第三方模型）'),routeRows()),
Object.entries(data.providers).map(([pn,p])=>provCard(pn,p)),h('div',{class:'card wide'},addProv)))}
draw()}catch(e){put('config',h('div',{class:'down'},e.message))}}

/* ---- 日志：解析成表格，可筛选、搜索、自动刷新 ---- */
let logSt={f:'all',q:'',auto:true,n:300};
function parseLog(l){const m=l.match(/^([0-9][0-9]-[0-9][0-9] [0-9:]{8}) (?:(?:GET|POST|PUT|DELETE|PATCH) ([^ ]+) )?(.*)$/);if(!m)return{raw:l,kind:'other',time:'',sum:l,lvl:'',el:'',st:''};
const rest=m[3];const o={raw:l,time:m[1],kind:'other',sum:rest,lvl:'',el:'',st:''};const g=rest.match(/^(.*?) -> openai ({.*})$/);
if(g){o.kind='gpt';try{const j=JSON.parse(g[2]);o.sum=g[1]+' · '+j.account+(j.attempt>1?' · 第'+j.attempt+'次尝试':'')+(j.code?' · '+j.code:'')+(j.retry_in?' · '+j.retry_in+'s 后重试':'')+(j.retry_stop?' · '+j.retry_stop:'');o.el=j.elapsed!=null?j.elapsed+'s':'';o.st=(j.http||'—')+' '+j.outcome;o.lvl=j.outcome=='ok'?'ok':j.outcome=='client_disconnect'?'warn':'down'}catch(e){}return o}
const p=rest.match(/^([^ ]+[/][^ ]+) ([a-z_]+) ([0-9.]+)s(.*)$/);if(p){o.kind='3p';o.sum=p[1]+p[4];o.el=p[3]+'s';o.st=p[2];o.lvl=p[2]=='ok'?'ok':'down';return o}
if(/error|fail|exception|timeout|disconnect|502|429/i.test(rest))o.lvl='down';return o}
async function rLog(){try{const d=await api('GET','/api/log?n='+logSt.n);const rows=d.lines.map(parseLog);
const q=logSt.q.toLowerCase();const shown=rows.filter(r=>(logSt.f=='all'||(logSt.f=='err'?r.lvl=='down':r.kind==logSt.f))&&(!q||r.raw.toLowerCase().includes(q)));
const tb=h('tbody',null,shown.reverse().map(r=>{const raw=h('tr',{style:'display:none'},h('td',{colspan:5},h('div',{class:'raw'},r.raw)));return[h('tr',{onclick:()=>raw.style.display=raw.style.display=='none'?'':'none'},h('td',{class:'m',style:'white-space:nowrap'},r.time),h('td',null,r.kind=='gpt'?'GPT':r.kind=='3p'?'第三方':''),h('td',{style:'word-break:break-all'},r.sum),h('td',{class:'m'},r.el),h('td',{class:r.lvl},r.st)),raw]}).flat());
const had=document.activeElement&&document.activeElement.dataset&&document.activeElement.dataset.q;const q2=h('input',{'data-q':'1',placeholder:'搜索（模型、账号、错误码…）',value:logSt.q,class:'grow',oninput:e=>{logSt.q=e.target.value;clearTimeout(q2._t);q2._t=setTimeout(rLog,250)}});
put('log',h('div',{class:'row'},h('select',{onchange:e=>{logSt.f=e.target.value;rLog()}},[['all','全部'],['gpt','GPT'],['3p','第三方'],['err','出错']].map(([v,l])=>h('option',{value:v,selected:logSt.f==v?true:null},l))),q2,h('select',{onchange:e=>{logSt.n=+e.target.value;rLog()}},[200,300,600,1000].map(n=>h('option',{value:n,selected:logSt.n==n?true:null},'最近 '+n+' 行'))),sw(logSt.auto,'自动刷新',v=>{logSt.auto=v;rLog()}),h('button',{class:'b',onclick:rLog},'刷新'),h('span',{class:'m'},'显示 '+shown.length+' / '+rows.length+' 条，新的在上；点一行看原文；不含对话内容')),
h('table',{class:'log'},h('thead',null,h('tr',null,['时间','类型','内容','耗时','结果'].map(x=>h('th',null,x)))),tb));
if(had&&cur=='log'){q2.focus();q2.setSelectionRange(q2.value.length,q2.value.length)}clearInterval(timer);if(logSt.auto&&cur=='log')timer=setInterval(()=>{if(!document.activeElement||document.activeElement.tagName!='INPUT')rLog()},3000)}catch(e){put('log',h('div',{class:'down'},e.message))}}
if(!T){$('#need').style.display='block'}show('status')
</script>"""

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


class ClientDisconnected(OSError):
    """下游已取消；不能按上游故障重试。"""


class RetryUpstream(Exception):
    """结束本次上游尝试，关闭连接后再退避。"""


class ClientWatch:
    """请求体读完后监视下游 EOF/RST，不消费 keep-alive/pipeline 的下一条请求。

    只 shutdown 上游 socket 来唤醒阻塞 I/O；HTTPResponse 的关闭仍由请求线程负责，
    避免跨线程 close 缓冲 reader 的锁。连接建立期间也持续检查新出现的 socket。
    """
    def __init__(self, client):
        self.client, self.upstream, self.upstream_socket = client, None, None
        self.cancelled, self.done = threading.Event(), threading.Event()
        self.cancelled_at, self.chain_id = None, None
        self.connect_phase, self.connection_route = None, None
        self.handled = False  # 请求线程确实因取消而终止；完整响应后的 EOF 不算取消
        self.lock = threading.Lock()
        self.poller = select.poll()  # 不受 select() 的 FD_SETSIZE 限制
        self.poller.register(client, select.POLLIN)
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join()

    def check(self):
        with self.lock:
            if not self.cancelled.is_set():
                disconnected = False
                try:
                    if self.poller.poll(0) and not self.client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        disconnected = True
                except BlockingIOError:
                    pass
                except OSError:
                    disconnected = True
                if disconnected:
                    self.cancelled_at = time.monotonic()
                    self.cancelled.set()
        if self.cancelled.is_set():
            raise ClientDisconnected()

    def bind(self, conn):
        with self.lock:
            self.upstream = conn
            self.upstream_socket = conn.sock
        self.check()

    def watch(self):
        while not self.done.wait(.1):
            try:
                self.check()
            except ClientDisconnected:
                with self.lock:
                    sock = (self.upstream.sock or self.upstream_socket) if self.upstream is not None else None
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def wait(self, seconds):
        self.check()
        if self.cancelled.wait(seconds):
            raise ClientDisconnected()
        self.check()


def fetch(method, url, body=None, headers=None, timeout=900, cancel=None):
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
        if cancel is not None:
            cancel.connection_route = 'proxy' if proxy else 'direct'
            cancel.connect_phase = 'tcp_or_proxy_connect'
            cancel.bind(conn)
            # HTTPSConnection.connect() 在 wrap_socket 返回前不会发布 SSLSocket；
            # 默认的阻塞握手会让监视器只能拿到已被 detach 的旧 socket。
            http.client.HTTPConnection.connect(conn)
            cancel.bind(conn)
            if https:
                cancel.connect_phase = 'tls_handshake'
                conn.sock = _ctx.wrap_socket(conn.sock, server_hostname=u.hostname,
                                             do_handshake_on_connect=False)
                cancel.bind(conn)  # 先发布可 shutdown 的 SSLSocket，再进入阻塞握手
                conn.sock.do_handshake()
                cancel.check()
            cancel.connect_phase = 'send_request'
        conn.request(method, path, body=body, headers=headers or {})
        if cancel is not None:
            cancel.connect_phase = 'response_headers'
            cancel.bind(conn)  # getresponse() 可能把 Connection: close 的 socket 从 conn 分离
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

    def acquire(self, timeout=None, cancel=None):
        with self.condition:
            self.waiting += 1
            try:
                deadline = None if timeout is None else time.monotonic() + timeout
                while True:
                    if cancel is not None:
                        cancel.check()
                    if not self.limit or self.active < self.limit:
                        break
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self.condition.wait((min(remaining, .1) if remaining is not None else .1)
                                        if cancel is not None else remaining)
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


CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'  # Codex CLI 的公开 OAuth client id
ACCOUNT_HEADERS = {'authorization', 'chatgpt-account-id', 'x-codex-turn-state'}  # 换账号时这些属于旧账号
_down, _down_lock, _refresh_lock = {}, threading.Lock(), threading.Lock()  # 账号 -> 额度恢复的 monotonic 时刻


def usable(acct):
    with _down_lock:
        return _down.get(acct, 0) <= time.monotonic()


def mark_down(acct, seconds):
    with _down_lock:
        _down[acct] = time.monotonic() + seconds


def quota_wait(err, advice=None):
    """额度多久恢复：优先用上游给的 resets_in_seconds / resets_at，没有就先歇 10 分钟。"""
    for v in (err.get('resets_in_seconds'), (err.get('resets_at') or 0) - time.time(), retry_after(advice)):
        if isinstance(v, (int, float)) and v > 0:
            return min(v, 7 * 86400)
    return 600


def jwt_exp(token):
    try:
        p = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)))['exp']
    except (IndexError, ValueError, KeyError, TypeError):
        return 0


def account_headers(path):
    """额外账户（Codex 格式的 auth.json）的请求头；access token 快过期就用 refresh_token 续，并写回原文件。"""
    with _refresh_lock:
        auth = json.loads(path.read_text())
        t = auth['tokens']
        if jwt_exp(t['access_token']) - time.time() < 120:
            r = fetch('POST', 'https://auth.openai.com/oauth/token', json.dumps({
                'client_id': CLIENT_ID, 'grant_type': 'refresh_token', 'refresh_token': t['refresh_token'],
                'scope': 'openid profile email'}).encode(), {'Content-Type': 'application/json'}, timeout=15)
            with r:
                if r.status != 200:
                    raise OSError(f'refresh failed: HTTP {r.status}')
                t.update({k: v for k, v in json.loads(r.read()).items() if k in ('id_token', 'access_token', 'refresh_token')})
            auth['last_refresh'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            tmp = path.with_name(path.name + '.tmp')
            tmp.write_text(json.dumps(auth, indent=2))
            tmp.chmod(0o600)
            os.replace(tmp, path)
    return {'Authorization': f"Bearer {t['access_token']}", 'chatgpt-account-id': t.get('account_id', '')}


_usage_cache, _usage_lock = {}, threading.Lock()


def window_name(seconds):
    return {18000: '5h', 604800: '7d'}.get(seconds, f'{seconds}s')


def account_usage(acct, fresh=False):
    """ChatGPT 账号的剩余额度（/wham/usage，Codex 自己用的同一个接口）；成功的结果缓存 60 秒。"""
    with _usage_lock:
        hit = _usage_cache.get(acct)
        if hit and not fresh and time.monotonic() - hit[0] < 60:
            return hit[1]
    out = {'account': account_name(acct)}
    try:
        if acct is None:
            t = json.loads(codex_auth().read_text())['tokens']
            h = {'Authorization': f"Bearer {t['access_token']}", 'chatgpt-account-id': t.get('account_id', '')}
        else:
            h = account_headers(acct)
        with fetch('GET', OPENAI['/backend-api/codex'].rsplit('/codex', 1)[0] + '/wham/usage', None, h, timeout=10) as r:
            body = r.read()
            if r.status != 200:
                raise OSError(f'HTTP {r.status}')
        d = json.loads(body)
        rl = d.get('rate_limit') or {}
        out.update(plan=d.get('plan_type'), limit_reached=bool(rl.get('limit_reached')), windows=[
            {'name': window_name(w.get('limit_window_seconds')), 'used_percent': w.get('used_percent'),
             'remaining_percent': 100 - (w.get('used_percent') or 0), 'resets_in_s': w.get('reset_after_seconds')}
            for w in (rl.get('primary_window'), rl.get('secondary_window')) if w])
        with _usage_lock:
            _usage_cache[acct] = (time.monotonic(), out)
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
        out['error'] = str(e) or type(e).__name__
    return out


def provider_balance(name, p):
    """目前只认 DeepSeek 的 /user/balance；别家没有统一接口。"""
    try:
        with fetch('GET', 'https://api.deepseek.com/user/balance', None,
                   {'Authorization': f"Bearer {p.get('api_key', '')}"}, timeout=10) as r:
            infos = json.loads(r.read())['balance_infos']
        return {'provider': name, 'balances': [{'currency': b['currency'], 'total': b['total_balance']} for b in infos]}
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
        return {'provider': name, 'error': str(e) or type(e).__name__}


def usage_report(fresh=False, labels=False):
    cfg = config()
    opts = openai_options(cfg)
    pool = account_pool(opts, every=True)
    live = account_pool(opts)
    now = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as ex:
        accounts = list(ex.map(lambda a: account_usage(a, fresh), pool))
        balances = list(ex.map(lambda kv: provider_balance(*kv), [
            (n, p) for n, p in cfg.get('providers', {}).items() if 'api.deepseek.com' in p.get('base_url', '')]))
    with _down_lock:
        for a, row in zip(pool, accounts):
            row['down_s'] = round(max(0, _down.get(a, 0) - now))
            row['disabled'] = a not in live
    if labels:  # 邮箱只给带面板令牌的请求
        accounts = [{**row, 'label': account_label(a)} for a, row in zip(pool, accounts)]
    return {'accounts': accounts, 'providers': balances}


CODEX_LOGIN = 'codex-login'  # 没有邮箱可显示时（匿名接口、日志）Codex 自己登录态的名字
_started = time.time()


def codex_auth():
    return CODEX_HOME / 'auth.json'


def _acct(n):
    p = Path(n).expanduser()
    return None if n == 'primary' or p.resolve() == codex_auth().resolve() else p


def account_pool(opts, every=False):
    """账号顺序：accounts 是 auth.json 路径列表，Codex 自己的登录态就是 ~/.codex/auth.json（旧写法 "primary" 也认）；
    列表里没有它就排第一。轮到它时请求头原样透传，None 代表它。disabled 里的账号保留在列表但不参与轮换
    （every=True 时连同停用的一起返回）。全部停用时 choose() 仍退回 Codex 自己的登录态。"""
    pool = [_acct(n) for n in opts.get('accounts') or []]
    if None not in pool:
        pool.insert(0, None)
    if every:
        return pool
    off = {a.resolve() if a else None for a in map(_acct, opts.get('disabled') or [])}
    return [a for a in pool if (a.resolve() if a else None) not in off]


def account_name(acct):
    return CODEX_LOGIN if acct is None else acct.stem


def claims(path):
    """auth.json 里 access_token 的 JWT 声明（只读本地，不验签）；坏文件返回空。"""
    try:
        p = json.loads(Path(path).read_text())['tokens']['access_token'].split('.')[1]
        return json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)))
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return {}


def account_label(acct):
    c = claims(codex_auth() if acct is None else acct)
    return (c.get('https://api.openai.com/profile') or {}).get('email') or account_name(acct)


def account_plan(acct):
    return (claims(codex_auth() if acct is None else acct).get('https://api.openai.com/auth') or {}).get('chatgpt_plan_type')


def choose(pool, h):
    """按顺序取第一个额度没用完的账号：None 是 Codex 自己的登录态（请求头原样），其余是额外账户。"""
    for acct in pool:
        if not usable(acct):
            continue
        if acct is None:
            return None, h
        try:
            return acct, {**{k: v for k, v in h.items() if k.lower() not in ACCOUNT_HEADERS}, **account_headers(acct)}
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            mark_down(acct, 60)  # 文件坏了/续期失败：先跳过一分钟，不拖垮其他账号
    return None, h  # 全部用完：交给 Codex 自己的登录态，让它看到真实的额度错误


def openai_options(cfg):
    return {**OPENAI_DEFAULTS, **cfg.get('openai', {})}


def health():
    opts = openai_options(config())
    with _slots_lock:
        gate = _slots.get('@openai')
    state = gate.snapshot() if gate else {'active': 0, 'waiting': 0, 'runtime_limit': opts['max_concurrency']}
    out = {**{k: opts[k] for k in OPENAI_DEFAULTS}, **state}
    if opts.get('accounts'):
        now = time.monotonic()
        with _down_lock:
            out['accounts'] = [{'account': account_name(a), 'down_s': round(max(0, _down.get(a, 0) - now))}
                               for a in account_pool(opts)]
    return {'ok': True, 'pid': os.getpid(), 'openai': out}


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


def empty_start_event(kind, event):
    """只延后明确为空的文本/推理壳；工具、密文、未知字段均保持立即交付。"""
    envelope = {'type', 'sequence_number', 'output_index', 'item_id',
                'content_index', 'summary_index', 'item', 'part'}
    if set(event) - envelope:
        return False
    if kind == 'response.output_item.added':
        item = event.get('item')
        if not isinstance(item, dict) or item.get('status') not in (None, 'in_progress'):
            return False
        if item.get('type') == 'reasoning':
            return not (set(item) - {'type', 'id', 'status', 'summary', 'content', 'encrypted_content'}) and \
                item.get('summary') in (None, []) and item.get('content') in (None, []) and \
                item.get('encrypted_content') in (None, '')
        if item.get('type') == 'message':
            return not (set(item) - {'type', 'id', 'status', 'role', 'content', 'phase'}) and \
                item.get('phase') in (None, 'commentary', 'final', 'final_answer') and \
                item.get('role') == 'assistant' and item.get('content') == []
    expected = {'response.content_part.added': 'output_text',
                'response.reasoning_summary_part.added': 'summary_text'}.get(kind)
    part = event.get('part')
    return expected is not None and isinstance(part, dict) and \
        not (set(part) - {'type', 'text', 'annotations', 'logprobs'}) and \
        part.get('type') == expected and part.get('text') == '' and \
        part.get('annotations') in (None, []) and part.get('logprobs') in (None, [])


class StreamProbe:
    """旁路检查 SSE，原始字节不重编码。只缓冲已知的启动事件，未知事件保守地提交。"""
    def __init__(self):
        self.buffer = b''
        self.output = self.terminal = self.opaque = False
        self.failure = None
        self.terminal_kind = None
        self.first_output_event = None

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
            elif empty_start_event(kind, event):
                pass
            elif kind not in ('response.created', 'response.in_progress', 'response.queued', 'ping') or \
                    (isinstance(event.get('response'), dict) and event['response'].get('output')):
                self.output = True
            if self.output and self.first_output_event is None:
                self.first_output_event = kind if re.fullmatch(r'response\.[a-z_.]{1,80}', kind) else 'unknown'
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


# ---------------------------------------------------------------- 面板 API（读写配置，需要面板令牌）

def panel_token():
    """面板令牌：~/.witches/panel.token（0600）。`witches panel` 会打开带令牌的地址；别人读不到这个文件就进不了写接口。"""
    f = HOME / 'panel.token'
    try:
        return f.read_text().strip()
    except OSError:
        HOME.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(24)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(tok)
        return tok


SECRET_KEY = re.compile(r'key|token|secret|password', re.I)


def secret_values(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and v and SECRET_KEY.search(k):
                yield v
            else:
                yield from secret_values(v)


def mask(secret):
    return f'••••{secret[-4:]}#{hashlib.sha1(secret.encode()).hexdigest()[:6]}'


def masked_config(text):
    for sec in sorted(set(secret_values(config())), key=len, reverse=True):
        text = text.replace(f'"{sec}"', f'"{mask(sec)}"')
    return text


def unmask_config(text):
    table = {mask(sec).rsplit('#', 1)[1]: sec for sec in secret_values(config())}

    def back(m):
        if m[1] not in table:
            raise ValueError('配置里有认不出的隐藏密钥（••••xxxx#hash），请重新填写完整的 key')
        return table[m[1]]
    return re.sub(r'••••[^"#\n]{0,8}#([0-9a-f]{6})', back, text)


BARE = re.compile(r'[A-Za-z0-9_-]+$')


def tkey(k):
    return k if BARE.match(k) else json.dumps(k, ensure_ascii=False)


def tval(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return '[' + ', '.join(map(tval, v)) + ']'
    if isinstance(v, dict):
        return '{ ' + ', '.join(f'{tkey(k)} = {tval(x)}' for k, x in v.items()) + ' }' if v else '{}'
    raise ValueError(f'不支持的配置值：{v!r}')


def toml_dumps(d, path=()):
    """我们配置的最小 TOML 写出：前三层是 [表]（providers.X.models），更深的是内联表。注释不保留。"""
    lines, tables = [], []
    for k, v in d.items():
        (tables if isinstance(v, dict) and len(path) + 1 <= 3 else lines).append((k, v))
    out = [f'{tkey(k)} = {tval(v)}' for k, v in lines]
    text = '\n'.join(out) + ('\n' if out else '')
    if path and not out:
        text = ''
    parts = [f"[{'.'.join(map(tkey, path))}]\n" + text] if path and out else [text] if out else []
    for k, v in tables:
        parts.append(toml_dumps(v, path + (k,)))
    return '\n'.join(p for p in parts if p)


def mask_data(node):
    if isinstance(node, dict):
        return {k: mask(v) if isinstance(v, str) and v and SECRET_KEY.search(k) else mask_data(v) for k, v in node.items()}
    return [mask_data(x) for x in node] if isinstance(node, list) else node


def unmask_data(node):
    if isinstance(node, dict):
        return {k: unmask_data(v) for k, v in node.items()}
    if isinstance(node, list):
        return [unmask_data(x) for x in node]
    return unmask_config(node) if isinstance(node, str) else node


def dump_checked(data):
    text = toml_dumps(data)
    if tomllib.loads(text) != data:
        raise ValueError('这份配置无法无损写成 TOML，请用“TOML 源码”页修改')
    return text


def write_config(text):
    """校验 TOML、留一份 config.toml.bak、原子写入（0600）。配置每个请求都会重新读，保存即生效（port 除外）。"""
    tomllib.loads(text)
    f = HOME / 'config.toml'
    if f.exists():
        shutil.copy2(f, f.with_name('config.toml.bak'))
    tmp = f.with_name('config.toml.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(text)
    os.replace(tmp, f)


def ref(p):
    """账号在配置里的写法：家目录下用 ~/ 开头。"""
    p = Path(p)
    try:
        return '~/' + str(p.resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(p)


def account_refs():
    return [ref(codex_auth() if a is None else a) for a in account_pool(config().get('openai', {}), every=True)]


def set_openai_list(key, refs):
    text = (HOME / 'config.toml').read_text() if (HOME / 'config.toml').exists() else ''
    line = f'{key} = ' + json.dumps(refs, ensure_ascii=False)
    pat = re.compile(rf'^{key}\s*=\s*\[[^\]]*\]', re.M)
    head = re.compile(r'^\[openai\][ \t]*$', re.M)
    if pat.search(text):
        text = pat.sub(lambda m: line, text, count=1)
    elif head.search(text):
        text = head.sub(lambda m: m[0] + '\n' + line, text, count=1)
    else:
        text = text.rstrip('\n') + '\n\n[openai]\n' + line + '\n'
    write_config(text)


def set_accounts(refs):
    set_openai_list('accounts', refs)


def disabled_refs():
    return [ref(codex_auth() if a is None else a) for a in map(_acct, config().get('openai', {}).get('disabled') or [])]


def toggle_account(r, disabled):
    if r not in account_refs():
        raise KeyError('没有这个账号')
    off = [x for x in disabled_refs() if x != r] + ([r] if disabled else [])
    set_openai_list('disabled', off)


def import_account(name, auth):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,40}', name or ''):
        raise ValueError('名称只能用字母、数字、点、下划线和短横线（最多 40 个字符）')
    auth = json.loads(auth) if isinstance(auth, str) else auth
    t = auth['tokens']
    if not (t.get('access_token') and t.get('refresh_token')):
        raise ValueError('auth.json 里需要 tokens.access_token 和 tokens.refresh_token')
    f = HOME / 'accounts' / f'{name}.json'
    if f.exists():
        raise FileExistsError(f'已经有叫 {name} 的账号')
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(auth, fh, indent=2)
    set_accounts(account_refs() + [ref(f)])
    return f


def remove_account(r):
    refs = account_refs()
    if r not in refs:
        raise KeyError('没有这个账号')
    p = Path(r).expanduser()
    if p.resolve() == codex_auth().resolve():
        raise ValueError('Codex 自己的登录态不能删除，只能调整顺序')
    set_accounts([x for x in refs if x != r])
    if r in disabled_refs():
        set_openai_list('disabled', [x for x in disabled_refs() if x != r])
    if p.resolve().parent == (HOME / 'accounts').resolve():
        p.unlink(missing_ok=True)


def model_rows(cfg):
    return [{'slug': f'{pn}/{mid}', 'provider': pn, 'context': m.get('context'), 'images': bool(m.get('images')),
             'efforts': list(levels({'upstream': mid, **m})), 'fallback': m.get('fallback', [])}
            for pn, p in cfg.get('providers', {}).items() for mid, m in p.get('models', {}).items()]


def upstream_rows(cfg):
    def ping(kv):
        name, p = kv
        t0 = time.monotonic()
        try:
            with fetch('GET', p['base_url'].rstrip('/') + '/models', None,
                       {'Authorization': f"Bearer {p.get('api_key', '')}"}, timeout=8) as r:
                r.read()
                return {'provider': name, 'status': r.status, 'ms': round((time.monotonic() - t0) * 1000)}
        except (OSError, http.client.HTTPException) as e:
            return {'provider': name, 'error': str(e) or type(e).__name__}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(ping, cfg.get('providers', {}).items()))


def codex_wired():
    f = CODEX_HOME / 'config.toml'
    return f.exists() and any(MARK in ln for ln in f.read_text().splitlines())


def tail_log(n):
    n = max(1, min(n, 1000))
    try:
        with open(HOME / 'witches.log', 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            return f.read().decode(errors='replace').splitlines()[-n:]
    except OSError:
        return []


def managed():
    return os.getppid() == 1 or 'INVOCATION_ID' in os.environ  # launchd / systemd 拉起的，退出后会被重启


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

    def send_bytes(self, status, b, ctype):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

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

    def api(self, raw):
        port = self.server.server_port
        hosts = {f'{h}:{port}' for h in ('127.0.0.1', 'localhost', '[::1]')}
        origin = self.headers.get('Origin')
        if self.headers.get('Host') not in hosts or origin and origin not in {f'http://{h}' for h in hosts}:
            return self.send_json(403, {'error': 'bad host/origin'})  # 防 DNS rebinding / 跨站
        path, _, query = self.path.partition('?')
        authed = hmac.compare_digest(self.headers.get('X-Witches-Token', ''), panel_token())
        if path == '/api/usage' and self.command == 'GET':
            return self.send_json(200, usage_report(fresh='refresh=1' in query, labels=authed))
        if not authed:
            return self.send_json(401, {'error': '需要面板令牌：在终端运行 witches panel 打开面板'})
        try:
            body = json.loads(raw) if raw else {}
            cfg = config()
            key = (self.command, path)
            if key == ('GET', '/api/state'):
                pool = account_pool(cfg.get('openai', {}), every=True)
                off = set(disabled_refs())
                with _down_lock:
                    now = time.monotonic()
                    down = [round(max(0, _down.get(a, 0) - now)) for a in pool]
                return self.send_json(200, {
                    'pid': os.getpid(), 'uptime_s': round(time.time() - _started), 'port': port, 'managed': managed(),
                    'codex': {'wired': codex_wired(), 'config': str(CODEX_HOME / 'config.toml')},
                    'accounts': [{'ref': r, 'label': account_label(a), 'plan': account_plan(a), 'codex_login': a is None,
                                  'disabled': r in off, 'down_s': d} for r, a, d in zip(account_refs(), pool, down)],
                    'models': model_rows(cfg), 'vision': cfg.get('vision', []), 'route': cfg.get('route', {}),
                    'switch_note': cfg.get('switch_note', True),
                    'openai': {k: openai_options(cfg)[k] for k in OPENAI_DEFAULTS}})
            if key == ('GET', '/api/upstreams'):
                return self.send_json(200, {'upstreams': upstream_rows(cfg)})
            if key == ('GET', '/api/config'):
                f = HOME / 'config.toml'
                return self.send_json(200, {'text': masked_config(f.read_text() if f.exists() else ''), 'data': mask_data(cfg)})
            if key == ('PUT', '/api/config'):
                write_config(unmask_config(body['text']) if 'text' in body else dump_checked(unmask_data(body['data'])))
                return self.send_json(200, {'ok': True})
            if key == ('POST', '/api/accounts/toggle'):
                toggle_account(body['ref'], bool(body['disabled']))
                return self.send_json(200, {'ok': True})
            if key == ('POST', '/api/accounts'):
                f = import_account(body.get('name'), body.get('auth'))
                return self.send_json(200, {'ok': True, 'ref': ref(f)})
            if key == ('PUT', '/api/accounts'):
                order = body['order']
                if sorted(order) != sorted(account_refs()):
                    raise ValueError('账号列表和当前不一致，刷新后再调整')
                set_accounts(order)
                return self.send_json(200, {'ok': True})
            if key == ('DELETE', '/api/accounts'):
                remove_account(body['ref'])
                return self.send_json(200, {'ok': True})
            if key == ('POST', '/api/test'):
                return self.send_json(200, self.test_model(body['model'], port, cfg))
            if key == ('GET', '/api/log'):
                n = next((int(v) for k, _, v in (q.partition('=') for q in query.split('&')) if k == 'n' and v.isdigit()), 200)
                return self.send_json(200, {'lines': tail_log(n)})
            if key == ('POST', '/api/codex'):
                try:
                    edit_codex(port if body.get('on') else None)
                except SystemExit as e:
                    raise ValueError(str(e.code))
                return self.send_json(200, {'ok': True, 'wired': codex_wired()})
            if key == ('POST', '/api/restart'):
                if not managed():
                    return self.send_json(409, {'error': '不是由 launchd/systemd 拉起的，自己退出后不会重启；请用 witches install 常驻'})
                threading.Timer(0.5, os._exit, [0]).start()
                return self.send_json(200, {'ok': True})
            return self.send_json(404, {'error': 'unknown api'})
        except (ValueError, KeyError, TypeError, AttributeError) as e:  # TOMLDecodeError / JSONDecodeError 都是 ValueError
            return self.send_json(400, {'error': f'{type(e).__name__}: {e}'})
        except FileExistsError as e:
            return self.send_json(409, {'error': str(e)})

    def test_model(self, slug, port, cfg):
        """给第三方模型发一个极小请求（用本机 Codex 登录令牌，和 Codex 自己走同一条路）；GPT 不在这里测，免得花额度。"""
        if '/' not in slug or not lookup(cfg, slug):
            raise ValueError('只能测试配置里的第三方模型')
        h = {'Content-Type': 'application/json'}
        try:
            h['Authorization'] = 'Bearer ' + json.loads(codex_auth().read_text())['tokens']['access_token']
        except (OSError, ValueError, KeyError):
            pass
        body = json.dumps({'model': slug, 'stream': True, 'instructions': 'Reply with: ok',
                           'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'say ok'}]}]}).encode()
        t0 = time.monotonic()
        try:
            with fetch('POST', f'http://127.0.0.1:{port}/backend-api/codex/responses', body, h, timeout=60) as r:
                data = r.read()
                ok = r.status == 200 and b'response.completed' in data
                return {'ok': ok, 'status': r.status, 'ms': round((time.monotonic() - t0) * 1000),
                        **({} if ok else {'error': data[:200].decode(errors='replace')})}
        except (OSError, http.client.HTTPException) as e:
            return {'ok': False, 'error': str(e) or type(e).__name__}

    def route(self):
        self.response_started = False
        t0 = time.time()
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''  # 先读完请求体，keep-alive 连接上的下一个请求才不会错位
        prefix = next((p for p in OPENAI if self.path.startswith(p + '/') or self.path == p), None)
        if self.path == '/health':
            return self.send_json(200, health())
        if self.path.startswith('/api/'):
            return self.api(raw)
        if self.path in ('/', '/panel'):
            return self.send_bytes(200, PANEL.encode(), 'text/html; charset=utf-8')
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

    def passthrough(self, prefix, body, headers=None, label='', note=None, recovery=None, cancel=None):
        """GPT 首次输出前有限重试；其他路径只重试连接异常。所有响应保留原始字节。"""
        h = headers or {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        # 需要检查 SSE 的 GPT 请求使用明文响应，原始请求（包括 zstd）仍原样转发。
        if recovery is not None:
            h = {k: v for k, v in h.items() if k.lower() != 'accept-encoding'}
            h['Accept-Encoding'] = 'identity'
        url = OPENAI[prefix] + self.path[len(prefix):]
        started = time.monotonic()
        chain_id = uuid.uuid4().hex[:16]
        if cancel is not None:
            cancel.chain_id = chain_id
        retries = max(0, int((recovery or {}).get('max_retries', 2)))
        window = max(0, float((recovery or {}).get('retry_window', 60)))
        pool = account_pool(recovery or {})
        switches, acct = 0, None

        def failover(err, advice=None):
            """当前账号额度用完：记下恢复时间，还有别的账号可用就换（换号不占重试次数）。"""
            nonlocal switches
            if len(pool) < 2:
                return False
            mark_down(acct, quota_wait(err, advice))
            switches += any(usable(a) for a in pool)
            return any(usable(a) for a in pool)

        def delay(attempt, advice=None):
            nonlocal retry_stop
            wait = retry_after(advice)
            if wait is None:
                wait = random.uniform(0.5, 1) * min(2 ** (attempt + 1), 15)
            if attempt - switches >= retries:
                retry_stop = 'retry_limit'
                return None
            if time.monotonic() - started + wait >= window:
                retry_stop = 'retry_window'
                return None
            return wait

        for attempt in range(retries + max(1, len(pool))):
            acct, hh = choose(pool, h)
            r, pending, committed, wait = None, bytearray(), False, None
            probe, outcome = StreamProbe(), 'ok'
            request_id = '-'
            stage, error_class, delivered = 'connect', None, 0
            truncated_after_terminal = False
            retry_stop = None

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
                if cancel is not None:
                    cancel.check()
                r = fetch(self.command, url, body or None, hh, **({'cancel': cancel} if cancel is not None else {}))
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
                    if {str(err.get('code', '')), str(err.get('type', ''))} & QUOTA_CODES:
                        if failover(err, r.getheader('Retry-After')):
                            wait = 0
                    else:
                        wait = delay(attempt, r.getheader('Retry-After'))
                    if wait is not None:
                        raise RetryUpstream()
                if r.status in (204, 304) or self.command == 'HEAD':
                    begin()
                    return
                if not inspect:
                    begin()
                while True:
                    stage = 'read_stream'
                    if cancel is not None:
                        cancel.check()
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
                            elif {str(probe.failure.get('code', '')), str(probe.failure.get('type', ''))} & QUOTA_CODES \
                                    and failover(probe.failure, r.getheader('Retry-After')):
                                wait = 0
                            if wait is not None:
                                break
                        if probe.output or probe.failure is not None or len(pending) > PRELUDE_LIMIT:
                            begin()
                    else:
                        stage = 'write_stream'
                        self.chunk(data)
                        delivered += len(data)
                if wait is not None:
                    raise RetryUpstream()
                if not committed:
                    begin()
                stage = 'write_end'
                self.end()
                return
            except RetryUpstream:
                pass
            except STREAM_ERRORS as e:
                error_class = type(e).__name__
                client_cancelled = cancel is not None and cancel.cancelled.is_set()
                outcome = 'client_disconnect' if client_cancelled or stage.startswith('write_') else 'disconnect'
                if client_cancelled:
                    cancel.handled = True
                    retry_stop = 'client_cancelled'
                elif outcome == 'client_disconnect':
                    retry_stop = 'client_write_failed'
                # 客户端取消不重放；上游断流只允许在首次交付前恢复。
                if client_cancelled or stage.startswith('write_') or committed or self.response_started:
                    wait = None
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
                          'account': account_name(acct),
                          'committed': committed, 'request_id': request_id,
                          'chain_id': chain_id, 'stage': stage, 'error_class': error_class,
                          'terminal': probe.terminal_kind, 'delivered_bytes': delivered,
                          'elapsed': round(time.monotonic() - started, 3)}
                if outcome != 'client_disconnect' and probe.failure is not None and transient_error(probe.failure) and probe.output:
                    retry_stop = 'output_started'
                if probe.first_output_event is not None:
                    fields['first_output_event'] = probe.first_output_event
                if retry_stop is not None:
                    fields['retry_stop'] = retry_stop
                if cancel is not None:
                    fields['connection_route'] = cancel.connection_route
                    if stage == 'connect':
                        fields['connect_phase'] = cancel.connect_phase
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
                if cancel is None:
                    time.sleep(wait)
                else:
                    try:
                        cancel.wait(wait)
                    except ClientDisconnected:
                        cancel.handled = True
                        self.close_connection = True
                        self.log(f'{label} -> openai_cancel ' + json.dumps({
                            'chain_id': chain_id, 'attempt': attempt + 1, 'stage': 'retry_wait',
                            'retry_stop': 'client_cancelled', 'elapsed': round(time.monotonic() - started, 3)}))
                        return

    def gpt(self, prefix, body, cfg, headers=None, label='', note=None):
        opts = openai_options(cfg)
        gate = slot('@openai', opts)
        t0 = time.monotonic()
        with ClientWatch(self.connection) as cancel:
            try:
                acquired = gate.acquire(timeout=max(0, float(opts['queue_timeout'])), cancel=cancel)
            except ClientDisconnected:
                self.close_connection = True
                self.log(f'{label} -> openai_cancel ' + json.dumps({
                    'stage': 'queue', 'retry_stop': 'client_cancelled', 'elapsed': round(time.monotonic() - t0, 3)}))
                return
            if not acquired:
                self.log(f'{label} openai_queue_timeout waited={time.monotonic() - t0:.3f}s')
                return self.send_json(503, {'error': {'code': 'witches_queue_timeout', 'type': 'witches_error',
                                      'message': 'witches: GPT concurrency queue timed out'}}, {'Retry-After': '2'})
            try:
                return self.passthrough(prefix, body, headers, label, note, recovery=opts, cancel=cancel)
            finally:
                gate.release()
                if cancel.handled and cancel.cancelled_at is not None:
                    elapsed_ms = round((time.monotonic() - cancel.cancelled_at) * 1000, 3)
                    self.log(f'{label} -> openai_release ' + json.dumps({
                        'chain_id': cancel.chain_id, 'reason': 'client_cancelled',
                        'cancel_to_release_ms': elapsed_ms, **gate.snapshot()}))

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
    elif cmd == 'panel':
        url = f'http://127.0.0.1:{port}/panel#token={panel_token()}'
        print(url)
        try:
            subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', url], check=False)
        except OSError:
            pass  # 没有浏览器（远程服务器）：把地址复制出去用
    elif cmd == 'deploy' and len(args) > 1:
        deploy(args[1])
    else:
        print(__doc__)
        print('用法: witches serve|install|uninstall|status|panel [--port N] | deploy <ssh-host>')


if __name__ == '__main__':
    main()
