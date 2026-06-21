from mail_bot.formatters import split_telegram_message


def test_split_telegram_message_preserves_short_text() -> None:
    assert split_telegram_message("hello", limit=10) == ["hello"]


def test_split_telegram_message_splits_long_text() -> None:
    chunks = split_telegram_message("a" * 25, limit=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 5]
