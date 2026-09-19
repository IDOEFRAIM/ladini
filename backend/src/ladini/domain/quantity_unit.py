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
    TETE/UNITE ; « maïs » → G/KG/TONNE/SAC avec un ordre de priorité). Cette
    config n'existe pas encore en base (à configurer côté site/Drizzle,
    hors de ce dépôt) — `category_config` est donc `None` par défaut
    aujourd'hui, ce qui fait tomber le comportement EXACTEMENT sur les règles
    1-4 historiques ci-dessous (zéro régression). Dès qu'un appelant peut
    fournir une config réelle (voir `services/database/base.py::
    get_product_category_unit_config`, résolution paresseuse et
    défensive), elle devient PRIORITAIRE sur tout le reste — voir la
    RÈGLE 0 ci-dessous.

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


def _find_scan_unit_token(window: str) -> Optional[str]:
    """Comme `_SCAN_UNIT_RE.search(window).group(1)`, mais applique la MÊME
    garde anti-élision que `extract_unit_only_from_text` (voir sa docstring,
    incidents 2026-09-08/2026-09-15) — jamais répliquée ici avant (Bug A,
    2026-09-19) : « l unite coute 495000 fcfa » matchait "l" isolé (symbole du
    LITRE) en première position, AVANT d'atteindre "unite" un peu plus loin,
    faisant afficher "FCFA/LITRE" pour une vente de bœufs. `_SCAN_UNIT_RE.search`
    ne renvoie que le PREMIER match par position ; on itère ici sur TOUS les
    matches et on saute ceux qui échouent la garde, au lieu de s'arrêter au
    premier trouvé."""
    for m in _SCAN_UNIT_RE.finditer(window):
        if m.end() < len(window) and window[m.end()] in _ELISION_CHARS:
            continue
        token = normalize_unit_token(m.group(1))
        if token in _SINGLE_LETTER_UNIT_SYMBOLS:
            before = window[: m.start()].rstrip()
            if not before or not before[-1].isdigit():
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
        raw = (m.group(1) or "").replace(" ", "").replace(",", ".")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        after = clean[m.end() : m.end() + _SCAN_WINDOW]
        before = clean[max(0, m.start() - _SCAN_WINDOW) : m.start()]
        near_currency = (
            bool(_SCAN_CURRENCY_RE.search(after))
            or bool(_SCAN_CURRENCY_RE.search(before))
            or _near_currency_fuzzy(after)
            or _near_currency_fuzzy(before)
        )
        unit_token = _find_scan_unit_token(after) or _find_scan_unit_token(before)
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


#: Motif "N <conditionnement> de/d' M <unité>" — ex: "60 bidons de 5 litres".
#: Réutilise le vocabulaire de conditionnement et l'alternance d'unités déjà
#: définis ci-dessus pour `extract_deterministic_pricing_tiers`.
_PACKAGE_COUNT_UNIT_RE = re.compile(
    r"(?P<count>\d[\d\s.,]*)\s*(?:" + "|".join(_TIER_PACKAGING_WORDS) + r")\b"
    r"\s*(?:de|d['’])\s*"
    r"(?P<qty>\d[\d\s.,]*)\s*"
    r"(?P<unit>kg|kgs|kilo|kilogramme|kilogrammes|tonnes?|tones?|tons?|"
    r"sacs?|sachets?|paniers?|t[êe]tes?|unit[ée]s?|litres?|l)\b",
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
    existant, `parse_quantity_unit_from_text`/`scan_number_candidates`)."""
    if not text:
        return QuantityUnitResult()
    # Jamais quand un PRIX est mentionné dans le texte : "1 bidon de 5 L
    # coûte 10000 fcfa" matche EXACTEMENT le même motif conditionnement que
    # "30 bidons de 20 L" (une vraie quantité de stock) — ce parseur ne sait
    # pas distinguer les deux (incident réel 2026-09-14 : sommait à tort les
    # deux clauses de PRIX comme des groupes de stock supplémentaires, total
    # gonflé de 900 à 925 L). Dès qu'un prix apparaît, le message mélange
    # quantité ET prix : structurellement du ressort du LLM (règle 5bis/
    # 5ter du prompt système), jamais d'une somme aveugle de tout ce qui
    # ressemble à un conditionnement.
    if _TIER_PRICE_CURRENCY_RE.search(text):
        return QuantityUnitResult()
    clauses = [c.strip() for c in _TIER_CLAUSE_SPLIT_RE.split(text) if c.strip()]
    if not clauses:
        return QuantityUnitResult()

    total = 0.0
    unit_canonical: Optional[str] = None
    matched_multiplier = False
    for clause in clauses:
        multiplier_match = _PACKAGE_COUNT_UNIT_RE.search(clause)
        if multiplier_match:
            count_val = _parse_number(multiplier_match.group("count"))
            content_val = _parse_number(multiplier_match.group("qty"))
            content_unit = UNIT_SYNONYMS.get(
                normalize_unit_token(multiplier_match.group("unit"))
            )
            if count_val is None or content_val is None or content_unit is None:
                return QuantityUnitResult()
            if unit_canonical is None:
                unit_canonical = content_unit
            elif unit_canonical != content_unit:
                return QuantityUnitResult()
            total += count_val * content_val
            matched_multiplier = True
            continue

        single = parse_quantity_unit_from_text(clause)
        if single.quantity is None or single.unit is None:
            continue
        if unit_canonical is None:
            unit_canonical = single.unit
        elif unit_canonical != single.unit:
            return QuantityUnitResult()
        total += single.quantity

    if matched_multiplier and unit_canonical and total > 0:
        return QuantityUnitResult(quantity=total, unit=unit_canonical)
    return QuantityUnitResult()


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
        # Un tarif est TOUJOURS "par UN conditionnement" ("le bidon de 5 L
        # coûte 10 000 FCFA", "1 sac de 50 kg à 25 000 FCFA") — jamais "par
        # groupe de N". Incident réel (2026-09-14) : même avec le découpage
        # corrigé ci-dessus, une clause qui décrit un STOCK ("30 bidons de
        # 20 L") reste structurellement identique à une clause de tarif (un
        # nombre+unité) si un prix traîne n'importe où à proximité — mieux
        # vaut refuser tout le résultat (jamais deviné à moitié) que produire
        # un tarif fantôme "20 L = <prix sans rapport>". `_PACKAGE_COUNT_UNIT_RE`
        # capture explicitement ce motif "N <conditionnement> de M <unité>" ;
        # un compte ≠ 1 dans CETTE clause signale une quantité de stock, pas
        # un prix unitaire.
        _pack_match = _PACKAGE_COUNT_UNIT_RE.search(clause)
        if _pack_match:
            _pack_count = _parse_number(_pack_match.group("count"))
            if _pack_count is not None and _pack_count != 1:
                return None
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
    "parse_packaged_compound_quantity",
    "extract_deterministic_pricing_tiers",
    "extract_single_pricing_tier_correction",
    "find_matching_tier_index",
    "convert_quantity",
    "extract_unit_only_from_text",
    "NumberCandidate",
    "scan_number_candidates",
    "LIVESTOCK_PRODUCT_KEYWORDS",
    "is_livestock_product",
    "default_unit_for_product",
    "resolve_product_unit",
]
