from __future__ import annotations

import argparse
import asyncio
import sys

from mail_bot.config import Settings
from mail_bot.db import Database
from mail_bot.gmail import run_oauth
from mail_bot.logging_config import configure_logging
from mail_bot.telegram_app import build_application


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mail-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run the Telegram bot")
    subparsers.add_parser("auth-gmail", help="Run Gmail OAuth bootstrap")
    subparsers.add_parser("init-db", help="Initialize SQLite schema")
    subparsers.add_parser("poll-once", help="Run one Gmail poll without starting Telegram polling")

    args = parser.parse_args(argv)
    require_runtime = args.command in {"run", "poll-once"}
    settings = Settings.from_env(require_runtime=require_runtime)
    configure_logging(settings.log_level)

    if args.command == "auth-gmail":
        run_oauth(settings)
        return 0
    if args.command == "init-db":
        Database(settings.sqlite_path).init()
        print(f"Initialized SQLite database at {settings.sqlite_path}")
        return 0
    if args.command == "poll-once":
        return asyncio.run(_poll_once(settings))
    if args.command == "run":
        application = build_application(settings)
        application.run_polling(allowed_updates=UpdateTypes.ALL)
        return 0
    parser.error("unknown command")
    return 2


async def _poll_once(settings: Settings) -> int:
    from mail_bot.db import Database
    from mail_bot.gmail import GmailClient
    from mail_bot.llm import LLMClient
    from mail_bot.redaction import Redactor
    from mail_bot.service import MailBotService, TelegramSender

    class StdoutTelegram(TelegramSender):
        async def send(self, text: str, *, disable_notification: bool) -> list[int]:
            print(f"--- telegram disable_notification={disable_notification} ---")
            print(text)
            return []

    db = Database(settings.sqlite_path)
    db.init()
    service = MailBotService(
        settings=settings,
        db=db,
        gmail=GmailClient(settings),
        redactor=Redactor(),
        llm=LLMClient(settings),
        telegram=StdoutTelegram(),
    )
    stats = await service.poll_once()
    print(stats)
    return 0 if stats.failed == 0 else 1


class UpdateTypes:
    ALL = [
        "message",
        "edited_message",
        "callback_query",
        "my_chat_member",
    ]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
