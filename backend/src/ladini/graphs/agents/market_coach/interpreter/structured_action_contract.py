"""Contrat + résolution du micro-prompt STRUCTURED_ACTION (chantier "State
Router + micro-prompts", Incrément D, 2026-09-12).

## Principe (spec §5/§9/§16) : LLM → sens humain, Python → référence métier

Le tunnel producteur/palier possède déjà UNE seule source de vérité pour le
contexte candidat (`domain/selection_actions.py::build_selection_context`) et
UNE seule porte de validation (`validate_action`) — ce module ne les
duplique JAMAIS, il les entoure :

```
build_selection_context(state)        (existant, inchangé)
        │
        ▼
options HUMAINES seulement (labels, jamais d'ID)  ← CE module
        │
        ▼
LLM : selection_index / selected_value / package_count / quantity / unit
        │
        ▼
résolution Python : index → ID réel (candidate[i-1])  ← CE module
        │
        ▼
validate_action(raw, context)         (existant, inchangé — seule porte)
        │
        ▼
extracted_entities canonique (agent_action/action_*)  ← MÊME FORME que
                                                          `routing.py::
                                                          _selection_action_output`,
                                                          pour que
                                                          `flows/buyer/cart.py`
                                                          reste 100% inchangé.
```

**Différence avec l'ancien chemin LLM** (`interpreter/routing.py`, prompt
unifié + `tier_menu_prompt_block`) : celui-ci exposait les VRAIS
`producer_id`/`pricing_tier_id` DANS le prompt et faisait confiance au LLM
pour les recopier exactement (`parse_raw_action` lit `action_producer_id`/
`action_pricing_tier_id` directement depuis `extracted_entities`). Le
nouveau micro-prompt ne montre JAMAIS d'identifiant technique — seulement
des labels numérotés — et c'est CE module qui résout l'index vers l'ID réel.

## Règle « one semantic action » (spec §20)

Une décision `ACTION` ne doit remplir QUE les champs correspondant à
L'ACTION qu'elle représente — jamais un mélange (ex: `selection_index` ET
`quantity` en même temps). C'est exactement l'incident réel qui a motivé
`domain/selection_actions.py` (voir sa docstring, "LE PREMIER, C'EST À DIRE
5 L") — protégé ici par un `model_validator` Pydantic, pas seulement par
l'obéissance du prompt."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, model_validator

from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    SelectionContext,
    validate_action,
)

# Même mapping que `interpreter/routing.py::_ACTION_EVENT` — dupliqué
# intentionnellement en une seule ligne plutôt qu'importé (éviter tout
# risque d'import circulaire avec routing.py, qui importe CE module) ; les
# deux DOIVENT rester synchronisés — voir le test structurel dédié.
_ACTION_EVENT: Dict[ActionType, str] = {
    ActionType.SELECT_PRODUCER: "SELECTION",
    ActionType.SELECT_PRICING_TIER: "SELECTION",
    ActionType.SET_PACKAGE_COUNT: "ANSWER",
    ActionType.SET_QUANTITY: "ANSWER",
}

#: Actions dont la sémantique de sélection est "un index/une valeur parmi
#: des options" (par opposition à un nombre libre).
_SELECTION_LIKE_ACTIONS = frozenset({ActionType.SELECT_PRODUCER, ActionType.SELECT_PRICING_TIER})


class StructuredActionDisposition(str, Enum):
    #: Le message correspond à l'action structurée attendue (ou, pour
    #: SET_PACKAGE_COUNT, à un changement explicite de palier — spec §13).
    ACTION = "ACTION"
    #: Nouvelle demande métier explicite, sans rapport avec le tunnel.
    DEVIATION = "DEVIATION"
    #: Refus/abandon SANS nouvelle demande.
    REJECT = "REJECT"
    #: Impossible à trancher de façon fiable.
    UNKNOWN = "UNKNOWN"


class StructuredActionDecision(BaseModel):
    """Sortie brute (déjà JSON-décodée) du micro-prompt — humaine
    uniquement, JAMAIS d'identifiant technique (spec §15) : Pydantic ignore
    silencieusement tout champ non déclaré (comportement par défaut, comme
    `selection_contract.py`) — un `producer_id` que le modèle tenterait
    d'ajouter n'atteint donc jamais ce modèle ni le canonique en aval."""

    disposition: StructuredActionDisposition
    action: Optional[ActionType] = None
    selection_index: Optional[int] = None
    selected_value: Optional[str] = None
    package_count: Optional[float] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    confidence: float = 0.0

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check_one_semantic_action(self) -> "StructuredActionDecision":
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence doit être comprise entre 0.0 et 1.0")

        if self.disposition != StructuredActionDisposition.ACTION:
            if any(
                v is not None
                for v in (
                    self.action,
                    self.selection_index,
                    self.selected_value,
                    self.package_count,
                    self.quantity,
                    self.unit,
                )
            ):
                raise ValueError(
                    f"{self.disposition.value} ne doit porter aucun champ "
                    "d'action (rien de fiable à en tirer)"
                )
            return self

        # disposition == ACTION : exactement les champs de l'action déclarée,
        # jamais un mélange (règle "one semantic action", spec §20).
        if self.action is None:
            raise ValueError("ACTION requiert un champ 'action' explicite")

        if self.action in _SELECTION_LIKE_ACTIONS:
            has_index = self.selection_index is not None
            has_value = bool(self.selected_value)
            if has_index == has_value:  # ni l'un ni l'autre, ou les deux
                raise ValueError(
                    f"{self.action.value} requiert exactement un de "
                    "selection_index/selected_value"
                )
            if self.package_count is not None or self.quantity is not None or self.unit is not None:
                raise ValueError(
                    f"{self.action.value} ne doit porter aucun champ "
                    "package_count/quantity/unit"
                )
        elif self.action == ActionType.SET_PACKAGE_COUNT:
            if self.package_count is None:
                raise ValueError("SET_PACKAGE_COUNT requiert package_count")
            if (
                self.selection_index is not None
                or self.selected_value is not None
                or self.quantity is not None
                or self.unit is not None
            ):
                raise ValueError(
                    "SET_PACKAGE_COUNT ne doit porter aucun autre champ "
                    "(selection_index/selected_value/quantity/unit)"
                )
        elif self.action == ActionType.SET_QUANTITY:
            if self.quantity is None:
                raise ValueError("SET_QUANTITY requiert quantity")
            if self.selection_index is not None or self.selected_value is not None or self.package_count is not None:
                raise ValueError(
                    "SET_QUANTITY ne doit porter aucun autre champ "
                    "(selection_index/selected_value/package_count)"
                )
        return self


@dataclass(frozen=True)
class StructuredActionOption:
    """Une option numérotée présentée au LLM — UNIQUEMENT le label humain,
    jamais l'id réel (spec §9/§15)."""

    index: int  # 1-based
    label: str


