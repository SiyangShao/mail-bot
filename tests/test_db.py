from datetime import UTC, datetime

from mail_bot.db import Database
from mail_bot.models import EmailAnalysis
from mail_bot.records import EmailRecord


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
