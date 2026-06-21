# Web Kanban 计划

把现有 Telegram mail-bot 扩展为 Web 看板：持久化 + 聚合邮件事件，公网经 cloudflared 暴露，全部跑在 Docker 里。bot 与 web 共享同一个 SQLite data volume（WAL + busy_timeout）。

## 已确认决策

- **存储**：沿用 SQLite，演进 `events` 表（不上 Postgres）。
- **看板列（status）**：`待处理 / 进行中 / 已完成`（todo/in_progress/done），拖拽改列 + 列内排序。
- **优先级（priority）**：`P0/P1/P2`，与 status 正交，驱动通知。
- **鉴权**：纵深防御 = 边缘 Cloudflare Access + 应用层密码登录（session cookie）。
- **隧道**：命名隧道 + `CLOUDFLARE_TUNNEL_TOKEN`（固定域名，dashboard 配 ingress → `http://web:8000`）。

## 优先级 ← 重要性映射（新事件）

importance(1-5) 决定新事件 priority，可 env 调：

- importance ≥ `PRIORITY_P0_IMPORTANCE_MIN`(5) → **P0**（响铃通知）
- importance ≥ `PRIORITY_P1_IMPORTANCE_MIN`(4) → **P1**（静默消息）
- 否则 → **P2**（只上看板，不发）

事件创建门槛从 imp≥4 降到 `EVENT_CREATE_IMPORTANCE_MIN`(3)，让 P2 事件也能上看板；imp 1-2 仍不建事件。
更新时 priority 只升不降（P2→P1→P0），手动改过（`priority_overridden`）后不再自动变。

## 通知路由（service.py）

邮件过 `EVENT_CREATE_IMPORTANCE_MIN` 门槛后 → `_resolve_event` 匹配/新建 → 得到 event_id、is_new、priority：
- P2 → 不发；P0 → `disable_notification=False`；P1 → `disable_notification=True`。
- 持久化 `last_update_zh`/`last_activity_at`/`email_count`/`importance(max)`；override 字段不被覆盖。

## 问题 1：LLM 聚合错误的纠正（手动 + 单事件重跑 LLM）

- **合并**（修 under-merge）：多事件合一，邮件重链接到目标，重算 count/last_activity，importance 取 max、priority 取最紧急、context 拼接。纯 DB 操作。
- **拆分/移出邮件**（修 over-merge）：事件详情列出来源邮件；把某封移出独立成新事件或移到别的事件；置 `event_locked`。纯 DB 操作。
- **单事件重跑 LLM**：对一个事件，用其全部来源邮件调 LLM 重新生成 title/context/category/importance（新增 `EventAggregation` 模型 + `reaggregate_event` 自由函数，web 与 bot 都能调，只依赖 db+llm）。重跑会重写 title/context 并按结果重设 priority（除非已 `priority_overridden`）。
- 手动编辑 title/context → `*_overridden`，LLM 普通更新不再覆盖（重跑是显式动作，可覆盖）。

## 问题 2：事件生命周期

```
新邮件(过 EVENT_CREATE_IMPORTANCE_MIN 门槛)
  ├─ 匹配到已有事件 → 更新 → 按 priority 路由通知
  │     若该事件 已完成/已归档 → 自动重开到「待处理」并取消归档
  └─ 无匹配 → 新建: status=待处理, priority←importance映射 → 进「待处理」列

看板手动: 拖拽改列+排序 / 改 P0·P1·P2 / 编辑标题概要 / 合并 / 拆分移出 / 单事件重跑LLM / 手动归档

离开看板(仍持久化, 可恢复, 新邮件到达会重开):
  ├─ 已完成 且 > DONE_AUTO_HIDE_DAYS(30) 天无新邮件 → 自动隐藏(query 过滤)
  └─ 手动归档(archived_at) → 隐藏
```

## Schema 演进（`_ensure_column` 幂等）

events: `status`(todo) / `priority`(由映射，迁移默认 P1) / `sort_order REAL` / `last_update_zh` /
`archived_at TEXT` / `title_overridden` / `context_overridden` / `priority_overridden`
emails: `event_locked INTEGER DEFAULT 0`
`connect()` 加 `PRAGMA busy_timeout = 5000`。

## Web 层（FastAPI + Jinja2 + 原生 JS）

`mail-bot web` 子命令跑 uvicorn。`build_web_app(settings)`：
- `SessionMiddleware`（`WEB_SESSION_SECRET`）+ 可选 CF Access JWT 校验（配了 `CF_ACCESS_TEAM_DOMAIN`+`CF_ACCESS_AUD` 才启用）
- 路由：`GET /healthz`（无鉴权）｜`GET/POST /login`、`POST /logout`｜`GET /`（三列看板，过滤隐藏，`?show_hidden=1` 可见）｜`GET /event/{id}`（详情+编辑+来源邮件）｜`POST /api/board/reorder`(`{status, ordered_ids}`)｜`POST /api/event/{id}/edit`｜`/merge`｜`/split`(move email)｜`/reaggregate`｜`/archive`
- 模板 board/login/event；静态 app.css + app.js（原生 HTML5 拖拽，无第三方 JS）；中文界面。

## 文件

新建：`webapp.py`、`web/templates/{board,login,event}.html`、`web/static/{app.css,app.js}`、`tests/test_web.py`
改：`db.py`、`records.py`、`models.py`、`llm.py`、`service.py`、`config.py`、`__main__.py`、`pyproject.toml`（fastapi/uvicorn[standard]/jinja2/itsdangerous/python-multipart/pyjwt[crypto] + hatchling 打包 web 资源）、`docker-compose.yml`（web+cloudflared）、`.env.example`、`README.md`、`tests/test_service.py`（改 P0 新事件的通知断言）

## 验证

`uv run pytest`、`uv run ruff check .`，本地 `uv run mail-bot web` 冒烟。
