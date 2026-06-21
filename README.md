# mail-bot

一个跑在 Docker Compose 里的个人邮件助手：Telegram bot + Web 看板。它从专用 Gmail 邮箱读取邮件，先在本地脱敏，再调用 OpenAI-compatible LLM（默认 DeepSeek）解析，把描述同一件事的多封邮件聚合成「事件」并结构化写入 SQLite。事件通过 Telegram 即时提醒（按优先级）和每天 09:00 的中文日报推送，同时呈现在一个公网可访问（经 cloudflared）的 Kanban 看板上，用于持久化浏览、排序和必要的编辑。

## 功能

- Gmail OAuth 只读收信，使用 `https://www.googleapis.com/auth/gmail.readonly`。
- 默认每 120 秒轮询 Gmail `historyId`，首次启动会回补最近 2 天邮件。
- 本地轻量脱敏：邮箱、电话、URL、IP、信用卡、SSN、token、账号类字段等会替换成占位符。
- LLM 输出按“不可靠 JSON”处理：JSON mode 只是辅助，代码会抽取 JSON、用 Pydantic 校验、把错误回传重试，最后给出保守 fallback。
- SQLite 保存原始标题、Gmail ID、脱敏正文和结构化分析；不保存原始正文或原始 MIME。
- 事件聚合：把描述同一件事的多封邮件合并成一个“事件”。即时提醒会让 LLM 判断这封邮件是否属于某个最近的开放事件，如果是就展示事件背景 + 本次更新；否则新建事件。窗口和数量由 `EVENT_WINDOW_DAYS=7`、`EVENT_MATCH_MAX_OPEN=12` 控制。
- 优先级（P0/P1/P2）按 LLM 重要性自动映射：重要性 ≥ `PRIORITY_P0_IMPORTANCE_MIN`(5) → P0，≥ `PRIORITY_P1_IMPORTANCE_MIN`(4) → P1，其余 → P2。事件更新时 P0 发带通知的 Telegram、P1 发静默消息、P2 不发。重要性低于 `EVENT_CREATE_IMPORTANCE_MIN`(3) 的邮件不建事件。
- Web 看板：事件以 `待处理 / 进行中 / 已完成` 三列呈现，可拖拽改列、列内排序、编辑标题/概要/优先级，并用合并、拆分、单事件重跑 LLM 来纠正聚合错误。详见下文。
- 每天 09:00 总结过去 24 小时重要邮件：LLM 先把邮件按事件聚类，再每个事件单独发一条 Telegram 消息（先发一条总览），Telegram 通知打开。
- 所有 Telegram 消息使用 Telegram 支持的 HTML 富文本（加粗 + 斜体）。

## 1. 准备 Telegram

1. 找 `@BotFather` 创建 bot，拿到 `TELEGRAM_BOT_TOKEN`。
2. 给 bot 发一条消息。
3. 获取你的 chat id 和 user id：

   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates"
   ```

4. 记录：
   - `message.chat.id` -> `TELEGRAM_CHAT_ID`
   - `message.from.id` -> `TELEGRAM_ALLOWED_USER_IDS`

`TELEGRAM_ALLOWED_USER_IDS` 可以写多个，用逗号分隔。bot 会忽略不在白名单里的用户。

## 2. 准备 Gmail OAuth

1. 打开 Google Cloud Console，创建或选择一个项目。
2. 启用 Gmail API。
3. 配置 OAuth consent screen。个人使用可以保持测试模式，并把你的 Gmail 账号加入 test users。
4. 创建 OAuth Client ID：
   - Application type 选择 `Desktop app`
   - 下载 JSON
5. 把下载的文件放到：

   ```text
   secrets/google_credentials.json
   ```

## 3. 配置环境变量

复制模板：

```bash
cp .env.example .env
```

至少填写：

```dotenv
TELEGRAM_BOT_TOKEN=123456:xxxx
TELEGRAM_CHAT_ID=123456789
TELEGRAM_ALLOWED_USER_IDS=123456789

LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

如果你不用 DeepSeek，只要服务兼容 OpenAI Chat Completions，就改 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。

