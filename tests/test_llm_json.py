import pytest

from mail_bot.llm import extract_json_object


def test_extracts_json_from_markdown_fence() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extracts_json_from_prefixed_text() -> None:
    assert extract_json_object('Here is the result: {"a": 1, "b": true}') == {
        "a": 1,
        "b": True,
    }


def test_removes_trailing_commas() -> None:
    assert extract_json_object('{"a": 1, "b": [2,],}') == {"a": 1, "b": [2]}


def test_rejects_missing_json() -> None:
    with pytest.raises(ValueError):
        extract_json_object("not json")
