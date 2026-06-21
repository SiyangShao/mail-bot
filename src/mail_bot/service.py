from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.formatters import (
    format_daily_event,
    format_daily_overview,
    format_immediate_email,
)
from mail_bot.gmail import (
    GmailClient,
    GmailHistoryExpiredError,
    GmailMessage,
    label_ids_json,
    message_received_at,
)
from mail_bot.llm import LLMClient
from mail_bot.models import DailyEvent, EventMatch
from mail_bot.records import AnalyzedEmail, EmailRecord
from mail_bot.redaction import Redactor
from mail_bot.time_utils import iso_utc, utc_now

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollStats:
    discovered: int
    processed: int
    skipped: int
    retry: int
    failed: int


@dataclass(frozen=True)
class DiscoveredMessages:
    ids: list[str]
    next_history_id: str | None = None
    suppress_immediate: bool = False


@dataclass(frozen=True)
class ResolvedEvent:
    match: EventMatch
    event_id: int
    priority: str
    is_new: bool


class TelegramSender:
    async def send(self, text: str, *, disable_notification: bool) -> list[int]:
        raise NotImplementedError


class MailBotService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        gmail: GmailClient,
        redactor: Redactor,
        llm: LLMClient,
        telegram: TelegramSender,
    ):
        self.settings = settings
        self.db = db
        self.gmail = gmail
        self.redactor = redactor
        self.llm = llm
        self.telegram = telegram
        self._poll_lock = asyncio.Lock()
        self._summary_lock = asyncio.Lock()

    async def poll_once(self) -> PollStats:
        async with self._poll_lock:
            retry_ids = self.db.list_retryable_gmail_ids(
                max_attempts=self.settings.email_retry_max_attempts
            )
            discovered = await self._discover_message_ids()
            ids = _dedupe_preserve_order([*retry_ids, *discovered.ids])
            processed = 0
            skipped = 0
            retry = 0
            failed = 0
            block_history_advance = False
            discovery_ids = set(discovered.ids)
            for message_id in ids:
                try:
                    result = await self.process_message_id(
                        message_id,
                        suppress_immediate=discovered.suppress_immediate
                        and message_id in discovery_ids,
                    )
                except Exception as exc:
                    LOGGER.exception("Failed to process Gmail message %s", message_id)
                    if (
                        message_id in discovery_ids
                        and self.db.get_email_by_gmail_id(message_id) is None
                        and not _is_missing_gmail_message_error(exc)
                    ):
                        block_history_advance = True
                    failed += 1
                    continue
                if result == "processed":
                    processed += 1
                elif result == "skipped":
                    skipped += 1
                elif result == "retry":
                    retry += 1
                else:
                    failed += 1
            if discovered.next_history_id and not block_history_advance:
                self.db.set_state("gmail_history_id", discovered.next_history_id)
            self.db.set_state("gmail_last_poll_at", iso_utc(utc_now()))
            return PollStats(
                discovered=len(ids),
                processed=processed,
                skipped=skipped,
                retry=retry,
                failed=failed,
            )

    async def process_message_id(self, message_id: str, *, suppress_immediate: bool = False) -> str:
        existing = self.db.get_email_by_gmail_id(message_id)
        if existing and existing.status == "processed" and self.db.get_analysis_for_email(existing.id):
            return "skipped"

        message = await asyncio.to_thread(self.gmail.fetch_message, message_id)
        email_id = self._store_processing_email(message, suppress_immediate=suppress_immediate)
        try:
            stored = self.db.get_email_by_gmail_id(message_id)
            if stored is None:
                raise RuntimeError("Stored email disappeared before analysis")
            analysis = await self.llm.analyze_email(
                subject=stored.sanitized_subject,
                from_domain=stored.from_domain,
                received_at=stored.received_at.isoformat(),
                sanitized_body=stored.sanitized_body[: self.settings.max_email_chars_for_llm],
            )
            self.db.insert_analysis(email_id, analysis, self.settings.llm_model)
            if analysis.is_fallback:
                return self.db.mark_email_retry(
                    email_id,
                    analysis.summary_zh,
                    max_attempts=self.settings.email_retry_max_attempts,
                    backoff_seconds=self.settings.email_retry_backoff_seconds,
                    max_backoff_seconds=self.settings.email_retry_max_backoff_seconds,
                )
            self.db.mark_email_processed(email_id)
            analyzed = AnalyzedEmail(
                email_id=email_id,
                gmail_id=message.gmail_id,
                subject=message.subject,
                sanitized_subject=stored.sanitized_subject,
                from_domain=message.from_domain,
                received_at=stored.received_at,
                analysis=analysis,
                suppress_immediate=stored.suppress_immediate,
            )
            await self._ingest_and_maybe_notify(analyzed)
            return "processed"
        except Exception as exc:
            self.db.mark_email_retry(
                email_id,
                str(exc),
                max_attempts=self.settings.email_retry_max_attempts,
                backoff_seconds=self.settings.email_retry_backoff_seconds,
                max_backoff_seconds=self.settings.email_retry_max_backoff_seconds,
            )
            raise

    async def send_daily_summary(self, *, manual: bool = False) -> int:
        async with self._summary_lock:
            now = utc_now()
            local_now = now.astimezone(self.settings.local_timezone())
            state_key = f"daily_summary_sent:{local_now.date().isoformat()}"
            if not manual and self.db.get_state(state_key):
                LOGGER.info("Daily summary already sent for %s", local_now.date())
                return 0

            end = now
            start = end - timedelta(hours=self.settings.daily_window_hours)
            emails = self.db.important_between(
                start=start,
                end=end,
                min_importance=self.settings.daily_importance_min,
                limit=self.settings.max_daily_items,
            )
            summary = await self.llm.summarize_daily(emails)
            summary_id = self.db.insert_daily_summary(
                window_start=start,
                window_end=end,
                summary_zh=summary.overview_zh,
                priorities=summary.priorities_zh,
                risks=summary.risks_zh,
                email_ids=[email.email_id for email in emails],
                llm_json=summary.model_dump_json(),
                status="created",
            )
            emails_by_id = {email.email_id: email for email in emails}
            if summary.events:
                events = list(summary.events)
                assigned = {eid for event in events for eid in event.email_ids}
                events.extend(
                    _events_from_emails(
                        [email for email in emails if email.email_id not in assigned]
                    )
                )
            else:
                events = _events_from_emails(emails)
            overview = format_daily_overview(
                start=start, end=end, summary=summary, event_count=len(events)
            )
            message_ids = await self.telegram.send(overview, disable_notification=False)
            for index, event in enumerate(events, start=1):
                text = format_daily_event(
                    index=index,
                    total=len(events),
                    event=event,
                    emails_by_id=emails_by_id,
                )
                message_ids.extend(
                    await self.telegram.send(text, disable_notification=True)
                )
            self.db.mark_daily_summary_sent(summary_id)
            self.db.record_notification(
                notification_type="manual_daily" if manual else "daily",
                summary_id=summary_id,
                window_start=start,
                window_end=end,
                disable_notification=False,
                telegram_message_ids=message_ids,
                status="sent",
            )
            if not manual:
                self.db.set_state(state_key, iso_utc(now))
            return summary_id

    async def _discover_message_ids(self) -> DiscoveredMessages:
        start_history_id = self.db.get_state("gmail_history_id")
        if not start_history_id:
            profile = await asyncio.to_thread(self.gmail.get_profile)
            ids = await asyncio.to_thread(self.gmail.list_recent_message_ids)
            LOGGER.info("Initial Gmail backfill discovered %s messages", len(ids))
            return DiscoveredMessages(
                ids=_dedupe_preserve_order(reversed(ids)),
                next_history_id=profile.history_id,
                suppress_immediate=True,
            )

        try:
            ids, latest_history_id = await asyncio.to_thread(
                self.gmail.list_history_message_ids, start_history_id
            )
        except GmailHistoryExpiredError:
            LOGGER.warning("Gmail historyId expired; falling back to recent sync")
            profile = await asyncio.to_thread(self.gmail.get_profile)
            ids = await asyncio.to_thread(self.gmail.list_recent_message_ids)
            return DiscoveredMessages(
                ids=_dedupe_preserve_order(reversed(ids)),
                next_history_id=profile.history_id,
                suppress_immediate=True,
            )

        return DiscoveredMessages(ids=_dedupe_preserve_order(ids), next_history_id=latest_history_id)

    def _store_processing_email(self, message: GmailMessage, *, suppress_immediate: bool) -> int:
        subject_redaction, body_redaction = self.redactor.redact_many(
            [message.subject, message.text or message.snippet]
        )
        raw_body = message.text or message.snippet
        body_sha256 = hashlib.sha256(raw_body.encode("utf-8", errors="replace")).hexdigest()
        sender_hash = _hash_value(message.from_email, self.settings.hash_salt)
        sanitized_body = body_redaction.text[:50000]
        record = EmailRecord(
            gmail_id=message.gmail_id,
            thread_id=message.thread_id,
            history_id=message.history_id,
            rfc822_message_id=message.rfc822_message_id,
            subject=message.subject or "(no subject)",
            sanitized_subject=subject_redaction.text or "(no subject)",
            from_domain=message.from_domain,
            sender_hash=sender_hash,
            received_at=message_received_at(message),
            internal_date_ms=message.received_at_ms,
            snippet=message.snippet,
            sanitized_body=sanitized_body,
            body_sha256=body_sha256,
            has_attachments=message.has_attachments,
            suppress_immediate=suppress_immediate,
            label_ids_json=label_ids_json(message),
            status="processing",
        )
        return self.db.upsert_email(record)

    async def _ingest_and_maybe_notify(self, item: AnalyzedEmail) -> None:
        analysis = item.analysis
        if analysis.importance < self.settings.event_create_importance_min:
            return
        # Idempotency: if a prior (possibly failed) attempt already linked this email to
        # an event, don't resolve again — avoids duplicate events / double counting.
        # Semantics: event creation is durable/idempotent, the immediate Telegram alert is
        # best-effort. If a crash lands between linking and sending, we skip the re-send
        # here; the 09:00 daily summary still covers the email by received_at as a backstop.
        if self.db.email_has_event(item.email_id):
            return
        # Always create/link the event so the Kanban reflects every important email,
        # including backfill / history-expired re-sync. Only the Telegram send is gated.
        resolved = await self._resolve_event(item)
        if item.suppress_immediate:
            return
        # Notification routing by the event's priority: P0 alerts, P1 silent, P2 nothing.
        if resolved.priority == "P2":
            return
        if self.db.has_notification(email_id=item.email_id, notification_type="immediate"):
            return
        disable_notification = resolved.priority != "P0"
        match = resolved.match
        text = format_immediate_email(
            item,
            event_title=match.title_zh,
            event_context=match.context_zh,
            update_note=match.update_note_zh,
            is_new_event=resolved.is_new,
        )
        try:
            message_ids = await self.telegram.send(text, disable_notification=disable_notification)
            self.db.record_notification(
                notification_type="immediate",
                email_id=item.email_id,
                disable_notification=disable_notification,
                telegram_message_ids=message_ids,
                status="sent",
            )
        except Exception as exc:
            LOGGER.exception("Failed to send immediate Telegram notification")
            self.db.record_notification(
                notification_type="immediate",
                email_id=item.email_id,
                disable_notification=disable_notification,
                telegram_message_ids=[],
                status="error",
                error=str(exc),
            )

    async def _resolve_event(self, item: AnalyzedEmail) -> ResolvedEvent:
        return await resolve_event_for(
            db=self.db,
            llm=self.llm,
            settings=self.settings,
            item=item,
            now=item.received_at if item.suppress_immediate else None,
        )


