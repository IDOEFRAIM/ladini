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


_UNIT_TO_KG: Dict[str, float] = {
    "KG": 1.0,
    "TONNE": 1000.0,
}


def parse_quantity_unit_from_text(text: str) -> QuantityUnitResult:
    """Extract ``(quantity, unit)`` from free text using regex + synonym map."""
    if not text:
        return QuantityUnitResult()
    match = _QUANTITY_UNIT_RE.search(text)
    if not match:
        return QuantityUnitResult()

    qty_raw = (
        (match.group("qty") or "")
        .replace(" ", "")
        .replace("\xa0", "")
        .replace(",", ".")
    )
    try:
        qty_val = float(qty_raw)
    except (ValueError, TypeError):
        qty_val = None

    unit_raw = normalize_unit_token(match.group("unit") or "")
    mapped_unit = UNIT_SYNONYMS.get(unit_raw)

    if qty_val is None and mapped_unit is None:
        return QuantityUnitResult()
    return QuantityUnitResult(quantity=qty_val, unit=mapped_unit)


def parse_compound_quantity(text: str) -> QuantityUnitResult:
    """Parse compound quantity expressions like '2 tonnes et 375 kg'.

    When multiple quantity+unit pairs are found and all units are convertible
    to KG (i.e. KG or TONNE), sums them in KG. Otherwise falls back to
    ``parse_quantity_unit_from_text`` (first match only).
    """
    if not text:
        return QuantityUnitResult()
    matches = list(_QUANTITY_UNIT_RE.finditer(text))
    if len(matches) < 2:
        return parse_quantity_unit_from_text(text)

    total_kg = 0.0
    all_convertible = True
    for m in matches:
        qty_raw = (
            (m.group("qty") or "")
            .replace(" ", "")
            .replace("\xa0", "")
            .replace(",", ".")
        )
        try:
            qty_val = float(qty_raw)
        except (ValueError, TypeError):
            continue
        unit_raw = normalize_unit_token(m.group("unit") or "")
        unit_canonical = UNIT_SYNONYMS.get(unit_raw)
        kg_factor = _UNIT_TO_KG.get(unit_canonical or "")
        if kg_factor is None:
            all_convertible = False
            break
        total_kg += qty_val * kg_factor

    if all_convertible and total_kg > 0:
        return QuantityUnitResult(quantity=total_kg, unit="KG")
    return parse_quantity_unit_from_text(text)


def extract_unit_only_from_text(text: str) -> Optional[str]:
    """Try to find a standalone unit token in *text* (no quantity required)."""
    if not text:
        return None
    for match in _UNIT_ONLY_RE.finditer(text):
        mapped = UNIT_SYNONYMS.get(normalize_unit_token(match.group(1)))
        if mapped:
            return mapped
    return None


# ---------------------------------------------------------------------------
# Currency-aware number scanning (slot-answer classification)
# ---------------------------------------------------------------------------
# Used by the interpreter fast-path to classify EACH number in a slot answer
# as a quantity (unit nearby) or a price (currency nearby). Different from
# `parse_quantity_unit_from_text` (which only pulls the FIRST adjacent
# number+unit pair and has no currency notion) — this one scans every number
# and tags it by its LOCAL neighbourhood so a single message carrying both a
# quantity and a price ("775 kg ... 175 fcfa") is disambiguated correctly.

#: How many chars around a number to inspect for a unit/currency token.
#: Covers "234000 fcfa/kg" or "coute 34500 fcfa".
_SCAN_WINDOW = 18

# ATTENTION — pas de `\b` en TÊTE du nombre : un chiffre et une lettre sont
# tous deux des caractères \w, donc "60kg"/"10000fcfa" n'ont PAS de frontière
# de mot entre eux. Seule la frontière de FIN d'unité/devise reste requise
# (éviter de matcher "cfaXYZ"/"kgXYZ"). Le lookbehind négatif empêche de capter
# une unité collée À GAUCHE d'une lettre (ex: pas de match dans "packg").
_SCAN_UNIT_RE = re.compile(
    r"(?<![a-zàâäéèêëïîôöùûüÿçA-ZÀÂÄÉÈÊËÏÎÔÖÙÛÜŸÇ])"
    r"(k|kg|kgs|kilo|kilogramme|kilogrammes|ton|tons|tone|tones|tonne|tonnes|t|"
    r"sac|sacs|sachet|sachets|panier|paniers|tete|têtes|tetes|unite|unité|unites|unités)\b"
)
_SCAN_CURRENCY_RE = re.compile(
    r"(?<![a-zàâäéèêëïîôöùûüÿçA-ZÀÂÄÉÈÊËÏÎÔÖÙÛÜŸÇ])(fcfa|cfa|francs?|balles?)\b"
)
_SCAN_NUMBER_RE = re.compile(r"(\d+[\d\s,.]*)")


