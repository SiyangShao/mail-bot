from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mail_bot.models import EmailAnalysis, more_urgent, priority_for_importance
from mail_bot.records import AnalyzedEmail, EmailRecord, EventSummary, StoredEmail
from mail_bot.time_utils import iso_utc, parse_iso, utc_now


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
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

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title_zh TEXT NOT NULL,
                    context_zh TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '其他',
                    importance INTEGER NOT NULL DEFAULT 1,
                    email_count INTEGER NOT NULL DEFAULT 0,
                    last_activity_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_events_last_activity
                    ON events(last_activity_at DESC);
                """
            )
            _ensure_column(conn, "emails", "suppress_immediate", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "emails", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "emails", "next_retry_at", "TEXT")
            _ensure_column(conn, "emails", "event_id", "INTEGER")
            _ensure_column(conn, "emails", "event_locked", "INTEGER NOT NULL DEFAULT 0")
            # Kanban / lifecycle columns on events.
            _ensure_column(conn, "events", "status", "TEXT NOT NULL DEFAULT 'todo'")
            _ensure_column(conn, "events", "priority", "TEXT NOT NULL DEFAULT 'P1'")
            _ensure_column(conn, "events", "sort_order", "REAL NOT NULL DEFAULT 0")
            _ensure_column(conn, "events", "last_update_zh", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "events", "archived_at", "TEXT")
            _ensure_column(conn, "events", "title_overridden", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "events", "context_overridden", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "events", "priority_overridden", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_board "
                "ON events(status, sort_order DESC)"
            )

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

    def create_event(
        self,
        *,
        title_zh: str,
        context_zh: str,
        category: str,
        importance: int,
        last_activity_at: datetime,
        priority: str = "P1",
        status: str = "todo",
        last_update_zh: str = "",
        sort_order: float | None = None,
        link_email_id: int | None = None,
    ) -> int:
        now_dt = utc_now()
        now = iso_utc(now_dt)
        if sort_order is None:
            sort_order = now_dt.timestamp()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO events (
                    title_zh, context_zh, category, importance, email_count,
                    last_activity_at, priority, status, last_update_zh, sort_order,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    title_zh,
                    context_zh,
                    category,
                    importance,
                    iso_utc(last_activity_at),
                    priority,
                    status,
                    last_update_zh,
                    sort_order,
                    now,
                    now,
                ),
            ).fetchone()
            event_id = int(row["id"])
            # Link the source email atomically so create+link is one transaction.
            if link_email_id is not None:
                conn.execute(
                    "UPDATE emails SET event_id = ?, updated_at = ? WHERE id = ?",
                    (event_id, now, link_email_id),
                )
            return event_id

    def update_event(
        self,
        event_id: int,
        *,
        title_zh: str | None = None,
        context_zh: str,
        category: str | None = None,
        importance: int,
        last_activity_at: datetime,
        mapped_priority: str = "P1",
        update_note_zh: str = "",
        reopen: bool = True,
        link_email_id: int | None = None,
    ) -> None:
        """Merge a new email into an existing event (LLM-driven aggregation path).

        Linking the email and bumping the event counters happen in one transaction so a
        retry cannot double-count. Manual overrides are respected (title/context/priority
        frozen once edited), priority only escalates, and a done/archived event reopens.
        """
        with self.connect() as conn:
            if link_email_id is not None:
                conn.execute(
                    "UPDATE emails SET event_id = ?, updated_at = ? WHERE id = ?",
                    (event_id, iso_utc(utc_now()), link_email_id),
                )
            row = conn.execute(
                """
                SELECT priority, priority_overridden, title_overridden, context_overridden,
                       status, archived_at
                FROM events WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Event not found: {event_id}")

            if row["priority_overridden"]:
                new_priority = row["priority"]
            else:
                new_priority = more_urgent(row["priority"], mapped_priority)

            apply_title = title_zh if not row["title_overridden"] else None
            new_context = context_zh if not row["context_overridden"] else None
            reopened = reopen and (row["status"] == "done" or row["archived_at"] is not None)
            new_status = "todo" if reopened else row["status"]
            new_archived = None if reopened else row["archived_at"]

            conn.execute(
                """
                UPDATE events
                SET title_zh = COALESCE(?, title_zh),
                    context_zh = COALESCE(?, context_zh),
                    category = COALESCE(?, category),
                    importance = MAX(importance, ?),
                    priority = ?,
                    status = ?,
                    archived_at = ?,
                    last_update_zh = CASE WHEN ? != '' THEN ? ELSE last_update_zh END,
                    email_count = email_count + 1,
                    last_activity_at = MAX(last_activity_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    apply_title,
                    new_context,
                    category,
                    importance,
                    new_priority,
                    new_status,
                    new_archived,
                    update_note_zh,
                    update_note_zh,
                    iso_utc(last_activity_at),
                    iso_utc(utc_now()),
                    event_id,
                ),
            )

    def link_email_event(self, email_id: int, event_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE emails SET event_id = ?, updated_at = ? WHERE id = ?",
                (event_id, iso_utc(utc_now()), email_id),
            )

    def email_has_event(self, email_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM emails WHERE id = ?", (email_id,)
            ).fetchone()
            return row is not None and row["event_id"] is not None

    def list_open_events(
        self,
        *,
        within_days: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[EventSummary]:
        cutoff = iso_utc((now or utc_now()) - timedelta(days=within_days))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE last_activity_at >= ?
                ORDER BY last_activity_at DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def list_event_match_candidates(
        self,
        *,
        open_within_days: int,
        reopen_within_days: int,
        limit: int,
        now: datetime | None = None,
    ) -> list[EventSummary]:
        """Candidates for matching a new email: recent active events plus done/archived
        events still within the auto-hide window (so they can be reopened).

        `now` is the reference time and also the upper bound: only events at or before it
        qualify. For backfill (now=email.received_at) this keeps newer events from crowding
        out the contemporaneous history that the old email actually belongs to."""
        ref = now or utc_now()
        as_of = iso_utc(ref)
        open_cutoff = iso_utc(ref - timedelta(days=open_within_days))
        reopen_cutoff = iso_utc(ref - timedelta(days=max(open_within_days, reopen_within_days)))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE last_activity_at <= ?
                  AND (
                    (status != 'done' AND archived_at IS NULL AND last_activity_at >= ?)
                    OR
                    ((status = 'done' OR archived_at IS NOT NULL) AND last_activity_at >= ?)
                  )
                ORDER BY last_activity_at DESC
                LIMIT ?
                """,
                (as_of, open_cutoff, reopen_cutoff, limit),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def get_event(self, event_id: int) -> EventSummary | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return self._event_from_row(row) if row else None

    # --- Kanban board / lifecycle / manual correction ---------------------------------

    def list_board_events(
        self,
        *,
        hide_done_after_days: int,
        include_hidden: bool = False,
        now: datetime | None = None,
    ) -> list[EventSummary]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if not include_hidden:
            cutoff = iso_utc((now or utc_now()) - timedelta(days=hide_done_after_days))
            query += " WHERE archived_at IS NULL AND NOT (status = 'done' AND last_activity_at < ?)"
            params.append(cutoff)
        query += " ORDER BY sort_order DESC, last_activity_at DESC, id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._event_from_row(row) for row in rows]

    def board_revision(self) -> str:
        """Cheap fingerprint of board state; changes whenever any event is written.

        ``updated_at`` is bumped on every relevant mutation (new email linked,
        new event, reorder, edit, archive), so ``COUNT + MAX(updated_at)`` is a
        reliable change token for the live-refresh stream. Not a counter — it is
        a snapshot of current state, so it never grows unbounded.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS m FROM events"
            ).fetchone()
        return f"{row['n']}:{row['m']}"

    def list_emails_for_event(self, event_id: int) -> list[AnalyzedEmail]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.id AS email_id, e.gmail_id, e.subject, e.sanitized_subject,
                    e.from_domain, e.received_at, e.suppress_immediate,
                    a.importance, a.information_density, a.category, a.summary_zh,
                    a.requires_action, a.action_items_json, a.key_dates_json,
                    a.rationale_zh, a.confidence, a.llm_json
                FROM emails e
                JOIN email_analysis a ON a.email_id = e.id
                WHERE e.event_id = ?
                ORDER BY e.received_at ASC
                """,
                (event_id,),
            ).fetchall()
            return [self._analyzed_from_joined_row(row) for row in rows]

    def set_column_order(self, *, status: str, ordered_ids: list[int]) -> None:
        now = iso_utc(utc_now())
        count = len(ordered_ids)
        with self.connect() as conn:
            for index, event_id in enumerate(ordered_ids):
                conn.execute(
                    "UPDATE events SET status = ?, sort_order = ?, updated_at = ? WHERE id = ?",
                    (status, float(count - index), now, int(event_id)),
                )

    def edit_event_fields(
        self,
        event_id: int,
        *,
        title_zh: str | None = None,
        context_zh: str | None = None,
        priority: str | None = None,
        category: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if title_zh is not None:
            sets.extend(["title_zh = ?", "title_overridden = 1"])
            params.append(title_zh)
        if context_zh is not None:
            sets.extend(["context_zh = ?", "context_overridden = 1"])
            params.append(context_zh)
        if priority is not None:
            sets.extend(["priority = ?", "priority_overridden = 1"])
            params.append(priority)
        if category is not None:
            sets.append("category = ?")
            params.append(category)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(iso_utc(utc_now()))
        params.append(event_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", params)

    def set_event_archived(self, event_id: int, archived: bool) -> None:
        now = iso_utc(utc_now())
        with self.connect() as conn:
            conn.execute(
                "UPDATE events SET archived_at = ?, updated_at = ? WHERE id = ?",
                (now if archived else None, now, event_id),
            )

    def merge_events(self, target_id: int, source_ids: list[int]) -> None:
        source_ids = [int(s) for s in source_ids if int(s) != int(target_id)]
        if not source_ids:
            return
        placeholders = ",".join("?" * len(source_ids))
        with self.connect() as conn:
            target = conn.execute(
                "SELECT * FROM events WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise ValueError(f"Target event not found: {target_id}")
            sources = conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})", source_ids
            ).fetchall()
            merged_priority = str(target["priority"])
            merged_importance = int(target["importance"])
            contexts = [str(target["context_zh"])]
            for src in sources:
                merged_priority = more_urgent(merged_priority, str(src["priority"]))
                merged_importance = max(merged_importance, int(src["importance"]))
                if src["context_zh"]:
                    contexts.append(str(src["context_zh"]))
            merged_context = "\n".join(c for c in contexts if c.strip())
            conn.execute(
                f"UPDATE emails SET event_id = ? WHERE event_id IN ({placeholders})",
                [target_id, *source_ids],
            )
            conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", source_ids)
            conn.execute(
                """
                UPDATE events
                SET context_zh = ?, priority = ?, importance = ?, updated_at = ?
                WHERE id = ?
                """,
                (merged_context, merged_priority, merged_importance, iso_utc(utc_now()), target_id),
            )
            self._recompute_event_aggregates(conn, target_id)

    def move_email_to_event(
        self, email_id: int, target_event_id: int, *, p0_min: int = 5, p1_min: int = 4
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM emails WHERE id = ?", (email_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Email not found: {email_id}")
            if conn.execute(
                "SELECT 1 FROM events WHERE id = ?", (target_event_id,)
            ).fetchone() is None:
                raise ValueError(f"Target event not found: {target_event_id}")
            old_event = row["event_id"]
            conn.execute(
                "UPDATE emails SET event_id = ?, event_locked = 1, updated_at = ? WHERE id = ?",
                (target_event_id, iso_utc(utc_now()), email_id),
            )
            if old_event and int(old_event) != int(target_event_id):
                self._recompute_event_aggregates(
                    conn, int(old_event), remap=True, p0_min=p0_min, p1_min=p1_min
                )
            self._recompute_event_aggregates(
                conn, target_event_id, remap=True, p0_min=p0_min, p1_min=p1_min
            )

    def split_email_to_new_event(
        self, email_id: int, *, p0_min: int = 5, p1_min: int = 4
    ) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT e.event_id, e.subject, e.received_at,
                       a.summary_zh, a.category, a.importance
                FROM emails e
                LEFT JOIN email_analysis a ON a.email_id = e.id
                WHERE e.id = ?
                """,
                (email_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Email not found: {email_id}")
            old_event = row["event_id"]
            importance = int(row["importance"] or 3)
            now = iso_utc(utc_now())
            new_row = conn.execute(
                """
                INSERT INTO events (
                    title_zh, context_zh, category, importance, email_count,
                    last_activity_at, priority, status, last_update_zh, sort_order,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, 'todo', '', ?, ?, ?)
                RETURNING id
                """,
                (
                    (row["subject"] or "未命名事件")[:120],
                    row["summary_zh"] or "",
                    row["category"] or "其他",
                    importance,
                    row["received_at"],
                    priority_for_importance(importance, p0_min=p0_min, p1_min=p1_min),
                    utc_now().timestamp(),
                    now,
                    now,
                ),
            ).fetchone()
            new_event_id = int(new_row["id"])
            conn.execute(
                "UPDATE emails SET event_id = ?, event_locked = 1, updated_at = ? WHERE id = ?",
                (new_event_id, now, email_id),
            )
            if old_event and int(old_event) != new_event_id:
                self._recompute_event_aggregates(
                    conn, int(old_event), remap=True, p0_min=p0_min, p1_min=p1_min
                )
            return new_event_id

    def apply_reaggregation(
        self,
        event_id: int,
        *,
        title_zh: str,
        context_zh: str,
        category: str,
        importance: int,
        priority: str | None = None,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT priority_overridden FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Event not found: {event_id}")
            set_priority = priority is not None and not row["priority_overridden"]
            base = (
                "UPDATE events SET title_zh = ?, context_zh = ?, category = ?, importance = ?, "
                "title_overridden = 0, context_overridden = 0"
            )
            params: list[Any] = [title_zh, context_zh, category, importance]
            if set_priority:
                base += ", priority = ?"
                params.append(priority)
            base += ", updated_at = ? WHERE id = ?"
            params.extend([iso_utc(utc_now()), event_id])
            conn.execute(base, params)

    def _recompute_event_aggregates(
        self,
        conn: sqlite3.Connection,
        event_id: int,
        *,
        remap: bool = False,
        p0_min: int = 5,
        p1_min: int = 4,
    ) -> None:
        agg = conn.execute(
            """
            SELECT COUNT(*) AS c, MAX(e.received_at) AS last, MAX(a.importance) AS imp
            FROM emails e
            LEFT JOIN email_analysis a ON a.email_id = e.id
            WHERE e.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        count = int(agg["c"])
        if count == 0:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return
        now = iso_utc(utc_now())
        if remap and agg["imp"] is not None:
            # Manual correction (split/move) changed the email set: re-derive importance,
            # and the auto-mapped priority unless the user pinned it.
            new_importance = int(agg["imp"])
            overridden = conn.execute(
                "SELECT priority_overridden FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if overridden is not None and not overridden["priority_overridden"]:
                conn.execute(
                    """
                    UPDATE events
                    SET email_count = ?, last_activity_at = COALESCE(?, last_activity_at),
                        importance = ?, priority = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (count, agg["last"], new_importance,
                     priority_for_importance(new_importance, p0_min=p0_min, p1_min=p1_min),
                     now, event_id),
                )
                return
            conn.execute(
                """
                UPDATE events
                SET email_count = ?, last_activity_at = COALESCE(?, last_activity_at),
                    importance = ?, updated_at = ?
                WHERE id = ?
                """,
                (count, agg["last"], new_importance, now, event_id),
            )
            return
        conn.execute(
            """
            UPDATE events
            SET email_count = ?, last_activity_at = COALESCE(?, last_activity_at), updated_at = ?
            WHERE id = ?
            """,
            (count, agg["last"], now, event_id),
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

    def list_unlinked_processed(
        self, *, min_importance: int, limit: int
    ) -> list[AnalyzedEmail]:
        """Processed emails above the gate that aren't attached to any event yet
        (for the one-time backfill), oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.id AS email_id, e.gmail_id, e.subject, e.sanitized_subject,
                    e.from_domain, e.received_at, e.suppress_immediate,
                    a.importance, a.information_density, a.category, a.summary_zh,
                    a.requires_action, a.action_items_json, a.key_dates_json,
                    a.rationale_zh, a.confidence, a.llm_json
                FROM emails e
                JOIN email_analysis a ON a.email_id = e.id
                WHERE e.status = 'processed' AND e.event_id IS NULL AND a.importance >= ?
                ORDER BY e.received_at ASC
                LIMIT ?
                """,
                (min_importance, limit),
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

    def _event_from_row(self, row: sqlite3.Row) -> EventSummary:
        archived_raw = row["archived_at"]
        return EventSummary(
            id=int(row["id"]),
            title_zh=str(row["title_zh"]),
            context_zh=str(row["context_zh"]),
            category=str(row["category"]),
            importance=int(row["importance"]),
            email_count=int(row["email_count"]),
            last_activity_at=parse_iso(str(row["last_activity_at"])),
            priority=str(row["priority"]),
            status=str(row["status"]),
            last_update_zh=str(row["last_update_zh"]),
            sort_order=float(row["sort_order"]),
            archived_at=parse_iso(str(archived_raw)) if archived_raw else None,
            title_overridden=bool(row["title_overridden"]),
            context_overridden=bool(row["context_overridden"]),
            priority_overridden=bool(row["priority_overridden"]),
        )

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
