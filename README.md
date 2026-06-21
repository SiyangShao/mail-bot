# mail-bot

一个跑在 Docker Compose 里的个人 Telegram 邮件 bot。它从专用 Gmail 邮箱读取邮件，先在本地脱敏，再调用 OpenAI-compatible LLM（默认 DeepSeek）解析，结构化写入 SQLite，并通过 Telegram 给你即时提醒和每天 09:00 的中文日报。

## 功能

- Gmail OAuth 只读收信，使用 `https://www.googleapis.com/auth/gmail.readonly`。
- 默认每 120 秒轮询 Gmail `historyId`，首次启动会回补最近 2 天邮件。
- 本地轻量脱敏：邮箱、电话、URL、IP、信用卡、SSN、token、账号类字段等会替换成占位符。
- LLM 输出按“不可靠 JSON”处理：JSON mode 只是辅助，代码会抽取 JSON、用 Pydantic 校验、把错误回传重试，最后给出保守 fallback。
- SQLite 保存原始标题、Gmail ID、脱敏正文和结构化分析；不保存原始正文或原始 MIME。
- 重要且信息量大的邮件会立刻 Telegram 提醒，但 `disable_notification=true`。
- 事件聚合：把描述同一件事的多封邮件合并成一个“事件”。即时提醒会让 LLM 判断这封邮件是否属于某个最近的开放事件，如果是就展示事件背景 + 本次更新；否则新建事件。窗口和数量由 `EVENT_WINDOW_DAYS=7`、`EVENT_MATCH_MAX_OPEN=12` 控制。
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