@dataclass(frozen=True)
class NumberCandidate:
    """A number found in a slot answer, tagged by its local neighbourhood."""

    value: float
    unit: Optional[str]  # canonical unit token near the number, else None
    near_currency: bool  # a currency word within ``_SCAN_WINDOW`` chars


def scan_number_candidates(text: str) -> "list[NumberCandidate]":
    """Scan every number in *text*, tagging each with its nearby unit / currency.

    Deterministic, currency-aware primitive shared by the interpreter
    fast-path. `unit` is ALWAYS the mapped unit token found nearby (even when a
    currency is also near) — the caller decides how to weigh unit vs currency.
    """
    clean = (text or "").strip().lower()
    out: list[NumberCandidate] = []
    for m in _SCAN_NUMBER_RE.finditer(clean):
        raw = (m.group(1) or "").replace(" ", "").replace(",", ".")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        after = clean[m.end() : m.end() + _SCAN_WINDOW]
        before = clean[max(0, m.start() - _SCAN_WINDOW) : m.start()]
        near_currency = bool(_SCAN_CURRENCY_RE.search(after)) or bool(
            _SCAN_CURRENCY_RE.search(before)
        )
        unit_match = _SCAN_UNIT_RE.search(after) or _SCAN_UNIT_RE.search(before)
        mapped_unit = (
            UNIT_SYNONYMS.get(normalize_unit_token(unit_match.group(1) or ""))
            if unit_match
            else None
        )
        out.append(
            NumberCandidate(value=val, unit=mapped_unit, near_currency=near_currency)
        )
    return out


# Livestock / poultry products are counted per head (TÊTE), never weighed in KG.
# Defaulting their unit to KG (e.g. "300 poussins" → "300 KG") is a recurring
# production bug. Keep singular + plural forms; matching is accent-folded.
LIVESTOCK_PRODUCT_KEYWORDS = frozenset(
    {
        "poussin",
        "poussins",
        "poule",
        "poules",
        "poulet",
        "poulets",
        "poulaille",
        "coq",
        "coqs",
        "volaille",
        "volailles",
        "pintade",
        "pintades",
        "canard",
        "canards",
        "canette",
        "canettes",
        "oie",
        "oies",
        "dindon",
        "dindons",
        "dinde",
        "dindes",
        "lapin",
        "lapins",
        "lapine",
        "lapines",
        "clapier",
        "mouton",
        "moutons",
        "brebis",
        "belier",
        "beliers",
        "agneau",
        "agneaux",
        "ovin",
        "ovins",
        "chevre",
        "chevres",
        "chevreau",
        "chevreaux",
        "cabri",
        "cabris",
        "bouc",
        "boucs",
        "caprin",
        "caprins",
        "boeuf",
        "boeufs",
        "vache",
        "vaches",
        "taureau",
        "taureaux",
        "veau",
        "veaux",
        "genisse",
        "genisses",
        "bovin",
        "bovins",
        "zebu",
        "zebus",
        "taurillon",
        "taurillons",
        "porc",
        "porcs",
        "cochon",
        "cochons",
        "porcelet",
        "porcelets",
        "truie",
        "truies",
        "porcin",
        "porcins",
        "ane",
        "anes",
        "anesse",
        "cheval",
        "chevaux",
        "jument",
        "juments",
        "poulain",
        "poulains",
        "dromadaire",
        "dromadaires",
        "chameau",
        "chameaux",
        "bete",
        "betes",
        "betail",
        "tete",
        "tetes",
    }
)

_WORD_RE = re.compile(r"[a-z]+")


def is_livestock_product(product: Any) -> bool:
    """True when *product* names an animal counted per head rather than weighed."""
    if not product:
        return False
    # Expand ligatures NFKD leaves intact (œ→oe, æ→ae) so "bœuf" matches "boeuf".
    folded = normalize_unit_token(str(product).replace("œ", "oe").replace("æ", "ae"))
    return any(word in LIVESTOCK_PRODUCT_KEYWORDS for word in _WORD_RE.findall(folded))


def default_unit_for_product(product: Any, fallback: str = "KG") -> str:
    """Pick the natural default unit for *product*: ``TETE`` for livestock, else *fallback*."""
    return "TETE" if is_livestock_product(product) else fallback


__all__ = [
    "UNIT_SYNONYMS",
    "VALID_UNITS",
    "normalize_unit_token",
    "normalize_unit",
    "is_valid_unit",
    "QuantityUnitResult",
    "parse_quantity_unit_from_text",
    "parse_compound_quantity",
    "extract_unit_only_from_text",
    "NumberCandidate",
    "scan_number_candidates",
    "LIVESTOCK_PRODUCT_KEYWORDS",
    "is_livestock_product",
    "default_unit_for_product",
]
