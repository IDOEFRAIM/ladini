"""Registre canonique des drafts transactionnels versionnés — UNE seule déclaration par
draft, dérivée par tout ce qui a besoin de connaître leur lifecycle.

Avant ce module, chaque draft ajouté au fil des chantiers (PROCUREMENT, PREORDER, SALES,
RECURRING) devait être déclaré INDÉPENDAMMENT à 3 endroits pour survivre correctement :
  1. `core/state_profile.py` (DURABLE — sinon le shrink du checkpointer peut le supprimer
     silencieusement sous pression de taille) ;
  2. `interpreter/goal_planner.py::_purge_transaction_state` (reset explicite sur un VRAI
     changement de goal — sinon un draft finalisé/périmé d'un ancien goal reste posé dans
     l'état et est réutilisé sans contrôle par `confirmation_gate`) ;
  3. `services/database/<name>_draft_store.py` + `marketplace.<name>_drafts` (persistance
     PostgreSQL — voir `tests/architecture/test_draft_tables_have_live_stores.py`).

`RECURRING_NEED_DRAFT` a manqué (1) et (2) pendant plusieurs mois (audit
`docs/CONVERSATIONAL_ENGINE_HARDENING_AUDIT_2026-09-24.md`, risque P1) — le draft n'était
DURABLE nulle part explicitement et pouvait rester posé après un changement de goal. Ce
registre rend cette classe d'oubli impossible à réintroduire : `state_profile.py` et
`goal_planner.py` DÉRIVENT leur comportement d'ici, et
`tests/architecture/test_draft_registry_completeness.py` vérifie que les 3 points ci-dessus
restent synchronisés avec CE registre (et lui avec les tables PostgreSQL réellement
migrées) — un futur 5ᵉ draft qui oublierait une déclaration ici échoue en CI, pas en prod.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class DraftFieldSpec:
    """Un draft transactionnel versionné (`replace_value`, jamais `merge_dict` — voir
    `core/state.py`)."""

    #: Clé du canal `MarketAgentState` (le module `domain/<state_key>.py` porte la même
    #: racine de nom, ex. `procurement_draft` -> `domain/procurement_draft.py`).
    state_key: str
    #: Goal métier qui possède ce draft — un SEUL goal par draft (voir `confirmation_gate.py`,
    #: qui bascule vers le cycle de vie canonique UNIQUEMENT pour ces goals).
    owning_goal: str


DRAFT_REGISTRY: Tuple[DraftFieldSpec, ...] = (
    DraftFieldSpec(state_key="procurement_draft", owning_goal="PROCUREMENT_CREATE_REQUEST"),
    DraftFieldSpec(state_key="preorder_draft", owning_goal="BUYER_PREORDER_CONFIRM"),
    DraftFieldSpec(state_key="sales_publish_draft", owning_goal="SALES_PUBLISH_PRODUCT"),
    DraftFieldSpec(state_key="recurring_need_draft", owning_goal="CREATE_RECURRING_NEED"),
)

DRAFT_STATE_KEYS: Tuple[str, ...] = tuple(spec.state_key for spec in DRAFT_REGISTRY)


def draft_reset_patch() -> dict:
    """Patch qui efface TOUS les drafts déclarés — utilisé par
    `goal_planner._purge_transaction_state` sur un vrai changement de goal. Un draft
    n'appartenant pas au NOUVEAU goal n'a aucune raison de rester posé (voir la docstring
    de module pour l'incident réel que cet effacement corrige)."""
    return {key: None for key in DRAFT_STATE_KEYS}


__all__ = ["DraftFieldSpec", "DRAFT_REGISTRY", "DRAFT_STATE_KEYS", "draft_reset_patch"]
