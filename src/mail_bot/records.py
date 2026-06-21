from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mail_bot.models import EmailAnalysis


@dataclass(frozen=True)
class EmailRecord:
    gmail_id: str
    thread_id: str | None
    history_id: str | None
    rfc822_message_id: str | None
    subject: str
    sanitized_subject: str
    from_domain: str | None
    sender_hash: str | None
    received_at: datetime
    internal_date_ms: int | None
    snippet: str
    sanitized_body: str
    body_sha256: str
    has_attachments: bool
    suppress_immediate: bool
    label_ids_json: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class StoredEmail:
    id: int
    gmail_id: str
    subject: str
    sanitized_subject: str
    from_domain: str | None
    received_at: datetime
    sanitized_body: str
    status: str
    suppress_immediate: bool


@dataclass(frozen=True)
class EventSummary:
    id: int
    title_zh: str
    context_zh: str
    category: str
    importance: int
    email_count: int
    last_activity_at: datetime
    priority: str = "P1"
    status: str = "todo"
    last_update_zh: str = ""
    sort_order: float = 0.0
    archived_at: datetime | None = None
    title_overridden: bool = False
    context_overridden: bool = False
    priority_overridden: bool = False


@dataclass(frozen=True)
class AnalyzedEmail:
    email_id: int
    gmail_id: str
    subject: str
    sanitized_subject: str
    from_domain: str | None
    received_at: datetime
    analysis: EmailAnalysis
    suppress_immediate: bool = False
