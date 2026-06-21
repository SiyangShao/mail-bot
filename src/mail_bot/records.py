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
class AnalyzedEmail:
    email_id: int
    gmail_id: str
    subject: str
    sanitized_subject: str
    from_domain: str | None
    received_at: datetime
    analysis: EmailAnalysis
    suppress_immediate: bool = False