邮件解析失败会按 `EMAIL_RETRY_MAX_ATTEMPTS=5`、`EMAIL_RETRY_BACKOFF_SECONDS=300`、`EMAIL_RETRY_MAX_BACKOFF_SECONDS=3600` 做有限次数指数退避重试；超过上限后进入终态 `error`，避免单封坏邮件无限调用 LLM。

## 4. Gmail 首次授权

启动 OAuth 授权命令：

```bash
docker compose run --rm --service-ports bot uv run --no-dev mail-bot auth-gmail
```

终端会打印 Google 授权 URL。复制到浏览器打开，选择 bot 专用 Gmail，授权后回调到本机 `localhost:8080`。成功后会写入：

```text
data/google_token.json
```

这个 token 是敏感文件，不会提交到 git。

## 5. 启动 bot

```bash
docker compose up -d --build
```

看日志：

```bash
docker compose logs -f bot
```

停止：

```bash
docker compose down
```

默认的 `docker compose up -d` **只启动 `bot`**（Telegram bot + Gmail 轮询 + 日报），不需要任何看板配置。

看板和隧道放在 `kanban` profile 里，需要时再一起拉起：

```bash
docker compose --profile kanban up -d --build
```

这会额外启动：

- `web`：Kanban 看板 Web 服务（`mail-bot web`，容器内监听 8000，不直接对宿主机暴露）
- `cloudflared`：把看板经 Cloudflare 命名隧道暴露到公网

启用 `kanban` profile 前，`.env` 必须填好 `WEB_PASSWORD`、`WEB_SESSION_SECRET`，以及 `CLOUDFLARE_TUNNEL_TOKEN`（否则 `web`/`cloudflared` 会反复重启）。三个容器共享 `./data` 卷里的同一个 SQLite（WAL + busy_timeout，支持 bot 写、web 读 + 少量编辑）。

## Web 看板

看板用来持久化、聚合地浏览邮件事件，操作限于浏览、排序和必要的编辑。

### 启动与鉴权

看板（`kanban` profile）需要这些环境变量（见 `.env`）：

```dotenv
WEB_PASSWORD=设一个访问密码
WEB_SESSION_SECRET=openssl rand -hex 32 生成的随机串
WEB_PORT=8000
CLOUDFLARE_TUNNEL_TOKEN=从 Cloudflare Zero Trust 拿到的隧道 token
```

鉴权是纵深防御的两层：边缘的 **Cloudflare Access** + 应用层的 **密码登录**（session cookie）。即使没接 Access，应用层密码也始终生效。

只想跑 bot、不开看板：用默认的 `docker compose up -d`，上面这些都不用填。

本地不经 Docker 调试看板（只需 `WEB_PASSWORD` + `WEB_SESSION_SECRET`）：

```bash
uv run mail-bot web   # 然后访问 http://localhost:8000
```

### 历史邮件补建事件

事件聚合是从启用后开始的。升级前已经处理过、但还没有事件的历史重要邮件，可以用一次性命令补建（按时间顺序跑 LLM 归并，不发通知，需要 `LLM_API_KEY`）：

```bash
docker compose run --rm bot uv run --no-dev mail-bot backfill-events
# 或本地： uv run mail-bot backfill-events
```

只补 `已处理` 且重要性 ≥ `EVENT_CREATE_IMPORTANCE_MIN` 的邮件，已经在事件里的会跳过，可重复运行。

### 通知语义

事件创建是持久、幂等的；即时 Telegram 提醒是 best-effort。万一进程在“已建事件、未发提醒”之间崩溃，重试不会重复建事件、但也不会补发那条即时提醒——每天 09:00 的日报按收件时间覆盖该邮件，作为兜底。

### 看板列与优先级

- 列（status）：`待处理 / 进行中 / 已完成`，拖拽改列、列内拖拽排序。
- 优先级（priority）：`P0 / P1 / P2`，与列正交，决定通知（P0 带通知、P1 静默、P2 不发）。新事件按重要性自动映射，手动改过之后不再被自动覆盖。

### 事件生命周期

