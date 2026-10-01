"""Quantity / unit parsing and normalisation — single source of truth.

All unit synonym resolution, quantity-from-text extraction, and unit
validation lives here. Consumers (validator, entities, helpers) import
from this module instead of maintaining their own copies.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Optional

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
    # (2026-09-30, Étape 6 — centralisation des unités) : sous-multiples MASS/VOLUME,
    # auparavant reconnus UNIQUEMENT par la table locale et délibérément isolée
    # `commercial_offer_flow.py::_CONTENT_UNIT_MAP` ("jamais ajoutés aux registres
    # globaux d'unités") — absents d'ici, donc invisibles à `normalize_unit`/
    # `convert_quantity`/au moteur de packaging déterministe (`_TIER_QTY_UNIT_RE` et
    # consorts, plus bas dans ce fichier), qui ne reconnaissaient donc PAS "500 ml"
    # comme une unité valide. Canonicalisés en tokens de famille PROPRES
    # (MILLILITRE/CENTILITRE/DECILITRE/GRAMME), jamais collapsés en LITRE/KG ici —
    # cette conversion est le rôle de `convert_quantity`/`measurement_family`
    # ci-dessous, pas de la table d'alias.
    "g": "GRAMME",
    "gramme": "GRAMME",
    "grammes": "GRAMME",
    "ml": "MILLILITRE",
    "millilitre": "MILLILITRE",
    "millilitres": "MILLILITRE",
    "cl": "CENTILITRE",
    "centilitre": "CENTILITRE",
    "centilitres": "CENTILITRE",
    "dl": "DECILITRE",
    "decilitre": "DECILITRE",
    "decilitres": "DECILITRE",
    # (2026-09-30, Étape 6 clôture) : QUINTAL était converti UNIQUEMENT par
    # `market_coach/actions/common.py::_UNIT_TO_KG` (1 quintal = 100 kg,
    # verrouillé par `tests/unit/test_commercial_offer_price_basis_hardening.py
    # ::test_quintal_still_converts`, preuve d'usage réel) — absent d'ici, donc
    # invisible à `measurement_family`/`convert_quantity`. Pas d'alias "q" :
    # aucune preuve d'usage trouvée dans le dépôt (prompts, interpréteur,
    # tests) — un caractère unique ambigu n'est ajouté que sur preuve.
    "quintal": "QUINTAL",
    "quintaux": "QUINTAL",
}

VALID_UNITS = frozenset(UNIT_SYNONYMS.values())

_QUANTITY_UNIT_RE = re.compile(
    r"(?P<qty>\d[\d\s.,]*)\s*(?P<unit>[a-zA-ZÀ-ÖØ-öø-ÿ]+)",
    re.IGNORECASE,
)

_UNIT_ONLY_RE = re.compile(r"\b([a-zA-ZÀ-ÖØ-öø-ÿ]{1,12})\b", re.IGNORECASE)


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
    return _collapse_to_base_unit(qty_val, mapped_unit)


#: Unités dont ce module collapse désormais la quantité vers la base de leur
#: famille AU POINT D'EXTRACTION (mandat Étape 6 §7/§11) — restreint
#: DÉLIBÉRÉMENT aux 4 tokens canoniques introduits par cette étape
#: (MILLILITRE/CENTILITRE/DECILITRE/GRAMME), jamais TONNE (ni aucune autre
#: unité déjà reconnue AVANT l'Étape 6) : TONNE a TOUJOURS été retournée telle
#: quelle par ce module (jamais auto-convertie en KG) — un collapse générique
#: "toute unité != sa base" aurait changé ce comportement établi
#: (`parse_quantity_unit_from_text("2 tonnes")` devait rester `(2.0, "TONNE")`,
#: pas devenir `(2000.0, "KG")` — régression réelle détectée et corrigée avant
#: ce correctif, voir `tests/unit/test_canonical_unit_system.py::
#: TestNoRegressionOnAlreadyRecognizedUnits`). Pur ADDITIF pour ces 4 tokens
#: précisément : avant l'Étape 6, aucun appelant ne pouvait les recevoir
#: (absents d'`UNIT_SYNONYMS`), donc ce collapse ne change AUCUN comportement
#: pour une unité déjà reconnue.
_COLLAPSIBLE_SUB_UNITS = frozenset({"MILLILITRE", "CENTILITRE", "DECILITRE", "GRAMME"})


def _collapse_to_base_unit(
    qty_val: Optional[float], mapped_unit: Optional[str]
) -> "tuple[Optional[float], Optional[str]]":
    """Convertit un sous-multiple NOUVELLEMENT introduit (voir
    `_COLLAPSIBLE_SUB_UNITS`) vers l'unité de base de sa famille (LITRE/KG) —
    mandat Étape 6 §7/§11 : "normalisation au bord du système", jamais une
    unité de sous-multiple qui circule dans l'état/le domaine."""
    if qty_val is None or mapped_unit is None or mapped_unit not in _COLLAPSIBLE_SUB_UNITS:
        return qty_val, mapped_unit
    base = base_unit_for(mapped_unit)
    if base is None or base == mapped_unit:
        return qty_val, mapped_unit
    converted = convert_quantity(qty_val, mapped_unit, base)
    if converted is None:
        return qty_val, mapped_unit
    return converted, base


#: Numéraux français SANS ambiguïté d'article (lexique FERMÉ, pas une liste d'intentions) :
#: chacun énonce un nombre ; voir `text_states_a_quantity`.
_FRENCH_NUMERALS = frozenset(
    "deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze "
    "quinze seize vingt trente quarante cinquante soixante cent cents mille douzaine "
    "dizaine vingtaine trentaine quinzaine centaine demi demie moitie".split()
)

#: « un/une » est aussi l'article indéfini : il ne vaut preuve de quantité que s'il n'ouvre
#: pas une locution non quantitative (« un peu de », « un autre », « un petit »…).
_NON_QUANTITATIVE_AFTER_UN = frozenset(
    "peu petit petite grand grande autre max maximum minimum tas truc machin "
    "bon bonne gros grosse bout brin".split()
)


def text_states_a_quantity(text: str, proposed: Optional[float] = None) -> bool:
    """Le message énonce-t-il VRAIMENT une quantité ?

    Garde de PROVENANCE : une quantité extraite par un LLM sans appui textuel est une
    invention (incident 2026-10-01 — « je veux acheter du lait » devenu `quantity=1`).
    Preuves acceptées : un chiffre ; un numéral français non ambigu ; « un/une » seulement
    s'il n'ouvre pas une locution non quantitative (« un peu de lait » n'énonce pas 1) ET
    que la valeur proposée est 1 (ou inconnue). Ne juge PAS la valeur extraite pour les
    autres preuves (conversions d'unité légitimes : « une demi-tonne » → 500 kg)."""
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    if re.search(r"\d", folded):
        return True
    tokens = re.findall(r"[a-z]+", folded)
    if any(tok in _FRENCH_NUMERALS for tok in tokens):
        return True
    if proposed is not None and float(proposed) != 1.0:
        return False
    for i, tok in enumerate(tokens):
        if tok in ("un", "une"):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt not in _NON_QUANTITATIVE_AFTER_UN:
                return True
    return False


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


def find_convertible_quantity_pairs(text: str) -> "list[QuantityUnitResult]":
    """Toutes les paires quantité+unité RECONNUES ET convertibles en KG (KG/TONNE), dans
    l'ordre d'apparition dans *text* — contrairement à `parse_compound_quantity` (qui ne
    renvoie QUE leur somme, en supposant qu'elles décrivent TOUTES la même quantité), sert à un
    appelant qui doit comparer chaque paire individuellement à une quantité déjà connue (garde
    générique "quantité orpheline", incident réel 2026-09-26 — voir
    `new_task_micro.py::_finalize`, "150 kg tomate et 200 kg chaque semaine" : deux paires
    convertibles, mais PAS la même quantité fragmentée — une deuxième quantité, orpheline)."""
    if not text:
        return []
    out: "list[QuantityUnitResult]" = []
    for m in _QUANTITY_UNIT_RE.finditer(text):
        qty_val, mapped_unit = _extract_match(m)
        if qty_val is not None and mapped_unit in _UNIT_TO_KG:
            out.append(QuantityUnitResult(quantity=qty_val, unit=mapped_unit))
    return out


_BARE_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[A-Za-zÀ-ÖØ-öø-ÿ]+")

#: Un nombre immédiatement suivi d'un de ces mots est une DURÉE ("pendant 2 semaines") ou une
#: CADENCE ("chaque 2 semaines" — le mot "chaque" précède, mais c'est bien le mot de durée
#: directement APRÈS le nombre qui le distingue d'une quantité), jamais une quantité de produit.
_DURATION_WORDS = frozenset(
    {
        "jour", "jours", "semaine", "semaines", "mois", "an", "ans", "annee", "annees",
        "heure", "heures", "trimestre", "trimestres",
    }
)

#: Un nombre immédiatement suivi OU précédé d'un de ces mots est un PRIX, jamais une quantité de
#: produit (déjà porté par `price`/`price_unit`, extraits séparément par le LLM).
_CURRENCY_WORDS = frozenset({"fcfa", "cfa", "franc", "francs", "f"})

#: "150 à 200 kg" : une PLAGE, une seule quantité éventuelle — jamais deux quantités candidates
#: distinctes. "à" accent-plié ("a") par `normalize_unit_token`, comme tout le reste du module.
_RANGE_CONNECTOR_WORDS = frozenset({"a"})


