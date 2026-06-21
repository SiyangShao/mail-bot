from __future__ import annotations

import base64
import http.server
import json
import logging
import re
import threading
import urllib.parse
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mail_bot.config import Settings
from mail_bot.time_utils import from_epoch_ms

LOGGER = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass(frozen=True)
class GmailProfile:
    email_address: str
    history_id: str


@dataclass(frozen=True)
class GmailMessage:
    gmail_id: str
    thread_id: str | None
    history_id: str | None
    rfc822_message_id: str | None
    subject: str
    from_header: str | None
    from_email: str | None
    from_domain: str | None
    received_at_ms: int | None
    snippet: str
    text: str
    has_attachments: bool
    label_ids: list[str]


class GmailHistoryExpiredError(RuntimeError):
    pass


def run_oauth(settings: Settings) -> None:
    if not settings.google_credentials_path.exists():
        raise FileNotFoundError(
            f"Google OAuth credentials not found: {settings.google_credentials_path}"
        )
    settings.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.google_credentials_path),
        scopes=GMAIL_SCOPES,
    )
    port = settings.gmail_oauth_port
    # redirect 用 localhost（Google 的 loopback OAuth 只认 localhost/127.0.0.1），
    # 服务器 bind 到 0.0.0.0 这样 Docker 端口映射能把回调转进容器。
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("打开下面的 URL 完成 Gmail 授权：\n" + auth_url + "\n")
    print("如果你在 Docker 里运行，请确认用 --service-ports 映射了同一个端口。")

    captured: dict[str, str] = {}
    done = threading.Event()

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params or "error" in params:
                captured["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write("Gmail 授权完成，可以关闭这个页面。".encode())
                done.set()
            else:
                # 忽略浏览器的预连接 / favicon 等无关请求，避免单请求服务器死锁。
                self.send_response(204)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("oauth callback: " + format, *args)

    # ThreadingHTTPServer：每个连接独立线程，浏览器的预连接不会卡住真正带 code 的回调。
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        if not done.wait(timeout=300):
            raise TimeoutError("等待 Gmail 授权回调超时（5 分钟）。请重试 auth-gmail。")
    finally:
        server.shutdown()
        server.server_close()

    if "error" in urllib.parse.parse_qs(urllib.parse.urlparse(captured["path"]).query):
        raise RuntimeError(f"Gmail 授权失败：{captured['path']}")

    # oauthlib 对 http 比较挑剔，这里换成 https 让它做 state 校验和 code 交换。
    authorization_response = f"https://localhost:{port}{captured['path']}"
    flow.fetch_token(authorization_response=authorization_response)
    settings.google_token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
    print(f"Saved Gmail OAuth token to {settings.google_token_path}")


class GmailClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._service: Any | None = None

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def get_profile(self) -> GmailProfile:
        raw = self.service.users().getProfile(userId="me").execute()
        return GmailProfile(
            email_address=str(raw.get("emailAddress", "")),
            history_id=str(raw.get("historyId", "")),
        )

    def list_recent_message_ids(self) -> list[str]:
        query_parts = [self.settings.gmail_query.strip()]
        query_parts.append(f"newer_than:{self.settings.gmail_backfill_days}d")
        query = " ".join(part for part in query_parts if part)
        ids: list[str] = []
        request = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100, includeSpamTrash=True)
        )
        while request is not None:
            response = request.execute()
            ids.extend(message["id"] for message in response.get("messages", []))
            request = (
                self.service.users()
                .messages()
                .list_next(previous_request=request, previous_response=response)
            )
        return ids

    def list_history_message_ids(self, start_history_id: str) -> tuple[list[str], str | None]:
        ids: list[str] = []
        latest_history_id: str | None = None
        request = (
            self.service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                maxResults=500,
            )
        )
        try:
            while request is not None:
                response = request.execute()
                latest_history_id = response.get("historyId") or latest_history_id
                for history in response.get("history", []):
                    for added in history.get("messagesAdded", []):
                        message = added.get("message") or {}
                        message_id = message.get("id")
                        if message_id:
                            ids.append(str(message_id))
                request = (
                    self.service.users()
                    .history()
                    .list_next(previous_request=request, previous_response=response)
                )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise GmailHistoryExpiredError(str(exc)) from exc
            raise
        return ids, latest_history_id

    def fetch_message(self, message_id: str) -> GmailMessage:
        raw = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return parse_gmail_message(raw)

    def _build_service(self) -> Any:
        creds = _load_credentials(self.settings.google_token_path)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise RuntimeError("Gmail OAuth token is invalid. Run `mail-bot auth-gmail` again.")
        return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _load_credentials(token_path: Path) -> Credentials:
    if not token_path.exists():
        raise FileNotFoundError(f"Gmail OAuth token not found: {token_path}")
    return Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)


def parse_gmail_message(raw: dict[str, Any]) -> GmailMessage:
    payload = raw.get("payload") or {}
    headers = _headers(payload)
    subject = _decode_mime_header(_header(headers, "subject") or "")
    from_header = _decode_mime_header(_header(headers, "from") or "")
    _, from_email = parseaddr(from_header)
    from_domain = _domain_from_email(from_email)
    text = extract_text(payload)

    return GmailMessage(
        gmail_id=str(raw.get("id", "")),
        thread_id=raw.get("threadId"),
        history_id=raw.get("historyId"),
        rfc822_message_id=_header(headers, "message-id"),
        subject=subject,
        from_header=from_header or None,
        from_email=from_email or None,
        from_domain=from_domain,
        received_at_ms=int(raw["internalDate"]) if raw.get("internalDate") else None,
        snippet=str(raw.get("snippet", "")),
        text=text,
        has_attachments=has_attachments(payload),
        label_ids=[str(label) for label in raw.get("labelIds", [])],
    )


def extract_text(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_text_parts(payload, plain_parts=plain_parts, html_parts=html_parts)
    if plain_parts:
        return "\n\n".join(part.strip() for part in plain_parts if part.strip()).strip()
    return "\n\n".join(part.strip() for part in html_parts if part.strip()).strip()


def has_attachments(part: dict[str, Any]) -> bool:
    filename = part.get("filename")
    body = part.get("body") or {}
    if filename and (body.get("attachmentId") or part.get("mimeType") != "text/plain"):
        return True
    return any(has_attachments(child) for child in part.get("parts", []) or [])


def _collect_text_parts(
    part: dict[str, Any],
    *,
    plain_parts: list[str],
    html_parts: list[str],
) -> None:
    filename = part.get("filename")
    body = part.get("body") or {}
    mime_type = str(part.get("mimeType", "")).lower()
    if not filename and body.get("data") and mime_type in {"text/plain", "text/html"}:
        decoded = _decode_body_data(str(body["data"]))
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(_html_to_text(decoded))
    for child in part.get("parts", []) or []:
        _collect_text_parts(child, plain_parts=plain_parts, html_parts=html_parts)


def _decode_body_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    return decoded.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in payload.get("headers", []) or []:
        name = str(item.get("name", "")).lower()
        value = str(item.get("value", ""))
        if name:
            result[name] = value
    return result


def _header(headers: dict[str, str], name: str) -> str | None:
    return headers.get(name.lower())


def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        LOGGER.debug("Failed to decode MIME header", exc_info=True)
        return value


def _domain_from_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    return value.rsplit("@", 1)[1].lower()


def message_received_at(message: GmailMessage):
    return from_epoch_ms(message.received_at_ms)


def label_ids_json(message: GmailMessage) -> str:
    return json.dumps(message.label_ids, ensure_ascii=False)
