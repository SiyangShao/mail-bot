from mail_bot.redaction import Redactor


def test_redacts_common_pii_with_stable_placeholders() -> None:
    redactor = Redactor()
    subject, body = redactor.redact_many(
        [
            "Receipt for alice@example.com",
            "Email alice@example.com or call 415-555-1212. Card 4111 1111 1111 1111.",
        ]
    )

    assert subject.text == "Receipt for <EMAIL_1>"
    assert "<EMAIL_1>" in body.text
    assert "<PHONE_1>" in body.text
    assert "<CREDIT_CARD_1>" in body.text
    assert "alice@example.com" not in body.text


def test_does_not_redact_non_luhn_long_number_as_credit_card() -> None:
    result = Redactor().redact("Reference 1234 5678 9012 3456 is not a card")
    assert "<CREDIT_CARD_1>" not in result.text


def test_account_rule_does_not_eat_plain_english_card_sentence() -> None:
    result = Redactor().redact("Card services available now for your membership.")
    assert result.text == "Card services available now for your membership."


def test_account_rule_requires_numeric_account_like_value() -> None:
    result = Redactor().redact("Routing number 021000021 is used for ACH.")
    assert "<ACCOUNT_1>" in result.text
