from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.llm import LLMClient
from mail_bot.records import AnalyzedEmail, EventSummary
from mail_bot.service import reaggregate_event

LOGGER = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

STATUSES: list[tuple[str, str]] = [
    ("todo", "待处理"),
    ("in_progress", "进行中"),
    ("done", "已完成"),
]
STATUS_KEYS = {key for key, _ in STATUSES}
PRIORITY_LABELS = {"P0": "P0 紧急", "P1": "P1 关注", "P2": "P2 留档"}
PRIORITIES = ("P0", "P1", "P2")


def build_web_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="mail-bot 看板", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.sqlite_path)
    app.state.db.init()
    app.state.llm = None
    app.state.tz = settings.local_timezone()
    app.state.jwks_client = _build_jwks_client(settings)

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.filters["dt"] = lambda value: _fmt_dt(value, app.state.tz)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # Auth middleware (added before SessionMiddleware so the session is available here).
    @app.middleware("http")
    async def _auth(request: Request, call_next):
        path = request.url.path
        # Health check is the only fully public path: the container healthcheck hits it
        # internally and has no Cloudflare Access JWT.
        if path == "/healthz":
            return await call_next(request)
        # Edge auth: when CF Access is configured, every other path — including /login and
        # /static — must carry a valid Access JWT.
        if not await _cf_access_ok(request, settings):
            return JSONResponse({"error": "Cloudflare Access 校验失败"}, status_code=403)
        # Application auth: only the login page and static assets are reachable without a session.
        if path == "/login" or path.startswith("/static"):
            return await call_next(request)
        if not request.session.get("authed"):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "未登录"}, status_code=401)
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.web_session_secret or secrets.token_hex(32),
        session_cookie="mailbot_session",
        https_only=False,
        same_site="lax",
    )

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if request.session.get("authed"):
            return RedirectResponse("/", status_code=303)
        return _render(request, "login.html", {"error": None})

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        password = str(form.get("password", ""))
        expected = request.app.state.settings.web_password or ""
        if expected and secrets.compare_digest(password, expected):
            request.session["authed"] = True
            return RedirectResponse("/", status_code=303)
        return _render(request, "login.html", {"error": "密码错误"}, status_code=401)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def board(request: Request) -> Response:
        db: Database = request.app.state.db
        settings: Settings = request.app.state.settings
        show_hidden = request.query_params.get("show_hidden") in {"1", "true", "yes"}
        events = db.list_board_events(
            hide_done_after_days=settings.done_auto_hide_days,
            include_hidden=show_hidden,
        )
        tz = request.app.state.tz
        columns = [
            {
                "key": key,
                "label": label,
                "events": [_event_view(ev, tz) for ev in events if ev.status == key],
            }
            for key, label in STATUSES
        ]
        return _render(
            request,
            "board.html",
            {
                "columns": columns,
                "show_hidden": show_hidden,
                "priorities": PRIORITIES,
                "priority_labels": PRIORITY_LABELS,
            },
        )

    @app.get("/event/{event_id}", response_class=HTMLResponse)
    async def event_detail(request: Request, event_id: int) -> Response:
        db: Database = request.app.state.db
        event = db.get_event(event_id)
        if event is None:
            return _render(request, "event.html", {"event": None, "emails": [], "others": []}, 404)
        tz = request.app.state.tz
        emails = db.list_emails_for_event(event_id)
        others = [
            {"id": ev.id, "title_zh": ev.title_zh}
            for ev in db.list_board_events(
                hide_done_after_days=request.app.state.settings.done_auto_hide_days,
                include_hidden=True,
            )
            if ev.id != event_id
        ]
        return _render(
            request,
            "event.html",
            {
                "event": _event_view(event, tz),
                "emails": [_email_view(item, tz) for item in emails],
                "others": others,
                "priorities": PRIORITIES,
                "priority_labels": PRIORITY_LABELS,
                "statuses": STATUSES,
            },
        )

    @app.post("/event/{event_id}")
    async def event_edit_form(request: Request, event_id: int) -> Response:
        db: Database = request.app.state.db
        form = await request.form()
        db.edit_event_fields(
            event_id,
            title_zh=_clean(form.get("title_zh")),
            context_zh=_clean(form.get("context_zh")),
            priority=_valid_priority(form.get("priority")),
            category=_clean(form.get("category")),
        )
        return RedirectResponse(f"/event/{event_id}", status_code=303)

    @app.post("/api/board/reorder")
    async def reorder(request: Request) -> Response:
        body = await request.json()
        status = body.get("status")
        ordered_ids = body.get("ordered_ids", [])
        if status not in STATUS_KEYS or not isinstance(ordered_ids, list):
            return JSONResponse({"error": "参数错误"}, status_code=400)
        request.app.state.db.set_column_order(
            status=status, ordered_ids=[int(x) for x in ordered_ids]
        )
        return JSONResponse({"ok": True})

    @app.post("/api/event/{event_id}/edit")
    async def event_edit(request: Request, event_id: int) -> Response:
        body = await request.json()
        request.app.state.db.edit_event_fields(
            event_id,
            title_zh=_clean(body.get("title_zh")),
            context_zh=_clean(body.get("context_zh")),
            priority=_valid_priority(body.get("priority")),
            category=_clean(body.get("category")),
        )
        return JSONResponse({"ok": True})

    @app.post("/api/event/{event_id}/archive")
    async def event_archive(request: Request, event_id: int) -> Response:
        body = await request.json()
        request.app.state.db.set_event_archived(event_id, bool(body.get("archived", True)))
        return JSONResponse({"ok": True})

    @app.post("/api/event/{event_id}/merge")
    async def event_merge(request: Request, event_id: int) -> Response:
        body = await request.json()
        source_ids = body.get("source_ids", [])
        if not isinstance(source_ids, list) or not source_ids:
            return JSONResponse({"error": "缺少 source_ids"}, status_code=400)
        request.app.state.db.merge_events(event_id, [int(x) for x in source_ids])
        return JSONResponse({"ok": True})

    @app.post("/api/event/{event_id}/reaggregate")
    async def event_reaggregate(request: Request, event_id: int) -> Response:
        settings: Settings = request.app.state.settings
        llm = _get_llm(request)
        if llm is None:
            return JSONResponse({"error": "未配置 LLM_API_KEY，无法重跑"}, status_code=503)
        ok = await reaggregate_event(
            db=request.app.state.db, llm=llm, settings=settings, event_id=event_id
        )
        return JSONResponse({"ok": ok})

    @app.post("/api/email/{email_id}/split")
    async def email_split(request: Request, email_id: int) -> Response:
        s: Settings = request.app.state.settings
        new_event_id = request.app.state.db.split_email_to_new_event(
            email_id,
            p0_min=s.priority_p0_importance_min,
            p1_min=s.priority_p1_importance_min,
        )
        return JSONResponse({"ok": True, "new_event_id": new_event_id})

    @app.post("/api/email/{email_id}/move")
    async def email_move(request: Request, email_id: int) -> Response:
        body = await request.json()
        target = body.get("target_event_id")
        if target is None:
            return JSONResponse({"error": "缺少 target_event_id"}, status_code=400)
        s: Settings = request.app.state.settings
        request.app.state.db.move_email_to_event(
            email_id,
            int(target),
            p0_min=s.priority_p0_importance_min,
            p1_min=s.priority_p1_importance_min,
        )
        return JSONResponse({"ok": True})


