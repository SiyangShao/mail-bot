import asyncio
from types import SimpleNamespace

from mail_bot.telegram_app import _allowed


def test_allowed_requires_user_and_chat_match() -> None:
    assert _run_allowed(user_id=1, chat_id=100) is True
    assert _run_allowed(user_id=1, chat_id=200) is False
    assert _run_allowed(user_id=2, chat_id=100) is False


def _run_allowed(*, user_id: int, chat_id: int) -> bool:
    settings = SimpleNamespace(telegram_allowed_user_ids=frozenset({1}), telegram_chat_id="100")
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": settings}))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return asyncio.run(_allowed(update, context))
