from datetime import UTC, datetime
from pathlib import Path

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.gmail import GmailMessage, GmailProfile
from mail_bot.models import EmailAnalysis
from mail_bot.redaction import Redactor
from mail_bot.service import MailBotService, TelegramSender


class FakeGmail:
    def __init__(self, db: Database | None = None, *, fail_fetch: bool = False):
        self.db = db
        self.fail_fetch = fail_fetch

    def get_profile(self) -> GmailProfile:
        return GmailProfile(email_address="bot@example.com", history_id="history-after-backfill")

    def list_recent_message_ids(self) -> list[str]:
        return ["m1"]

    def list_history_message_ids(self, start_history_id: str):
        return [], "history-next"

    def fetch_message(self, message_id: str) -> GmailMessage:
        if self.fail_fetch:
            raise RuntimeError("temporary Gmail fetch failure")
        if self.db is not None:
            assert self.db.get_state("gmail_history_id") is None
        return GmailMessage(
            gmail_id=message_id,
            thread_id="t1",
            history_id="h-message",
            rfc822_message_id="<m1@example.com>",
            subject="Very important",
            from_header="Alice <alice@example.com>",
            from_email="alice@example.com",
            from_domain="example.com",
            received_at_ms=int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000),
            snippet="snippet",
            text="Please act by tomorrow.",
            has_attachments=False,
            label_ids=["INBOX"],
        )


class FakeLLM:
    def __init__(self, analysis: EmailAnalysis):
        self.analysis = analysis

    async def analyze_email(self, **kwargs) -> EmailAnalysis:
        return self.analysis

    async def summarize_daily(self, emails):
        raise AssertionError("not used")


class FakeTelegram(TelegramSender):
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send(self, text: str, *, disable_notification: bool) -> list[int]:
        self.sent.append((text, disable_notification))
        return [1]


def test_backfill_suppresses_immediate_and_advances_history_after_processing(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    telegram = FakeTelegram()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(db),
        redactor=Redactor(),
        llm=FakeLLM(
            EmailAnalysis(
                importance=5,
                information_density=5,
                category="工作",
                summary_zh="重要邮件。",
                confidence=1,
            )
        ),
        telegram=telegram,
    )

    stats = _run(service.poll_once())

    assert stats.processed == 1
    assert telegram.sent == []
    assert db.get_state("gmail_history_id") == "history-after-backfill"
    stored = db.get_email_by_gmail_id("m1")
    assert stored is not None
    assert stored.suppress_immediate is True


def test_fallback_analysis_is_retryable_not_processed(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(EmailAnalysis.fallback("timeout")),
        telegram=FakeTelegram(),
    )

    result = _run(service.process_message_id("m1"))

    assert result == "retry"
    stored = db.get_email_by_gmail_id("m1")
    assert stored is not None
    assert stored.status == "retry"
    assert db.list_retryable_gmail_ids(max_attempts=5) == []
    assert db.list_retryable_gmail_ids(
        max_attempts=5,
        now=datetime(9999, 1, 1, tzinfo=UTC),
    ) == ["m1"]
    analysis = db.get_analysis_for_email(stored.id)
    assert analysis is not None
    assert analysis.is_fallback is True


def test_discovered_fetch_failure_does_not_advance_history(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(fail_fetch=True),
        redactor=Redactor(),
        llm=FakeLLM(
            EmailAnalysis(
                importance=5,
                information_density=5,
                category="工作",
                summary_zh="重要邮件。",
                confidence=1,
            )
        ),
        telegram=FakeTelegram(),
    )

    stats = _run(service.poll_once())

    assert stats.failed == 1
    assert db.get_state("gmail_history_id") is None
    assert db.get_email_by_gmail_id("m1") is None


def test_retry_result_is_not_counted_as_failed(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(EmailAnalysis.fallback("timeout")),
        telegram=FakeTelegram(),
    )

    stats = _run(service.poll_once())

    assert stats.retry == 1
    assert stats.failed == 0
    assert db.get_state("gmail_history_id") == "history-after-backfill"


def test_retry_reaches_terminal_error_at_max_attempts(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    service = MailBotService(
        settings=_settings(tmp_path, email_retry_max_attempts=1),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(EmailAnalysis.fallback("timeout")),
        telegram=FakeTelegram(),
    )

    result = _run(service.process_message_id("m1"))

    assert result == "error"
    stored = db.get_email_by_gmail_id("m1")
    assert stored is not None
    assert stored.status == "error"
    assert db.list_retryable_gmail_ids(
        max_attempts=1,
        now=datetime(9999, 1, 1, tzinfo=UTC),
    ) == []


def _settings(tmp_path: Path, *, email_retry_max_attempts: int = 5) -> Settings:
    return Settings(
        data_dir=tmp_path,
        sqlite_path=tmp_path / "mail.sqlite3",
        google_credentials_path=tmp_path / "google_credentials.json",
        google_token_path=tmp_path / "google_token.json",
        gmail_oauth_port=8080,
        gmail_poll_seconds=120,
        gmail_backfill_days=2,
        gmail_query="in:anywhere",
        telegram_bot_token="token",
        telegram_chat_id="123",
        telegram_allowed_user_ids=frozenset({123}),
        llm_api_key="key",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-v4-flash",
        llm_timeout_seconds=90,
        llm_max_retries=3,
        llm_json_mode=True,
        llm_user_id="mail-bot",
        timezone="America/Los_Angeles",
        log_level="INFO",
        hash_salt="",
        urgent_importance_min=4,
        urgent_info_density_min=3,
        daily_importance_min=3,
        daily_summary_time="09:00",
        daily_window_hours=24,
        max_email_chars_for_llm=12000,
        max_daily_items=20,
        email_retry_max_attempts=email_retry_max_attempts,
        email_retry_backoff_seconds=300,
        email_retry_max_backoff_seconds=3600,
    )


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
