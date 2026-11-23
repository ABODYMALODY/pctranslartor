"""Small helpers for comparing OCR text."""
from __future__ import annotations

import difflib
import re

_NON_WORD = re.compile(r"[^a-z0-9' ]+")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, drop punctuation and extra spaces. Used as the memory key."""
    text = _NON_WORD.sub(" ", text.lower())
    return _SPACES.sub(" ", text).strip()


def letter_count(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def similar(a: str, b: str, threshold: float = 0.88) -> bool:
    """True when two OCR readings are the same line with small recognition differences."""
    if a == b:
        return True
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold
