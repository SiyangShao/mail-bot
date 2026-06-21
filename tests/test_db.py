from datetime import UTC, datetime, timedelta

from mail_bot.db import Database
from mail_bot.models import EmailAnalysis
from mail_bot.records import EmailRecord


def _linked_email(db: Database, gmail_id: str, event_id: int, importance: int) -> int:
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
            importance=importance,
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


def test_split_recomputes_importance_and_priority_of_old_event(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    event_id = db.create_event(
        title_zh="混合事件",
        context_zh="背景",
        category="工作",
        importance=5,
        last_activity_at=datetime.now(UTC),
        priority="P0",
        status="todo",
    )
    high = _linked_email(db, "g-high", event_id, importance=5)
    _linked_email(db, "g-low", event_id, importance=3)

    db.split_email_to_new_event(high)

    old = db.get_event(event_id)
    assert old is not None
    assert old.email_count == 1
    # Old event no longer carries the moved-out P0/importance-5 email.
    assert old.importance == 3
    assert old.priority == "P2"


def test_update_event_does_not_move_last_activity_backwards(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    recent = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    event_id = db.create_event(
        title_zh="事件",
        context_zh="背景",
        category="工作",
        importance=3,
        last_activity_at=recent,
        priority="P2",
        status="todo",
    )

    # An older email (e.g. out-of-order / retry) must not pull last_activity backwards.
    db.update_event(
        event_id,
        context_zh="旧更新",
        importance=3,
        last_activity_at=recent - timedelta(days=5),
        mapped_priority="P2",
        update_note_zh="",
    )

    ev = db.get_event(event_id)
    assert ev is not None
    assert ev.last_activity_at == recent


def test_split_priority_uses_configured_thresholds(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    event_id = db.create_event(
        title_zh="混合事件",
        context_zh="背景",
        category="工作",
        importance=5,
        last_activity_at=datetime.now(UTC),
        priority="P0",
        status="todo",
    )
    high = _linked_email(db, "g-high", event_id, importance=5)
    _linked_email(db, "g-low", event_id, importance=4)

    # With custom thresholds (P0>=4), the remaining importance-4 email maps to P0, not P1.
    db.split_email_to_new_event(high, p0_min=4, p1_min=3)

    old = db.get_event(event_id)
    assert old is not None
    assert old.importance == 4
    assert old.priority == "P0"


def test_email_storage_uses_sanitized_body_only(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    email_id = db.upsert_email(
        EmailRecord(
            gmail_id="g1",
            thread_id="t1",
            history_id="h1",
            rfc822_message_id="<m@example.com>",
            subject="Secret original subject",
            sanitized_subject="Secret original subject",
            from_domain="example.com",
            sender_hash="hash",
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            internal_date_ms=1,
            snippet="snippet",
            sanitized_body="hello <EMAIL_1>",
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
            category="通知",
            summary_zh="测试摘要",
            requires_action=False,
            confidence=1,
        ),
        "test-model",
    )
    db.mark_email_processed(email_id)

    stored = db.get_email_by_gmail_id("g1")
    assert stored is not None
    assert stored.sanitized_body == "hello <EMAIL_1>"
    assert "alice@example.com" not in stored.sanitized_body
    assert db.important_between(
        start=datetime(2025, 12, 31, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
        min_importance=3,
        limit=10,
    )[0].analysis.summary_zh == "测试摘要"
