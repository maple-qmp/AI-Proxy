# AI-Proxy

本地 LiteLLM 网关，独立登录 ChatGPT，并使用 CodeBuddy 桌面端登录态。提供 `/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 和 `/healthz`。LiteLLM 固定为 `1.106.1`，依赖锁定在 `uv.lock`。

## 安装与运行

需要 Python 3.12 和 `uv`：

```sh
cd /Users/maple/AI-Proxy
uv sync --extra test
.venv/bin/python -m ai_proxy login
.venv/bin/python -m ai_proxy
```

登录命令会打开浏览器，使用 `127.0.0.1:1455/auth/callback` 接收授权结果。浏览器无法访问回调时，可在终端粘贴完整的回调 URL。ChatGPT 凭据和独立设备 ID 保存在 `~/.config/ai-proxy/`，权限为 `0600`；凭据过期时服务自动刷新并原子写回。CodeBuddy 仍需先在桌面端登录。服务默认只监听 `127.0.0.1:8787`。无需从其他项目复制凭据。

启动及登录时读取 `~/.config/ai-proxy/service.env`，已有进程环境变量优先。支持 `HOST`、`PORT`、`GATEWAY_API_KEY`、`GATEWAY_AUTH_FILE`、`CODEBUDDY_AUTH_FILE`、`CODEBUDDY_PROMPT_COMPAT`、`CODEBUDDY_DEBUG`、`SAFEGUARDS_CLASSIFIER_MODEL` 以及下文控制台相关变量。可通过 `SERVICE_ENV_FILE` 指向另一个配置文件。`GATEWAY_AUTH_FILE` 可覆盖默认的 ChatGPT 凭据路径，登录命令和服务须使用相同配置。非回环地址必须设置 `GATEWAY_API_KEY`。`/healthz` 与 `/dashboard` 页面无需密钥；其他接口要求 Bearer 密钥，Messages 也接受 `x-api-key`。

模型目录按登录身份缓存 5 分钟。同名模型优先使用 ChatGPT；显式 `codebuddy/<模型 ID>` 使用 CodeBuddy。

## 本地控制台

服务启动后访问 <http://127.0.0.1:8787/dashboard>，可以查看：

- **LLM 日志**：每次网关请求的记录，含完整请求与响应正文，可按时间范围、状态、Provider、端点、客户端、模型筛选，支持分页与详情查看。
- **可用模型**：按当前账号实时发现的两个上游模型目录，可强制刷新。
- **登录状态**：ChatGPT 与 CodeBuddy / WorkBuddy 的凭据状态、到期倒计时与凭据文件位置，并可在线刷新 token。

日志保存在 SQLite（默认 `~/.config/ai-proxy/logs.db`，文件权限 `0600`，目录 `0700`），WAL 模式，默认保留最近 5000 条。写日志不会阻塞请求：队列满时丢弃并在概览里计数。

**日志正文包含完整的 prompt 与响应内容**，仅保存在本机，页面不会外发。如果网关设置了 `GATEWAY_API_KEY`，页面本身可以直接打开，但 `/dashboard/api/*` 需要密钥；页面会把密钥保存在当前标签页的 `sessionStorage`，不写入磁盘。

控制台相关配置（可写入 `service.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHBOARD_ENABLED` | `1` | 设为 `0` 时不注册控制台页面与接口 |
| `LOG_ENABLED` | `1` | 设为 `0` 时不记录请求日志 |
| `LOG_DB_PATH` | `~/.config/ai-proxy/logs.db` | SQLite 路径 |
| `LOG_MAX_ROWS` | `5000` | 保留条数（100–1000000） |
| `LOG_BODY_LIMIT_BYTES` | `262144` | 单条请求/响应正文上限，超出标记截断 |
| `LOG_QUEUE_SIZE` | `256` | 日志写队列长度（8–10000） |

## Claude Code

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY="${GATEWAY_API_KEY:-local}"
export ANTHROPIC_MODEL=codebuddy/glm-5.3-flash
claude
```

若没有设置 `GATEWAY_API_KEY`，`ANTHROPIC_API_KEY` 只作为客户端占位值。模型 ID 请以 `curl http://127.0.0.1:8787/v1/models` 的结果为准。Claude Code 可能对自定义模型打印 `unrecognized_model` 提示，但请求仍可正常发送。

Claude Code 的普通对话、图片输入、文本流、工具调用、工具结果续答、thinking 和工具搜索请求优先交由 LiteLLM 的 Anthropic Messages 转换处理。LiteLLM 把 thinking 设置映射成上游的 reasoning effort，并把上游 reasoning 转回 thinking 内容块；历史 thinking 块也可随会话继续传入。thinking 内容的形式取决于模型：实测 CodeBuddy `glm-5.3-flash` 返回 thinking 文本，ChatGPT `gpt-6.1-sol` 返回 redacted thinking。Auto 模式的 `safeguards` 结果由独立分类调用补充；分类失败时返回 `unavailable`。CodeBuddy 不支持强制指定某个工具；可用 `auto` 或 `any`。

两个目标上游均没有 Anthropic 原生工具搜索。网关在 LiteLLM 完成转换之后移除工具搜索声明，把本次请求中的延迟加载工具作为普通工具发送给上游。这样 Claude Code 能调用这些工具并继续对话，但不会获得 Anthropic 服务端的搜索排序与 `tool_reference` 事件；工具目录很大时会增加上下文用量。其他扩展参数尽量交由 LiteLLM 与目标上游处理，若上游不支持则返回其错误。

LiteLLM 的[Claude Code 兼容矩阵](https://docs.litellm.ai/docs/claude_code_compatibility)没有覆盖这里使用的订阅和 CodeBuddy 路径，因此以本项目的模拟上游与真实请求测试为准。

ChatGPT 订阅上游只接受流式 Responses 请求，网关可将其收集为普通 JSON 响应。ChatGPT 的 Chat Completions 先用 LiteLLM 转换输入，再使用原生 Responses 上游；CodeBuddy 的 Responses 和 Messages 由 LiteLLM 转成 Chat Completions。非流式请求需要等上游完成后才收到响应。断开流式连接时，网关会关闭相应上游流。

## CodeBuddy 提示兼容

只有最终发往 CodeBuddy `chat/completions` 的 system 文本会按旧项目规则处理：精确指纹、开头 billing header、最长词优先且有词边界的安全术语和客户端品牌词。用户消息、助手历史、工具内容以及 ChatGPT 请求不改写。设置 `CODEBUDDY_PROMPT_COMPAT=0` 可关闭。`CODEBUDDY_DEBUG=1` 只记录命中类别、状态、耗时和脱敏后的上游错误分类；`11128` 标为 `upstream_rejected_11128`。

## 验证

```sh
uv run --extra test pytest -q
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/v1/models
```

## macOS Homebrew service

与 chatgpt-gateway 一样，使用本地 formula 注册当前项目目录，由 `brew services` 管理。先运行 `uv sync`，再安装并启动：

```sh
uv sync --extra test
.venv/bin/python service/homebrew_service.py
brew services start local/ai-proxy/ai-proxy
curl --fail http://127.0.0.1:8787/healthz
```

日常使用 `brew services list`、`brew services restart local/ai-proxy/ai-proxy`、`brew services stop local/ai-proxy/ai-proxy`。服务使用项目的 `.venv` 和 `~/.config/ai-proxy/service.env`，不会复制登录凭据。项目移动后重新运行安装脚本并重启服务。日志位于 `~/Library/Logs/ai-proxy/`。卸载时先停止服务，再运行 `brew uninstall local/ai-proxy/ai-proxy` 和 `brew untap local/ai-proxy`。

仅生成安装资料进行检查：`.venv/bin/python service/homebrew_service.py --prepare-only /tmp/ai-proxy-service-check`。