# --- helpers ----------------------------------------------------------------------------


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )


def _get_llm(request: Request) -> LLMClient | None:
    if request.app.state.llm is not None:
        return request.app.state.llm
    try:
        request.app.state.llm = LLMClient(request.app.state.settings)
    except Exception:
        LOGGER.warning("LLM client unavailable for web re-aggregation")
        return None
    return request.app.state.llm


def _event_view(ev: EventSummary, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "id": ev.id,
        "title_zh": ev.title_zh,
        "context_zh": ev.context_zh,
        "category": ev.category,
        "importance": ev.importance,
        "email_count": ev.email_count,
        "priority": ev.priority,
        "priority_label": PRIORITY_LABELS.get(ev.priority, ev.priority),
        "status": ev.status,
        "last_update_zh": ev.last_update_zh,
        "last_activity_at": ev.last_activity_at,
        "archived": ev.archived_at is not None,
        "priority_overridden": ev.priority_overridden,
    }


def _email_view(item: AnalyzedEmail, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "email_id": item.email_id,
        "subject": item.subject,
        "from_domain": item.from_domain or "未知",
        "received_at": item.received_at,
        "summary_zh": item.analysis.summary_zh,
        "importance": item.analysis.importance,
    }


def _fmt_dt(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "—"
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _valid_priority(value: Any) -> str | None:
    text = _clean(value)
    return text if text in PRIORITIES else None


def _build_jwks_client(settings: Settings):
    """One cached JWKS client per app (keys cached internally, with a network timeout)."""
    if not settings.cf_access_team_domain or not settings.cf_access_aud:
        return None
    from jwt import PyJWKClient

    certs_url = f"https://{settings.cf_access_team_domain}/cdn-cgi/access/certs"
    return PyJWKClient(certs_url, cache_keys=True, lifespan=600, timeout=5)


async def _cf_access_ok(request: Request, settings: Settings) -> bool:
    """Verify the Cloudflare Access JWT when CF Access is configured; else allow.

    Uses the app-level cached JWKS client and runs the blocking verify off the event loop.
    """
    client = request.app.state.jwks_client
    if client is None:
        return True
    token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
    if not token:
        return False
    try:
        return await run_in_threadpool(_verify_cf_jwt, client, token, settings)
    except Exception:
        LOGGER.warning("Cloudflare Access JWT verification failed")
        return False


def _verify_cf_jwt(client, token: str, settings: Settings) -> bool:
    import jwt

    signing_key = client.get_signing_key_from_jwt(token)
    jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.cf_access_aud,
        issuer=f"https://{settings.cf_access_team_domain}",
    )
    return True