- 新邮件命中已有事件 → 更新并按优先级路由通知；若命中的是已完成/已归档事件，会自动重开到「待处理」。
- 无命中 → 新建事件，进入「待处理」。
- `已完成` 且超过 `DONE_AUTO_HIDE_DAYS`(30) 天没有新邮件的事件会自动从看板隐藏（仍持久化，`?show_hidden=1` 可见）。
- 重开是「窗口内」的：匹配候选 = 活跃事件（`EVENT_WINDOW_DAYS` 内）+ 仍在自动隐藏窗口内（`DONE_AUTO_HIDE_DAYS` 内）的已完成/已归档事件，且受 `EVENT_MATCH_MAX_OPEN` 数量上限约束。已经被自动隐藏很久、或候选过多被挤出的老事件不会自动重开——这种情况用看板上的合并手动处理。

### 纠正 LLM 聚合错误

LLM 聚合会出错，看板提供人工纠错（都不额外烧 token，除重跑外都是纯数据操作）：

- **合并**（该合没合）：在事件详情页勾选其他事件合并进来。
- **拆分 / 移动**（不该合却合了）：在事件详情页的来源邮件上，把某封邮件拆成新事件或移到别的事件。
- **单事件重跑 LLM**：对一个事件用它的全部来源邮件重新生成标题/概要/优先级（需要配置 `LLM_API_KEY`）。

手动编辑过的标题/概要/优先级会被「锁定」，普通的自动更新不再覆盖（重跑是显式动作，会刷新）。

## Cloudflare 隧道与 Access

使用**命名隧道 + Token**（固定域名）：

1. 在 Cloudflare Zero Trust → Networks → Tunnels 新建一个 tunnel，拿到 token。
2. 把 token 写进 `.env`：`CLOUDFLARE_TUNNEL_TOKEN=...`
3. 在该 tunnel 的 Public Hostname 里配置：你的域名 → 服务 `http://web:8000`（ingress 在 dashboard 配置，token 模式不需要本地 config 文件）。
4. 强烈建议在 Zero Trust → Access → Applications 给这个域名加一个 Access 策略（按邮箱放行），这就是边缘那层鉴权。
5. 可选：把 Access 的 team 域名和 Application Audience (AUD) 填进 `.env` 的 `CF_ACCESS_TEAM_DOMAIN`、`CF_ACCESS_AUD`，应用会额外校验 Access 签发的 JWT（双保险）。

## Telegram 命令

- `/start`：检查 bot 是否可用
- `/help`：查看命令
- `/status`：查看 Gmail/SQLite/处理状态
- `/recent [n]`：查看最近 n 封已处理邮件，默认 5
- `/summarize`：手动生成过去 24 小时总结
- `/poll`：手动触发一次 Gmail 轮询

## 数据和隐私

- SQLite 默认位置：`data/mail_bot.sqlite3`
- Gmail OAuth token：`data/google_token.json`
- Google OAuth client secret：`secrets/google_credentials.json`
- 原始邮件正文和原始 MIME 不会保存。
- LLM 请求只发送脱敏后的标题和正文。
- Telegram 日报按你的要求保留原始标题；如果标题本身包含敏感信息，它会出现在 Telegram 消息里。
- Web 看板会展示事件标题（含原始邮件标题）、中文概要、来源邮件列表。它经 cloudflared 暴露到公网，务必同时启用 Cloudflare Access 和 `WEB_PASSWORD`，不要把它当作完全私密的页面。

## 本地开发

安装依赖：

```bash
uv sync
```

运行测试：

```bash
uv run pytest
```

格式检查：

```bash
uv run ruff check .
```

本地直接运行：

```bash
uv run mail-bot run
```

## 资源取舍

v1 默认不启动独立 Presidio/Guardrails 服务，也不下载大型 NLP 模型。脱敏使用本地 pattern pipeline，资源占用低，适合个人 Docker Compose 常驻服务。`pyproject.toml` 提供了 `pii` optional extra，后续可以接入 Presidio Analyzer/Anonymizer 或更强的多语言 NER，但默认路径保持轻量。
