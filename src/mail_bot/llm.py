from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from mail_bot.config import Settings
from mail_bot.models import DailySummaryOutput, EmailAnalysis
from mail_bot.records import AnalyzedEmail

LOGGER = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required")
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def analyze_email(
        self,
        *,
        subject: str,
        from_domain: str | None,
        received_at: str,
        sanitized_body: str,
    ) -> EmailAnalysis:
        system = _analysis_system_prompt()
        user = _analysis_user_prompt(
            subject=subject,
            from_domain=from_domain,
            received_at=received_at,
            sanitized_body=sanitized_body,
        )
        return await self._request_validated(
            system=system,
            user=user,
            validator=EmailAnalysis,
            fallback=lambda reason: EmailAnalysis.fallback(reason),
            max_tokens=1200,
        )

    async def summarize_daily(self, emails: list[AnalyzedEmail]) -> DailySummaryOutput:
        if not emails:
            return DailySummaryOutput(
                overview_zh="过去 24 小时没有达到重要性阈值的邮件。",
                priorities_zh=[],
                risks_zh=[],
            )
        payload = [
            {
                "email_id": item.email_id,
                "subject_redacted": item.sanitized_subject,
                "from_domain": item.from_domain,
                "received_at": item.received_at.isoformat(),
                "importance": item.analysis.importance,
                "information_density": item.analysis.information_density,
                "summary_zh": item.analysis.summary_zh,
                "requires_action": item.analysis.requires_action,
                "action_items": item.analysis.action_items,
                "key_dates": [date.model_dump() for date in item.analysis.key_dates],
            }
            for item in emails
        ]
        system = _daily_system_prompt()
        user = (
            "下面是过去 24 小时内已脱敏的邮件分析结果。请输出 JSON。\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        return await self._request_validated(
            system=system,
            user=user,
            validator=DailySummaryOutput,
            fallback=lambda reason: DailySummaryOutput.fallback(),
            max_tokens=1400,
        )

    async def _request_validated(
        self,
        *,
        system: str,
        user: str,
        validator: type[EmailAnalysis] | type[DailySummaryOutput],
        fallback,
        max_tokens: int,
    ):
        repair_context = ""
        last_error = "unknown error"
        for attempt in range(1, self.settings.llm_max_retries + 1):
            prompt = user
            if repair_context:
                prompt += (
                    "\n\n上一次输出无法解析或不符合 schema。"
                    "请只输出一个合法 JSON object，不要 Markdown，不要解释。\n"
                    f"错误信息：{repair_context}"
                )
            try:
                raw_text = await self._chat(system=system, user=prompt, max_tokens=max_tokens)
                parsed = extract_json_object(raw_text)
                return validator.model_validate(parsed)
            except (json.JSONDecodeError, ValueError, ValidationError) as exc:
                last_error = str(exc)
                repair_context = last_error[:1500]
                LOGGER.warning("LLM JSON validation failed on attempt %s: %s", attempt, last_error)
            except Exception as exc:
                last_error = str(exc)
                LOGGER.exception("LLM request failed on attempt %s", attempt)
                repair_context = last_error[:1500]
        return fallback(last_error[:300])

    async def _chat(self, *, system: str, user: str, max_tokens: int) -> str:
        kwargs: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if self.settings.llm_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self.settings.llm_user_id:
            kwargs["user"] = self.settings.llm_user_id

        response = await self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        if not content.strip():
            raise ValueError("LLM returned empty content")
        return content


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(text.strip())
    for candidate in _candidate_json_strings(cleaned):
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                loaded = json.loads(_remove_trailing_commas(candidate))
            except json.JSONDecodeError:
                continue
        if isinstance(loaded, dict):
            return loaded
        raise ValueError("LLM JSON root must be an object")
    raise json.JSONDecodeError("No valid JSON object found", text, 0)


def _candidate_json_strings(text: str) -> list[str]:
    candidates = [text]
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        candidates.append(text[index : index + end])
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _analysis_system_prompt() -> str:
    return """
你是一个邮件分析器。输入已经做过本地脱敏，占位符如 <EMAIL_1>、<PHONE_1> 代表敏感信息。
无论原邮件语言是什么，summary_zh、rationale_zh、action_items、key_dates.description_zh 必须使用简体中文。
你必须只输出一个合法 JSON object，不要 Markdown，不要解释。

JSON schema:
{
  "importance": 1-5 的整数,
  "information_density": 1-5 的整数,
  "category": "账单/安全/工作/旅行/购物/社交/通知/其他 等短分类",
  "summary_zh": "1-3 句中文概要",
  "requires_action": true 或 false,
  "action_items": ["中文待办；没有则空数组"],
  "key_dates": [{"date": "原文日期或时间表达", "description_zh": "中文说明"}],
  "rationale_zh": "简短中文说明为什么这样打分",
  "confidence": 0 到 1 的数字
}

评分规则:
- importance=5: 安全风险、付款/到期、法律/工作关键事项、旅行变更、明确需要尽快处理。
- importance=4: 对个人安排或财务有明显影响，但不一定紧急。
- importance=3: 有用通知、账单状态、预约确认等。
- importance=1-2: 营销、低价值通知、重复提醒。
- information_density 衡量邮件包含多少可行动事实、日期、金额、链接目的、状态变化。
""".strip()


def _analysis_user_prompt(
    *,
    subject: str,
    from_domain: str | None,
    received_at: str,
    sanitized_body: str,
) -> str:
    return f"""
请分析下面这封已脱敏邮件，并输出 JSON。

subject: {subject}
from_domain: {from_domain or ""}
received_at: {received_at}

body:
{sanitized_body}
""".strip()


def _daily_system_prompt() -> str:
    return """
你是个人邮件日报助手。输入是过去 24 小时内重要邮件的结构化分析，内容已经脱敏。
请只输出一个合法 JSON object，不要 Markdown，不要解释。
输出必须使用简体中文。

JSON schema:
{
  "overview_zh": "总体总结，2-5 句",
  "priorities_zh": ["最值得优先处理的事项，中文，最多 5 条"],
  "risks_zh": ["潜在风险或容易错过的时间点，中文，没有则空数组"]
}
""".strip()
