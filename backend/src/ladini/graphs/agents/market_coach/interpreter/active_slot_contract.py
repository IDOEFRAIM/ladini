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

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, model_validator

from ladini.domain.quantity_unit import text_states_a_quantity
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
    #: Produit déjà suivi par la transaction (`transaction_payload.product`) —
    #: sert UNIQUEMENT au garde `buyer_slot_answer_conflict` ci-dessous.
    current_product: Optional[str] = None
    #: Texte du tour — sert au garde de provenance de `quantity`.
    message_text: Optional[str] = None


def build_active_slot_context(state: Dict[str, Any]) -> ActiveSlotContext:
    """Construit `ActiveSlotContext` depuis les SEULES sources canoniques
    déjà existantes — `get_pending_interaction`/`to_tunnel_category`/
    `resolve_current_goal` — jamais une resynthèse indépendante."""
    pending = get_pending_interaction(state)
    payload = state.get("transaction_payload")
    product = payload.get("product") if isinstance(payload, dict) else None
    return ActiveSlotContext(
        category=to_tunnel_category(pending),
        field_name=pending.field,
        goal=resolve_current_goal(state),
        current_product=str(product).strip() if product else None,
        message_text=str(
            state.get("normalized_text") or state.get("user_query") or ""
        ),
    )


#: Slots « valeur numérique/unité » : une réponse légitime y porte au moins une
#: valeur, et ne peut pas désigner un AUTRE produit que celui déjà suivi.
_VALUE_SLOT_CATEGORIES = frozenset({"QUANTITY", "UNIT", "PRICE"})


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _product_key(value: Any) -> str:
    """Clé de comparaison tolérante (casse, accents, article, pluriel) —
    « poulet »/« Poulets »/« des poulets » désignent le même produit."""
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).strip()
    text = re.sub(r"^(des|du|de la|de l'|de|le|la|les|l'|un|une)\s+", "", text)
    return re.sub(r"[sx]$", "", text)


def buyer_slot_answer_conflict(
    decision: ActiveSlotDecision, context: ActiveSlotContext
) -> Optional[str]:
    """Raison pour laquelle un ANSWER/UPDATE du micro-prompt ne peut PAS être
    une réponse au slot acheteur en attente, sinon `None`.

    Incident réel (2026-10-01) : « je veux acheter du lait » pendant
    `ENTER_QUANTITY` d'un achat de poulets était classé ANSWER ; la fast-path
    acheteur (`FastPathPolicy.for_buyer`) sautait alors `cognitive_guard`, le
    produit restait « poulets » et 1 UNITE était ajoutée au panier. Le garde
    de produit du chemin legacy (`routing.py`, `_is_different_product`) ne
    couvre pas ce micro-prompt, qui retourne avant. Deux preuves structurelles,
    jamais lexicales :

    - ``product_switch`` : un produit DIFFÉRENT du produit suivi est extrait —
      une vraie réponse de quantité/unité/prix ne change jamais de produit ;
    - ``no_slot_value`` : aucune valeur extraite — ce n'est pas une réponse,
      rien ne peut remplir le slot (et en laisser passer un ANSWER vide revient
      à valider une quantité par défaut).

    Limité aux buts acheteur et aux slots valeur (QUANTITY/UNIT/PRICE)."""
    from ladini.graphs.agents.market_coach.core.goals import ALL_BUYER_TUNNEL_GOALS

    if decision.disposition not in (
        ActiveSlotDisposition.ANSWER,
        ActiveSlotDisposition.UPDATE,
    ):
        return None
    if str(context.goal or "").upper() not in ALL_BUYER_TUNNEL_GOALS:
        return None
    if context.category not in _VALUE_SLOT_CATEGORIES:
        return None
    entities = decision.extracted_entities
    if not entities:
        return "no_slot_value"
    # Provenance : une quantité sans AUCUN nombre dans le message est inventée.
    if (
        entities.get("quantity") is not None
        and context.message_text is not None
        and not text_states_a_quantity(
            context.message_text, _as_float(entities.get("quantity"))
        )
    ):
        return "no_slot_value"
    said_product = entities.get("product")
    if (
        said_product
        and context.current_product
        and _product_key(said_product) != _product_key(context.current_product)
    ):
        return "product_switch"
    return None


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
    "buyer_slot_answer_conflict",
    "adapt_active_slot_to_canonical",
]
