"""Validation PURE d'une édition structurée — le modèle n'est jamais l'autorité.

Le modèle interprète une correction (« non 250 kg », « c'est pas 300 c'est 250 ») en {champ, opération, valeur, unité}. Avant toute mutation,
ce module vérifie que cette lecture est COHÉRENTE avec le texte réellement dit et avec l'état métier :

* un nombre qualifié monétairement (« 250 francs », « 280/kg », « 600 le sachet ») ne peut pas devenir une QUANTITÉ ;
* un nombre suivi d'une unité de mesure (« 250 kg », « 8 litres ») ne peut pas devenir un PRIX ;
* « c'est pas 300 c'est 250 » : l'ANCIENNE valeur désigne le champ visé dans l'état courant (jamais une devinette du modèle) ;
* un nombre nu reste gouverné par la question en attente, pas par ce module (il ne tranche pas) ;
* si la lecture est incohérente ET non réparable de façon déterministe -> CLARIFY (jamais de mutation).

Aucune phrase codée en dur : les signaux viennent des primitives unités/devises du domaine (`scan_number_candidates`,
`price_unit_next_to_amount`, `normalize_unit`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Collection, Dict, List, Mapping, Optional, Tuple

from ladini.domain.commercial_offer_flow import price_unit_next_to_amount
from ladini.domain.quantity_unit import normalize_unit, scan_number_candidates

# Champs d'une édition numérique.
QUANTITY = "quantity"
PRICE = "price"
MAX_PRICE = "max_price"
PACKAGE_COUNT = "package_count"

_MONEY_FIELDS = frozenset({PRICE, MAX_PRICE})
_MEASURE_FIELDS = frozenset({QUANTITY, PACKAGE_COUNT})


class ValueCue(str, Enum):
    """Ce que le TEXTE dit d'un nombre."""

    MONEY = "MONEY"  # « 250 francs », « 280/kg », « 600 le sachet »
    MEASURE = "MEASURE"  # « 250 kg », « 8 litres »
    BARE = "BARE"  # nombre nu : le contexte (question en attente) décide, pas ce module


class EditVerdict(str, Enum):
    OK = "OK"
    RECLASSIFIED = "RECLASSIFIED"  # le champ proposé contredisait le texte ; réparé de façon déterministe
    CLARIFY = "CLARIFY"  # incohérent / ambigu : aucune mutation


@dataclass(frozen=True)
class EditCheck:
    verdict: EditVerdict
    field: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    reason: str = ""
    question: Optional[str] = None


