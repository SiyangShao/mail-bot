from __future__ import annotations

import html
from datetime import datetime

from mail_bot.models import DailyEvent, DailySummaryOutput
from mail_bot.records import AnalyzedEmail


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{escape(value)}</b>"


def italic(value: object) -> str:
    return f"<i>{escape(value)}</i>"


def _labeled(label: str, value: object) -> str:
    return f"{bold(label)}{italic(value)}"


def format_immediate_email(
    item: AnalyzedEmail,
    *,
    event_title: str | None = None,
    event_context: str | None = None,
    update_note: str | None = None,
    is_new_event: bool = True,
) -> str:
    analysis = item.analysis
    heading = "🆕 新事件" if is_new_event else "🔔 事件更新"
    title = event_title or item.subject
    lines = [f"{bold(heading)}：{italic(title)}"]

    if not is_new_event and event_context:
        lines.append("")
        lines.append(_labeled("事件背景：", event_context))
    if not is_new_event and update_note:
        lines.append("")
        lines.append(bold("本次更新"))
        lines.append(escape(update_note))

    lines.extend(
        [
            "",
            _labeled("标题：", item.subject),
            _labeled("来源域名：", item.from_domain or "未知"),
            _labeled("时间：", item.received_at.isoformat()),
            "",
            f"{bold('概要：')}{escape(analysis.summary_zh)}",
            f"{bold('重要性：')}{analysis.importance}/5　{bold('信息量：')}{analysis.information_density}/5",
        ]
    )
    if analysis.requires_action and analysis.action_items:
        lines.append("")
        lines.append(bold("待办"))
        lines.extend(f"• {escape(action)}" for action in analysis.action_items[:5])
    if analysis.key_dates:
        lines.append("")
        lines.append(bold("关键时间"))
        lines.extend(
            f"• {italic(kd.date)}：{escape(kd.description_zh)}" for kd in analysis.key_dates[:5]
        )
    return "\n".join(lines)


def format_recent(items: list[AnalyzedEmail]) -> str:
    if not items:
        return "还没有已处理邮件。"
    lines = [bold("最近已处理邮件")]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                "",
                f"{bold(f'{index}.')} {italic(item.subject)}",
                f"{bold('概要：')}{escape(item.analysis.summary_zh)}",
                f"{bold('重要性：')}{item.analysis.importance}/5　"
                f"{bold('信息量：')}{item.analysis.information_density}/5",
            ]
        )
    return "\n".join(lines)


def format_daily_overview(
    *,
    start: datetime,
    end: datetime,
    summary: DailySummaryOutput,
    event_count: int,
) -> str:
    lines = [
        bold("📬 过去 24 小时邮件总结"),
        _labeled("时间窗：", f"{start.isoformat()} 到 {end.isoformat()}"),
        _labeled("事件数：", event_count),
        "",
        escape(summary.overview_zh),
    ]
    if summary.risks_zh:
        lines.append("")
        lines.append(bold("⚠️ 注意点"))
        lines.extend(f"• {escape(item)}" for item in summary.risks_zh[:5])
    return "\n".join(lines)


def format_daily_event(
    *,
    index: int,
    total: int,
    event: DailyEvent,
    emails_by_id: dict[int, AnalyzedEmail],
) -> str:
    lines = [
        f"{bold(f'事件 {index}/{total}：')}{italic(event.title_zh)}",
        f"{bold('概要：')}{escape(event.summary_zh)}",
        f"{bold('重要性：')}{event.importance}/5",
    ]
    related = [emails_by_id[eid] for eid in event.email_ids if eid in emails_by_id]
    if related:
        lines.append("")
        lines.append(bold(f"相关邮件（{len(related)}）"))
        lines.extend(f"• {italic(mail.subject)}" for mail in related[:8])
    if event.action_items:
        lines.append("")
        lines.append(bold("待办"))
        lines.extend(f"• {escape(action)}" for action in event.action_items[:5])
    if event.key_dates:
        lines.append("")
        lines.append(bold("关键时间"))
        lines.extend(
            f"• {italic(kd.date)}：{escape(kd.description_zh)}" for kd in event.key_dates[:5]
        )
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
