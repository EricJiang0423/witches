# witches

给 [Codex](https://github.com/openai/codex) 加第三方模型的单文件代理：零依赖、无状态，对 Codex 只改一行配置。

```
Codex ──openai_base_url──▶ witches (127.0.0.1:10777) ──┬─▶ chatgpt.com          GPT 模型：原样转发
                                                        ├─▶ 你的 Chat 上游        glm / kimi / deepseek / mimo …
                                                        └─▶ 你的 Responses 上游
```

## 为什么

[opencodex](https://github.com/lidge-jun/opencodex) 和 [magpie](https://github.com/yetone/magpie) 功能很全，但在 Codex remote（远程服务器开发）的场景里太重：每台机器都要装一整套，还要处理环境变量、令牌、shim、状态文件。witches 只做一件事，尽量不碰 Codex：

- **对 Codex 只改一行**：`config.toml` 顶部加一行 `openai_base_url`，不加 provider、不加模型目录、不改 features。Codex 继续用内置的 `openai` provider，所以**登录态不动、旧线程照常显示和继续**。
- **GPT 透传与恢复**：请求体字节级透传（连 zstd 压缩一起），保留 `x-codex-turn-state` 粘性路由及上游响应头。为检查流内错误，GPT 请求使用 `Accept-Encoding: identity`；响应体保留原始字节。明确为空的文本/推理启动条目保持缓冲，容量错误可在后台有限重试；首个实质内容、工具调用或未知事件立即交付，此后不重放。缓冲沿用 256 KiB 上限，不增加固定等待，也不存会话状态；witches 自己从不生成 429。
- **同一线程里随便切模型**：GPT → glm → GPT 都能接着聊。切回 GPT 时，witches 会把第三方留下的推理条目、陌生 id、压缩摘要清理成 OpenAI 认的样子。
- **单文件**：`witches.py` + `prompt.md`，只需要 [uv](https://docs.astral.sh/uv/)（会自动装 Python 3.14）。每台机器各跑各的，互不影响。

## 安装

前提：装了 uv，Codex 用 ChatGPT 账号登录过。

```bash
git clone https://github.com/EricJiang0423/witches.git && cd witches
mkdir -p ~/.witches && cp config.example.toml ~/.witches/config.toml   # 填上你的上游和 key
./witches.py install
```

`install` 做了这些事：

1. 把 `witches.py`、`prompt.md` 复制到 `~/.witches/`，并生成命令入口 `~/.local/bin/witches`
2. 注册常驻服务（Linux：`systemd --user`；macOS：launchd），会带上当前 shell 的 `http(s)_proxy`/`no_proxy`
3. 往 `~/.codex/config.toml` 顶部加一行 `openai_base_url = "http://127.0.0.1:10777/backend-api/codex"  # witches`（改之前备份为 `config.toml.witches-bak`）

已经在运行的 Codex 线程要重启 app-server 才会走 witches：`codex app-server daemon restart`（会打断正在跑的任务）。

卸载：`witches uninstall`（删掉那一行、停服务，`~/.witches` 保留）。

> 如果 `config.toml` 里已经有别人写的 `openai_base_url`（比如 opencodex），install 会拒绝安装。请先卸载那个工具。

### 远程服务器（Codex remote）

```bash
./witches.py deploy <ssh-host>
```

把脚本、提示词和配置同步到远端 `~/.witches/`，然后在远端用户的登录 shell（Linux 一般是 bash，macOS 是 zsh）里执行 `install`，这样能拿到 `.profile`/`.zprofile` 里的代理变量和 PATH。远端只需要装好 uv、并用 ChatGPT 登录过 Codex。

**每台远端可以配不同的模型**：Codex remote 的模型选择器显示的是那台远端 app-server 的模型列表，而每台机器的 witches 各读各的配置。`deploy <host>` 时，如果本机有 `~/.witches/hosts/<host>.toml` 就推它，否则推 `~/.witches/config.toml`。本机这些文件是唯一真源，`deploy` 会覆盖远端的配置。

```
~/.witches/
├── config.toml               默认（本机自己用，也是没有单独配置的远端用的）
└── hosts/
    ├── gpu-box.toml          deploy gpu-box 时用：比如只配内网能连到的模型
    └── cloud-dev.toml        deploy cloud-dev 时用：比如只配公网 API
```

## 配置

`~/.witches/config.toml`，改完立即生效（不用重启服务）。完整说明见 [config.example.toml](config.example.toml)。

```toml
port = 10777
vision = ["gateway/kimi-k3", "gateway/glm-5.3-flash", "mimo/mimo-v2.6-pro"] # 依次回退，先验证上游收图能力

[openai]                            # GPT 通道独立并发限制；不与第三方 provider 共用
max_concurrency = 3
queue_timeout = 30
max_retries = 2
retry_window = 60

[route]                              # 可选：子代理/审查改用第三方模型
collab_spawn = "gateway/glm-5.3"

[providers.gateway]                  # provider 名就是模型前缀
base_url = "https://your-gateway.example.com/v1"
api_key = "sk-..."
wire = "chat"                        # chat（默认）| responses
max_concurrency = 6

[providers.gateway.models]
"glm-5.3" = { context = 200000, fallback = ["mimo/mimo-v2.6-pro"] }
"glm-5.3-flash" = { context = 128000, images = true } # 仅用于已验证收图的上游
"kimi-k3" = { context = 262144, images = true }
```

配好之后，Codex 的模型选择器（CLI 的 `/model` 和桌面端的下拉框都一样）会在 GPT 模型后面列出 `gateway/glm-5.3`、`mimo/mimo-v2.6-pro` 这类条目，每个都带自己的推理档位；配了 `vision` 的纯文本模型也会显示为能收图。显示名默认是 `provider/model`，可以用 `name` 改。

## 功能

| 功能 | 说明 |
|---|---|
| Chat 上游 | Responses ↔ Chat Completions 双向翻译：流式输出、并行工具调用、freeform 工具（`apply_patch`）、MCP 命名空间工具、`reasoning_content` 回传（kimi/deepseek 思考模式需要）、推理强度映射 |
| 明文代理通信 | Chat 模型生成的 `collaboration.spawn_agent` / `send_message` / `followup_task` 调用显式带 `encrypted_function_args = []`，符合 Codex 的明文参数协议，避免把普通代理消息存成密文后触发审批解密失败。GPT 透传、原生 Responses 上游及已有加密标记保持原样 |
| Responses 上游 | 原样转发，只给条目 id 打 `_wch` 标记、去掉别家的加密推理 |
| 压缩 | 接住 Codex 的远程压缩（`compaction_trigger`）：把对话转成纯文本交给模型写摘要，再包成 compaction 条目还给 Codex；之后切回 GPT 时自动还原成明文 |
| 推理档位 | 按官方文档内置各家档位：GLM-5.x / Kimi K3 是 low·high·max，DeepSeek V4 是 none·low·high·max（none 关闭思考），MiMo 是 low·medium·high。Codex 发来不支持的档位时取最近的一档（距离相同往高取），比如给 GLM 发 `medium` 会变成 `high`，因为 GLM 收到 `medium` 会直接报错。每个模型都带 Codex 原生的 **Ultra**：主代理按该模型最高档推理，并由 harness 主动拉起子代理群 |
| 切换提示 | 线程里换模型后的第一次回答，前面会多一行 `✓ 已切换到 gateway/glm-5.3`（切回 GPT 也有）。这行只给人看，之后发给任何模型前都会删掉；配置里写 `switch_note = false` 可以关掉 |
| 子代理/审查改派 | `[route]` 按 Codex 的 `x-openai-subagent` 请求头（`collab_spawn` / `guardian` / `review` / `memory_consolidation`）把原本发给 GPT 的请求改派给第三方模型，不用改 Codex 配置 |
| 看图代理 | 配了 `vision` 后，纯文本模型也会声明能收图；图片（用户贴的截图、`view_image`、浏览器/computer-use 截图）先交给 vision 模型用低推理强度转写成文字（`vision` 可写成列表依次回退），结果按图片哈希缓存在 `~/.witches/vision/`，同一张图只看一次，重启也不用重看。实测一张 1570×1312 的高密度中文截图：kimi-k3 约 33 秒转写完，第二次 1.7 秒 |
| 429 规避 | `fallback` 遇到 429/5xx/连不上就在首字节前换下一个模型；整条链最多走 3 轮，按 `Retry-After` 或指数退避；`max_concurrency` 让突发请求排队 |
| GPT 恢复 | `[openai]` 默认同时运行 3 个请求，排队最多 30 秒。HTTP 临时错误、流内明确的容量/限流错误、输出前断流最多额外重试 2 次；在 60 秒窗口内安排带随机退避的重试，支持秒数与 HTTP 日期格式的 `Retry-After`。等待要求超出窗口就把最后的错误交给 Codex，不缩短等待时间 |
| 代理 | 遵循 `http(s)_proxy`/`no_proxy`；私网 IP（含 Tailscale `100.64.0.0/10`）总是直连 |
| 安全 | 只有带着本机 Codex 登录令牌（`auth.json`）的请求才能用第三方模型；只监听 127.0.0.1 |

## 命令

```
witches install [--port N]     本机安装并接入 Codex
witches uninstall              移除那一行配置并停服务
witches status                 接入状态、服务、各上游连通性、route
witches serve [--port N]       前台运行（调试用）
witches deploy <ssh-host>      同步到远端并安装
```

排查问题时可以设置 `WITCHES_DUMP=<目录>` 再前台运行 `witches serve`，每个请求体都会存一份到这个目录（里面是完整对话内容，用完记得删）。

日志在 `~/.witches/witches.log`。GPT 每次尝试记录 HTTP 状态、流结果、错误码、request ID、耗时、是否已经交付事件及下次重试延迟，例如：

```text
gpt-6.1-sol -> openai {"http": 200, "outcome": "stream_error", "attempt": 1, "committed": false, "request_id": "...", "elapsed": 1.2, "code": "server_overloaded", "retry_in": 1.5}
```

流诊断不记录对话、错误 message、凭据或 Cookie。第三方模型仍有 `gateway/glm-5.3 ok 6.2s` 和看图转写日志，流内失败单独记为 `stream_error`。排队超时使用独立的 `witches_queue_timeout`，与上游满载区分。

GPT 日志中的 `chain_id` 关联同一请求的全部尝试，`request_id` 是每次尝试的上游 ID。`stage`、`error_class`、`terminal` 和 `delivered_bytes` 用于区分上游连接/读取异常与客户端写入失败（`client_disconnect`）；客户端取消不重放。`response.incomplete` 单独记为 `incomplete`，不会记成成功。`invalid_encrypted_content` 额外记录加密字段的结构计数及合法的 `input` 字段路径，不记录或删除加密内容，不修改审批策略。

明文代理通信修复只影响新生成的 Chat 工具调用；不会把历史中的 `encrypted_content` 猜测转换成明文。已经保存了错误密文类型的旧审批上下文，仍可能需要由 Codex 重建。

只缓冲 `response.created` / `in_progress` / `queued` 和心跳等启动事件，缓冲上限 256 KiB；任何输出条目（包括推理、工具）、未知事件或超出缓冲上限都会提交响应，此后代理不再重放。授权、配额耗尽和无效请求错误不因流内重试而掩盖。完整终态前断流时关闭客户端连接，不伪造成功结束，让 Codex 识别截断；已经收到完整终态时，即使上游缺 HTTP 结束块，也不重放。

**多账户（可选）**：`[openai] accounts = ["~/.witches/accounts/b.json"]`，每个是单独 `CODEX_HOME=… codex login` 得到的 auth.json。Codex 自己的登录态用 `"primary"` 表示，不写就排第一，可以放在列表任意位置（例如 `["plus.json", "primary"]` 是先用 Plus、最后才用 Codex 登录的账号）；轮到 primary 时请求头原样透传。当前账号报 `usage_limit_reached`（HTTP 429 或流内 `response.failed`）且还没有任何输出时，witches 换掉 `Authorization` / `chatgpt-account-id` 并去掉 `x-codex-turn-state`，用下一个账户重放，换号不占 `max_retries`。用完的账户按上游给的 `resets_in_seconds` 记下恢复时间（没有就 10 分钟），期间新请求直接走还有额度的账户；全部用完则把主账号的原始错误交给 Codex。access token 过期前会自动用 refresh token 续并写回该文件；`/health` 的 `openai.accounts` 显示各账户还要多久恢复。

**管理面板**：终端运行 `witches panel` 会打开 `http://127.0.0.1:<port>/panel#token=…`（令牌在 `~/.witches/panel.token`，0600；没有令牌只能看额度）。五个页签：

- **状态**：各 GPT 账号（显示登录邮箱）的 5 小时 / 7 天剩余额度和重置倒计时、DeepSeek 余额、上游连通性检测、Codex 接入的启用/停用、重启 witches。
- **账号**：调整使用顺序、启用/停用某个账号（停用的保留在列表里但不参与轮换，配置里是 `[openai] disabled = [...]`）、粘贴 auth.json 添加账号、删除。
- **模型**：列出所有第三方模型，一键测试能不能通。
- **配置**：表单（端口、vision、GPT 通道、子代理改派、provider 和模型）和 TOML 源码两种方式；api_key 显示成 `••••xxxx#hash`，不改就不会动；保存前校验并留 `config.toml.bak`。表单保存会重新生成文件（不保留注释），要保留注释请用源码模式。
- **日志**：把日志解析成表格，可按 GPT / 第三方 / 出错筛选、搜索、自动刷新，点一行看原文。

写操作都要令牌，并校验 Host / Origin（防 DNS rebinding 和跨站）；页面适配大屏。通过反向代理用域名访问时，在配置里加 `[panel] hosts = ["witches.example.com"]` 放行该域名（代理只应转发 `/panel`、`/api/`、`/health`，不要把 `/backend-api/` 和 `/v1/` 暴露出去）。额度的 JSON 接口：

```
GET /api/usage            各账号 {account, plan, limit_reached, down_s, windows:[{name:"5h"|"7d", used_percent, remaining_percent, resets_in_s}]}，
                          和 providers 余额 [{provider, balances:[{currency,total}]}]；成功结果缓存 60 秒
GET /api/usage?refresh=1  跳过缓存
GET /health               进程、并发、各账号 down_s（只含参与轮换的账号）
```

额度来自 ChatGPT 自己的 `/wham/usage`（Codex 同一个接口）。这两个接口只读，匿名访问只返回账号文件名（Codex 自己的登录态叫 `codex-login`），带面板令牌才返回邮箱；同样只监听 127.0.0.1。

`max_concurrency = 0` 关闭并发限制，`max_retries = 0` 关闭 GPT 内部重试。并发设置可热更新；`retry_window` 限制安排重试的时间，不限制一次已开始的模型生成。多台机器或多个 witches 进程共用账号时，需要自行协调总并发。

GPT 请求会在排队、连接/读取上游及重试退避期间监视客户端连接。Codex 取消任务并断开连接后，witches 停止后续重试、shutdown 已建立的上游 socket，及时释放并发名额；不再等模型输出后才通过 BrokenPipe 发现取消。监视器不读取后续请求内容，不提前发送 HTTP 头或改写 SSE，也不限制仍在线客户端的正常生成时长。尚未建立 socket 的 DNS/连接阶段仍受系统连接超时约束，返回后会再次检查取消。

TLS socket 在握手开始前即交给取消监视器，因此直连和 CONNECT 代理的 TLS 握手停顿都可被取消打断，仍保留证书校验和原来的目标 SNI。GPT 尝试日志增加 `connection_route=direct/proxy`；在 `stage=connect` 时增加 `connect_phase`，区分 `tcp_or_proxy_connect`、`tls_handshake`、`send_request`、`response_headers`。字段不包含代理地址、凭据或请求内容。未收到上游 HTTP 响应时的 TLS/连接异常在重试耗尽后由 witches 返回 502；这类故障与上游明确返回的 capacity/429 不同，应先排查出网代理及 TLS 链路。

尝试日志的 `retry_stop` 区分 `client_cancelled`（因下游 EOF/RST 终止请求）、`client_write_failed`（写回失败）、`retry_limit`、`retry_window` 和 `output_started`。完整响应交付后的正常关闭不会被标成取消，也不会覆盖已有的重试停止原因。排队/退避期间取消单独记录 `-> openai_cancel`，`stage` 为 `queue` / `retry_wait`；退避取消携带相同的 `chain_id`，不会被计作一次新的上游尝试。客户端主动取消不代表容量重试已耗尽，也不能据 BrokenPipe 的时间推断客户端的实际超时时间。

因检测到客户端取消而终止的在途请求，在实际释放并发名额后单独记录 `-> openai_release`：包含相同的 `chain_id`、`reason=client_cancelled`、`cancel_to_release_ms` 和释放后的 `active` / `waiting` / `runtime_limit` 快照。耗时从代理检测到 EOF/RST 开始计算，不包含监视器检测前的时间（轮询间隔约 100 ms）。排队取消尚未占用名额，不记录释放事件；完整响应后的关闭也不记录。快照可能已经包含刚被唤醒的新请求，因此判断某条请求是否释放应按 `chain_id` 关联，不能要求总 `active` 一定下降。

本机 `GET /health` 返回进程 PID、GPT 配置及 `active` / `waiting` / `runtime_limit`，可用于重启后核验运行进程、并发和排队。配置值来自当前文件，`runtime_limit` 是上一次 GPT 请求应用的上限；仅检查健康不会修改闸门或触发模型请求。

## 已知限制

- **witches 停了，GPT 也用不了**：这是 Codex 的限制。provider 按线程绑定、模型没有自己的 base URL，所以"同一线程能切第三方模型"和"GPT 完全不经过代理"只能二选一。witches 选了前者，由 systemd/launchd 守护（`Restart=always` / `KeepAlive`）。
- **GPT 走 HTTP 而不是 WebSocket**：witches 对 WebSocket 升级请求回 426，Codex 会立刻退回 HTTP。
- **用 witches 模型压缩过的线程依赖 witches**：压缩条目是 witches 格式，只有 witches 能把它还原给 GPT。卸载前先在这类线程里用 GPT 执行一次 `/compact`。
- **被改派的子代理在界面上仍显示 GPT 的名字**：实际由 `[route]` 指定的模型应答，看 witches 日志里的 `[collab_spawn]` 标记。
- 只支持 http 代理（不支持 socks）；需要 Python 3.14（标准库的 `compression.zstd`，uv 会自动安装）。

## 开发

```bash
./test_witches.py      # 端到端自检：假 ChatGPT 后端 + 假上游，覆盖透传、翻译、压缩、fallback、改派、看图
uv run --script test_recovery.py  # 容量错误、首输出边界、断流、Retry-After、并发与排队超时
```

## 致谢

- 设计参考了 [opencodex](https://github.com/lidge-jun/opencodex) 和 [magpie](https://github.com/yetone/magpie)，没有复制它们的代码。
- `prompt.md` 和压缩提示词来自 [openai/codex](https://github.com/openai/codex)（Apache-2.0），见 [NOTICE](NOTICE)。

MIT License