def find_bare_number_candidates(
    text: str, *, exclude_values: "Iterable[float]" = ()
) -> "list[float]":
    """Nombres NUS (sans unité littérale accolée) dans *text*, dans l'ordre d'apparition —
    candidats à une DEUXIÈME quantité de produit sans unité écrite (bétail compté en TETE,
    sacs/paniers sans répétition du mot, ou tout produit compté à l'unité). Complète
    `find_convertible_quantity_pairs` (qui ne voit que KG/TONNE) pour le même usage : détecter
    une quantité candidate que ni le produit principal ni `additional_items` n'expliquent, SANS
    jamais reconnaître un nom de produit précis (aucun mot d'espèce/d'animal ici — seulement des
    marqueurs GÉNÉRIQUES de rôle sémantique : durée, prix, plage).

    Exclut, génériquement :
    - un nombre suivi d'une unité littérale RECONNUE (déjà couvert par
      `find_convertible_quantity_pairs`/le parseur d'unité — jamais compté deux fois ici) ;
    - un nombre suivi d'un mot de DURÉE — couvre aussi la CADENCE ("chaque N semaines") ;
    - un nombre suivi OU précédé d'un mot de PRIX/devise ;
    - un nombre faisant partie d'une PLAGE ("N1 à N2") — les deux bornes sont exclues ;
    - les valeurs de `exclude_values` (typiquement la quantité déjà attribuée au produit
      principal), à `1e-9` près.

    Incident réel 2026-09-26 (suite) : "40 chèvres et 20 chaque semaine" — aucun mot d'unité
    littéral pour "20", donc invisible à `find_convertible_quantity_pairs` ; sans cette fonction,
    le filet Python de `new_task_micro.py::_finalize` ne détectait QUE les quantités orphelines
    en kg/tonne, laissant le bétail (et tout produit compté sans mot d'unité) entièrement
    dépendant du LLM pour `orphan_quantities`."""
    if not text:
        return []
    tokens = _BARE_NUMBER_TOKEN_RE.findall(text)

    def _as_number(tok: str) -> Optional[float]:
        try:
            return float(tok.replace(",", "."))
        except ValueError:
            return None

    numeric: "list[tuple[int, float]]" = []
    for i, tok in enumerate(tokens):
        val = _as_number(tok)
        if val is not None:
            numeric.append((i, val))

    excluded_positions: set = set()
    for i, _val in numeric:
        prev_word = normalize_unit_token(tokens[i - 1]) if i > 0 else ""
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
        next_word = normalize_unit_token(next_tok)
        if next_tok and normalize_unit(next_tok) is not None:
            excluded_positions.add(i)
        if next_word in _DURATION_WORDS:
            excluded_positions.add(i)
        if next_word in _CURRENCY_WORDS or prev_word in _CURRENCY_WORDS:
            excluded_positions.add(i)
        if next_word in _RANGE_CONNECTOR_WORDS and i + 2 < len(tokens) and _as_number(tokens[i + 2]) is not None:
            excluded_positions.add(i)
            excluded_positions.add(i + 2)

    excluded_values = list(exclude_values)
    result: "list[float]" = []
    for i, val in numeric:
        if i in excluded_positions:
            continue
        if any(abs(val - ev) < 1e-9 for ev in excluded_values):
            continue
        result.append(val)
    return result


# ---------------------------------------------------------------------------
# Familles MASS/VOLUME — source canonique unique (2026-09-30, Étape 6)
# ---------------------------------------------------------------------------
# Avant ce correctif, le facteur de conversion MASS/VOLUME le plus complet du
# dépôt vivait dans `domain/pricing_tiers.py::_MASS_FACTORS`/`_VOLUME_FACTORS`
# (documenté comme tel, "la plus complète du dépôt" — déjà réutilisé par
# `commercial_offer.py`, `commercial_pricing_snapshot.py`, `price_basis.py`,
# `domain/analytics/units.py`) — mais CE module, plus bas dans la même
# dépendance (`pricing_tiers.py` importe déjà `normalize_unit`/`convert_quantity`
# d'ici, jamais l'inverse), ne connaissait que KG/TONNE via `_UNIT_TO_KG`
# (ci-dessus, volontairement INCHANGÉ — voir sa propre docstring d'usage :
# `parse_compound_quantity`/`find_convertible_quantity_pairs` restent scopés à
# leur usage historique). Cette table-ci est la SOURCE UNIQUE désormais pour
# `measurement_family`/`are_units_compatible`/`convert_quantity` (ci-dessous) —
# `pricing_tiers.py::unit_family`/`unit_factor` délèguent ici plutôt que de
# maintenir leur propre copie (voir leur nouvelle implémentation).
#
# Clés = tokens CANONIQUES (sortie de `normalize_unit`/`UNIT_SYNONYMS`),
# jamais les alias bruts — `measurement_family`/`convert_quantity` normalisent
# leurs entrées avant consultation.
_MASS_FACTORS: Dict[str, float] = {
    "KG": 1.0,
    "GRAMME": 0.001,
    "TONNE": 1000.0,
    "QUINTAL": 100.0,
}
_VOLUME_FACTORS: Dict[str, float] = {
    "LITRE": 1.0,
    "MILLILITRE": 0.001,
    "CENTILITRE": 0.01,
    "DECILITRE": 0.1,
}

#: Unité de base physique retenue par famille (même convention déjà établie et
#: documentée dans `domain/commercial_offer.py::_BASE_UNIT_BY_FAMILY` — reprise
#: ici à l'identique, jamais une seconde décision divergente).
BASE_UNIT_BY_FAMILY: Dict[str, str] = {"MASS": "KG", "VOLUME": "LITRE"}


def _canonicalize_for_conversion(unit: Optional[str]) -> str:
    """Normalise *unit* (alias brut OU déjà canonique) pour la consultation des
    tables ci-dessus — jamais d'exception, une unité non reconnue traverse
    telle quelle (elle ne matchera simplement aucune table)."""
    if not unit:
        return ""
    return normalize_unit(unit) or str(unit).strip().upper()


def measurement_family(unit: Optional[str]) -> Optional[str]:
    """``MASS`` / ``VOLUME`` / ``None`` (unité inconnue OU famille singleton —
    SAC/PANIER/TETE/UNITE : aucune conversion déterministe n'existe pour elles,
    voir le module docstring de `commercial_offer.py::convertible_measurement_
    family`, dont ceci est désormais la source)."""
    canonical = _canonicalize_for_conversion(unit)
    if canonical in _MASS_FACTORS:
        return "MASS"
    if canonical in _VOLUME_FACTORS:
        return "VOLUME"
    return None


def are_units_compatible(unit_a: Optional[str], unit_b: Optional[str]) -> bool:
    """True si une quantité dans *unit_a* peut être convertie en *unit_b* SANS
    information métier supplémentaire — même famille MASS/VOLUME uniquement.
    Une unité identique à elle-même (y compris une famille singleton comme
    TETE==TETE) est toujours compatible ; jamais entre familles différentes
    (KG/LITRE, UNITE/KG...) ni entre deux familles singleton distinctes."""
    a = _canonicalize_for_conversion(unit_a)
    b = _canonicalize_for_conversion(unit_b)
    if not a or not b:
        return False
    if a == b:
        return True
    fam_a, fam_b = measurement_family(a), measurement_family(b)
    return fam_a is not None and fam_a == fam_b