async def resolve_event_for(
    *,
    db: Database,
    llm: LLMClient,
    settings: Settings,
    item: AnalyzedEmail,
    reopen: bool = True,
    now: datetime | None = None,
) -> ResolvedEvent:
    """Match an analyzed email to an existing event or create a new one, and link it.

    Shared by the live ingest path and the one-time backfill command. `reopen` controls
    whether matching a done/archived event moves it back to 待处理 — true for live mail,
    false for backfill (a historical email must not change the current board lifecycle).
    `now` is the reference time for the candidate window: live mail uses utc_now(), while
    historical/suppressed paths pass the email's received_at so old emails can still match
    contemporaneous history.
    """
    analysis = item.analysis
    # Candidates: recent active events, plus done/archived events still on the board
    # (within the auto-hide window) so they can be reopened.
    open_events = db.list_event_match_candidates(
        open_within_days=settings.event_window_days,
        reopen_within_days=settings.done_auto_hide_days,
        limit=settings.event_match_max_open,
        now=now,
    )
    match = await llm.resolve_event(
        subject=item.sanitized_subject,
        from_domain=item.from_domain,
        received_at=item.received_at.isoformat(),
        summary_zh=analysis.summary_zh,
        action_items=analysis.action_items,
        key_dates=analysis.key_dates,
        importance=analysis.importance,
        open_events=open_events,
    )
    valid_ids = {event.id for event in open_events}
    matched_id = match.matched_event_id if match.matched_event_id in valid_ids else None
    # The event match is a second LLM pass; never let it downgrade the email's own
    # analyzed importance (which would silently mute a genuinely urgent email).
    event_importance = max(analysis.importance, match.importance)
    mapped_priority = settings.priority_for_importance(event_importance)
    if matched_id is not None:
        db.update_event(
            matched_id,
            title_zh=match.title_zh,
            context_zh=match.context_zh,
            category=match.category,
            importance=event_importance,
            last_activity_at=item.received_at,
            mapped_priority=mapped_priority,
            update_note_zh=match.update_note_zh,
            reopen=reopen,
            link_email_id=item.email_id,
        )
        event = db.get_event(matched_id)
        priority = event.priority if event else mapped_priority
        event_id = matched_id
        is_new = False
    else:
        event_id = db.create_event(
            title_zh=match.title_zh,
            context_zh=match.context_zh,
            category=match.category,
            importance=event_importance,
            last_activity_at=item.received_at,
            priority=mapped_priority,
            last_update_zh=match.update_note_zh,
            link_email_id=item.email_id,
        )
        priority = mapped_priority
        is_new = True
    return ResolvedEvent(
        match=match.model_copy(update={"matched_event_id": matched_id}),
        event_id=event_id,
        priority=priority,
        is_new=is_new,
    )


