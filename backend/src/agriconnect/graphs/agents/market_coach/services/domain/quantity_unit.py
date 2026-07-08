"""Quantity / unit parsing and normalisation — single source of truth.

All unit synonym resolution, quantity-from-text extraction, and unit
validation lives here. Consumers (validator, entities, helpers) import
from this module instead of maintaining their own copies.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional


UNIT_SYNONYMS: Dict[str, str] = {
    "k": "KG",
    "kg": "KG",
    "kgs": "KG",
    "kilo": "KG",
    "kilos": "KG",
    "kilogramme": "KG",
    "kilogrammes": "KG",
    "ton": "TONNE",
    "tons": "TONNE",
    "tone": "TONNE",
    "tones": "TONNE",
    "tonne": "TONNE",
    "tonnes": "TONNE",
    "t": "TONNE",
    "sac": "SAC",
    "sacs": "SAC",
    "sachet": "SAC",
    "sachets": "SAC",
    "panier": "PANIER",
    "paniers": "PANIER",
    "tete": "TETE",
    "tetes": "TETE",
    "unite": "UNITE",
    "unites": "UNITE",
}

VALID_UNITS = frozenset(UNIT_SYNONYMS.values())

_QUANTITY_UNIT_RE = re.compile(
    r"(?P<qty>\d[\d\s.,]*)\s*(?P<unit>[a-zA-ZÀ-ÖØ-öø-ÿ.]+)",
    re.IGNORECASE,
)

_UNIT_ONLY_RE = re.compile(r"\b([a-zA-ZÀ-ÖØ-öø-ÿ.]{1,12})\b", re.IGNORECASE)


def normalize_unit_token(raw: str) -> str:
    """Fold accents and lowercase a raw unit token for synonym lookup."""
    if not raw:
        return ""
    token = unicodedata.normalize("NFKD", str(raw).strip().lower())
    token = "".join(ch for ch in token if not unicodedata.combining(ch))
    return token.replace(".", "")


def normalize_unit(value: str) -> Optional[str]:
    """Map a user-entered unit string to its canonical form, or ``None``."""
    return UNIT_SYNONYMS.get(normalize_unit_token(value))


def is_valid_unit(value: str) -> bool:
    return normalize_unit(value) is not None


@dataclass(frozen=True)
class QuantityUnitResult:
    quantity: Optional[float] = None
    unit: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.quantity is not None:
            out["quantity"] = self.quantity
        if self.unit is not None:
            out["unit"] = self.unit
        return out

    def __bool__(self) -> bool:
        return self.quantity is not None or self.unit is not None


def parse_quantity_unit_from_text(text: str) -> QuantityUnitResult:
    """Extract ``(quantity, unit)`` from free text using regex + synonym map."""
    if not text:
        return QuantityUnitResult()
    match = _QUANTITY_UNIT_RE.search(text)
    if not match:
        return QuantityUnitResult()

    qty_raw = (match.group("qty") or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        qty_val = float(qty_raw)
    except (ValueError, TypeError):
        qty_val = None

    unit_raw = normalize_unit_token(match.group("unit") or "")
    mapped_unit = UNIT_SYNONYMS.get(unit_raw)

    if qty_val is None and mapped_unit is None:
        return QuantityUnitResult()
    return QuantityUnitResult(quantity=qty_val, unit=mapped_unit)


def extract_unit_only_from_text(text: str) -> Optional[str]:
    """Try to find a standalone unit token in *text* (no quantity required)."""
    if not text:
        return None
    for match in _UNIT_ONLY_RE.finditer(text):
        mapped = UNIT_SYNONYMS.get(normalize_unit_token(match.group(1)))
        if mapped:
            return mapped
    return None


__all__ = [
    "UNIT_SYNONYMS",
    "VALID_UNITS",
    "normalize_unit_token",
    "normalize_unit",
    "is_valid_unit",
    "QuantityUnitResult",
    "parse_quantity_unit_from_text",
    "extract_unit_only_from_text",
]
