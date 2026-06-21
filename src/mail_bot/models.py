from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Notification priority, most urgent first. P0 = alert, P1 = silent message, P2 = no message.
PRIORITY_ORDER: tuple[str, ...] = ("P0", "P1", "P2")


def priority_rank(priority: str) -> int:
    try:
        return PRIORITY_ORDER.index(priority)
    except ValueError:
        return PRIORITY_ORDER.index("P2")


def more_urgent(a: str, b: str) -> str:
    """Return the more urgent of two priorities (lower rank wins)."""
    return a if priority_rank(a) <= priority_rank(b) else b


def priority_for_importance(importance: int, *, p0_min: int = 5, p1_min: int = 4) -> str:
    if importance >= p0_min:
        return "P0"
    if importance >= p1_min:
        return "P1"
    return "P2"


class KeyDate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str = Field(description="Date or time expression from the email")
    description_zh: str = Field(description="Chinese description of what happens at that date/time")


class EmailAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    importance: int = Field(ge=1, le=5)
    information_density: int = Field(ge=1, le=5)
    category: str = Field(default="其他")
    summary_zh: str
    requires_action: bool = False
    action_items: list[str] = Field(default_factory=list)
    key_dates: list[KeyDate] = Field(default_factory=list)
    rationale_zh: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    is_fallback: bool = False

    @field_validator("summary_zh")
    @classmethod
    def summary_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary_zh must not be empty")
        return value

    @classmethod
    def fallback(cls, reason: str) -> EmailAnalysis:
        return cls(
            importance=1,
            information_density=1,
            category="解析失败",
            summary_zh=f"LLM 未能返回可解析的结构化结果：{reason}",
            requires_action=False,
            action_items=[],
            key_dates=[],
            rationale_zh="自动 fallback，避免阻塞邮件处理。",
            confidence=0.0,
            is_fallback=True,
        )


class DailyEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title_zh: str = Field(description="Chinese title of the event")
    summary_zh: str = Field(description="Chinese summary that merges all emails of this event")
    importance: int = Field(default=3, ge=1, le=5)
    email_ids: list[int] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    key_dates: list[KeyDate] = Field(default_factory=list)

    @field_validator("title_zh", "summary_zh")
    @classmethod
    def not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event field must not be empty")
        return value


class DailySummaryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    overview_zh: str
    events: list[DailyEvent] = Field(default_factory=list)
    priorities_zh: list[str] = Field(default_factory=list)
    risks_zh: list[str] = Field(default_factory=list)

    @field_validator("overview_zh")
    @classmethod
    def overview_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("overview_zh must not be empty")
        return value

    @classmethod
    def fallback(cls) -> DailySummaryOutput:
        return cls(
            overview_zh="LLM 未能生成稳定的日报总览，下面按单封邮件分析结果列出重要邮件。",
            events=[],
            priorities_zh=[],
            risks_zh=[],
        )


class EventAggregation(BaseModel):
    """Result of re-aggregating one event from all its source emails (manual web action)."""

    model_config = ConfigDict(extra="ignore")

    title_zh: str = Field(description="Chinese title of the event")
    context_zh: str = Field(description="Accumulated Chinese context covering all source emails")
    category: str = Field(default="其他")
    importance: int = Field(default=3, ge=1, le=5)

    @field_validator("title_zh", "context_zh")
    @classmethod
    def not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event field must not be empty")
        return value

    @classmethod
    def fallback(cls, title_zh: str, context_zh: str, *, importance: int = 3) -> EventAggregation:
        return cls(
            title_zh=title_zh.strip() or "未命名事件",
            context_zh=context_zh.strip() or "（无可用概要）",
            category="其他",
            importance=importance,
        )


class EventMatch(BaseModel):
    """Result of matching a new email against existing open events (immediate flow)."""

    model_config = ConfigDict(extra="ignore")

    matched_event_id: int | None = Field(
        default=None, description="Existing event id, or null to start a new event"
    )
    title_zh: str = Field(description="Chinese title of the event")
    context_zh: str = Field(description="Accumulated Chinese context of the event after this email")
    update_note_zh: str = Field(
        default="", description="What this email changed relative to the event"
    )
    category: str = Field(default="其他")
    importance: int = Field(default=3, ge=1, le=5)
    is_fallback: bool = False

    @field_validator("title_zh", "context_zh")
    @classmethod
    def not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event field must not be empty")
        return value

    @classmethod
    def fallback(cls, title_zh: str, context_zh: str, *, importance: int = 3) -> EventMatch:
        return cls(
            matched_event_id=None,
            title_zh=title_zh.strip() or "未命名事件",
            context_zh=context_zh.strip() or "（无可用概要）",
            update_note_zh="",
            category="其他",
            importance=importance,
            is_fallback=True,
        )