async def backfill_events(
    *,
    db: Database,
    llm: LLMClient,
    settings: Settings,
    limit: int = 1000,
) -> int:
    """Aggregate already-processed emails that predate the event model into events.

    Processes them in chronological order (so aggregation builds up naturally) and never
    sends notifications. Returns the number of emails newly attached to an event.
    """
    items = db.list_unlinked_processed(
        min_importance=settings.event_create_importance_min, limit=limit
    )
    count = 0
    for item in items:
        if db.email_has_event(item.email_id):
            continue
        # Backfill is historical: never reopen a done/archived event, and evaluate the
        # candidate window relative to the email's own time so same-event old mail merges.
        await resolve_event_for(
            db=db,
            llm=llm,
            settings=settings,
            item=item,
            reopen=False,
            now=item.received_at,
        )
        count += 1
    return count


async def reaggregate_event(
    *,
    db: Database,
    llm: LLMClient,
    settings: Settings,
    event_id: int,
) -> bool:
    """Re-run the LLM over an event's source emails and rewrite its title/context/priority.

    Manual web action to fix LLM aggregation. Returns False if the event has no emails.
    """
    emails = db.list_emails_for_event(event_id)
    if not emails:
        return False
    aggregation = await llm.reaggregate_event(emails)
    db.apply_reaggregation(
        event_id,
        title_zh=aggregation.title_zh,
        context_zh=aggregation.context_zh,
        category=aggregation.category,
        importance=aggregation.importance,
        priority=settings.priority_for_importance(aggregation.importance),
    )
    return True


def _events_from_emails(emails: list[AnalyzedEmail]) -> list[DailyEvent]:
    return [
        DailyEvent(
            title_zh=email.subject.strip() or email.analysis.summary_zh.strip()[:30] or "未命名事件",
            summary_zh=email.analysis.summary_zh,
            importance=email.analysis.importance,
            email_ids=[email.email_id],
            action_items=email.analysis.action_items,
            key_dates=email.analysis.key_dates,
        )
        for email in emails
    ]


def _hash_value(value: str | None, salt: str) -> str | None:
    if not value:
        return None
    payload = f"{salt}:{value.lower()}".encode()
    return hashlib.sha256(payload).hexdigest()


def _dedupe_preserve_order(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _is_missing_gmail_message_error(exc: Exception) -> bool:
    response = getattr(exc, "resp", None)
    return getattr(response, "status", None) == 404
