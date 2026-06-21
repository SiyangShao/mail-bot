import base64

from mail_bot.gmail import extract_text, has_attachments, parse_gmail_message


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_extracts_plain_text_preferred_over_html() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>Hello <b>HTML</b></p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("Hello plain")}},
        ],
    }

    assert extract_text(payload) == "Hello plain"


def test_extracts_html_when_plain_missing() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<html><body><p>Hello</p><p>World</p></body></html>")},
    }

    assert extract_text(payload) == "Hello\nWorld"


def test_parse_message_headers_and_attachment_flag() -> None:
    raw = {
        "id": "g1",
        "threadId": "t1",
        "historyId": "h1",
        "internalDate": "1710000000000",
        "labelIds": ["INBOX"],
        "snippet": "snippet",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "Message-ID", "value": "<m@example.com>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Body")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "a.pdf",
                    "body": {"attachmentId": "att1"},
                },
            ],
        },
    }

    message = parse_gmail_message(raw)

    assert message.subject == "Hello"
    assert message.from_domain == "example.com"
    assert message.text == "Body"
    assert message.has_attachments is True
    assert has_attachments(raw["payload"]) is True
