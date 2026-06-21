from __future__ import annotations

import logging
from datetime import time

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.formatters import format_recent, split_telegram_message
from mail_bot.gmail import GmailClient
from mail_bot.llm import LLMClient
from mail_bot.redaction import Redactor
from mail_bot.service import MailBotService, TelegramSender

LOGGER = logging.getLogger(__name__)


class TelegramNotifier(TelegramSender):
    def __init__(self, bot: Bot, chat_id: str):
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, text: str, *, disable_notification: bool) -> list[int]:
        message_ids: list[int] = []
        for chunk in split_telegram_message(text):
            message = await self.bot.send_message(
                chat_id=self.chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_notification=disable_notification,
                disable_web_page_preview=True,
            )
            message_ids.append(message.message_id)
        return message_ids


def build_application(settings: Settings) -> Application:
    db = Database(settings.sqlite_path)
    db.init()

    application = ApplicationBuilder().token(settings.telegram_bot_token or "").build()
    notifier = TelegramNotifier(application.bot, settings.telegram_chat_id or "")
    service = MailBotService(
        settings=settings,
        db=db,
        gmail=GmailClient(settings),
        redactor=Redactor(),
        llm=LLMClient(settings),
        telegram=notifier,
    )
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["service"] = service

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("summarize", summarize_command))
    application.add_handler(CommandHandler("poll", poll_command))

    application.job_queue.run_repeating(
        poll_job,
        interval=settings.gmail_poll_seconds,
        first=10,
        name="gmail-poll",
    )
    hour, minute = settings.daily_time_parts()
    application.job_queue.run_daily(
        daily_summary_job,
        time=time(hour=hour, minute=minute, tzinfo=settings.local_timezone()),
        name="daily-summary",
    )
    return application


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    await update.effective_message.reply_text(
        "mail-bot 已启动。发送 /help 查看命令。",
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    await update.effective_message.reply_text(
        "\n".join(
            [
                "/status - 查看状态",
                "/recent [n] - 查看最近 n 封已处理邮件",
                "/summarize - 手动生成过去 24 小时总结",
                "/poll - 手动触发一次 Gmail 轮询",
            ]
        )
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    db: Database = context.application.bot_data["db"]
    counts = db.counts()
    history_id = db.get_state("gmail_history_id") or "未初始化"
    last_poll = db.get_state("gmail_last_poll_at") or "从未"
    lines = [
        "状态",
        f"邮件总数：{counts['total_emails']}",
        f"已分析：{counts['analyzed_emails']}",
        f"按状态：{counts['by_status']}",
        f"Gmail historyId：{history_id}",
        f"上次轮询：{last_poll}",
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    limit = 5
    if context.args:
        try:
            limit = max(1, min(20, int(context.args[0])))
        except ValueError:
            limit = 5
    db: Database = context.application.bot_data["db"]
    text = format_recent(db.list_recent(limit))
    await _reply_html(update, text)


async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    service: MailBotService = context.application.bot_data["service"]
    await update.effective_message.reply_text("开始生成过去 24 小时总结。")
    summary_id = await service.send_daily_summary(manual=True)
    await update.effective_message.reply_text(f"总结已发送。summary_id={summary_id}")


async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    service: MailBotService = context.application.bot_data["service"]
    await update.effective_message.reply_text("开始轮询 Gmail。")
    stats = await service.poll_once()
    await update.effective_message.reply_text(
        f"完成。发现 {stats.discovered}，处理 {stats.processed}，跳过 {stats.skipped}，"
        f"待重试 {stats.retry}，失败 {stats.failed}。"
    )


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MailBotService = context.application.bot_data["service"]
    try:
        stats = await service.poll_once()
        LOGGER.info(
            "Gmail poll complete: discovered=%s processed=%s skipped=%s retry=%s failed=%s",
            stats.discovered,
            stats.processed,
            stats.skipped,
            stats.retry,
            stats.failed,
        )
    except Exception:
        LOGGER.exception("Gmail poll job failed")


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    service: MailBotService = context.application.bot_data["service"]
    try:
        await service.send_daily_summary(manual=False)
    except Exception:
        LOGGER.exception("Daily summary job failed")


async def _allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    chat = update.effective_chat
    allowed_chat = chat is not None and str(chat.id) == str(settings.telegram_chat_id)
    allowed_user = user is not None and user.id in settings.telegram_allowed_user_ids
    if allowed_user and allowed_chat:
        return True
    LOGGER.warning(
        "Ignoring Telegram update from unauthorized user_id=%s chat_id=%s",
        user.id if user else None,
        chat.id if chat else None,
    )
    return False


async def _reply_html(update: Update, text: str) -> None:
    for chunk in split_telegram_message(text):
        await update.effective_message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