@dataclass(frozen=True)
class StructuredActionPromptContext:
    """Ce qui est réellement injecté dans le prompt — dérivé de
    `SelectionContext` (existant, inchangé) en RETIRANT tout identifiant
    technique. Jamais persisté (même discipline que `ActiveSlotContext`,
    Phase C)."""

    expected_action: ActionType
    options: List[StructuredActionOption] = field(default_factory=list)
    active_tier_label: Optional[str] = None


def build_structured_action_prompt_context(
    context: SelectionContext,
) -> Optional[StructuredActionPromptContext]:
    """`None` si aucune action n'est attendue — l'appelant ne doit alors pas
    utiliser ce micro-prompt du tout."""
    if context.expected_action is None:
        return None

    if context.expected_action == ActionType.SELECT_PRODUCER:
        # `index` == index AFFICHÉ dans le menu (`display_index`), pas la
        # position dans une liste filtrée.
        options = [
            StructuredActionOption(index=p.display_index, label=p.label)
            for p in context.producer_options
        ]
        return StructuredActionPromptContext(
            expected_action=context.expected_action, options=options
        )

    if context.expected_action == ActionType.SELECT_PRICING_TIER:
        options = [
            StructuredActionOption(index=i, label=t.label)
            for i, t in enumerate(context.tier_options, start=1)
        ]
        return StructuredActionPromptContext(
            expected_action=context.expected_action, options=options
        )

    if context.expected_action == ActionType.SET_PACKAGE_COUNT:
        # Re-sélection de palier explicitement autorisée pendant ce slot
        # (règle métier existante, `validate_action` l'accepte déjà) — les
        # options restent donc exposées, label seul.
        options = [
            StructuredActionOption(index=i, label=t.label)
            for i, t in enumerate(context.tier_options, start=1)
        ]
        active_label = next(
            (t.label for t in context.tier_options if t.tier_id == context.active_tier_id),
            None,
        )
        return StructuredActionPromptContext(
            expected_action=context.expected_action,
            options=options,
            active_tier_label=active_label,
        )

    if context.expected_action == ActionType.SET_QUANTITY:
        return StructuredActionPromptContext(expected_action=context.expected_action)

    return None


