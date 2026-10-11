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
- **GPT 原样转发**：请求体字节级透传（连 zstd 压缩一起），请求头/响应头原样往返（包括 `x-codex-turn-state` 粘性路由）。不存状态、不 replay，witches 自己从不生成 429。
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
vision = ["gateway/kimi-k3", "mimo/mimo-v2.6-pro"]   # 可选：纯文本模型的"眼睛"，依次回退

[route]                              # 可选：子代理/审查改用第三方模型
collab_spawn = "gateway/glm-5.3"

[providers.gateway]                  # provider 名就是模型前缀
base_url = "https://your-gateway.example.com/v1"
api_key = "sk-..."
wire = "chat"                        # chat（默认）| responses
max_concurrency = 6

[providers.gateway.models]
"glm-5.3" = { context = 200000, fallback = ["mimo/mimo-v2.6-pro"] }
"kimi-k3" = { context = 262144, images = true }
```

配好之后，Codex 的模型选择器（CLI 的 `/model` 和桌面端的下拉框都一样）会在 GPT 模型后面列出 `gateway/glm-5.3`、`mimo/mimo-v2.6-pro` 这类条目，每个都带自己的推理档位；配了 `vision` 的纯文本模型也会显示为能收图。显示名默认是 `provider/model`，可以用 `name` 改。

## 功能

| 功能 | 说明 |
|---|---|
| Chat 上游 | Responses ↔ Chat Completions 双向翻译：流式输出、并行工具调用、freeform 工具（`apply_patch`）、MCP 命名空间工具、`reasoning_content` 回传（kimi/deepseek 思考模式需要）、推理强度映射 |
| Responses 上游 | 原样转发，只给条目 id 打 `_wch` 标记、去掉别家的加密推理 |
| 压缩 | 接住 Codex 的远程压缩（`compaction_trigger`）：把对话转成纯文本交给模型写摘要，再包成 compaction 条目还给 Codex；之后切回 GPT 时自动还原成明文 |
| 子代理/审查改派 | `[route]` 按 Codex 的 `x-openai-subagent` 请求头（`collab_spawn` / `guardian` / `review` / `memory_consolidation`）把原本发给 GPT 的请求改派给第三方模型，不用改 Codex 配置 |
| 看图代理 | 配了 `vision` 后，纯文本模型也会声明能收图；图片（用户贴的截图、`view_image`、浏览器/computer-use 截图）先交给 vision 模型用低推理强度转写成文字（`vision` 可写成列表依次回退），结果按图片哈希缓存在 `~/.witches/vision/`，同一张图只看一次，重启也不用重看。实测一张 1570×1312 的高密度中文截图：kimi-k3 约 33 秒转写完，第二次 1.7 秒 |
| 429 规避 | `fallback` 遇到 429/5xx/连不上就在首字节前换下一个模型；整条链最多走 3 轮，按 `Retry-After` 或指数退避；`max_concurrency` 让突发请求排队 |
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

日志在 `~/.witches/witches.log`，每个请求一行：`gpt-6.1-sol -> openai 200`、`gateway/glm-5.3 ok 6.2s`、`vision mimo/mimo-v2.6-pro 6.7s ok`。

## 已知限制

- **witches 停了，GPT 也用不了**：这是 Codex 的限制。provider 按线程绑定、模型没有自己的 base URL，所以"同一线程能切第三方模型"和"GPT 完全不经过代理"只能二选一。witches 选了前者，由 systemd/launchd 守护（`Restart=always` / `KeepAlive`）。
- **GPT 走 HTTP 而不是 WebSocket**：witches 对 WebSocket 升级请求回 426，Codex 会立刻退回 HTTP。
- **用 witches 模型压缩过的线程依赖 witches**：压缩条目是 witches 格式，只有 witches 能把它还原给 GPT。卸载前先在这类线程里用 GPT 执行一次 `/compact`。
- **被改派的子代理在界面上仍显示 GPT 的名字**：实际由 `[route]` 指定的模型应答，看 witches 日志里的 `[collab_spawn]` 标记。
- 只支持 http 代理（不支持 socks）；需要 Python 3.14（标准库的 `compression.zstd`，uv 会自动安装）。

## 开发

```bash
./test_witches.py      # 端到端自检：假 ChatGPT 后端 + 假上游，覆盖透传、翻译、压缩、fallback、改派、看图
```

## 致谢

- 设计参考了 [opencodex](https://github.com/lidge-jun/opencodex) 和 [magpie](https://github.com/yetone/magpie)，没有复制它们的代码。
- `prompt.md` 和压缩提示词来自 [openai/codex](https://github.com/openai/codex)（Apache-2.0），见 [NOTICE](NOTICE)。

MIT License
