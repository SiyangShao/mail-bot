from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionFinding:
    entity_type: str
    start: int
    end: int
    placeholder: str


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: list[RedactionFinding]


@dataclass(frozen=True)
class _Match:
    entity_type: str
    start: int
    end: int
    value: str


class Redactor:
    """Resource-light, Presidio-style pattern redactor.

    The map from raw value to placeholder lives only for one redaction call.
    It is intentionally not returned, logged, or persisted.
    """

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "JWT",
            re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        ),
        (
            "EMAIL",
            re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        ),
        (
            "URL",
            re.compile(r"\bhttps?://[^\s<>\]\)\"']+", re.IGNORECASE),
        ),
        (
            "IP_ADDRESS",
            re.compile(
                r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
            ),
        ),
        (
            "SSN",
            re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        ),
        (
            "PHONE",
            re.compile(
                r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)"
            ),
        ),
        (
            "SECRET",
            re.compile(
                r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|pwd)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=:-]{8,}[\"']?"
            ),
        ),
        (
            "ACCOUNT",
            re.compile(
                r"\b(?i:account|acct|routing|iban|swift|invoice|card)\s*"
                r"(?:(?i:number|no\.?)|#)?\s*(?::|=)?\s*"
                r"(?=[A-Z0-9 -]{6,}\b)(?=[A-Z0-9 -]*\d)"
                r"[A-Z0-9]+(?:[ -][A-Z0-9]+)*\b"
            ),
        ),
        (
            "CREDIT_CARD",
            re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        ),
    )
    _PRIORITY = {
        "JWT": 100,
        "SECRET": 95,
        "CREDIT_CARD": 90,
        "URL": 80,
        "EMAIL": 70,
        "SSN": 70,
        "PHONE": 60,
        "IP_ADDRESS": 50,
        "ACCOUNT": 10,
    }

    def redact(self, text: str) -> RedactionResult:
        return self.redact_many([text])[0]

    def redact_many(self, texts: list[str]) -> list[RedactionResult]:
        counters: dict[str, int] = {}
        placeholders: dict[tuple[str, str], str] = {}
        return [
            self._redact_one(text, counters=counters, placeholders=placeholders)
            for text in texts
        ]

    def _redact_one(
        self,
        text: str,
        *,
        counters: dict[str, int],
        placeholders: dict[tuple[str, str], str],
    ) -> RedactionResult:
        matches = self._collect_matches(text)
        findings: list[RedactionFinding] = []
        redacted = text

        for match in reversed(matches):
            key = (match.entity_type, match.value)
            placeholder = placeholders.get(key)
            if placeholder is None:
                counters[match.entity_type] = counters.get(match.entity_type, 0) + 1
                placeholder = f"<{match.entity_type}_{counters[match.entity_type]}>"
                placeholders[key] = placeholder
            redacted = redacted[: match.start] + placeholder + redacted[match.end :]
            findings.append(
                RedactionFinding(
                    entity_type=match.entity_type,
                    start=match.start,
                    end=match.end,
                    placeholder=placeholder,
                )
            )

        findings.reverse()
        return RedactionResult(text=redacted, findings=findings)

    def _collect_matches(self, text: str) -> list[_Match]:
        raw_matches: list[_Match] = []
        for entity_type, pattern in self._PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0)
                if entity_type == "CREDIT_CARD" and not _looks_like_credit_card(value):
                    continue
                raw_matches.append(_Match(entity_type, match.start(), match.end(), value))

        raw_matches.sort(
            key=lambda item: (
                -self._PRIORITY.get(item.entity_type, 0),
                item.start,
                -(item.end - item.start),
            )
        )
        selected: list[_Match] = []
        occupied: list[tuple[int, int]] = []
        for item in raw_matches:
            if any(item.start < end and item.end > start for start, end in occupied):
                continue
            selected.append(item)
            occupied.append((item.start, item.end))
        selected.sort(key=lambda item: item.start)
        return selected


def _looks_like_credit_card(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not (13 <= len(digits) <= 19):
        return False
    # Avoid redacting ordinary repeated digits unless the number passes Luhn.
    total = 0
    reverse_digits = digits[::-1]
    for index, char in enumerate(reverse_digits):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