def _resolve_selection(
    decision: StructuredActionDecision, options: List[StructuredActionOption]
) -> Optional[int]:
    """Retourne un index 1-based RÉSOLU et BORNÉ, ou `None` si irrésolvable
    — le LLM n'est jamais la source de vérité sur les bornes (même principe
    que `selection_contract.py::index_within_bounds`, Incrément B)."""
    if decision.selection_index is not None:
        if 1 <= decision.selection_index <= len(options):
            return decision.selection_index
        return None
    if decision.selected_value:
        needle = decision.selected_value.strip().lower()
        matches = [o.index for o in options if needle in o.label.lower()]
        if len(matches) == 1:
            return matches[0]
        return None
    return None


def resolve_decision_to_raw_action(
    decision: StructuredActionDecision,
    prompt_context: StructuredActionPromptContext,
    domain_context: SelectionContext,
) -> Optional[Dict[str, Any]]:
    """Traduit une `StructuredActionDecision` (humaine) en `raw` compatible
    avec `domain/selection_actions.py::validate_action` — résolution
    d'INDEX VERS ID RÉEL faite ICI, en Python, jamais par le LLM.

    Retourne `None` si la décision ne peut pas être résolue de façon sûre
    (index hors bornes, `selected_value` ambigu ou sans correspondance) —
    l'appelant doit alors traiter le tour comme `UNKNOWN`, jamais deviner."""
    action = decision.action
    if action is None:
        return None

    if action == ActionType.SELECT_PRODUCER:
        idx = _resolve_selection(decision, prompt_context.options)
        if idx is None:
            return None
        option = next(
            (o for o in domain_context.producer_options if o.display_index == idx), None
        )
        if option is None:
            return None
        return {
            "action": action,
            "offer_id": option.offer_id,
            "producer_id": option.producer_id,
        }

    if action == ActionType.SELECT_PRICING_TIER:
        idx = _resolve_selection(decision, prompt_context.options)
        if idx is None:
            return None
        tier_id = domain_context.tier_options[idx - 1].tier_id
        return {"action": action, "pricing_tier_id": tier_id}

    if action == ActionType.SET_PACKAGE_COUNT:
        if decision.package_count is None or decision.package_count <= 0:
            return None
        return {"action": action, "package_count": decision.package_count}

    if action == ActionType.SET_QUANTITY:
        if decision.quantity is None or decision.quantity <= 0:
            return None
        return {
            "action": action,
            "quantity": decision.quantity,
            "unit": decision.unit,
        }

    return None


def adapt_structured_action_to_canonical(
    raw: Dict[str, Any], locked_goal: Optional[str], path: str
) -> Dict[str, Any]:
    """MÊME forme exacte que `interpreter/routing.py::_selection_action_output`
    (agent_action/action_producer_id/...) — `flows/buyer/cart.py` (via
    `parse_raw_action`) reste 100% inchangé, il ne sait pas d'où vient cette
    action structurée."""
    action: ActionType = raw["action"]
    entities: Dict[str, Any] = {"agent_action": action.value}
    if raw.get("offer_id"):
        entities["action_offer_id"] = raw["offer_id"]
    if raw.get("producer_id"):
        entities["action_producer_id"] = raw["producer_id"]
    if raw.get("pricing_tier_id"):
        entities["action_pricing_tier_id"] = raw["pricing_tier_id"]
    if raw.get("package_count") is not None:
        entities["action_package_count"] = raw["package_count"]
    if raw.get("quantity") is not None:
        entities["action_quantity"] = raw["quantity"]
    if raw.get("unit"):
        entities["action_unit"] = raw["unit"]
    return {
        "interpreted_event": _ACTION_EVENT[action],
        "detected_intent": str(locked_goal or "UNKNOWN").upper(),
        "interpreter_confidence": 0.95,
        "extracted_entities": entities,
        "raw_analysis": {"path": path},
    }


def resolve_and_validate(
    decision: StructuredActionDecision,
    prompt_context: StructuredActionPromptContext,
    domain_context: SelectionContext,
) -> Optional[Dict[str, Any]]:
    """Pipeline complet résolution + validation — `None` si la décision ne
    peut être exécutée en toute sécurité (jamais une supposition)."""
    raw = resolve_decision_to_raw_action(decision, prompt_context, domain_context)
    if raw is None:
        return None
    # Seule porte de validation (spec §16) — RÉUTILISÉE, jamais court-circuitée
    # même si l'ID vient d'être résolu depuis CE MÊME contexte.
    validated = validate_action(raw, domain_context)
    if validated is None:
        return None
    return raw


__all__ = [
    "StructuredActionDisposition",
    "StructuredActionDecision",
    "StructuredActionOption",
    "StructuredActionPromptContext",
    "build_structured_action_prompt_context",
    "resolve_decision_to_raw_action",
    "resolve_and_validate",
    "adapt_structured_action_to_canonical",
]
