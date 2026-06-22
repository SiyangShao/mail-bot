from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.models import EmailAnalysis
from mail_bot.records import EmailRecord
from mail_bot.webapp import build_web_app

HTML = {"accept": "text/html"}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "mail.sqlite3"))
    monkeypatch.setenv("WEB_PASSWORD", "secret")
    monkeypatch.setenv("WEB_SESSION_SECRET", "0" * 32)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("TZ", "UTC")
    return Settings.from_env(require_runtime=False)


@pytest.fixture
def db(settings) -> Database:
    database = Database(settings.sqlite_path)
    database.init()
    return database


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(build_web_app(settings), follow_redirects=False)


def _seed_event(db: Database, *, title: str, status: str = "todo", priority: str = "P1") -> int:
    return db.create_event(
        title_zh=title,
        context_zh=f"{title} 的背景",
        category="工作",
        importance=4,
        last_activity_at=datetime.now(UTC),
        priority=priority,
        status=status,
    )


def _login(client: TestClient) -> None:
    resp = client.post("/login", data={"password": "secret"})
    assert resp.status_code == 303


def test_healthz_is_public(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}


def test_board_requires_auth(client: TestClient) -> None:
    # Browser-style request redirects to login.
    resp = client.get("/", headers=HTML)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # API-style request gets 401.
    assert client.get("/").status_code == 401


def test_wrong_password_rejected(client: TestClient) -> None:
    assert client.post("/login", data={"password": "nope"}).status_code == 401


def test_board_lists_events_grouped_by_status(client: TestClient, db: Database) -> None:
    _seed_event(db, title="待办事件", status="todo")
    _seed_event(db, title="进行事件", status="in_progress")
    _login(client)
    resp = client.get("/", headers=HTML)
    assert resp.status_code == 200
    assert "待办事件" in resp.text
    assert "进行事件" in resp.text


def test_reorder_moves_event_to_new_column(client: TestClient, db: Database) -> None:
    event_id = _seed_event(db, title="拖动我", status="todo")
    _login(client)
    resp = client.post(
        "/api/board/reorder", json={"status": "done", "ordered_ids": [event_id]}
    )
    assert resp.status_code == 200
    assert db.get_event(event_id).status == "done"


def test_edit_sets_priority_override(client: TestClient, db: Database) -> None:
    event_id = _seed_event(db, title="改优先级", priority="P1")
    _login(client)
    resp = client.post(f"/api/event/{event_id}/edit", json={"priority": "P0"})
    assert resp.status_code == 200
    event = db.get_event(event_id)
    assert event.priority == "P0"
    assert event.priority_overridden is True


def test_archive_hides_from_board(client: TestClient, db: Database) -> None:
    event_id = _seed_event(db, title="归档我", status="todo")
    _login(client)
    client.post(f"/api/event/{event_id}/archive", json={"archived": True})
    board = client.get("/", headers=HTML)
    assert "归档我" not in board.text
    # Visible again with show_hidden.
    assert "归档我" in client.get("/?show_hidden=1", headers=HTML).text


def test_merge_relinks_emails_and_drops_source(client: TestClient, db: Database) -> None:
    target = _seed_event(db, title="目标事件")
    source = _seed_event(db, title="来源事件")
    email_id = _insert_linked_email(db, "g-src", source)
    _login(client)
    resp = client.post(f"/api/event/{target}/merge", json={"source_ids": [source]})
    assert resp.status_code == 200
    assert db.get_event(source) is None
    moved = db.list_emails_for_event(target)
    assert any(m.email_id == email_id for m in moved)


def test_board_revision_changes_on_write(db: Database) -> None:
    rev0 = db.board_revision()
    event_id = _seed_event(db, title="版本事件")
    rev1 = db.board_revision()
    assert rev1 != rev0  # new event changes the fingerprint
    db.edit_event_fields(event_id, priority="P0")
    assert db.board_revision() != rev1  # an edit also changes it


def test_board_page_exposes_revision(client: TestClient, db: Database) -> None:
    _seed_event(db, title="带版本号")
    _login(client)
    resp = client.get("/", headers=HTML)
    assert resp.status_code == 200
    assert 'data-rev="' in resp.text


def test_board_fragment_returns_columns(client: TestClient, db: Database) -> None:
    _seed_event(db, title="片段事件", status="todo")
    # Requires auth like the rest of /api.
    assert client.get("/api/board/fragment").status_code == 401
    _login(client)
    resp = client.get("/api/board/fragment")
    assert resp.status_code == 200
    assert "片段事件" in resp.text
    # Fragment is the columns only — no full-page chrome.
    assert "<html" not in resp.text


def test_board_fragment_respects_show_hidden(client: TestClient, db: Database) -> None:
    event_id = _seed_event(db, title="归档片段", status="todo")
    db.set_event_archived(event_id, True)
    _login(client)
    assert "归档片段" not in client.get("/api/board/fragment").text
    assert "归档片段" in client.get("/api/board/fragment?show_hidden=1").text


def test_board_stream_requires_auth(client: TestClient) -> None:
    # Auth middleware rejects before the stream starts, so this returns promptly.
    assert client.get("/api/board/stream").status_code == 401


def test_split_email_creates_new_event(client: TestClient, db: Database) -> None:
    event_id = _seed_event(db, title="含两封邮件")
    e1 = _insert_linked_email(db, "g1", event_id)
    _insert_linked_email(db, "g2", event_id)
    _login(client)
    resp = client.post(f"/api/email/{e1}/split", json={})
    assert resp.status_code == 200
    new_id = resp.json()["new_event_id"]
    assert new_id != event_id
    assert [m.email_id for m in db.list_emails_for_event(new_id)] == [e1]


def _insert_linked_email(db: Database, gmail_id: str, event_id: int) -> int:
    email_id = db.upsert_email(
        EmailRecord(
            gmail_id=gmail_id,
            thread_id=f"t-{gmail_id}",
            history_id="h1",
            rfc822_message_id=f"<{gmail_id}@example.com>",
            subject=f"邮件 {gmail_id}",
            sanitized_subject=f"邮件 {gmail_id}",
            from_domain="example.com",
            sender_hash="hash",
            received_at=datetime.now(UTC) - timedelta(minutes=1),
            internal_date_ms=1,
            snippet="snippet",
            sanitized_body="body",
            body_sha256="sha",
            has_attachments=False,
            suppress_immediate=False,
            label_ids_json="[]",
            status="processing",
        )
    )
    db.insert_analysis(
        email_id,
        EmailAnalysis(
            importance=4,
            information_density=3,
            category="工作",
            summary_zh="摘要",
            confidence=1,
        ),
        "test-model",
    )
    db.mark_email_processed(email_id)
    db.link_email_event(email_id, event_id)
    return email_id
