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
    # Litre (2026-08-29) : absent, un producteur laitier répondant "25 L à 500
    # fcfa" voyait son "25" sans unité reconnue — le scanner le reclassait
    # alors en PRIX par simple proximité de "fcfa" (voir scan_number_candidates
    # plus bas), écrasant le prix réel du tour avec une valeur de quantité.
    "l": "LITRE",
    "litre": "LITRE",
    "litres": "LITRE",
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


def _extract_match(match: "re.Match") -> "tuple[Optional[float], Optional[str]]":
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
    return qty_val, mapped_unit


def parse_quantity_unit_from_text(text: str) -> QuantityUnitResult:
    """Extract ``(quantity, unit)`` from free text using regex + synonym map.

    Scans EVERY number+trailing-word pair, not just the first — a price
    mentioned earlier in the same message ("250 fcfa le kg ... 2 tonnes")
    would otherwise be mistaken for the quantity, since the regex's "unit"
    group is a bare word match and matches "fcfa" just as readily as "kg"
    (incident réel 2026-09-03: returned quantity=250/unit=None from a
    message whose actual quantity was further along the text). The first
    match whose trailing word is an ACTUALLY recognized unit wins; only
    when NONE of the matches carry a recognized unit does this fall back to
    the historical behaviour (first raw number, unit=None)."""
    if not text:
        return QuantityUnitResult()
    matches = list(_QUANTITY_UNIT_RE.finditer(text))
    if not matches:
        return QuantityUnitResult()

    for m in matches:
        qty_val, mapped_unit = _extract_match(m)
        if mapped_unit is not None:
            return QuantityUnitResult(quantity=qty_val, unit=mapped_unit)

    qty_val, mapped_unit = _extract_match(matches[0])
    if qty_val is None and mapped_unit is None:
        return QuantityUnitResult()
    return QuantityUnitResult(quantity=qty_val, unit=mapped_unit)


def parse_compound_quantity(text: str) -> QuantityUnitResult:
    """Parse compound quantity expressions like '2 tonnes et 375 kg'.

    When multiple quantity+unit pairs are found and all units are convertible
    to KG (i.e. KG or TONNE), sums them in KG. Otherwise falls back to
    ``parse_quantity_unit_from_text`` (first match only).

    Candidate pairs are filtered to those carrying an ACTUALLY recognized
    unit before counting "how many pairs" — a price ("250 fcfa") elsewhere
    in the same message is noise, not a 3rd (non-convertible) pair that
    should abort the whole sum (same incident as
    ``parse_quantity_unit_from_text`` above)."""
    if not text:
        return QuantityUnitResult()
    matches = list(_QUANTITY_UNIT_RE.finditer(text))
    unit_matches = [m for m in matches if _extract_match(m)[1] is not None]
    if len(unit_matches) < 2:
        return parse_quantity_unit_from_text(text)

    total_kg = 0.0
    all_convertible = True
    for m in unit_matches:
        qty_val, unit_canonical = _extract_match(m)
        if qty_val is None:
            continue
        kg_factor = _UNIT_TO_KG.get(unit_canonical or "")
        if kg_factor is None:
            all_convertible = False
            break
        total_kg += qty_val * kg_factor

    if all_convertible and total_kg > 0:
        return QuantityUnitResult(quantity=total_kg, unit="KG")
    return parse_quantity_unit_from_text(text)