def convert_quantity(quantity: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Convert *quantity* from one unit to another — canonical or a raw alias
    (both are normalized before lookup, e.g. "ml"/"millilitres"/"MILLILITRE"
    all resolve identically).

    Only safe for units with a FIXED, universal factor (MASS: KG/GRAMME/TONNE ;
    VOLUME: LITRE/MILLILITRE/CENTILITRE/DECILITRE — see `_MASS_FACTORS`/
    `_VOLUME_FACTORS` above, the single canonical source for this repo).
    Returns ``None`` when no safe conversion exists (e.g. SAC/PANIER/TETE have
    no universal equivalent, and MASS<->VOLUME is never attempted) — callers
    must not guess in that case, only ask the user to restate the quantity in
    the target unit. NEVER an inter-family conversion (mandat Étape 6 §6) :
    `are_units_compatible(KG, LITRE)` is False, and so is this function's
    result for that pair.
    """
    if from_unit == to_unit:
        # Préserve le raccourci d'identité brut d'origine (y compris pour une
        # chaîne vide/inconnue non canonisable) — jamais de régression sur ce
        # cas dégénéré, même s'il est peu probable en pratique.
        return quantity
    from_canonical = _canonicalize_for_conversion(from_unit)
    to_canonical = _canonicalize_for_conversion(to_unit)
    if from_canonical and from_canonical == to_canonical:
        return quantity
    from_family = measurement_family(from_canonical)
    to_family = measurement_family(to_canonical)
    if from_family is None or from_family != to_family:
        return None
    table = _MASS_FACTORS if from_family == "MASS" else _VOLUME_FACTORS
    return quantity * table[from_canonical] / table[to_canonical]


def base_unit_for(unit: Optional[str]) -> Optional[str]:
    """Unité de base canonique de la famille de *unit* : KG pour MASS, LITRE
    pour VOLUME, sinon l'unité elle-même (TETE, SAC, PANIER... comptés tels
    quels — une famille singleton EST sa propre base). ``None`` uniquement si
    *unit* est vide/absent. Même décision que `commercial_offer.py::
    base_unit_for` (dont c'est désormais la source canonique — voir Étape 6)."""
    if not unit:
        return None
    canonical = _canonicalize_for_conversion(unit)
    family = measurement_family(canonical)
    if family:
        return BASE_UNIT_BY_FAMILY[family]
    return canonical


#: Apostrophes (droite et typographique) marquant une ÉLISION française.
_ELISION_CHARS = ("'", "’")


#: Symboles d'unité à UNE SEULE LETTRE (« l »=litre, « t »=tonne, « k »=kg) —
#: lexicalement INDISCERNABLES d'un article élidé sans apostrophe (« l'unité »,
#: « l'année », « t'as »…) une fois découpés en tokens par `_UNIT_ONLY_RE`, quel
#: que soit le mot qui suit. Voir la garde par proximité dans
#: `extract_unit_only_from_text`.
_SINGLE_LETTER_UNIT_SYMBOLS = frozenset(k for k in UNIT_SYNONYMS if len(k) == 1)


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
        _folded = normalize_unit_token(match.group(1))
        if _folded in _SINGLE_LETTER_UNIT_SYMBOLS:
            # Incident réel (2026-09-15) : « L unite coute 425000 fcfa » —
            # l'apostrophe de « l'unité » était simplement OMISE (faute de
            # frappe WhatsApp courante), donc le garde ci-dessus (qui ne
            # détecte qu'une apostrophe collée) ne voyait rien à sauter, et
            # « L » isolé valait LITRE pour un producteur vendant des BOEUFS.
            # Patcher le seul mot « unité » serait fragile : « l'année »,
            # « l'animal », « t'inquiète »… sans apostrophe tombent dans le
            # même piège, quel que soit le mot qui suit. La règle générale et
            # robuste : un VRAI symbole d'une seule lettre s'écrit TOUJOURS
            # collé à la quantité qu'il qualifie (« 25 L », « 10T »), jamais
            # isolé ailleurs dans la phrase — on exige donc un CHIFFRE
            # immédiatement avant (espaces mis à part), qu'importe le mot qui
            # suit. Un article élidé n'a jamais de chiffre juste devant lui.
            before = text[: match.start()].rstrip()
            if not before or not before[-1].isdigit():
                continue
        mapped = UNIT_SYNONYMS.get(_folded)
        if mapped:
            return mapped
    return None


#: Unités PHYSIQUEMENT IMPOSSIBLES pour un animal vivant — masse (on ne pèse
#: pas un animal au kilo pour le vendre) et volume (un animal n'est pas un
#: liquide). Volontairement PAS de conditionnement (SAC/PANIER) : un
#: producteur qui vend « 20 sacs de poussins » (poussins en vrac) reste dans
#: le domaine du possible — voir `test_a_plausible_unit_on_livestock_is_left
#: _alone`, une décision de conception délibérée que la correction de la
#: règle 2 de `resolve_product_unit` ne doit pas écraser.
_LIVESTOCK_IMPOSSIBLE_UNITS = frozenset({"KG", "TONNE", "LITRE"})


def _clean_category_config(
    category_config: Optional[Dict[str, Any]],
) -> "Optional[tuple[Optional[str], frozenset[str]]]":
    """Normalise un `category_config` brut (venant d'un outil MCP, donc
    potentiellement `None`/mal formé) en `(priority_unit, allowed_units)`.
    Renvoie `None` si la config est absente ou inexploitable — jamais une
    exception : une config catégorie mal formée ne doit JAMAIS faire échouer
    la résolution d'unité, seulement la dégrader au comportement historique."""
    if not category_config or not isinstance(category_config, dict):
        return None
    raw_allowed = category_config.get("allowed_units") or []
    if not isinstance(raw_allowed, (list, tuple)):
        return None
    allowed = frozenset(
        normalize_unit(u) or str(u).strip().upper() for u in raw_allowed if u
    )
    if not allowed:
        return None
    raw_priority = category_config.get("priority_unit")
    priority = (normalize_unit(raw_priority) or str(raw_priority).strip().upper()) if raw_priority else None
    if priority and priority not in allowed:
        # Config incohérente (l'unité prioritaire n'est pas dans l'ensemble
        # autorisé) — on préfère l'ignorer plutôt que de retourner une unité
        # que l'appelant n'attend pas dans `allowed_units`.
        priority = None
    if priority is None and len(allowed) == 1:
        priority = next(iter(allowed))
    return priority, allowed


def resolve_product_unit(
    product: Any,
    current_unit: Any = None,
    text_unit: Any = None,
    category_config: Optional[Dict[str, Any]] = None,
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

    (2026-09-19, retour produit — DESIGN PATTERN, plus une regex) : deviner
    l'unité depuis le texte libre (élevage → mots-clés codés en dur, sinon
    rien) reste fondamentalement un pari — chaque incident (LITRE pour des
    bœufs, KG pour des poulets...) a forcé un correctif ad hoc de plus. La
    VRAIE source de vérité doit être la configuration ADMIN par
    catégorie/sous-catégorie (déjà le patron établi pour
    `SubCategory.minimum_order_quantity`/`minimum_order_unit`, voir
    `domain/governance/models.py`) : un ENSEMBLE d'unités autorisées + une
    unité PRIORITAIRE pour standardiser (« lait » → LITRE seul ; « bœuf » →
    TETE/UNITE ; « maïs » → G/KG/TONNE/SAC avec un ordre de priorité). Les
    colonnes existent bien en base (`priority_unit text`, `allowed_units
    text[]` sur `governance.sub_categories` — déjà dans
    `schema_contract/migrations/0000_baseline.sql`, corrigé 2026-09-28 :
    l'affirmation précédente qu'elles "n'existent pas encore" était fausse,
    voir `services/database/base.py::get_product_category_unit_config`).
    `category_config` reste `None` en pratique tant qu'AUCUNE sous-catégorie
    n'a ces colonnes RENSEIGNÉES (un problème de donnée administrative à
    combler côté site, pas de schéma manquant) — ce qui fait tomber le
    comportement EXACTEMENT sur les règles 1-4 historiques ci-dessous (zéro
    régression). Dès qu'un appelant peut fournir une config réelle (voir
    `services/database/base.py::get_product_category_unit_config`,
    résolution paresseuse et défensive), elle devient PRIORITAIRE sur tout
    le reste — voir la RÈGLE 0 ci-dessous.

    ## La règle

    Précédence par FIABILITÉ de la source, jamais par ordre d'exécution :

    0. CONFIG CATÉGORIE (si fournie) — `text_unit`/`current_unit` ne sont
       respectés QUE s'ils appartiennent à `allowed_units` (l'admin a
       explicitement autorisé cette unité pour cette catégorie) ; sinon
       `priority_unit` s'applique, quel que soit ce que le texte libre ou
       l'état courant portaient — c'est la définition même de « standardiser ».
    1. `text_unit` — l'utilisateur a écrit l'unité noir sur blanc.
    2. NATURE DU PRODUIT, en CORRECTION — `current_unit` est une unité de
       MASSE ou de VOLUME (`_LIVESTOCK_IMPOSSIBLE_UNITS` : KG, TONNE, LITRE)
       alors que le produit est un animal compté à la tête : un animal
       vivant ne se pèse ni ne se mesure en litres pour être vendu, donc
       c'est `current_unit` qui a tort, quelle qu'en soit la provenance.
       Incident réel (2026-09-15) : cette règle ne couvrait à l'origine QUE
       KG/TONNE (`_UNIT_TO_KG`) — un « LITRE » posé pour des chèvres (par le
       même défaut d'élision déjà corrigé ailleurs, ou toute autre source)
       restait donc collé DÉFINITIVEMENT, jamais corrigé, puisque LITRE n'a
       pas d'équivalent kg connu. Volontairement PAS de conditionnement
       (SAC/PANIER) dans cette liste : « 20 sacs de poussins » reste
       plausible, ce n'est pas à cette fonction d'en juger (voir
       `test_a_plausible_unit_on_livestock_is_left_alone`).
    3. `current_unit` — déjà posée et plausible : on n'y touche pas.
    4. NATURE DU PRODUIT, en DÉFAUT — rien de connu : TETE pour un élevage,
       `None` sinon (à l'appelant de demander plutôt que de deviner).

    Les règles 1-4 restent le FILET DE SÉCURITÉ pour tout produit dont la
    catégorie n'est pas encore configurée (ou pas encore résolue côté
    catalogue) — jamais supprimées, seulement court-circuitées dès qu'une
    config catégorie fiable existe.

    ## Périmètre assumé

    Cette fonction décide « quelle unité pour CE produit », pas « l'utilisateur
    est-il en train de corriger son unité ». La correction explicite en cours
    de conversation reste la propriété de `interpreter/routing.py` (garde
    anti-ancrage texte>LLM) et de `nodes/memory.py::_apply_slot` (qui purge le
    prix en cascade quand l'unité change — un prix « 3000/KG » ne survit pas à
    un passage en SAC). Les appelants ne fournissent donc `text_unit` que
    lorsqu'ils sont légitimes à trancher ce point.
    """
    cleaned_category = _clean_category_config(category_config)
    if cleaned_category is not None:
        priority_unit, allowed_units = cleaned_category
        if text_unit:
            canonical = normalize_unit(text_unit) or str(text_unit).strip().upper()
            if canonical in allowed_units:
                return canonical
        if current_unit not in (None, "", [], {}):
            canonical = normalize_unit(current_unit) or str(current_unit).strip().upper()
            if canonical in allowed_units:
                return canonical
        if priority_unit:
            return priority_unit
        # Config présente mais sans unité prioritaire exploitable (ensemble à
        # plusieurs unités, aucune priorité valide) : on retombe sur les
        # règles historiques plutôt que de deviner arbitrairement laquelle
        # des `allowed_units` choisir.

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
        if natural and current_canonical in _LIVESTOCK_IMPOSSIBLE_UNITS:
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
    # (2026-09-30, Étape 6) : sous-multiples MASS/VOLUME — voir UNIT_SYNONYMS
    # plus haut pour pourquoi ils étaient absents (table locale isolée dans
    # commercial_offer_flow.py). Formes accentuées ET non-accentuées pour
    # "décilitre" : ce scan ne passe QUE par `.lower()` (pas de pliage
    # d'accent) avant consultation, contrairement à `normalize_unit_token`.
    r"g|gramme|grammes|ml|millilitre|millilitres|cl|centilitre|centilitres|"
    r"dl|decilitre|decilitres|décilitre|décilitres|"
    r"l|litre|litres)\b(?!['’])"
)
_SCAN_CURRENCY_RE = re.compile(
    r"(?<![a-zàâäéèêëïîôöùûüÿçA-ZÀÂÄÉÈÊËÏÎÔÖÙÛÜŸÇ])(fcfa|cfa|francs?|balles?)\b"
)
_SCAN_NUMBER_RE = re.compile(r"(\d+[\d\s,.]*)")

# Mots-like tokens à comparer à "fcfa"/"cfa" en repli flou (Bug B, 2026-09-19).
_CURRENCY_WORDLIKE_RE = re.compile(r"[a-zàâäéèêëïîôöùûüÿç]+")


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    """True si *a* et *b* diffèrent d'au plus UNE insertion/suppression/
    substitution/transposition de deux caractères ADJACENTS (distance de
    Damerau-Levenshtein <= 1) — implémentation minimale (pas de dépendance
    externe), déjà utilisée nulle part ailleurs dans ce module, mais
    volontairement générique (pas un correctif ad hoc pour un seul mot) :
    voir `_looks_like_fcfa_typo`. La transposition est incluse car c'est un
    type de faute de frappe mobile aussi courant que l'insertion/omission
    (ex: "fcaf" pour "fcfa")."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        mismatches = [i for i in range(la) if a[i] != b[i]]
        if len(mismatches) <= 1:
            return True
        if (
            len(mismatches) == 2
            and mismatches[1] == mismatches[0] + 1
            and a[mismatches[0]] == b[mismatches[1]]
            and a[mismatches[1]] == b[mismatches[0]]
        ):
            return True  # transposition de deux caractères adjacents
        return False
    longer, shorter = (a, b) if la > lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(longer) and j < len(shorter):
        if longer[i] == shorter[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        i += 1
    return True


def _looks_like_fcfa_typo(word: str) -> bool:
    """Incident réel (2026-09-19) : « 495000 fcfca par unite » — faute de
    frappe WhatsApp sur "fcfa" (une lettre insérée) faisait échouer
    `_SCAN_CURRENCY_RE` (correspondance EXACTE), donc `near_currency=False` ;
    le nombre était alors classé QUANTITÉ (voir la logique de sélection du
    module appelant) au lieu de PRIX, écrasant la quantité déjà connue par le
    montant du prix. Tolérance à UNE faute de frappe (edit-distance <= 1) sur
    "fcfa" UNIQUEMENT — PAS "cfa" (3 lettres) : "ça" (mot français très
    courant) est à distance 1 de "cfa" (suppression du "f"), ce qui aurait
    classé "à PRIX" n'importe quel nombre suivi de "ça" ("ça coûte...", "25
    sacs, ça part demain"...) — testé et confirmé faux positif avant ce
    garde. "fcfa" (4 lettres, jamais un mot français par ailleurs) n'a pas ce
    risque. Pas de tolérance non plus sur "francs"/"balles" (mots plus longs,
    plus rares en faute de frappe à ce point)."""
    return _edit_distance_at_most_one(word, "fcfa")


def _near_currency_fuzzy(window: str) -> bool:
    return any(_looks_like_fcfa_typo(w) for w in _CURRENCY_WORDLIKE_RE.findall(window))


def _find_scan_unit_token(
    window: str, *, digit_precedes_window: bool = False
) -> Optional[str]:
    """Comme `_SCAN_UNIT_RE.search(window).group(1)`, mais applique la MÊME
    garde anti-élision que `extract_unit_only_from_text` (voir sa docstring,
    incidents 2026-09-08/2026-09-15) — jamais répliquée ici avant (Bug A,
    2026-09-19) : « l unite coute 495000 fcfa » matchait "l" isolé (symbole du
    LITRE) en première position, AVANT d'atteindre "unite" un peu plus loin,
    faisant afficher "FCFA/LITRE" pour une vente de bœufs. `_SCAN_UNIT_RE.search`
    ne renvoie que le PREMIER match par position ; on itère ici sur TOUS les
    matches et on saute ceux qui échouent la garde, au lieu de s'arrêter au
    premier trouvé.

    §BUG CORRIGÉ ICI (2026-09-21, incident réel récurrent — déjà signalé lors
    d'une session précédente, jamais fermé jusqu'ici) : pour un symbole
    mono-lettre (`L`), la garde "un chiffre précède immédiatement" ne
    regardait QUE l'intérieur de `window` — or `scan_number_candidates`
    appelle cette fonction avec `after = clean[m.end():m.end()+SCAN_WINDOW]`,
    une tranche qui EXCLUT PAR CONSTRUCTION le chiffre qui vient d'être
    scanné (il est juste AVANT le début de `after`, jamais dedans). Résultat :
    pour "5 L coûte 500 fcfa", la garde ne pouvait JAMAIS être satisfaite
    pour le "L" collé à "5" (rien avant lui DANS `after`) — elle retombait
    alors sur le "L"/LITRE suivant, capté par erreur pour "500" (le PRIX,
    pas la quantité). Reproduit et confirmé (pas une supposition) :
    `scan_number_candidates("25 L a 500 fcfa")` renvoyait déjà
    `candidates[0].unit is None` et `candidates[1].unit == "LITRE"` AVANT ce
    correctif — l'ancien test de non-régression associé
    (`test_dairy_producer_selling_by_the_litre_is_not_regressed`) ne
    vérifiait jamais QUEL nombre recevait l'unité, laissant ce bug passer
    inaperçu malgré son propre docstring affirmant le contraire (corrigé
    dans le même correctif, voir `tests/unit/test_scan_number_candidates_livestock_price_bug.py`).

    `digit_precedes_window=True` (passé UNIQUEMENT par l'appel sur `after`,
    jamais sur `before`) indique à cette fonction qu'un chiffre est déjà
    connu comme précédant IMMÉDIATEMENT le tout début de `window` (c'est la
    définition même de `after`) — un match mono-lettre trouvé tout au début
    de `window` (rien avant lui dans `window`, hormis des espaces) est donc
    accepté SANS avoir besoin de revoir ce chiffre dans `window` lui-même,
    qui ne peut structurellement pas s'y trouver. Un match mono-lettre plus
    loin dans `window` (donc PAS adjacent au chiffre scanné) garde la garde
    stricte d'origine, inchangée. Le scan de `before` (unité AVANT le
    nombre) n'est PAS concerné par ce correctif — sa sémantique reste celle
    d'origine, aucun changement de comportement pour cette direction."""
    for m in _SCAN_UNIT_RE.finditer(window):
        if m.end() < len(window) and window[m.end()] in _ELISION_CHARS:
            continue
        token = normalize_unit_token(m.group(1))
        if token in _SINGLE_LETTER_UNIT_SYMBOLS:
            before = window[: m.start()].rstrip()
            if before:
                if not before[-1].isdigit():
                    continue
            elif not digit_precedes_window:
                continue
        return m.group(1)
    return None


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
        val = _parse_number(m.group(1))
        if val is None:
            continue
        after = clean[m.end() : m.end() + _SCAN_WINDOW]
        before = clean[max(0, m.start() - _SCAN_WINDOW) : m.start()]
        near_currency = (
            bool(_SCAN_CURRENCY_RE.search(after))
            or bool(_SCAN_CURRENCY_RE.search(before))
            or _near_currency_fuzzy(after)
            or _near_currency_fuzzy(before)
        )
        unit_token = (
            _find_scan_unit_token(after, digit_precedes_window=True)
            or _find_scan_unit_token(before)
        )
        mapped_unit = (
            UNIT_SYNONYMS.get(normalize_unit_token(unit_token))
            if unit_token
            else None
        )
        out.append(
            NumberCandidate(value=val, unit=mapped_unit, near_currency=near_currency)
        )
    return out


# ---------------------------------------------------------------------------
# Fast-path purity guard (2026-09-30, incident réel : "60 L de miel" avec
# product courant="lait" perdait silencieusement "miel" — le fast-path
# numérique QUANTITY/PRICE ne regardait QUE les nombres/unités/devises du
# message, jamais le reste du texte, et un mot métier explicite (nom de
# produit, marqueur de correction "finalement"/"non"...) disparaissait sans
# jamais atteindre la logique de conflit produit existante de
# `nodes/memory.py::_apply_slot`.
# ---------------------------------------------------------------------------
# Mots-outils du français (articles/prépositions/conjonctions/pronoms, verbes
# de déclaration courants, marqueurs de correction/hedging) qui ne portent
# JAMAIS de sens métier propre — un ensemble FERMÉ, PAS une liste de produits
# ni de packaging (voir §5 du mandat : ne jamais hardcoder un catalogue ici).
# "l" n'y figure pas : c'est déjà un symbole d'unité valide dans
# `UNIT_SYNONYMS` (LITRE), donc déjà couvert par cette vérification-là.
#
# (2026-09-30, revue) — élargi après un premier passage trop étroit (une
# simple liaison grammaticale ne suffisait pas : "je veux 60 litre",
# "non, 500 kg" — sans nom de produit différent en jeu, ces corrections
# chiffrées légitimes se sont retrouvées bloquées à tort, cassant des tests de
# non-régression déjà en place). Modelé sur le primitive SŒUR déjà existante
# et du même esprit, `domain/commercial_offer_flow.py::_is_pure_content_reply`
# / `_CONTENT_REPLY_FILLER` ("après retrait des nombres/unités/liaisons, il ne
# reste aucun mot de fond") — non importée ici pour éviter un import circulaire
# (`commercial_offer_flow.py` importe déjà de ce module), mais son ensemble de
# mots-outils partage la même philosophie et sert de référence.
_ANSWER_GLUE_WORDS = frozenset(
    {
        # articles / prépositions / pronoms
        "de", "du", "des", "d", "le", "la", "les", "l", "un", "une",
        "par", "pour", "a", "au", "aux", "et", "ou", "en", "ca", "ça",
        "cela", "il", "elle", "moi", "je", "j", "ce", "cet", "cette",
        # verbes de déclaration/possession courants ("je veux X", "j'ai X",
        # "il faut X") — ne désignent jamais un produit à eux seuls
        "veux", "voudrais", "ai", "as", "faut", "avoir", "vends",
        "vend", "est", "cest", "c",
        # marqueurs de correction/négation/hedging — signalent une correction
        # mais ne portent, seuls, aucune entité métier (voir docstring de
        # `is_pure_numeric_answer` : un nom de produit accolé à l'un de ces
        # mots reste, lui, détecté comme impur — ce ne sont que les mots eux-
        # mêmes qui sont neutres)
        "non", "finalement", "plutot", "plutôt", "mais", "environ",
    }
)

_ANSWER_WORD_TOKEN_RE = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)


def is_pure_numeric_answer(text: str) -> bool:
    """True si *text* ne contient RIEN d'autre qu'un nombre et son
    unité/devise/liaison grammaticale — aucun autre mot.

    Utilisé par les fast-paths de réponse numérique (QUANTITY/PRICE) de
    l'interpréteur pour décider s'ils peuvent résoudre le message sans passer
    par l'interprétation complète. Un fast-path ne doit JAMAIS se contenter de
    reconnaître les nombres qu'il comprend et ignorer silencieusement le
    reste — si un mot survit après avoir écarté unités/devises/liaisons, une
    entité métier explicite (nom de produit, marqueur de correction) peut être
    présente et le message doit repasser par l'interprétation complète.

    Conservateur par construction : dès qu'un mot n'est reconnu dans aucune
    des catégories sûres ci-dessous, la fonction renvoie `False` — mieux vaut
    un appel LLM/interpreter de plus que perdre une entité métier explicite.
    """
    clean = unicodedata.normalize("NFKD", (text or "").strip().lower())
    clean = "".join(ch for ch in clean if not unicodedata.combining(ch))
    for word in _ANSWER_WORD_TOKEN_RE.findall(clean):
        if word in UNIT_SYNONYMS or word in _CURRENCY_WORDS:
            continue
        if word in _ANSWER_GLUE_WORDS:
            continue
        if _looks_like_fcfa_typo(word):
            continue
        return False
    return True


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
#
# Incident réel (2026-09-14) : le découpage ne coupait QUE sur "et"/"ou"/
# "puis"/","/";" — une fin de PHRASE ("... 20 L. prix : 3000fcfa/L ...")
# n'en fait pas partie, donc une clause de QUANTITÉ ("30 bidons de 20 L")
# et la PHRASE DE PRIX suivante ("prix : 3000fcfa/L") fusionnaient en une
# seule clause à 2 nombres — associés à tort comme un tarif "20 L = 3000
# FCFA" (alors que 3000 FCFA/L est un prix de RÉFÉRENCE global, sans lien
# avec ce bidon précis, dont le vrai tarif de 50 000 FCFA était donné plus
# loin). `\.(?:\s+|$)` coupe sur un point de fin de phrase (point suivi
# d'un espace ou de fin de texte) sans toucher aux nombres décimaux
# ("3.5", jamais suivi d'un espace immédiatement après le point).
_TIER_CLAUSE_SPLIT_RE = re.compile(
    r"\bet\b|\bou\b|\bpuis\b|,|;|\.(?:\s+|$)|\n", re.IGNORECASE
)
#: Alternance d'unités partagée par `_TIER_QTY_UNIT_RE`/`_PACKAGE_COUNT_UNIT_RE`
#: (2026-09-30, Étape 6 — un seul endroit à étendre plutôt que deux copies qui
#: pourraient diverger). Sous-multiples MASS/VOLUME ajoutés (ml/cl/dl/g) — même
#: raison que `_SCAN_UNIT_RE` plus haut : auparavant absents, "50 sachets de
#: 500 ml" ne matchait PAS cette alternance et retombait sur une lecture
#: ambiguë/LLM au lieu du moteur de packaging déterministe.
_TIER_UNIT_ALTERNATION = (
    r"kg|kgs|kilo|kilogramme|kilogrammes|tonnes?|tones?|tons?|"
    r"sacs?|sachets?|paniers?|t[êe]tes?|unit[ée]s?|"
    r"g|grammes?|ml|millilitres?|cl|centilitres?|dl|d[ée]cilitres?|"
    r"litres?|l"
)
_TIER_QTY_UNIT_RE = re.compile(
    r"(\d+[\d\s,.]*)\s*(" + _TIER_UNIT_ALTERNATION + r")\b",
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
    """Point UNIQUE d'analyse numérique — `scan_number_candidates`
    (ci-dessus) délègue ici plutôt que de dupliquer la même logique
    (2026-09-28, audit fiabilité agent : les deux avaient chacun leur
    propre copie, corrigées séparément avant ce correctif — source de
    dérive garantie)."""
    cleaned = (raw or "").replace(" ", "")
    # (2026-09-28, audit fiabilité agent — bug réel confirmé, sous-évaluation
    # x1000) : un SEUL point suivi d'EXACTEMENT 3 chiffres ("500.000",
    # "12.500") est structurellement AMBIGU — convention francophone
    # courante du point comme séparateur de milliers (500 000 / 12 500 FCFA)
    # OU un vrai décimal à 3 chiffres après la virgule (rare mais pas
    # impossible, ex. "0.250" kg = 250 g). Rien dans le texte ne permet de
    # trancher de façon fiable ici. Deviner l'une des deux interprétations
    # produirait silencieusement une valeur 1000x trop PETITE dans le cas le
    # plus fréquent (un prix/une quantité) — jamais deviner (mandat "Safe
    # Failure") : rejeter (`None`, l'appelant redemande) plutôt que
    # certifier une transaction sur un montant faux. Vérifié AVANT la
    # conversion virgule->point : une virgule reste sans ambiguïté ici
    # (toujours décimale, jamais un séparateur de milliers dans ce
    # contexte) et ne doit jamais déclencher ce rejet.
    if re.fullmatch(r"\d+\.\d{3}", cleaned):
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


#: Motif "N <conditionnement> de/d' M <unité>" — ex: "60 bidons de 5 litres".
#: Réutilise le vocabulaire de conditionnement et l'alternance d'unités déjà
#: définis ci-dessus pour `extract_deterministic_pricing_tiers`.
_PACKAGE_COUNT_UNIT_RE = re.compile(
    r"(?P<count>\d[\d\s.,]*)\s*(?:" + "|".join(_TIER_PACKAGING_WORDS) + r")\b"
    r"\s*(?:de|d['’])\s*"
    r"(?P<qty>\d[\d\s.,]*)\s*"
    r"(?P<unit>" + _TIER_UNIT_ALTERNATION + r")\b",
    re.IGNORECASE,
)


def parse_packaged_compound_quantity(text: str) -> QuantityUnitResult:
    """Parse une quantité totale exprimée en paquets comptés d'un contenu,
    ex: "60 bidons de 5 litres et 20 bidons de 20 litres" (total 700 litres).

    Incident réel (2026-09-14) : `scan_number_candidates` (fenêtre de
    `_SCAN_WINDOW` caractères, utilisée par le fast-path de
    `interpreter/routing.py`) associe un nombre à n'importe quelle unité
    trouvée à proximité, sans vérifier qu'elle lui est réellement ADJACENTE.
    Sur le message ci-dessus, le "60" — un NOMBRE DE PAQUETS, sans dimension
    propre — captait l'unité "litres" du bidon voisin, produisant
    `quantity=60, unit=LITRE` au lieu du volume total réel (700 L) : le
    second groupe "20 bidons de 20 litres" disparaissait purement et
    simplement. Ce parseur reconnaît explicitement le motif "N
    <conditionnement> de M <unité>", multiplie compte × contenu par clause
    et somme les clauses — jamais de résultat partiel deviné : une clause
    ambiguë (0 ou plusieurs correspondances) ou des unités mélangées entre
    clauses annulent le résultat entier (retombe alors sur le comportement
    existant, `parse_quantity_unit_from_text`/`scan_number_candidates`).

    (2026-09-21) Devenue une VUE de `parse_packaging_message` — contrat
    public strictement inchangé. Le "jamais quand un PRIX est mentionné"
    est désormais porté par le moteur (il marque le message ambigu), au
    lieu d'être re-testé ici sur un découpage en clauses parallèle."""
    return packaged_compound_total(parse_packaging_message(text))


def packaged_compound_total(parsed: "PackagingParse") -> QuantityUnitResult:
    """Quantité totale exprimée EN PAQUETS, depuis une analyse déjà faite.

    Même règle que `parse_packaged_compound_quantity` (dont c'est le corps),
    exposée séparément pour les appelants qui ont DÉJÀ le résultat de
    `parse_packaging_message` sous la main — pas de seconde analyse du même
    texte, c'est précisément ce que cette refonte supprime."""
    if parsed.ambiguous or parsed.quantity is None or parsed.quantity <= 0:
        return QuantityUnitResult()
    # Ce parseur est spécifiquement celui des quantités exprimées EN
    # PAQUETS : sans au moins un groupe "N <conditionnement> de M <unité>",
    # il ne répond pas (une quantité simple relève de
    # `parse_quantity_unit_from_text`) — contrat historique.
    if not any(
        c.role is PackagingClauseRole.PACKAGED_GROUP for c in parsed.clauses
    ):
        return QuantityUnitResult()
    return QuantityUnitResult(quantity=parsed.quantity, unit=parsed.unit)


# ===========================================================================
# MOTEUR UNIFIÉ "packaging / multi-tarification" (2026-09-21, refonte
# STRUCTURELLE — remplace 3 analyses divergentes du MÊME texte)
# ===========================================================================
# Pourquoi cette refonte (et pas une Nième rustine) : ce thème a cassé
# plusieurs fois, sous des FORMES DIFFÉRENTES (2026-08-29, 2026-08-30,
# 2026-09-14, 2026-09-19, 2026-09-21). La cause commune n'était jamais la
# regex du jour, mais la TOPOLOGIE : trois mécanismes INDÉPENDANTS
# analysaient le même message sans jamais se recouper —
#
#   1. `scan_number_candidates` : fenêtre glissante de N caractères, AUCUNE
#      notion de clause. Servait à COMPTER les candidats pour décider QUEL
#      fast-path tenter (`_qty_unit_candidates`, `_currency_candidates`).
#   2. `extract_deterministic_pricing_tiers` : découpage en CLAUSES, ses
#      propres regexes, faisait l'extraction RÉELLE des tarifs.
#   3. `parse_packaged_compound_quantity` : découpage en CLAUSES aussi, mais
#      ENCORE d'autres regexes, pour les groupes de conditionnements.
#
# Décider avec (1) puis extraire avec (2)/(3) = deux modèles de segmentation
# pour répondre à des questions qui se recouvrent, sans garantie d'accord.
# Chaque incident ajoutait une branche spéciale de plus à l'arbitrage, sans
# jamais supprimer la divergence de fond.
#
# Ce moteur classe CHAQUE clause UNE FOIS, selon UN seul jeu de règles, et
# expose le résultat complet (quantité globale, tarifs, nombres non
# expliqués, ambiguïté). Tous les consommateurs en dérivent désormais :
# `extract_deterministic_pricing_tiers` et `parse_packaged_compound_quantity`
# deviennent de simples vues de CE résultat (contrats publics inchangés,
# zéro changement pour leurs appelants existants), et
# `interpreter/routing.py` interroge directement le moteur au lieu de
# recouper lui-même des comptages issus d'une autre analyse.
#
# Règles métier préservées à l'identique (chacune issue d'un incident réel,
# aucune n'est réinventée ici) :
#   - un tarif est TOUJOURS "par UN conditionnement" : une clause "N
#     <conditionnement> de M <unité>" avec N != 1 décrit un STOCK, jamais un
#     tarif unitaire (2026-09-14) ;
#   - il faut 2+ tarifs pour lever l'ambiguïté avec un prix simple
#     (2026-08-30) ;
#   - un groupe de conditionnements ne se somme JAMAIS si un prix apparaît
#     dans le texte (2026-09-14) ;
#   - des unités mélangées entre groupes annulent tout le résultat ;
#   - le conditionnement d'un tarif s'hérite du tarif précédent quand la
#     clause y fait référence sans le renommer ("celui de 10 L à 900f").


class PackagingClauseRole(str, Enum):
    """Rôle d'UNE clause dans un message de mise en vente."""

    #: "le bidon de 5 L coûte 500 FCFA" — contenu d'UN paquet + son prix.
    TIER = "TIER"
    #: "60 bidons de 5 L" — un compte de paquets × leur contenu, sans prix.
    PACKAGED_GROUP = "PACKAGED_GROUP"
    #: "600 L de lait" — une quantité globale énoncée directement.
    BARE_QUANTITY = "BARE_QUANTITY"
    #: Aucune information chiffrée exploitable ("je vends du lait frais").
    NO_NUMBER = "NO_NUMBER"
    #: Des nombres, mais un rôle non déterminable sans deviner.
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class PackagingClause:
    role: PackagingClauseRole
    text: str
    #: TIER -> contenu d'UN paquet ; PACKAGED_GROUP -> total du groupe
    #: (compte × contenu) ; BARE_QUANTITY -> la valeur énoncée.
    quantity: Optional[float] = None
    #: Unité canonique (`UNIT_SYNONYMS`), ex "LITRE".
    unit: Optional[str] = None
    #: Unité telle qu'écrite par l'utilisateur — `pricing_tiers` conserve
    #: historiquement cette forme brute ("L"), pas la forme canonique.
    unit_raw: Optional[str] = None
    price: Optional[float] = None
    packaging: Optional[str] = None
    #: TOUTES les valeurs numériques de la clause — sert au contrôle de
    #: complétude (`all_numbers_accounted_for`), jamais à l'extraction.
    numbers: tuple = ()
    #: Nombres que le rôle retenu n'explique PAS (ex: un prix traînant dans
    #: une clause classée BARE_QUANTITY).
    unexplained: tuple = ()


@dataclass(frozen=True)
class PackagingParse:
    """Analyse COMPLÈTE d'un message — source unique de vérité."""

    clauses: tuple = ()
    quantity: Optional[float] = None
    unit: Optional[str] = None
    #: Liste de dicts {"quantity","unit","price","packaging"} — même forme
    #: que ce que le LLM produit, contrat historique inchangé.
    pricing_tiers: tuple = ()
    #: Au moins une clause porte des nombres dont le rôle est indécidable :
    #: l'appelant ne doit JAMAIS produire un résultat partiel, il défère
    #: (LLM). Même discipline que le reste de ce module.
    ambiguous: bool = False
    #: Nombres du message qu'AUCUN champ du résultat n'explique.
    unexplained_numbers: tuple = ()


def _clause_numbers(clause: str) -> tuple:
    out = []
    for m in _SCAN_NUMBER_RE.finditer(clause):
        val = _parse_number(m.group(1))
        if val is not None:
            out.append(val)
    return tuple(out)


def _classify_packaging_clause(clause: str) -> PackagingClause:
    """Classe UNE clause. Aucune décision d'agrégation ici (2+ tarifs,
    unités mélangées, présence d'un prix ailleurs dans le message...) —
    c'est `parse_packaging_message` qui arbitre, avec la vue d'ensemble."""
    numbers = _clause_numbers(clause)
    if not numbers:
        return PackagingClause(
            role=PackagingClauseRole.NO_NUMBER, text=clause, numbers=numbers
        )

    qty_matches = list(_TIER_QTY_UNIT_RE.finditer(clause))
    price_matches = list(_TIER_PRICE_CURRENCY_RE.finditer(clause))
    pack_match = _PACKAGE_COUNT_UNIT_RE.search(clause)
    packaging_match = _TIER_PACKAGING_RE.search(clause)
    packaging = packaging_match.group(1).lower() if packaging_match else None

    # --- "N <conditionnement> de M <unité>" -------------------------------
    if pack_match:
        count_val = _parse_number(pack_match.group("count"))
        content_val = _parse_number(pack_match.group("qty"))
        content_unit = UNIT_SYNONYMS.get(
            normalize_unit_token(pack_match.group("unit"))
        )
        if count_val == 1 and len(price_matches) == 1:
            # "1 sac de 50 kg à 25 000 FCFA" — un tarif, écrit avec le
            # compte explicite. La branche TIER générique plus bas ne sait
            # PAS le lire : "sac" appartient aussi à l'alternance d'unités,
            # donc `_TIER_QTY_UNIT_RE` y voit DEUX quantités ("1 sac" et
            # "50 kg") et renonce. Ici la structure est explicite, le
            # contenu du paquet est sans ambiguïté.
            price_val = _parse_number(price_matches[0].group(1))
            if (
                content_val is not None
                and content_unit is not None
                and price_val is not None
                and content_val > 0
                and price_val > 0
            ):
                explained = {1.0, content_val, price_val}
                # PAS de collapse ici, délibérément : `parse_packaging_message`
                # reconstruit `pricing_tiers` depuis `(clause.quantity,
                # clause.unit_raw)` COUPLÉS (voir plus bas — jamais `.unit`) —
                # un `PricingTier.unit` reste littéral PAR CONTRAT
                # (`pricing_tiers.py::PricingTier.unit`, "jamais normalisé").
                # `.quantity`/`.unit` DOIVENT donc rester dans la MÊME unité
                # que `.unit_raw` pour ce rôle ; c'est `validate_pricing_tiers`
                # (via `_unit_factor`, désormais délégué à ce module — voir sa
                # nouvelle implémentation) qui calcule `base_unit_quantity`
                # correctement à partir de cette paire littérale, PAS un
                # collapse anticipé ici.
                return PackagingClause(
                    role=PackagingClauseRole.TIER,
                    text=clause,
                    quantity=content_val,
                    unit=content_unit,
                    unit_raw=pack_match.group("unit"),
                    price=price_val,
                    packaging=packaging,
                    numbers=numbers,
                    unexplained=tuple(n for n in numbers if n not in explained),
                )
        if count_val is not None:
            # Un STOCK, jamais un tarif (2026-09-14). Avec un prix dans la
            # MÊME clause, la structure est indécidable ("30 bidons de 20 L
            # à 50000" = 30 paquets vendus 50000 l'unité ? au total ?).
            if price_matches:
                return PackagingClause(
                    role=PackagingClauseRole.AMBIGUOUS,
                    text=clause,
                    numbers=numbers,
                    unexplained=numbers,
                )
            if (
                count_val is None
                or content_val is None
                or content_unit is None
                or count_val <= 0
                or content_val <= 0
            ):
                return PackagingClause(
                    role=PackagingClauseRole.AMBIGUOUS,
                    text=clause,
                    numbers=numbers,
                    unexplained=numbers,
                )
            total = count_val * content_val
            # `count` et `content` sont CONSOMMÉS par la multiplication —
            # seul le total survit dans le contrat canonique. `explained`
            # compare aux nombres BRUTS ("100"/"500" pour "100 sachets de
            # 500 ml") — le collapse vers l'unité de base (mandat §7/§8 :
            # "100 sachets de 500 ml" -> disponibilité 50 L) n'intervient
            # qu'APRÈS, sur `.quantity`/`.unit` uniquement.
            explained = {count_val, content_val}
            collapsed_total, collapsed_unit = _collapse_to_base_unit(total, content_unit)
            return PackagingClause(
                role=PackagingClauseRole.PACKAGED_GROUP,
                text=clause,
                quantity=collapsed_total,
                unit=collapsed_unit,
                unit_raw=pack_match.group("unit"),
                packaging=packaging,
                numbers=numbers,
                unexplained=tuple(n for n in numbers if n not in explained),
            )

    # --- Tarif : exactement 1 quantité+unité ET 1 prix+devise -------------
    if len(qty_matches) == 1 and len(price_matches) == 1:
        qty_val = _parse_number(qty_matches[0].group(1))
        unit_raw = qty_matches[0].group(2)
        price_val = _parse_number(price_matches[0].group(1))
        if (
            qty_val is None
            or price_val is None
            or qty_val <= 0
            or price_val <= 0
        ):
            return PackagingClause(
                role=PackagingClauseRole.AMBIGUOUS,
                text=clause,
                numbers=numbers,
                unexplained=numbers,
            )
        explained = {qty_val, price_val}
        # Un compte de paquets ("1 bidon") n'est pas une donnée du tarif,
        # mais il est bien EXPLIQUÉ par cette lecture — jamais "perdu".
        if pack_match:
            pack_count = _parse_number(pack_match.group("count"))
            if pack_count is not None:
                explained.add(pack_count)
        return PackagingClause(
            role=PackagingClauseRole.TIER,
            text=clause,
            quantity=qty_val,
            unit=UNIT_SYNONYMS.get(normalize_unit_token(unit_raw)),
            unit_raw=unit_raw.strip(),
            price=price_val,
            packaging=packaging,
            numbers=numbers,
            unexplained=tuple(n for n in numbers if n not in explained),
        )

    # --- Quantité globale énoncée directement ("600 L de lait") -----------
    if len(qty_matches) == 1 and not price_matches:
        qty_val = _parse_number(qty_matches[0].group(1))
        unit_raw = qty_matches[0].group(2)
        if qty_val is None or qty_val <= 0:
            return PackagingClause(
                role=PackagingClauseRole.AMBIGUOUS,
                text=clause,
                numbers=numbers,
                unexplained=numbers,
            )
        # `.quantity`/`.unit` ALIMENTENT DIRECTEMENT `PackagingParse.quantity`/
        # `.unit` (la disponibilité globale, voir `parse_packaging_message`
        # plus bas — contrairement au rôle TIER ci-dessus) : collapse vers
        # l'unité de base requis ici (mandat §7/§8). `unexplained` compare
        # au nombre BRUT (`qty_val`), jamais à la valeur collapsée.
        collapsed_qty, collapsed_unit = _collapse_to_base_unit(
            qty_val, UNIT_SYNONYMS.get(normalize_unit_token(unit_raw))
        )
        return PackagingClause(
            role=PackagingClauseRole.BARE_QUANTITY,
            text=clause,
            quantity=collapsed_qty,
            unit=collapsed_unit,
            unit_raw=unit_raw.strip(),
            packaging=packaging,
            numbers=numbers,
            unexplained=tuple(n for n in numbers if n != qty_val),
        )

    # --- Quantité nue exprimée dans une unité hors alternance tarifaire ---
    # `_TIER_QTY_UNIT_RE` ne couvre volontairement qu'un sous-ensemble
    # d'unités (celles qui apparaissent dans des tarifs). Pour une clause
    # SANS prix, on délègue au parseur de quantité canonique du module
    # plutôt que d'élargir la regex — un seul parseur de quantité simple
    # dans le dépôt, pas deux vocabulaires d'unités qui divergent.
    if not price_matches and not qty_matches:
        single = parse_quantity_unit_from_text(clause)
        if (
            single.quantity is not None
            and single.unit is not None
            and single.quantity > 0
        ):
            return PackagingClause(
                role=PackagingClauseRole.BARE_QUANTITY,
                text=clause,
                quantity=single.quantity,
                unit=single.unit,
                unit_raw=single.unit,
                packaging=packaging,
                numbers=numbers,
                unexplained=tuple(
                    n for n in numbers if n != single.quantity
                ),
            )

    # --- Tout le reste : des nombres, un rôle indécidable -----------------
    # (prix seul "à 500 fcfa", plusieurs quantités dans la même clause,
    # "3000 FCFA le litre" — prix de RÉFÉRENCE, dont l'extraction
    # déterministe n'est volontairement PAS tentée ici : mieux vaut déférer
    # au LLM que d'inventer une 4e famille de regexes.)
    return PackagingClause(
        role=PackagingClauseRole.AMBIGUOUS,
        text=clause,
        numbers=numbers,
        unexplained=numbers,
    )


def parse_packaging_message(text: str) -> PackagingParse:
    """Analyse UNIQUE d'un message "quantité / conditionnements / tarifs".

    Source de vérité unique de ce thème : tout ce que le dépôt sait extraire
    de façon DÉTERMINISTE (quantité globale, groupes de conditionnements,
    tarifs multiples) sort d'ICI, d'une seule segmentation en clauses et
    d'un seul jeu de règles. Voir le commentaire de section au-dessus pour
    l'historique complet et les règles métier préservées."""
    clean = (text or "").strip()
    if not clean:
        return PackagingParse()

    clauses = tuple(
        _classify_packaging_clause(c.strip())
        for c in _TIER_CLAUSE_SPLIT_RE.split(clean)
        if c.strip()
    )
    if not clauses:
        return PackagingParse()

    roles = [c.role for c in clauses]
    ambiguous = PackagingClauseRole.AMBIGUOUS in roles
    unexplained: list = []
    for clause in clauses:
        unexplained.extend(clause.unexplained)

    # --- Tarifs : 2+ requis (2026-08-30) ---------------------------------
    tier_clauses = [c for c in clauses if c.role is PackagingClauseRole.TIER]
    tiers: list = []
    if len(tier_clauses) >= 2:
        for clause in tier_clauses:
            tiers.append(
                {
                    "quantity": clause.quantity,
                    "unit": clause.unit_raw,
                    "price": clause.price,
                    # Héritage du conditionnement précédent quand la clause
                    # y fait référence sans le renommer ("celui de 10 L").
                    "packaging": clause.packaging
                    or (tiers[-1]["packaging"] if tiers else None),
                }
            )
    elif tier_clauses:
        # UN seul tarif est structurellement ambigu avec un prix simple —
        # ses nombres restent donc inexpliqués, jamais "devinés" en tarif.
        for clause in tier_clauses:
            unexplained.extend(clause.numbers)

    # --- Quantité globale -------------------------------------------------
    group_clauses = [
        c for c in clauses if c.role is PackagingClauseRole.PACKAGED_GROUP
    ]
    bare_clauses = [
        c for c in clauses if c.role is PackagingClauseRole.BARE_QUANTITY
    ]
    quantity: Optional[float] = None
    unit: Optional[str] = None

    if group_clauses:
        # Somme des groupes (et des quantités nues du même message, qui
        # s'y ajoutent : "60 bidons de 5 L et 100 L en vrac") — JAMAIS si
        # un prix apparaît ailleurs dans le message (2026-09-14 : une
        # clause de PRIX a la même forme qu'un groupe de stock, le total se
        # gonflait silencieusement). Des unités mélangées annulent tout.
        has_price_anywhere = any(c.price is not None for c in clauses) or bool(
            _TIER_PRICE_CURRENCY_RE.search(clean)
        )
        summable = group_clauses + bare_clauses
        units = {c.unit for c in summable if c.unit}
        if has_price_anywhere or len(units) != 1:
            ambiguous = True
            for clause in summable:
                unexplained.extend(clause.numbers)
        else:
            quantity = sum(c.quantity or 0.0 for c in summable)
            unit = next(iter(units))
    elif len(bare_clauses) == 1:
        quantity = bare_clauses[0].quantity
        unit = bare_clauses[0].unit
    elif len(bare_clauses) > 1:
        # 2+ quantités nues ("600 L de lait et 5 poulets") : les sommer
        # serait une invention — deux produits distincts peuvent coexister.
        ambiguous = True
        for clause in bare_clauses:
            unexplained.extend(clause.numbers)

    explained_values = set()
    if quantity is not None:
        explained_values.add(quantity)
    for tier in tiers:
        explained_values.add(tier["quantity"])
        explained_values.add(tier["price"])

    return PackagingParse(
        clauses=clauses,
        quantity=quantity,
        unit=unit,
        pricing_tiers=tuple(tiers),
        ambiguous=ambiguous,
        unexplained_numbers=tuple(
            n for n in unexplained if n not in explained_values
        ),
    )


def extract_deterministic_pricing_tiers(text: str) -> Optional[list]:
    """Construit `pricing_tiers` déterministement depuis *text*, ou `None`
    si une clause est ambiguë (jamais de résultat deviné à moitié).

    Renvoie une liste de {"quantity","unit","price","packaging"} — même
    forme que ce que le LLM produit pour `extracted_entities.pricing_tiers`
    (voir routing.py règle 5bis) — uniquement si CHAQUE clause candidate a
    livré EXACTEMENT une paire quantité+unité et prix+devise sans ambiguïté.

    (2026-09-21) Devenue une VUE de `parse_packaging_message` — contrat
    public strictement inchangé (mêmes entrées, mêmes sorties, mêmes
    refus), mais plus de jeu de règles parallèle à maintenir. Reste
    volontairement TOLÉRANTE aux clauses non tarifaires du message (elles
    sont ignorées ici) : ses appelants historiques
    (`flows/producer/flow.py`, garde booléenne de `routing.py`) ne
    s'intéressent qu'aux tarifs. Le contrôle de complétude STRICT est fait
    par `interpreter/routing.py` via `parse_packaging_message` +
    `all_numbers_accounted_for`, là où le résultat alimente réellement un
    brouillon."""
    parsed = parse_packaging_message(text)
    # Un groupe de stock ("30 bidons de 20 L") dans le message interdit
    # toute lecture tarifaire (2026-09-14) — comportement historique.
    if any(
        c.role is PackagingClauseRole.PACKAGED_GROUP for c in parsed.clauses
    ):
        return None
    if any(
        c.role is PackagingClauseRole.AMBIGUOUS
        and _PACKAGE_COUNT_UNIT_RE.search(c.text)
        for c in parsed.clauses
    ):
        return None
    if len(parsed.pricing_tiers) < 2:
        return None
    return [dict(t) for t in parsed.pricing_tiers]


# ---------------------------------------------------------------------------
# Filet de sécurité GÉNÉRIQUE — complétude d'une extraction de quantité/prix/
# tarifs (2026-09-21, correctif STRUCTUREL, pas un rustine de plus)
# ---------------------------------------------------------------------------
# Constat après plusieurs incidents empilés sur ce même thème ("packaging",
# tarifs multiples, quantités composées) : deux analyses INDÉPENDANTES du
# même texte coexistent sans jamais se recouper — `scan_number_candidates`
# (fenêtre glissante, AUCUNE notion de clause) sert à décider QUEL fast-path
# tenter, tandis que `extract_deterministic_pricing_tiers`/
# `parse_packaged_compound_quantity` (découpage en clauses, AUCUNE notion de
# fenêtre) font l'extraction réelle. Rien ne garantit qu'elles restent
# d'accord — c'est exactement ce qui a permis à un nombre de "disparaître"
# silencieusement (incident du 2026-09-21 : "600 L de lait... le bidon de
# 5 L coûte 500 fcfa...", la quantité globale 600 perdue par le fast-path
# `pricing_tiers`, sans qu'aucun garde-fou ne le détecte).
#
# Plutôt que de continuer à rustiner CHAQUE nouvelle forme de message
# composé au fur et à mesure qu'elle casse (l'approche qui a produit cette
# accumulation de branches spéciales dans `interpreter/routing.py`), cette
# fonction ajoute une INVARIANT STRUCTUREL, appliqué en sortie de n'importe
# quel fast-path déterministe qui touche à la quantité/au prix/aux tarifs :
# CHAQUE nombre du message doit être RETROUVABLE quelque part dans le
# résultat extrait. Si un seul nombre n'est expliqué par aucun champ, le
# résultat est structurellement INCOMPLET — l'appelant doit renoncer
# (`return None`, repli sur le LLM) plutôt que de le renvoyer tel quel. Ce
# garde ne sait PAS si le résultat est correct — seulement s'il est
# COMPLET — mais une extraction incomplète est exactement la classe de bug
# qui s'est reproduite plusieurs fois sous des formes différentes ; ce garde
# la ferme UNE FOIS, structurellement, plutôt qu'un cas à la fois.
def all_numbers_accounted_for(
    values: "list[float]", entities: Dict[str, Any]
) -> bool:
    """True si chaque valeur de *values* (typiquement TOUTES les valeurs
    numériques du message source, via `scan_number_candidates`) apparaît
    dans *entities* — `quantity`, `price`, ou le `quantity`/`price` d'un
    `pricing_tiers`. Comparaison à 1e-6 près (valeurs `float`).

    Délibérément TOLÉRANT sur les compteurs de conditionnement ("N bidons
    de M unité" — seul N×M apparaît dans le contrat canonique, jamais N
    isolé) : ce garde ne revalide pas la logique métier de chaque parseur,
    il détecte seulement un nombre purement et simplement OUBLIÉ. Un
    appelant dont le parseur CONSOMME légitimement certains nombres (ex:
    les compteurs de paquets) doit les retirer de *values* avant l'appel —
    voir les commentaires d'utilisation dans `interpreter/routing.py`."""

    def _norm(raw: Any) -> Optional[float]:
        try:
            return round(float(raw), 6)
        except (TypeError, ValueError):
            return None

    used: set = set()
    for key in ("quantity", "price"):
        norm = _norm(entities.get(key))
        if norm is not None:
            used.add(norm)
    for tier in entities.get("pricing_tiers") or []:
        if not isinstance(tier, dict):
            continue
        for key in ("quantity", "price"):
            norm = _norm(tier.get(key))
            if norm is not None:
                used.add(norm)

    for value in values:
        norm = _norm(value)
        if norm is None or norm not in used:
            return False
    return True


def extract_single_pricing_tier_correction(text: str) -> Optional[Dict[str, Any]]:
    """Extrait UN SEUL tarif décrit dans *text* — ex: "prix bidon de 20 L à
    70 000 FCFA", lors d'une correction ciblant UN palier précis d'un produit
    déjà multi-tarifs (2026-09-14, incident réel : un producteur voulant
    corriger le tarif du bidon de 20L n'avait aucun moyen de le faire — le
    flux de mise à jour catalogue ne connaissait QUE prix/quantité/nom/unité
    scalaires, jamais `pricing_tiers`).

    Contrairement à `extract_deterministic_pricing_tiers` (qui exige 2+
    clauses car UNE seule paire quantité+prix est structurellement ambiguë
    avec un prix simple — "500 FCFA" seul), une correction de palier cible
    explicitement un tarif existant : exige exactement UNE paire
    quantité+unité et UNE paire prix+devise dans le texte entier (pas de
    découpage en clauses), sinon `None` — jamais de résultat deviné."""
    if not text:
        return None
    qty_matches = list(_TIER_QTY_UNIT_RE.finditer(text))
    price_matches = list(_TIER_PRICE_CURRENCY_RE.finditer(text))
    if len(qty_matches) != 1 or len(price_matches) != 1:
        return None
    qty_val = _parse_number(qty_matches[0].group(1))
    unit_raw = qty_matches[0].group(2)
    price_val = _parse_number(price_matches[0].group(1))
    if qty_val is None or price_val is None or qty_val <= 0 or price_val <= 0:
        return None
    packaging_match = _TIER_PACKAGING_RE.search(text)
    return {
        "quantity": qty_val,
        "unit": unit_raw.strip(),
        "price": price_val,
        "packaging": packaging_match.group(1).lower() if packaging_match else None,
    }


def find_matching_tier_index(
    tiers: "list", quantity: float, unit: str
) -> Optional[int]:
    """Retrouve l'index du tarif de *tiers* dont (quantité, unité) correspond
    à (*quantity*, *unit*) — comparaison par unité CANONIQUE (accepte "L" ==
    "litres") et quantité à 1e-6 près. `None` si aucune correspondance
    unique (absente ou ambiguë) : jamais de choix arbitraire entre paliers."""
    target_unit = normalize_unit(unit) or str(unit or "").strip().upper()
    matches = [
        i
        for i, tier in enumerate(tiers or [])
        if isinstance(tier, dict)
        and (normalize_unit(tier.get("unit")) or str(tier.get("unit") or "").strip().upper())
        == target_unit
        and tier.get("quantity") is not None
        and abs(float(tier["quantity"]) - float(quantity)) < 1e-6
    ]
    return matches[0] if len(matches) == 1 else None


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
        "cobaye",
        "cobayes",
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

#: Mots qui font d'un nom un PRODUIT DÉRIVÉ d'un animal (vendu au litre, au kg, à la pièce…),
#: jamais l'animal lui-même. Incident réel (2026-09-28) : « lait de vache » contenait le mot
#: « vache » → produit d'élevage → unité LITRE « corrigée » en TETE (50 litres de lait devenaient
#: 50 têtes) et, côté service, LITRE rejeté pour un « animal ». Le nom porte l'animal comme
#: COMPLÉMENT (« de vache »), pas comme tête du groupe nominal. Accents pliés, singulier+pluriel.
ANIMAL_DERIVED_PRODUCT_MARKERS = frozenset(
    {
        "lait", "laits", "laitier", "laitiere", "laitieres",
        "oeuf", "oeufs",
        "viande", "viandes", "carcasse", "carcasses", "abats", "gigot", "cotelette", "cotelettes",
        "fromage", "fromages", "beurre", "creme", "yaourt", "yaourts", "yogourt", "caille",
        "peau", "peaux", "cuir", "laine", "plume", "plumes",
        "fumier", "crottin", "engrais",
        "graisse", "suif", "saucisse", "saucisses", "jambon", "charcuterie",
        "bouillon", "miel",
    }
)


def is_livestock_product(product: Any) -> bool:
    """True when *product* names an animal counted per head rather than weighed.

    Un produit DÉRIVÉ d'un animal (« lait de vache », « œufs de poule », « viande de bœuf »,
    « peau de mouton ») n'est pas un animal : voir `ANIMAL_DERIVED_PRODUCT_MARKERS`."""
    if not product:
        return False
    # Expand ligatures NFKD leaves intact (œ→oe, æ→ae) so "bœuf" matches "boeuf".
    folded = normalize_unit_token(str(product).replace("œ", "oe").replace("æ", "ae"))
    words = _WORD_RE.findall(folded)
    if any(word in ANIMAL_DERIVED_PRODUCT_MARKERS for word in words):
        return False
    return any(word in LIVESTOCK_PRODUCT_KEYWORDS for word in words)


def default_unit_for_product(product: Any, fallback: str = "KG") -> str:
    """Pick the natural default unit for *product*: ``TETE`` for livestock, else *fallback*."""
    return "TETE" if is_livestock_product(product) else fallback


__all__ = [
    "text_states_a_quantity",
    "UNIT_SYNONYMS",
    "VALID_UNITS",
    "normalize_unit_token",
    "normalize_unit",
    "is_valid_unit",
    "QuantityUnitResult",
    "parse_quantity_unit_from_text",
    "parse_compound_quantity",
    "find_convertible_quantity_pairs",
    "find_bare_number_candidates",
    "parse_packaged_compound_quantity",
    "extract_deterministic_pricing_tiers",
    "extract_single_pricing_tier_correction",
    "find_matching_tier_index",
    "convert_quantity",
    "measurement_family",
    "are_units_compatible",
    "base_unit_for",
    "BASE_UNIT_BY_FAMILY",
    "extract_unit_only_from_text",
    "NumberCandidate",
    "scan_number_candidates",
    "LIVESTOCK_PRODUCT_KEYWORDS",
    "ANIMAL_DERIVED_PRODUCT_MARKERS",
    "is_livestock_product",
    "default_unit_for_product",
    "resolve_product_unit",
]
