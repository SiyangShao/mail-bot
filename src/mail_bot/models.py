from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class DailySummaryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    overview_zh: str
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
            priorities_zh=[],
            risks_zh=[],
        )
