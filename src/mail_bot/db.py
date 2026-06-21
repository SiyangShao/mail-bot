from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mail_bot.models import EmailAnalysis
from mail_bot.records import AnalyzedEmail, EmailRecord, StoredEmail
from mail_bot.time_utils import iso_utc, parse_iso, utc_now


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT,
                    history_id TEXT,
                    rfc822_message_id TEXT,
                    subject TEXT NOT NULL DEFAULT '',
                    sanitized_subject TEXT NOT NULL DEFAULT '',
                    from_domain TEXT,
                    sender_hash TEXT,
                    received_at TEXT NOT NULL,
                    internal_date_ms INTEGER,
                    snippet TEXT NOT NULL DEFAULT '',
                    sanitized_body TEXT NOT NULL DEFAULT '',
                    body_sha256 TEXT NOT NULL DEFAULT '',
                    has_attachments INTEGER NOT NULL DEFAULT 0,
                    suppress_immediate INTEGER NOT NULL DEFAULT 0,
                    label_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    processed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS email_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id INTEGER NOT NULL UNIQUE REFERENCES emails(id) ON DELETE CASCADE,
                    importance INTEGER NOT NULL,
                    information_density INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    summary_zh TEXT NOT NULL,
                    requires_action INTEGER NOT NULL,
                    action_items_json TEXT NOT NULL DEFAULT '[]',
                    key_dates_json TEXT NOT NULL DEFAULT '[]',
                    rationale_zh TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    llm_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
                    summary_id INTEGER,
                    type TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    disable_notification INTEGER NOT NULL,
                    telegram_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    error TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_immediate
                    ON notifications(email_id, type)
                    WHERE type = 'immediate';

                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    summary_zh TEXT NOT NULL,
                    priorities_json TEXT NOT NULL DEFAULT '[]',
                    risks_json TEXT NOT NULL DEFAULT '[]',
                    email_ids_json TEXT NOT NULL DEFAULT '[]',
                    llm_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            _ensure_column(conn, "emails", "suppress_immediate", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "emails", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "emails", "next_retry_at", "TEXT")

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return None if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, iso_utc(utc_now())),
            )

    def upsert_email(self, record: EmailRecord) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO emails (
                    gmail_id, thread_id, history_id, rfc822_message_id, subject, sanitized_subject,
                    from_domain, sender_hash, received_at, internal_date_ms, snippet, sanitized_body,
                    body_sha256, has_attachments, suppress_immediate, label_ids_json, status, error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gmail_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    history_id = excluded.history_id,
                    rfc822_message_id = excluded.rfc822_message_id,
                    subject = excluded.subject,
                    sanitized_subject = excluded.sanitized_subject,
                    from_domain = excluded.from_domain,
                    sender_hash = excluded.sender_hash,
                    received_at = excluded.received_at,
                    internal_date_ms = excluded.internal_date_ms,
                    snippet = excluded.snippet,
                    sanitized_body = excluded.sanitized_body,
                    body_sha256 = excluded.body_sha256,
                    has_attachments = excluded.has_attachments,
                    suppress_immediate = CASE
                        WHEN emails.suppress_immediate = 1 OR excluded.suppress_immediate = 1 THEN 1
                        ELSE 0
                    END,
                    label_ids_json = excluded.label_ids_json,
                    status = excluded.status,
                    error = excluded.error,
                    next_retry_at = NULL,
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (
                    record.gmail_id,
                    record.thread_id,
                    record.history_id,
                    record.rfc822_message_id,
                    record.subject,
                    record.sanitized_subject,
                    record.from_domain,
                    record.sender_hash,
                    iso_utc(record.received_at),
                    record.internal_date_ms,
                    record.snippet,
                    record.sanitized_body,
                    record.body_sha256,
                    int(record.has_attachments),
                    int(record.suppress_immediate),
                    record.label_ids_json,
                    record.status,
                    record.error,
                    iso_utc(utc_now()),
                ),
            ).fetchone()
            return int(row["id"])

    def get_email_by_gmail_id(self, gmail_id: str) -> StoredEmail | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
            return self._stored_email_from_row(row) if row else None

    def mark_email_processed(self, email_id: int) -> None:
        now = iso_utc(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emails
                SET status = 'processed',
                    error = NULL,
                    next_retry_at = NULL,
                    processed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, email_id),
            )

    def mark_email_error(self, email_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE emails
                SET status = 'error', error = ?, next_retry_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], iso_utc(utc_now()), email_id),
            )

    def mark_email_retry(
        self,
        email_id: int,
        error: str,
        *,
        max_attempts: int,
        backoff_seconds: int,
        max_backoff_seconds: int,
    ) -> str:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT retry_count FROM emails WHERE id = ?", (email_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Email id not found: {email_id}")
            attempt = int(row["retry_count"]) + 1
            status = "error" if attempt >= max_attempts else "retry"
            if status == "retry":
                delay_seconds = min(
                    backoff_seconds * (2 ** max(0, attempt - 1)),
                    max_backoff_seconds,
                )
                next_retry_at = iso_utc(now + timedelta(seconds=delay_seconds))
            else:
                next_retry_at = None
            conn.execute(
                """
                UPDATE emails
                SET status = ?,
                    error = ?,
                    retry_count = ?,
                    next_retry_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, error[:2000], attempt, next_retry_at, iso_utc(now), email_id),
            )
            return status

    def insert_analysis(self, email_id: int, analysis: EmailAnalysis, model: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO email_analysis (
                    email_id, importance, information_density, category, summary_zh,
                    requires_action, action_items_json, key_dates_json, rationale_zh,
                    confidence, llm_json, model
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email_id) DO UPDATE SET
                    importance = excluded.importance,
                    information_density = excluded.information_density,
                    category = excluded.category,
                    summary_zh = excluded.summary_zh,
                    requires_action = excluded.requires_action,
                    action_items_json = excluded.action_items_json,
                    key_dates_json = excluded.key_dates_json,
                    rationale_zh = excluded.rationale_zh,
                    confidence = excluded.confidence,
                    llm_json = excluded.llm_json,
                    model = excluded.model
                """,
                (
                    email_id,
                    analysis.importance,
                    analysis.information_density,
                    analysis.category,
                    analysis.summary_zh,
                    int(analysis.requires_action),
                    json.dumps(analysis.action_items, ensure_ascii=False),
                    json.dumps([item.model_dump() for item in analysis.key_dates], ensure_ascii=False),
                    analysis.rationale_zh,
                    analysis.confidence,
                    analysis.model_dump_json(),
                    model,
                ),
            )

    def get_analysis_for_email(self, email_id: int) -> EmailAnalysis | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_analysis WHERE email_id = ?", (email_id,)
            ).fetchone()
            if row is None:
                return None
            return self._analysis_from_row(row)

    def has_notification(self, *, email_id: int | None, notification_type: str) -> bool:
        with self.connect() as conn:
            if email_id is None:
                row = conn.execute(
                    "SELECT 1 FROM notifications WHERE email_id IS NULL AND type = ? LIMIT 1",
                    (notification_type,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM notifications WHERE email_id = ? AND type = ? LIMIT 1",
                    (email_id, notification_type),
                ).fetchone()
            return row is not None

    def record_notification(
        self,
        *,
        notification_type: str,
        disable_notification: bool,
        telegram_message_ids: list[int],
        status: str,
        email_id: int | None = None,
        summary_id: int | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    email_id, summary_id, type, window_start, window_end, disable_notification,
                    telegram_message_ids_json, status, error, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email_id,
                    summary_id,
                    notification_type,
                    iso_utc(window_start) if window_start else None,
                    iso_utc(window_end) if window_end else None,
                    int(disable_notification),
                    json.dumps(telegram_message_ids),
                    status,
                    error,
                    iso_utc(utc_now()) if status == "sent" else None,
                ),
            )

    def insert_daily_summary(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        summary_zh: str,
        priorities: list[str],
        risks: list[str],
        email_ids: list[int],
        llm_json: str,
        status: str,
        error: str | None = None,
    ) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO daily_summaries (
                    window_start, window_end, summary_zh, priorities_json, risks_json,
                    email_ids_json, llm_json, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    iso_utc(window_start),
                    iso_utc(window_end),
                    summary_zh,
                    json.dumps(priorities, ensure_ascii=False),
                    json.dumps(risks, ensure_ascii=False),
                    json.dumps(email_ids),
                    llm_json,
                    status,
                    error,
                ),
            ).fetchone()
            return int(row["id"])

    def mark_daily_summary_sent(self, summary_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE daily_summaries SET status = 'sent', sent_at = ? WHERE id = ?",
                (iso_utc(utc_now()), summary_id),
            )

    def list_recent(self, limit: int) -> list[AnalyzedEmail]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.id AS email_id,
                    e.gmail_id,
                    e.subject,
                    e.sanitized_subject,
                    e.from_domain,
                    e.received_at,
                    e.suppress_immediate,
                    a.importance,
                    a.information_density,
                    a.category,
                    a.summary_zh,
                    a.requires_action,
                    a.action_items_json,
                    a.key_dates_json,
                    a.rationale_zh,
                    a.confidence,
                    a.llm_json
                FROM emails e
                JOIN email_analysis a ON a.email_id = e.id
                WHERE e.status = 'processed'
                ORDER BY e.received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._analyzed_from_joined_row(row) for row in rows]

    def important_between(
        self,
        *,
        start: datetime,
        end: datetime,
        min_importance: int,
        limit: int,
    ) -> list[AnalyzedEmail]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.id AS email_id,
                    e.gmail_id,
                    e.subject,
                    e.sanitized_subject,
                    e.from_domain,
                    e.received_at,
                    e.suppress_immediate,
                    a.importance,
                    a.information_density,
                    a.category,
                    a.summary_zh,
                    a.requires_action,
                    a.action_items_json,
                    a.key_dates_json,
                    a.rationale_zh,
                    a.confidence,
                    a.llm_json
                FROM emails e
                JOIN email_analysis a ON a.email_id = e.id
                WHERE e.status = 'processed'
                  AND e.received_at >= ?
                  AND e.received_at < ?
                  AND a.importance >= ?
                ORDER BY a.importance DESC, a.information_density DESC, e.received_at DESC
                LIMIT ?
                """,
                (iso_utc(start), iso_utc(end), min_importance, limit),
            ).fetchall()
            return [self._analyzed_from_joined_row(row) for row in rows]

    def list_retryable_gmail_ids(
        self,
        *,
        limit: int = 50,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> list[str]:
        cutoff = iso_utc(now or utc_now())
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT gmail_id
                FROM emails
                WHERE status = 'retry'
                  AND retry_count < ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (max_attempts, cutoff, limit),
            ).fetchall()
            return [str(row["gmail_id"]) for row in rows]

    def counts(self) -> dict[str, Any]:
        with self.connect() as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM emails GROUP BY status"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS count FROM emails").fetchone()["count"]
            analyzed = conn.execute("SELECT COUNT(*) AS count FROM email_analysis").fetchone()["count"]
            return {
                "total_emails": int(total),
                "analyzed_emails": int(analyzed),
                "by_status": {row["status"]: int(row["count"]) for row in status_rows},
            }

    def _stored_email_from_row(self, row: sqlite3.Row) -> StoredEmail:
        return StoredEmail(
            id=int(row["id"]),
            gmail_id=str(row["gmail_id"]),
            subject=str(row["subject"]),
            sanitized_subject=str(row["sanitized_subject"]),
            from_domain=row["from_domain"],
            received_at=parse_iso(str(row["received_at"])),
            sanitized_body=str(row["sanitized_body"]),
            status=str(row["status"]),
            suppress_immediate=bool(row["suppress_immediate"]),
        )

    def _analysis_from_row(self, row: sqlite3.Row) -> EmailAnalysis:
        try:
            llm_json = row["llm_json"]
        except (IndexError, KeyError):
            llm_json = None
        if llm_json:
            try:
                return EmailAnalysis.model_validate_json(str(llm_json))
            except Exception:
                pass
        payload = {
            "importance": row["importance"],
            "information_density": row["information_density"],
            "category": row["category"],
            "summary_zh": row["summary_zh"],
            "requires_action": bool(row["requires_action"]),
            "action_items": json.loads(row["action_items_json"] or "[]"),
            "key_dates": json.loads(row["key_dates_json"] or "[]"),
            "rationale_zh": row["rationale_zh"] or "",
            "confidence": row["confidence"] or 0,
        }
        return EmailAnalysis.model_validate(payload)

    def _analyzed_from_joined_row(self, row: sqlite3.Row) -> AnalyzedEmail:
        return AnalyzedEmail(
            email_id=int(row["email_id"]),
            gmail_id=str(row["gmail_id"]),
            subject=str(row["subject"]),
            sanitized_subject=str(row["sanitized_subject"]),
            from_domain=row["from_domain"],
            received_at=parse_iso(str(row["received_at"])),
            analysis=self._analysis_from_row(row),
            suppress_immediate=bool(row["suppress_immediate"]),
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
