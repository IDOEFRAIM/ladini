"""Corpus BUSINESS EDITS, organisé par phénomène (quantity / price / remove / constraint / confirmation_edit / ambiguity)."""
from __future__ import annotations

from typing import Any, Dict, List

from . import ambiguity, confirmation_edit, constraint, price, quantity, remove

ALL_CASES: List[Dict[str, Any]] = [
    *quantity.CASES,
    *price.CASES,
    *remove.CASES,
    *constraint.CASES,
    *confirmation_edit.CASES,
    *ambiguity.CASES,
]