def _same(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def cue_for_value(text: Any, value: Any) -> Tuple[ValueCue, Optional[str]]:
    """(signal, unité canonique de MESURE éventuelle) du nombre `value` dans `text`."""
    raw = str(text or "")
    if value is None or not raw.strip():
        return ValueCue.BARE, None
    if price_unit_next_to_amount(raw, float(value)):
        return ValueCue.MONEY, None
    for cand in scan_number_candidates(raw):
        if not _same(cand.value, value):
            continue
        if cand.near_currency:
            return ValueCue.MONEY, None
        if cand.unit:
            return ValueCue.MEASURE, str(cand.unit)
    return ValueCue.BARE, None


def message_carries_value(text: Any) -> bool:
    """Le message contient un nombre (quantité, prix…). Un accord/refus PUR (« oui », « ok vas-y », « laisse tomber ») n'en contient jamais :
    « vas-y mais plutôt 20 » n'est donc pas une confirmation, quelle que soit l'étiquette posée par le modèle."""
    return bool(scan_number_candidates(str(text or "")))


def _clarify_question(field_name: str) -> str:
    if field_name in _MONEY_FIELDS:
        return "Tu parles du *prix* ou de la *quantité* ? Dis-moi par exemple « 280 francs le kg » ou « 250 kg »."
    return "Tu parles de la *quantité* ou du *prix* ? Dis-moi par exemple « 250 kg » ou « 280 francs le kg »."


def validate_structured_edit(
    *,
    field: str,
    value: Optional[float],
    unit: Optional[str],
    text: Any,
) -> EditCheck:
    """Cohérence {champ, valeur, unité} <-> texte. Pure, déterministe, sans I/O."""
    if value is None:
        return EditCheck(EditVerdict.CLARIFY, field=field, reason="valeur absente", question=None)
    cue, measure_unit = cue_for_value(text, value)

    if field in _MEASURE_FIELDS:
        if unit and normalize_unit(str(unit)) is None:
            return EditCheck(EditVerdict.CLARIFY, field=field, value=value, reason="unité de quantité invalide", question=_clarify_question(field))
        if cue is ValueCue.MONEY:
            # « 280 francs le kg » lu comme quantité : le texte dit un PRIX. Réparable seulement par le champ prix.
            return EditCheck(EditVerdict.RECLASSIFIED, field=PRICE, value=value, unit=None, reason="valeur monétaire lue comme quantité")
        return EditCheck(EditVerdict.OK, field=field, value=value, unit=unit or measure_unit)

    if field in _MONEY_FIELDS:
        if cue is ValueCue.MEASURE:
            # « 250 kg » lu comme prix : le texte dit une QUANTITÉ.
            return EditCheck(EditVerdict.RECLASSIFIED, field=QUANTITY, value=value, unit=measure_unit, reason="mesure lue comme prix")
        return EditCheck(EditVerdict.OK, field=field, value=value, unit=unit)

    return EditCheck(EditVerdict.OK, field=field, value=value, unit=unit)


# ── « c'est pas 300 c'est 250 » : ancienne / nouvelle valeur ────────────────────────────────────────────────────────────
_NUM = r"\d[\d\s]*(?:[.,]\d+)?"
_NEGATED_NUMBER_RE = re.compile(rf"\b(?:pas|plus)\s+(?:de\s+)?({_NUM})")
_ANY_NUMBER_RE = re.compile(_NUM)


def _to_float(raw: str) -> Optional[float]:
    cleaned = re.sub(r"\s+", "", raw).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_old_new_values(text: Any) -> Optional[Tuple[float, float]]:
    """(ancienne, nouvelle) pour un rejet de valeur suivi d'une autre (« c'est pas 300 c'est 250 », « pas 10, 15 », « 500 kg pas 300 »).

    L'ANCIENNE est le nombre directement nié (« pas X ») ; la NOUVELLE est l'autre nombre. « non 250 kg » n'a pas d'ancienne valeur : `None`."""
    raw = str(text or "").lower()
    negated = _NEGATED_NUMBER_RE.search(raw)
    if not negated:
        return None
    old = _to_float(negated.group(1))
    if old is None:
        return None
    rest = raw[: negated.start()] + " " + raw[negated.end():]
    news = [v for v in (_to_float(m.group(0)) for m in _ANY_NUMBER_RE.finditer(rest)) if v is not None]
    if len(news) != 1 or _same(news[0], old):
        return None
    return old, news[0]


@dataclass(frozen=True)
class FieldResolution:
    field: Optional[str]
    ambiguous: bool = False
    candidates: Tuple[str, ...] = ()


def resolve_edit_field_from_state(
    *,
    old_value: Optional[float],
    old_cue: ValueCue = ValueCue.BARE,
    field_hint: Optional[str] = None,
    facts: Mapping[str, Any],
) -> FieldResolution:
    """Champ visé par une correction qui NOMME l'ancienne valeur, d'après l'état métier courant.

    `facts` = {champ: valeur courante} (ex. {"quantity": 300, "price": 250}). Un seul champ portant cette valeur (et compatible avec le signal
    monétaire/mesure de l'ancien nombre) -> ce champ. Plusieurs -> ambigu (on clarifie, on ne choisit pas). Aucun -> `None` (le modèle décide)."""
    if old_value is None:
        return FieldResolution(None)
    matches: List[str] = [name for name, current in facts.items() if current is not None and _same(current, old_value)]
    if old_cue is ValueCue.MONEY:
        matches = [m for m in matches if m in _MONEY_FIELDS]
    elif old_cue is ValueCue.MEASURE:
        matches = [m for m in matches if m in _MEASURE_FIELDS]
    if field_hint and len(matches) > 1 and field_hint in matches:
        matches = [field_hint]
    if len(matches) == 1:
        return FieldResolution(matches[0])
    if len(matches) > 1:
        return FieldResolution(None, ambiguous=True, candidates=tuple(matches))
    return FieldResolution(None)


@dataclass(frozen=True)
class NormalizedEntities:
    entities: Dict[str, Any]
    clarify: Optional[str] = None
    events: Tuple[str, ...] = ()


_NUMERIC_ENTITY_FIELDS = (QUANTITY, PRICE)


def normalize_edit_entities(
    entities: Mapping[str, Any],
    *,
    text: Any,
    facts: Mapping[str, Any],
    named_fields: Collection[str] = (),
) -> NormalizedEntities:
    """Entités de correction vendeur (quantity / price) rendues COHÉRENTES avec le texte et l'état courant.

    1. « pas OLD, NEW » : OLD désigne le champ dans `facts` ; seule NEW est appliquée à ce champ (les autres nombres du modèle sont écartés).
    2. Sinon chaque nombre est validé (`validate_structured_edit`) ; une incohérence réparable est reclassée, sinon on clarifie.
    3. Un nombre NU, sans signal monétaire/mesure, sans champ nommé dans le texte (`named_fields`), alors que l'état porte PLUSIEURS champs numériques
       (« non 280 » face à quantité 300 et prix 250) : on ne choisit pas -> CLARIFY.
    Les clés non numériques (produit, is_correction…) traversent inchangées."""
    out: Dict[str, Any] = dict(entities)
    events: List[str] = []
    old_new = extract_old_new_values(text)
    if old_new is not None:
        old, new = old_new
        old_cue, _ = cue_for_value(text, old)
        resolution = resolve_edit_field_from_state(old_value=old, old_cue=old_cue, field_hint=None, facts=facts)
        if resolution.ambiguous:
            events.append("business_edit_safe_clarification | reason=ambiguous_old_value")
            return NormalizedEntities(dict(entities), clarify=_clarify_question(QUANTITY), events=tuple(events))
        if resolution.field is not None:
            for key in _NUMERIC_ENTITY_FIELDS:
                out.pop(key, None)
            out.pop("unit", None)
            out.pop("price_unit", None)
            out[resolution.field] = new
            check = validate_structured_edit(field=resolution.field, value=new, unit=None, text=text)
            if check.verdict is EditVerdict.RECLASSIFIED and check.field and check.field != resolution.field:
                # le texte contredit la lecture « état courant » : ne rien deviner
                events.append("business_edit_safe_clarification | reason=old_value_vs_text_cue")
                return NormalizedEntities(dict(entities), clarify=_clarify_question(resolution.field), events=tuple(events))
            events.append(f"business_edit_validation | old_value_resolved={resolution.field}")
            return NormalizedEntities(out, events=tuple(events))

    numeric_in_state = [name for name in _NUMERIC_ENTITY_FIELDS if facts.get(name) is not None]
    proposed = [key for key in _NUMERIC_ENTITY_FIELDS if out.get(key) is not None]
    if len(proposed) == 1 and len(numeric_in_state) > 1 and not set(named_fields) & set(_NUMERIC_ENTITY_FIELDS):
        try:
            only = float(out[proposed[0]])
        except (TypeError, ValueError):
            only = None
        if only is not None and cue_for_value(text, only)[0] is ValueCue.BARE and not out.get("unit") and not out.get("price_unit"):
            events.append("business_edit_safe_clarification | reason=bare_number_two_candidate_fields")
            return NormalizedEntities(dict(entities), clarify=_clarify_question(proposed[0]), events=tuple(events))

    for key in _NUMERIC_ENTITY_FIELDS:
        value = out.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        check = validate_structured_edit(field=key, value=number, unit=out.get("unit") if key == QUANTITY else out.get("price_unit"), text=text)
        if check.verdict is EditVerdict.CLARIFY:
            events.append(f"business_edit_safe_clarification | field={key} | reason={check.reason}")
            return NormalizedEntities(dict(entities), clarify=check.question or _clarify_question(key), events=tuple(events))
        if check.verdict is EditVerdict.RECLASSIFIED and check.field and check.field != key:
            if out.get(check.field) is not None and not _same(out[check.field], number):
                events.append(f"business_edit_safe_clarification | field={key} | reason=conflicting_cues")
                return NormalizedEntities(dict(entities), clarify=_clarify_question(key), events=tuple(events))
            out.pop(key, None)
            if key == QUANTITY:
                out.pop("unit", None)
            out[check.field] = number
            if check.field == QUANTITY and check.unit:
                out["unit"] = check.unit
            events.append(f"business_edit_validation | reclassified={key}->{check.field}")
    return NormalizedEntities(out, events=tuple(events))


__all__ = [
    "EditCheck",
    "EditVerdict",
    "FieldResolution",
    "NormalizedEntities",
    "ValueCue",
    "cue_for_value",
    "extract_old_new_values",
    "message_carries_value",
    "normalize_edit_entities",
    "resolve_edit_field_from_state",
    "validate_structured_edit",
]
