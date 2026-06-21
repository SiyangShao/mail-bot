from datetime import UTC, datetime, timedelta
from pathlib import Path

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.gmail import GmailMessage, GmailProfile
from mail_bot.models import DailyEvent, DailySummaryOutput, EmailAnalysis, EventMatch
from mail_bot.records import EmailRecord
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
            received_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            snippet="snippet",
            text="Please act by tomorrow.",
            has_attachments=False,
            label_ids=["INBOX"],
        )


class FakeLLM:
    def __init__(
        self,
        analysis: EmailAnalysis,
        *,
        event_match: EventMatch | None = None,
        daily: DailySummaryOutput | None = None,
    ):
        self.analysis = analysis
        self.event_match = event_match
        self.daily = daily
        self.resolve_calls = 0

    async def analyze_email(self, **kwargs) -> EmailAnalysis:
        return self.analysis

    async def summarize_daily(self, emails):
        if self.daily is not None:
            return self.daily
        raise AssertionError("not used")

    async def resolve_event(self, **kwargs) -> EventMatch:
        self.resolve_calls += 1
        if self.event_match is not None:
            return self.event_match
        return EventMatch.fallback(
            kwargs["subject"], kwargs["summary_zh"], importance=kwargs["importance"]
        )


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


def _urgent_analysis() -> EmailAnalysis:
    return EmailAnalysis(
        importance=5,
        information_density=5,
        category="旅行",
        summary_zh="航班相关重要更新。",
        requires_action=True,
        action_items=["尽快值机"],
        confidence=1,
    )


def test_immediate_creates_new_event(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    telegram = FakeTelegram()
    llm = FakeLLM(_urgent_analysis())
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=llm,
        telegram=telegram,
    )

    result = _run(service.process_message_id("m1", suppress_immediate=False))

    assert result == "processed"
    assert llm.resolve_calls == 1
    assert len(telegram.sent) == 1
    text, disable_notification = telegram.sent[0]
    assert "新事件" in text
    assert disable_notification is True
    events = db.list_open_events(within_days=7, limit=10)
    assert len(events) == 1
    assert events[0].email_count == 1


def test_immediate_merges_into_existing_event(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    event_id = db.create_event(
        title_zh="东京行程",
        context_zh="已预订往返机票。",
        category="旅行",
        importance=4,
        last_activity_at=datetime.now(UTC),
    )
    match = EventMatch(
        matched_event_id=event_id,
        title_zh="东京行程",
        context_zh="已预订机票，航班时间发生变更。",
        update_note_zh="出发时间提前两小时。",
        category="旅行",
        importance=5,
    )
    telegram = FakeTelegram()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(_urgent_analysis(), event_match=match),
        telegram=telegram,
    )

    _run(service.process_message_id("m1", suppress_immediate=False))

    text, _ = telegram.sent[0]
    assert "事件更新" in text
    assert "航班时间发生变更" in text
    assert "出发时间提前两小时" in text
    updated = db.get_event(event_id)
    assert updated is not None
    assert updated.email_count == 2
    assert updated.importance == 5


def test_immediate_ignores_hallucinated_event_id(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    match = EventMatch(
        matched_event_id=999,
        title_zh="虚构事件",
        context_zh="不存在的事件背景。",
        importance=5,
    )
    telegram = FakeTelegram()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(_urgent_analysis(), event_match=match),
        telegram=telegram,
    )

    _run(service.process_message_id("m1", suppress_immediate=False))

    text, _ = telegram.sent[0]
    assert "新事件" in text
    events = db.list_open_events(within_days=7, limit=10)
    assert len(events) == 1


def test_daily_summary_sends_one_message_per_event(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    received_at = datetime.now(UTC) - timedelta(hours=1)
    id1 = _insert_processed(db, "g1", "机票确认", "已出票。", 4, received_at)
    id2 = _insert_processed(db, "g2", "账单到期", "本月账单待缴。", 3, received_at)
    daily = DailySummaryOutput(
        overview_zh="今天有一次行程和一笔账单。",
        events=[
            DailyEvent(title_zh="东京行程", summary_zh="机票已出票。", importance=4, email_ids=[id1]),
            DailyEvent(title_zh="本月账单", summary_zh="账单待缴。", importance=3, email_ids=[id2]),
        ],
        risks_zh=["账单别忘了缴。"],
    )
    telegram = FakeTelegram()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(_urgent_analysis(), daily=daily),
        telegram=telegram,
    )

    _run(service.send_daily_summary(manual=True))

    assert len(telegram.sent) == 3
    overview_text, overview_disable = telegram.sent[0]
    assert "过去 24 小时" in overview_text
    assert overview_disable is False
    assert "事件 1/2" in telegram.sent[1][0]
    assert "东京行程" in telegram.sent[1][0]
    assert "事件 2/2" in telegram.sent[2][0]


def test_daily_summary_covers_emails_the_llm_dropped(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    received_at = datetime.now(UTC) - timedelta(hours=1)
    id1 = _insert_processed(db, "g1", "机票确认", "已出票。", 4, received_at)
    _insert_processed(db, "g2", "账单到期", "本月账单待缴。", 3, received_at)
    # LLM clusters only the first email and silently drops the second one.
    daily = DailySummaryOutput(
        overview_zh="只聚类了一封邮件。",
        events=[DailyEvent(title_zh="东京行程", summary_zh="机票已出票。", importance=4, email_ids=[id1])],
    )
    telegram = FakeTelegram()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(_urgent_analysis(), daily=daily),
        telegram=telegram,
    )

    _run(service.send_daily_summary(manual=True))

    # overview + clustered event + a fallback event for the dropped email
    assert len(telegram.sent) == 3
    all_text = "\n".join(text for text, _ in telegram.sent)
    assert "账单到期" in all_text


def test_daily_summary_falls_back_to_per_email_events(tmp_path) -> None:
    db = Database(tmp_path / "mail.sqlite3")
    db.init()
    received_at = datetime.now(UTC) - timedelta(hours=1)
    _insert_processed(db, "g1", "机票确认", "已出票。", 4, received_at)
    daily = DailySummaryOutput(overview_zh="无法聚类，按邮件列出。", events=[])
    telegram = FakeTelegram()
    service = MailBotService(
        settings=_settings(tmp_path),
        db=db,
        gmail=FakeGmail(),
        redactor=Redactor(),
        llm=FakeLLM(_urgent_analysis(), daily=daily),
        telegram=telegram,
    )

    _run(service.send_daily_summary(manual=True))

    assert len(telegram.sent) == 2
    assert "机票确认" in telegram.sent[1][0]


def _insert_processed(
    db: Database,
    gmail_id: str,
    subject: str,
    summary_zh: str,
    importance: int,
    received_at: datetime,
) -> int:
    email_id = db.upsert_email(
        EmailRecord(
            gmail_id=gmail_id,
            thread_id=f"t-{gmail_id}",
            history_id="h1",
            rfc822_message_id=f"<{gmail_id}@example.com>",
            subject=subject,
            sanitized_subject=subject,
            from_domain="example.com",
            sender_hash="hash",
            received_at=received_at,
            internal_date_ms=int(received_at.timestamp() * 1000),
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
            category="通知",
            summary_zh=summary_zh,
            confidence=1,
        ),
        "test-model",
    )
    db.mark_email_processed(email_id)
    return email_id


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
        event_window_days=7,
        event_match_max_open=12,
    )


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
