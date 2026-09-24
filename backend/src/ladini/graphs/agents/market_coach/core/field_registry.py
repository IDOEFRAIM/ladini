"""Registre canonique des champs `ENTER_FIELD` (Phase 2 hardening, commit 7).

## Le problème que ce registre ferme

`PendingInteraction(kind=ENTER_FIELD, field=<nom>)` peut être créé par n'importe quel flow
avec n'importe quel `field_name` (voir tous les sites d'appel de `set_pending_interaction`/
`replace_pending_interaction`). Rien ne garantissait qu'un `field` ainsi créé avait un
CONSOMMATEUR réel au tour suivant — incident réel (H2, audit 2026-09-24) : `ambiguous_quantity`
et `correction_scope` (des clarifications STRUCTURÉES, avec `target`) étaient interceptées par
le RECOVER générique de `cognitive_guard` sur `event=UNKNOWN` avant même d'atteindre leur flow
propriétaire — créées, mais rendues silencieusement inconsommables par une couche en amont qui
ignorait leur existence.

## Deux registres existants, une seule question chacun

Ce module ne DUPLIQUE ni ne remplace :
  - `core/slots.py::SLOT_REGISTRY`/`_EXPECTED_INPUT_MAP` — LA source de vérité pour un champ
    SCALAIRE générique (répondu via le micro-prompt ACTIVE_SLOT, fusionné dans
    `transaction_payload` par `nodes/memory.py`). Ce registre-ci n'a RIEN à dire sur eux —
    voir `is_known_field()`, qui les consulte directement plutôt que de les recopier.
  - `core/pending_interaction.py::SUBFLOW_OWNED_KINDS` — quels **kinds** entiers (pas des
    champs) appartiennent à un sous-flux dédié (CONFIRM_ACTION, PROVIDE_LOCATION, VERIFY_OTP).

Ce module répond à la question RESTANTE, une fois qu'un champ n'est NI un slot scalaire NI un
kind entier sous-flux : "ce `field` ENTER_FIELD précis a-t-il un consommateur, et de quelle
nature ?" — la question posée par l'incident H2.

## Les trois catégories

  - `SLOT` : redondant avec `core/slots.py` (documenté ici pour l'INVENTAIRE, jamais pour
    redécider quoi que ce soit — la source de vérité reste `core/slots.py`).
  - `STRUCTURED` : porte un `PendingInteraction.target` non vide et un résolveur DÉDIÉ dans le
    flow propriétaire, capable de traiter une réponse même quand le classifieur générique
    renvoie `UNKNOWN` (voir `nodes/cognitive.py::structured_field_owns_resolution` — le point
    de consultation qui ferme H2 pour toute LA CLASSE, pas un cas spécial "ambiguous_quantity").
  - `MINI_FLOW` : routé par son propre nœud/branche (ex: `order_id`, `update_field`), sans
    `target` structuré ni éligibilité ACTIVE_SLOT. Dette historique documentée pour rendre
    l'inventaire exhaustif — aucune migration forcée ici (mandat : pas de big-bang).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Tuple

from ladini.graphs.agents.market_coach.core.slots import (
    SLOT_REGISTRY,
    expected_input_for_field,
)


class FieldContractKind(str, Enum):
    SLOT = "SLOT"
    STRUCTURED = "STRUCTURED"
    MINI_FLOW = "MINI_FLOW"


@dataclass(frozen=True)
class FieldContract:
    field: str
    kind: FieldContractKind
    #: Pointeur documentaire (module/fonction propriétaire) — jamais importé/exécuté depuis
    #: ici, seulement pour l'audit humain et les messages d'erreur des tests d'architecture.
    owner: str


#: Champs STRUCTURED/MINI_FLOW connus — PAS les champs SLOT (déjà couverts par
#: `core/slots.py`, voir `is_known_field()`). Toute nouvelle clarification structurée
#: (`target` non vide) ou tout nouveau mini-flow dédié s'ajoute ICI, jamais en la laissant
#: implicite dans le flow qui la crée.
FIELD_REGISTRY: Tuple[FieldContract, ...] = (
    FieldContract(
        "ambiguous_quantity",
        FieldContractKind.STRUCTURED,
        "flows/buyer/recurring_need.py::_resolve_ambiguous_group_reply",
    ),
    FieldContract(
        "correction_scope",
        FieldContractKind.STRUCTURED,
        "flows/buyer/recurring_need.py::_resolve_correction_scope_reply",
    ),
    FieldContract("order_id", FieldContractKind.MINI_FLOW, "flows/buyer/order_tracking.py"),
    FieldContract("cancellation_reason", FieldContractKind.MINI_FLOW, "flows/buyer/order_tracking.py"),
    FieldContract(
        "update_field",
        FieldContractKind.MINI_FLOW,
        "flows/buyer/order_tracking.py, flows/producer/flow.py",
    ),
    FieldContract("received_quantity", FieldContractKind.MINI_FLOW, "flows/buyer/order_tracking.py"),
    FieldContract("reception_detail", FieldContractKind.MINI_FLOW, "flows/buyer/order_tracking.py"),
)

STRUCTURED_FIELDS: FrozenSet[str] = frozenset(
    c.field for c in FIELD_REGISTRY if c.kind is FieldContractKind.STRUCTURED
)
MINI_FLOW_FIELDS: FrozenSet[str] = frozenset(
    c.field for c in FIELD_REGISTRY if c.kind is FieldContractKind.MINI_FLOW
)

_SLOT_CANONICALS: FrozenSet[str] = frozenset(s.canonical for s in SLOT_REGISTRY)


def is_known_slot_field(field: str) -> bool:
    """Vrai si `field` est un slot scalaire générique — membre de
    `core/slots.py::SLOT_REGISTRY` OU de `_EXPECTED_INPUT_MAP` (ex: `recurrence_type`,
    `weekly_days`, qui n'ont pas de `SlotDefinition` propre mais sont bien résolus par
    `expected_input_for_field`)."""
    return field in _SLOT_CANONICALS or expected_input_for_field(field) != "NONE"


def is_known_field(field: str) -> bool:
    """Vrai si CE `field` ENTER_FIELD a un consommateur connu — slot générique, clarification
    structurée, ou mini-flow dédié. C'est l'invariant que le test d'architecture fait respecter
    à CHAQUE site de création : aucun champ hors de cette union."""
    return is_known_slot_field(field) or field in STRUCTURED_FIELDS or field in MINI_FLOW_FIELDS


__all__ = [
    "FieldContractKind",
    "FieldContract",
    "FIELD_REGISTRY",
    "STRUCTURED_FIELDS",
    "MINI_FLOW_FIELDS",
    "is_known_slot_field",
    "is_known_field",
]