def convert_quantity(quantity: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Convert *quantity* from one canonical unit to another.

    Only safe for units with a FIXED, universal factor (weight units in
    `_UNIT_TO_KG`: KG, TONNE). Returns ``None`` when no safe conversion
    exists (e.g. SAC/PANIER/TETE have no universal kg-equivalent) — callers
    must not guess in that case, only ask the user to restate the quantity
    in the target unit.
    """
    if from_unit == to_unit:
        return quantity
    from_kg = _UNIT_TO_KG.get(from_unit or "")
    to_kg = _UNIT_TO_KG.get(to_unit or "")
    if from_kg is None or to_kg is None:
        return None
    return quantity * from_kg / to_kg


#: Apostrophes (droite et typographique) marquant une ÉLISION française.
_ELISION_CHARS = ("'", "’")


def extract_unit_only_from_text(text: str) -> Optional[str]:
    """Try to find a standalone unit token in *text* (no quantity required)."""
    if not text:
        return None
    for match in _UNIT_ONLY_RE.finditer(text):
        # Incident réel (2026-09-08) : « c'est 3000 f l'unité » (= prix À LA
        # PIÈCE) était lu comme `unit=LITRE`. `_UNIT_ONLY_RE` découpe sur les
        # frontières de mot et l'apostrophe n'en fait pas partie : « l'unité »
        # produit donc DEUX tokens, « l » et « unité » — et « l » isolé est le
        # symbole du litre dans `UNIT_SYNONYMS`. Même piège pour « t' »
        # (« t'as ») qui vaut TONNE. Un token collé à une apostrophe est un
        # ARTICLE ÉLIDÉ (l', d', j', n', t'…), jamais une unité : on le saute.
        if match.end() < len(text) and text[match.end()] in _ELISION_CHARS:
            continue
        mapped = UNIT_SYNONYMS.get(normalize_unit_token(match.group(1)))
        if mapped:
            return mapped
    return None


def resolve_product_unit(
    product: Any,
    current_unit: Any = None,
    text_unit: Any = None,
) -> Optional[str]:
    """AUTORITÉ UNIQUE : quelle unité pour ce produit ?

    ## Le problème que cette fonction existe pour supprimer

    L'unité était décidée par CINQ écrivains successifs (interpréteur,
    héritage `stable_entities`, enrichissement texte, règle élevage, défaut
    du registre de slots), chacun gardé par un `if unité est vide`. La
    précédence réelle était donc « le PREMIER qui écrit gagne » — c'est-à-dire
    l'ordre d'exécution dans le graphe, pas la fiabilité de la source. Un
    défaut aveugle « KG », posé sans la moindre preuve, gagnait ainsi
    DÉFINITIVEMENT contre la nature du produit : incident réel (2026-09-08),
    « Vente de 6500 KG de poulets » — des poulets se comptent à la tête, et
    l'utilisateur avait écrit « poulets » deux fois. La règle élevage→TETE
    existait pourtant, mais ne pouvait que COMBLER un vide, jamais CORRIGER
    une valeur déjà posée.

    ## La règle

    Précédence par FIABILITÉ de la source, jamais par ordre d'exécution :

    1. `text_unit` — l'utilisateur a écrit l'unité noir sur blanc.
    2. NATURE DU PRODUIT, en CORRECTION — `current_unit` est une unité de
       MASSE alors que le produit est un animal compté à la tête : c'est
       physiquement impossible, donc c'est `current_unit` qui a tort, quelle
       qu'en soit la provenance.
    3. `current_unit` — déjà posée et plausible : on n'y touche pas.
    4. NATURE DU PRODUIT, en DÉFAUT — rien de connu : TETE pour un élevage,
       `None` sinon (à l'appelant de demander plutôt que de deviner).

    ## Périmètre assumé

    Cette fonction décide « quelle unité pour CE produit », pas « l'utilisateur
    est-il en train de corriger son unité ». La correction explicite en cours
    de conversation reste la propriété de `interpreter/routing.py` (garde
    anti-ancrage texte>LLM) et de `nodes/memory.py::_apply_slot` (qui purge le
    prix en cascade quand l'unité change — un prix « 3000/KG » ne survit pas à
    un passage en SAC). Les appelants ne fournissent donc `text_unit` que
    lorsqu'ils sont légitimes à trancher ce point.
    """
    if text_unit:
        canonical = normalize_unit(text_unit)
        if canonical:
            return canonical

    natural = "TETE" if is_livestock_product(product) else None

    if current_unit not in (None, "", [], {}):
        # `normalize_unit` renvoie None pour une unité inconnue du registre —
        # on préserve alors la valeur telle quelle plutôt que de détruire une
        # donnée utilisateur qu'on ne sait simplement pas interpréter.
        current_canonical = (
            normalize_unit(current_unit) or str(current_unit).strip().upper()
        )
        if natural and current_canonical in _UNIT_TO_KG:
            return natural
        return current_canonical

    return natural


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
    r"sac|sacs|sachet|sachets|panier|paniers|tete|têtes|tetes|unite|unité|unites|unités|"
    r"l|litre|litres)\b(?!['’])"
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


# ---------------------------------------------------------------------------
# Deterministic multi-tier pricing parser
# ---------------------------------------------------------------------------
# Incident réel (2026-08-30) : la règle 5bis du prompt LLM ("plusieurs
# tarifs/conditionnements") N'EST PAS fiable à 100% — la MÊME phrase exacte
# ("je vend le bidon de 5l a 500 fcfa et celui de 10 l a 900 fcfa") a produit
# `pricing_tiers` correctement une fois, puis a échoué (retour à un seul
# tarif plat) sur un appel ultérieur avec le MÊME modèle, la MÊME
# temperature=0.0 — non-déterminisme MoE connu côté Groq, pas un bug de
# code. Ce parser déterministe retire la dépendance au LLM pour CE cas
# précis : découpe le texte en clauses ("et"/"ou"/","/"puis"), et pour
# chaque clause à EXACTEMENT 2 nombres, associe qty+unit et prix+devise par
# adjacence SERRÉE (pas la fenêtre large de `scan_number_candidates`, qui
# classe les DEUX nombres "near_currency" dès qu'ils sont proches l'un de
# l'autre — inutilisable ici pour distinguer leurs rôles). Si UNE SEULE
# clause est ambiguë (0 ou 2+ matches d'un type), la fonction entière renvoie
# `None` — jamais de résultat partiel deviné.
_TIER_CLAUSE_SPLIT_RE = re.compile(r"\bet\b|\bou\b|\bpuis\b|,|;", re.IGNORECASE)
_TIER_QTY_UNIT_RE = re.compile(
    r"(\d+[\d\s,.]*)\s*"
    r"(kg|kgs|kilo|kilogramme|kilogrammes|tonnes?|tones?|tons?|"
    r"sacs?|sachets?|paniers?|t[êe]tes?|unit[ée]s?|litres?|l)\b",
    re.IGNORECASE,
)
_TIER_PRICE_CURRENCY_RE = re.compile(
    r"(\d+[\d\s,.]*)\s*(fcfa|cfa|francs?|balles?)\b", re.IGNORECASE
)
_TIER_PACKAGING_WORDS = (
    "bidon", "bidons", "sac", "sacs", "sachet", "sachets", "carton", "cartons",
    "casier", "casiers", "bouteille", "bouteilles", "seau", "seaux",
    "cuvette", "cuvettes", "panier", "paniers",
)
_TIER_PACKAGING_RE = re.compile(
    r"\b(" + "|".join(_TIER_PACKAGING_WORDS) + r")\b", re.IGNORECASE
)


def _parse_number(raw: str) -> Optional[float]:
    cleaned = (raw or "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def extract_deterministic_pricing_tiers(text: str) -> Optional[list]:
    """Construit `pricing_tiers` déterministement depuis *text*, ou `None`
    si une clause est ambiguë (jamais de résultat deviné à moitié).

    Renvoie une liste de {"quantity","unit","price","packaging"} — même
    forme que ce que le LLM produit pour `extracted_entities.pricing_tiers`
    (voir routing.py règle 5bis) — uniquement si CHAQUE clause candidate a
    livré EXACTEMENT une paire quantité+unité et prix+devise sans ambiguïté.
    """
    clean = (text or "").strip()
    if not clean:
        return None
    clauses = [c.strip() for c in _TIER_CLAUSE_SPLIT_RE.split(clean) if c.strip()]
    if len(clauses) < 2:
        return None

    tiers: list = []
    for clause in clauses:
        qty_matches = list(_TIER_QTY_UNIT_RE.finditer(clause))
        price_matches = list(_TIER_PRICE_CURRENCY_RE.finditer(clause))
        if len(qty_matches) != 1 or len(price_matches) != 1:
            continue  # clause sans info tarifaire (ex: "je vend le bidon de")
        qty_val = _parse_number(qty_matches[0].group(1))
        unit_raw = qty_matches[0].group(2)
        price_val = _parse_number(price_matches[0].group(1))
        if qty_val is None or price_val is None or qty_val <= 0 or price_val <= 0:
            return None
        packaging_match = _TIER_PACKAGING_RE.search(clause)
        # "celui de 10 L à 900f" (référence au conditionnement du tarif
        # précédent, ex: "bidon") sans le renommer — hérite du dernier
        # conditionnement vu plutôt que de le laisser vide.
        packaging = (
            packaging_match.group(1).lower()
            if packaging_match
            else (tiers[-1]["packaging"] if tiers else None)
        )
        tiers.append(
            {
                "quantity": qty_val,
                "unit": unit_raw.strip(),
                "price": price_val,
                "packaging": packaging,
            }
        )

    if len(tiers) < 2:
        return None
    return tiers


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
    "convert_quantity",
    "extract_unit_only_from_text",
    "NumberCandidate",
    "scan_number_candidates",
    "LIVESTOCK_PRODUCT_KEYWORDS",
    "is_livestock_product",
    "default_unit_for_product",
    "resolve_product_unit",
]
