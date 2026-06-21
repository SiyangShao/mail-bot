from __future__ import annotations

import html
from datetime import datetime

from mail_bot.models import DailySummaryOutput
from mail_bot.records import AnalyzedEmail


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def format_immediate_email(item: AnalyzedEmail) -> str:
    analysis = item.analysis
    lines = [
        "<b>重要邮件</b>",
        f"标题：{escape(item.subject)}",
        f"来源域名：{escape(item.from_domain or '未知')}",
        f"时间：{escape(item.received_at.isoformat())}",
        "",
        f"概要：{escape(analysis.summary_zh)}",
        f"重要性：{analysis.importance}/5；信息量：{analysis.information_density}/5",
    ]
    if analysis.requires_action and analysis.action_items:
        lines.append("")
        lines.append("<b>待办</b>")
        lines.extend(f"- {escape(action)}" for action in analysis.action_items[:5])
    if analysis.key_dates:
        lines.append("")
        lines.append("<b>关键时间</b>")
        lines.extend(
            f"- {escape(kd.date)}：{escape(kd.description_zh)}" for kd in analysis.key_dates[:5]
        )
    return "\n".join(lines)


def format_recent(items: list[AnalyzedEmail]) -> str:
    if not items:
        return "还没有已处理邮件。"
    lines = ["<b>最近已处理邮件</b>"]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                "",
                f"{index}. {escape(item.subject)}",
                f"概要：{escape(item.analysis.summary_zh)}",
                f"重要性：{item.analysis.importance}/5；信息量：{item.analysis.information_density}/5",
            ]
        )
    return "\n".join(lines)


def format_daily_summary(
    *,
    start: datetime,
    end: datetime,
    summary: DailySummaryOutput,
    emails: list[AnalyzedEmail],
) -> str:
    lines = [
        "<b>过去 24 小时邮件总结</b>",
        f"时间窗：{escape(start.isoformat())} 到 {escape(end.isoformat())}",
        "",
        escape(summary.overview_zh),
    ]
    if summary.priorities_zh:
        lines.append("")
        lines.append("<b>优先事项</b>")
        lines.extend(f"- {escape(item)}" for item in summary.priorities_zh[:5])
    if summary.risks_zh:
        lines.append("")
        lines.append("<b>注意点</b>")
        lines.extend(f"- {escape(item)}" for item in summary.risks_zh[:5])
    lines.append("")
    lines.append("<b>重要邮件</b>")
    if not emails:
        lines.append("无。")
    for index, item in enumerate(emails, start=1):
        lines.extend(
            [
                f"{index}. {escape(item.subject)}",
                f"概要：{escape(item.analysis.summary_zh)}",
                f"重要性：{item.analysis.importance}/5；信息量：{item.analysis.information_density}/5",
            ]
        )
        if item.analysis.action_items:
            lines.append("待办：" + "；".join(escape(action) for action in item.analysis.action_items[:3]))
    return "\n".join(lines)


def split_telegram_message(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        addition = paragraph if not current else "\n\n" + paragraph
        if len(current) + len(addition) <= limit:
            current += addition
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= limit:
            current = paragraph
            continue
        for start in range(0, len(paragraph), limit):
            chunks.append(paragraph[start : start + limit])
        current = ""
    if current:
        chunks.append(current)
    return chunks
