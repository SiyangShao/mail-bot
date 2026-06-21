from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _int_env(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _bool_env(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain integers") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    sqlite_path: Path
    google_credentials_path: Path
    google_token_path: Path
    gmail_oauth_port: int
    gmail_poll_seconds: int
    gmail_backfill_days: int
    gmail_query: str

    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_allowed_user_ids: frozenset[int]

    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: int
    llm_max_retries: int
    llm_json_mode: bool
    llm_user_id: str | None

    timezone: str
    log_level: str
    hash_salt: str
    urgent_importance_min: int
    urgent_info_density_min: int
    daily_importance_min: int
    daily_summary_time: str
    daily_window_hours: int
    max_email_chars_for_llm: int
    max_daily_items: int
    email_retry_max_attempts: int
    email_retry_backoff_seconds: int
    email_retry_max_backoff_seconds: int

    @classmethod
    def from_env(cls, *, require_runtime: bool = True) -> Settings:
        data_dir = Path(_env("DATA_DIR", "data") or "data")
        sqlite_path = Path(_env("SQLITE_PATH", str(data_dir / "mail_bot.sqlite3")) or "")
        settings = cls(
            data_dir=data_dir,
            sqlite_path=sqlite_path,
            google_credentials_path=Path(
                _env("GOOGLE_CREDENTIALS_PATH", "secrets/google_credentials.json") or ""
            ),
            google_token_path=Path(_env("GOOGLE_TOKEN_PATH", str(data_dir / "google_token.json")) or ""),
            gmail_oauth_port=_int_env("GMAIL_OAUTH_PORT", 8080),
            gmail_poll_seconds=_int_env("GMAIL_POLL_SECONDS", 120),
            gmail_backfill_days=_int_env("GMAIL_BACKFILL_DAYS", 2),
            gmail_query=_env("GMAIL_QUERY", "in:anywhere") or "",
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            telegram_allowed_user_ids=_parse_user_ids(_env("TELEGRAM_ALLOWED_USER_IDS")),
            llm_api_key=_env("LLM_API_KEY"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.deepseek.com") or "",
            llm_model=_env("LLM_MODEL", "deepseek-v4-flash") or "",
            llm_timeout_seconds=_int_env("LLM_TIMEOUT_SECONDS", 90),
            llm_max_retries=_int_env("LLM_MAX_RETRIES", 3),
            llm_json_mode=_bool_env("LLM_JSON_MODE", True),
            llm_user_id=_env("LLM_USER_ID", "mail-bot"),
            timezone=_env("TZ", "America/Los_Angeles") or "America/Los_Angeles",
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
            hash_salt=_env("HASH_SALT", "") or "",
            urgent_importance_min=_int_env("URGENT_IMPORTANCE_MIN", 4),
            urgent_info_density_min=_int_env("URGENT_INFO_DENSITY_MIN", 3),
            daily_importance_min=_int_env("DAILY_IMPORTANCE_MIN", 3),
            daily_summary_time=_env("DAILY_SUMMARY_TIME", "09:00") or "09:00",
            daily_window_hours=_int_env("DAILY_WINDOW_HOURS", 24),
            max_email_chars_for_llm=_int_env("MAX_EMAIL_CHARS_FOR_LLM", 12000),
            max_daily_items=_int_env("MAX_DAILY_ITEMS", 20),
            email_retry_max_attempts=_int_env("EMAIL_RETRY_MAX_ATTEMPTS", 5),
            email_retry_backoff_seconds=_int_env("EMAIL_RETRY_BACKOFF_SECONDS", 300),
            email_retry_max_backoff_seconds=_int_env("EMAIL_RETRY_MAX_BACKOFF_SECONDS", 3600),
        )
        settings.validate_common()
        if require_runtime:
            settings.validate_runtime()
        return settings

    def validate_common(self) -> None:
        if self.gmail_poll_seconds < 30:
            raise ValueError("GMAIL_POLL_SECONDS must be >= 30")
        if self.gmail_backfill_days < 1:
            raise ValueError("GMAIL_BACKFILL_DAYS must be >= 1")
        if self.llm_max_retries < 1:
            raise ValueError("LLM_MAX_RETRIES must be >= 1")
        if self.daily_window_hours < 1:
            raise ValueError("DAILY_WINDOW_HOURS must be >= 1")
        if self.max_email_chars_for_llm < 1000:
            raise ValueError("MAX_EMAIL_CHARS_FOR_LLM must be >= 1000")
        if self.email_retry_max_attempts < 1:
            raise ValueError("EMAIL_RETRY_MAX_ATTEMPTS must be >= 1")
        if self.email_retry_backoff_seconds < 1:
            raise ValueError("EMAIL_RETRY_BACKOFF_SECONDS must be >= 1")
        if self.email_retry_max_backoff_seconds < self.email_retry_backoff_seconds:
            raise ValueError("EMAIL_RETRY_MAX_BACKOFF_SECONDS must be >= EMAIL_RETRY_BACKOFF_SECONDS")
        self.local_timezone()
        self.daily_time_parts()

    def validate_runtime(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if not self.telegram_allowed_user_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS")
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))
        if not self.google_token_path.exists():
            raise ValueError(
                f"Gmail OAuth token not found at {self.google_token_path}. "
                "Run `mail-bot auth-gmail` first."
            )

    def local_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError(f"Invalid TZ: {self.timezone}") from exc

    def daily_time_parts(self) -> tuple[int, int]:
        try:
            hour_raw, minute_raw = self.daily_summary_time.split(":", 1)
            hour = int(hour_raw)
            minute = int(minute_raw)
        except ValueError as exc:
            raise ValueError("DAILY_SUMMARY_TIME must use HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("DAILY_SUMMARY_TIME must use HH:MM in 24-hour time")
        return hour, minute
