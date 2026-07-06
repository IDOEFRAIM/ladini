"""Text cleaning utilities for ingestion hashing and embedding consistency."""

from __future__ import annotations

import re


class TextCleaner:
    """Removes repetitive noise and normalizes whitespace for stable hashing."""

    _NOISE_PATTERNS = [
        re.compile(r"(?im)^\s*bulletin\s+fews\s+net\s*$"),
        re.compile(r"(?im)^\s*page\s+\d+(\s*/\s*\d+)?\s*$"),
        re.compile(r"(?im)^\s*confidential\s*$"),
    ]

    @classmethod
    def clean(cls, text: str) -> str:
        value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        for pat in cls._NOISE_PATTERNS:
            value = pat.sub("", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        value = re.sub(r"[ \t]{2,}", " ", value)
        return value.strip()
