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

from ladini.graphs.agents.market_coach.domain.context_answers import ContextQuestion
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    SelectionContext,
    validate_action,
)
from ladini.graphs.agents.market_coach.domain.selection_reference import (
    ReferenceType,
    Resolution,
    SelectionReference,
    Status,
    VisibleOption,
    option_from_vendor,
    options_from_tiers,
    resolve_reference,
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
    #: QUESTION de contexte (« il livre ? », « c'est combien au total ? », « lequel est moins cher ? ») : le modèle la COMPREND (sujet,
    #: cible) mais ne la répond jamais — la réponse est calculée par le domaine (`domain/context_answers.py`), sans rien muter.
    QUESTION = "QUESTION"


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
    #: Référence NATURELLE structurée (« le moins cher », « Gilbert », « celui à 500 »…) — résolue en Python contre les options
    #: AFFICHÉES ; alternative à `selection_index`/`selected_value`, jamais un identifiant.
    reference: Optional[SelectionReference] = None
    package_count: Optional[float] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    #: QUESTION uniquement.
    question: Optional[ContextQuestion] = None
    confidence: float = 0.0

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _tolerant_reader(cls, data: Any) -> Any:
        """Lecteur TOLÉRANT : un vrai modèle remplit souvent `action`/`selection_index` par réflexe alors que la disposition n'est pas ACTION
        (« DEVIATION » avec l'action attendue). Ce n'est pas une réponse invalide, c'est du bruit : on l'ignore (sinon repair puis UNKNOWN —
        la phrase n'était plus comprise). Les champs d'action ne sont conservés QUE pour ACTION ; `question` que pour QUESTION."""
        if not isinstance(data, dict):
            return data
        disposition = str(data.get("disposition") or "").upper()
        cleaned = dict(data)
        if disposition != "ACTION":
            for key in ("action", "selection_index", "selected_value", "reference", "package_count", "quantity", "unit"):
                cleaned[key] = None
        if disposition != "QUESTION":
            cleaned["question"] = None
        # `reference` ET `selection_index`/`selected_value` ensemble (vu avec le vrai modèle : « le quatrième » -> ORDINAL 4 + index 4) :
        # la référence STRUCTURÉE fait foi (résolue par Python contre le menu), l'index redondant du modèle est ignoré.
        if disposition == "ACTION" and cleaned.get("reference") is not None:
            cleaned["selection_index"] = None
            cleaned["selected_value"] = None
        return cleaned

    @model_validator(mode="after")
    def _check_one_semantic_action(self) -> "StructuredActionDecision":
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence doit être comprise entre 0.0 et 1.0")

        if self.disposition == StructuredActionDisposition.QUESTION:
            if self.question is None:
                raise ValueError("QUESTION requiert question")
            return self

        if self.disposition != StructuredActionDisposition.ACTION:
            if any(
                v is not None
                for v in (
                    self.action,
                    self.selection_index,
                    self.selected_value,
                    self.reference,
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
            designations = sum((self.selection_index is not None, bool(self.selected_value), self.reference is not None))
            if designations != 1:  # aucune, ou plusieurs
                raise ValueError(
                    f"{self.action.value} requiert exactement un de "
                    "selection_index/selected_value/reference"
                )
            # Seule une RÉFÉRENCE structurée peut s'accompagner d'une quantité/d'un nombre de paquets dits dans la même phrase
            # (« je prends Gilbert, 10 litres ») : deux FAITS, le domaine valide chacun. Un `selection_index` avec une
            # quantité reste interdit (incident « le premier, c'est-à-dire 5 L » : 5 lu comme un index).
            if self.reference is None and (self.package_count is not None or self.quantity is not None or self.unit is not None):
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
    #: Producteurs que l'utilisateur peut encore choisir (changement d'avis) quand l'étape courante n'est pas le choix du producteur.
    producer_options: List[StructuredActionOption] = field(default_factory=list)
    #: Réponse attendue à une question qu'on vient de poser (`PRICE_CEILING`).
    awaiting: Optional[str] = None


def build_structured_action_prompt_context(
    context: SelectionContext,
) -> Optional[StructuredActionPromptContext]:
    """`None` si aucune action n'est attendue — l'appelant ne doit alors pas
    utiliser ce micro-prompt du tout."""
    if context.expected_action is None:
        return None

    prompt = _build_prompt_context(context)
    if prompt is not None and context.expected_action != ActionType.SELECT_PRODUCER and context.producer_options:
        prompt = StructuredActionPromptContext(
            expected_action=prompt.expected_action,
            options=prompt.options,
            active_tier_label=prompt.active_tier_label,
            producer_options=[StructuredActionOption(index=p.display_index, label=p.label) for p in context.producer_options],
        )
    return prompt


def _build_prompt_context(context: SelectionContext) -> Optional[StructuredActionPromptContext]:
    if context.expected_action == ActionType.SELECT_PRODUCER:
        # `index` == index AFFICHÉ dans le menu (`display_index`), pas la
        # position dans une liste filtrée.
        options = [
            StructuredActionOption(index=p.display_index, label=p.label)
            for p in context.producer_options
        ]
        return StructuredActionPromptContext(
            expected_action=context.expected_action, options=options, awaiting=context.awaiting
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


def visible_options_for(action: ActionType, context: SelectionContext) -> List[VisibleOption]:
    """Options VISIBLES (faits lisibles par l'utilisateur) du snapshot courant — jamais une nouvelle recherche."""
    if action == ActionType.SELECT_PRODUCER:
        return [
            option_from_vendor({**p.facts, "offer_id": p.offer_id, "display_index": p.display_index}, p.display_index)
            for p in context.producer_options
        ]
    tier_options: List[VisibleOption] = options_from_tiers([t.facts for t in context.tier_options])
    return tier_options


def resolve_reference_decision(decision: StructuredActionDecision, context: SelectionContext) -> Optional[Resolution]:
    """Résout `decision.reference` en Python. `None` si la décision ne porte pas de référence."""
    if decision.reference is None or decision.action is None:
        return None
    if context.created_at is not None and decision.action == ActionType.SELECT_PRODUCER:
        import time

        from ladini.graphs.agents.market_coach.domain.menu_facts import is_stale

        if is_stale({"created_at": context.created_at}, time.time()):
            # Menu PÉRIMÉ : une référence naturelle (« le quatrième ») ne se résout jamais contre une liste qui a pu changer.
            return Resolution(
                Status.NOT_FOUND, reason="stale_menu",
                message="Cette liste date un peu et a pu changer — redis-moi ce que tu cherches et je te la réaffiche à jour.",
            )
    hidden = context.hidden_count if decision.action == ActionType.SELECT_PRODUCER else 0
    return resolve_reference(
        decision.reference, visible_options_for(decision.action, context), displayed_count=None, hidden_count=hidden
    )


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
        idx = _resolve_selection(decision, prompt_context.producer_options or prompt_context.options)
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
        if not 1 <= idx <= len(domain_context.tier_options):
            # Le modèle a choisi un palier alors que le contexte n'en expose pas (ou moins) : jamais un IndexError
            # qui ferait tomber le tour — la décision est simplement non résolue (UNKNOWN côté appelant).
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
    "ReferenceType",
    "Status",
    "resolve_reference_decision",
    "visible_options_for",
    "StructuredActionDisposition",
    "StructuredActionDecision",
    "StructuredActionOption",
    "StructuredActionPromptContext",
    "build_structured_action_prompt_context",
    "resolve_decision_to_raw_action",
    "resolve_and_validate",
    "adapt_structured_action_to_canonical",
]
