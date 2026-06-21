from datetime import UTC, datetime

from mail_bot.formatters import (
    format_daily_event,
    format_daily_overview,
    format_immediate_email,
    split_telegram_message,
)
from mail_bot.models import DailyEvent, DailySummaryOutput, EmailAnalysis, KeyDate
from mail_bot.records import AnalyzedEmail


def _analyzed() -> AnalyzedEmail:
    return AnalyzedEmail(
        email_id=1,
        gmail_id="g1",
        subject="航班变更通知",
        sanitized_subject="航班变更通知",
        from_domain="airline.com",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis=EmailAnalysis(
            importance=5,
            information_density=4,
            category="旅行",
            summary_zh="出发时间提前。",
            requires_action=True,
            action_items=["重新值机"],
            key_dates=[KeyDate(date="2026-01-05", description_zh="新出发日期")],
            confidence=1,
        ),
    )


def test_split_telegram_message_preserves_short_text() -> None:
    assert split_telegram_message("hello", limit=10) == ["hello"]


def test_split_telegram_message_splits_long_text() -> None:
    chunks = split_telegram_message("a" * 25, limit=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 5]


def test_format_immediate_new_event_uses_bold_and_italic() -> None:
    text = format_immediate_email(_analyzed(), event_title="航班变更", is_new_event=True)
    assert "🆕 新事件" in text
    assert "<b>" in text
    assert "<i>" in text
    # New events do not show prior context.
    assert "事件背景" not in text


def test_format_immediate_update_shows_context_and_note() -> None:
    text = format_immediate_email(
        _analyzed(),
        event_title="东京行程",
        event_context="已预订机票。",
        update_note="出发时间提前两小时。",
        is_new_event=False,
    )
    assert "事件更新" in text
    assert "事件背景" in text
    assert "已预订机票。" in text
    assert "本次更新" in text
    assert "出发时间提前两小时。" in text


def test_format_daily_event_lists_related_subjects() -> None:
    mail = _analyzed()
    event = DailyEvent(
        title_zh="东京行程",
        summary_zh="机票已出票且时间变更。",
        importance=5,
        email_ids=[1],
        action_items=["重新值机"],
        key_dates=[KeyDate(date="2026-01-05", description_zh="新出发日期")],
    )
    text = format_daily_event(index=1, total=2, event=event, emails_by_id={1: mail})
    assert "事件 1/2" in text
    assert "<b>" in text and "<i>" in text
    assert "航班变更通知" in text
    assert "新出发日期" in text


def test_format_daily_overview_includes_overview_and_risks() -> None:
    summary = DailySummaryOutput(
        overview_zh="今天有一次行程更新。",
        events=[],
        risks_zh=["别忘了重新值机。"],
    )
    text = format_daily_overview(
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
        summary=summary,
        event_count=1,
    )
    assert "今天有一次行程更新。" in text
    assert "注意点" in text
    assert "别忘了重新值机。" in text
    assert "<b>" in text
