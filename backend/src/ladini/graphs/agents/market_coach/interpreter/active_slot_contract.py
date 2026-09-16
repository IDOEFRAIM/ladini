"""Contrat + contexte du micro-prompt ACTIVE_SLOT (chantier "State Router +
micro-prompts", Phase C, 2026-09-12).

## `ActiveSlotContext` — projection ÉPHÉMÈRE, jamais un second état durable

```
ACTIVE_SLOT = projection de PendingInteraction
```

et non un nouveau canal. `ActiveSlotContext` est construit à CHAQUE tour
depuis `get_pending_interaction(state)`/`to_tunnel_category(...)`/
`resolve_current_goal(state)` — jamais persisté dans `state`, jamais un
nouveau champ `MarketAgentState.active_slot` (voir
`tests/interpreter/test_state_router.py::TestNoDurableActiveSlotStateWasIntroduced`
pour l'invariant testé). Ce système a déjà connu des bugs réels dus à
plusieurs signaux concurrents représentant « qu'attend-on de
l'utilisateur ? » (voir la docstring de module de `core/pending_interaction.py`)
— cette phase ne recrée pas ce problème.

## `ActiveSlotDecision` — sortie brute du LLM, avant adaptation

Volontairement PROCHE du contrat suggéré par la spec Phase C §13, adapté
aux conventions déjà en place dans ce module (`selection_contract.py`:
`frozen=True`, `model_validator(mode="after")`).

`extracted_entities` reste un dict LIBRE (pas de schéma Pydantic rigide par
champ) — les champs possibles varient selon la catégorie de slot
(PRODUCT/QUANTITY/UNIT/PRICE/DATE/FARM_NAME/LOCATION/MOVEMENT_TYPE), et
l'extraction multi-champs explicite doit rester possible (spec §9 : « le
slot actif sert d'ancre conversationnelle, pas de filtre destructeur » —
incident réel déjà documenté : une réponse "tomates, 500 kg à 200 FCFA/kg"
amputée pour ne garder que le produit). Validation des BORNES/cohérence
métier faite ailleurs (downstream, `memory_update`/domaine) — ce contrat ne
valide que la FORME (dict de clés/valeurs JSON-compatibles), jamais le sens
métier d'un champ.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, model_validator

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal


class ActiveSlotDisposition(str, Enum):
    #: L'utilisateur répond au slot attendu (et éventuellement à d'autres
    #: champs explicitement fournis dans le même message).
    ANSWER = "ANSWER"
    #: L'utilisateur corrige un champ déjà connu (le slot attendu ou un
    #: AUTRE, ex: "le prix est plutôt 36500" pendant qu'on attend une date).
    UPDATE = "UPDATE"
    #: Rejet/abandon explicite ("annule", "laisse tomber").
    REJECT = "REJECT"
    #: Le message ne concerne PAS le tunnel courant — une autre tâche.
    #: Interne à ce micro-interpréteur (spec §12) : n'est JAMAIS renvoyé
    #: tel quel comme `InterpreterResult.event` — l'appelant
    #: (`interpreter/routing.py`) retombe sur le classifier NEW_TASK
    #: existant, qui produit le contrat canonique final.
    DEVIATION = "DEVIATION"
    #: Trop ambigu pour trancher — ni une réponse fiable, ni une déviation
    #: clairement identifiable.
    UNKNOWN = "UNKNOWN"


class ActiveSlotDecision(BaseModel):
    """Sortie brute (déjà JSON-décodée) du micro-prompt ACTIVE_SLOT, avant
    adaptation vers le contrat canonique — voir `adapt_active_slot_to_canonical`."""

    disposition: ActiveSlotDisposition
    extracted_entities: Dict[str, Any] = {}
    confidence: float = 0.0

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check_field_consistency(self) -> "ActiveSlotDecision":
        if (
            self.disposition in (ActiveSlotDisposition.DEVIATION, ActiveSlotDisposition.UNKNOWN)
            and self.extracted_entities
        ):
            raise ValueError(
                f"{self.disposition.value} ne doit porter aucune entité extraite "
                "(rien n'est fiable à en tirer)"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence doit être comprise entre 0.0 et 1.0")
        return self


@dataclass(frozen=True)
class ActiveSlotContext:
    """Contexte MINIMAL du tour courant — jamais persisté (spec §8)."""

    category: str
    field_name: Optional[str]
    goal: Optional[str]


def build_active_slot_context(state: Dict[str, Any]) -> ActiveSlotContext:
    """Construit `ActiveSlotContext` depuis les SEULES sources canoniques
    déjà existantes — `get_pending_interaction`/`to_tunnel_category`/
    `resolve_current_goal` — jamais une resynthèse indépendante."""
    pending = get_pending_interaction(state)
    return ActiveSlotContext(
        category=to_tunnel_category(pending),
        field_name=pending.field,
        goal=resolve_current_goal(state),
    )


def adapt_active_slot_to_canonical(
    decision: ActiveSlotDecision, context: ActiveSlotContext
) -> Dict[str, Any]:
    """Traduit vers le contrat `input_interpreter` canonique historique.

    `detected_intent = context.goal` pour ANSWER/UPDATE/REJECT (spec §14 :
    « l'intent n'a pas besoin d'être redécouvert », `current_goal` est déjà
    connu — MÊME convention que le fast-path slot numérique historique,
    `interpreter/routing.py::fast_path_slot_numeric_answer`, qui pose déjà
    `detected_intent=str(locked_goal or "UNKNOWN").upper()` pour un ANSWER).

    N'accepte PAS `ActiveSlotDisposition.DEVIATION` : géré par l'appelant
    (`interpreter/routing.py`), qui retombe sur le classifier NEW_TASK
    existant plutôt que produire un résultat ici (spec §11/§12/§18)."""
    if decision.disposition == ActiveSlotDisposition.UNKNOWN:
        return {
            "interpreted_event": "UNKNOWN",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": decision.confidence,
            "extracted_entities": {},
            "raw_analysis": {"path": "active_slot_micro"},
        }
    if decision.disposition == ActiveSlotDisposition.DEVIATION:
        raise ValueError(
            "DEVIATION n'est pas adaptable ici — l'appelant doit retomber "
            "sur le classifier NEW_TASK (spec §11/§12/§18)."
        )
    # ANSWER / UPDATE / REJECT
    return {
        "interpreted_event": decision.disposition.value,
        "detected_intent": str(context.goal or "UNKNOWN").upper(),
        "interpreter_confidence": decision.confidence,
        "extracted_entities": dict(decision.extracted_entities),
        "raw_analysis": {"path": "active_slot_micro"},
    }


__all__ = [
    "ActiveSlotDisposition",
    "ActiveSlotDecision",
    "ActiveSlotContext",
    "build_active_slot_context",
    "adapt_active_slot_to_canonical",
]
