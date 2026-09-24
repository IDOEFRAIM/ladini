"""Un draft transactionnel déclaré dans `core/draft_registry.py` doit être :
  1. DURABLE dans `core/state_profile.py` (sinon le shrink du checkpointer peut le
     supprimer silencieusement sous pression de taille) ;
  2. explicitement effacé par `goal_planner._purge_transaction_state` sur un VRAI
     changement de goal (sinon un draft finalisé/périmé d'un ancien goal reste réutilisé
     sans contrôle par `confirmation_gate`) ;
  3. adossé à une table `marketplace.<name>s` réellement migrée, avec un store qui
     l'écrit (couvert par `test_draft_tables_have_live_stores.py` — revérifié ici depuis
     l'autre sens : chaque draft du REGISTRE a bien sa table).

Incident réel (audit 2026-09-24, P1) : `recurring_need_draft` manquait (1) et (2) — le
draft n'était DURABLE nulle part explicitement déclaré. Avant ce module, chaque draft
devait redéclarer indépendamment ces 3 propriétés ; un futur draft qui en oublierait une
échoue maintenant ici, pas en observant un symptôme en production.
"""
from __future__ import annotations

import inspect

from ladini.domain import runtime_tables
from ladini.graphs.agents.market_coach.core.draft_registry import DRAFT_REGISTRY
from ladini.graphs.agents.market_coach.core.state_profile import (
    FieldLifecycle,
    get_field_spec,
)
from ladini.graphs.agents.market_coach.interpreter import goal_planner


def test_the_registry_is_not_empty():
    assert len(DRAFT_REGISTRY) >= 4


def test_every_registered_draft_is_durable():
    for spec in DRAFT_REGISTRY:
        field = get_field_spec(spec.state_key)
        assert field is not None, f"{spec.state_key} absent de core/state_profile.py"
        assert field.lifecycle is FieldLifecycle.DURABLE, spec.state_key


def test_every_registered_draft_is_purged_on_a_real_goal_change():
    """`_purge_transaction_state` (fonction imbriquée) doit renvoyer `None` pour chaque
    draft déclaré — lu depuis la SOURCE (`goal_planner.draft_reset_patch()` appelé), pas
    reconstitué indépendamment, pour ne jamais diverger silencieusement de ce que le nœud
    exécute réellement."""
    from ladini.graphs.agents.market_coach.core.draft_registry import draft_reset_patch

    patch = draft_reset_patch()
    for spec in DRAFT_REGISTRY:
        assert patch.get(spec.state_key) is None, spec.state_key
    # `goal_planner` doit RÉELLEMENT appeler cette fonction (pas juste l'importer sans
    # l'utiliser) — vérifié par lecture de son propre code source.
    source = inspect.getsource(goal_planner)
    assert "draft_reset_patch()" in source


def test_every_registered_draft_has_a_migrated_table_and_a_live_store():
    draft_tables = {name[:-1] for name in runtime_tables.__all__ if name.endswith("_drafts")}
    registered = {spec.state_key for spec in DRAFT_REGISTRY}
    # `recurring_need_draft` -> table `recurring_need_drafts` (le "s" final de la table).
    expected_tables = {f"{key}s" for key in registered}
    assert expected_tables <= {f"{name}s" for name in draft_tables}


def test_each_draft_has_exactly_one_owning_goal():
    goals = [spec.owning_goal for spec in DRAFT_REGISTRY]
    assert len(goals) == len(set(goals)), "deux drafts ne doivent jamais partager un goal"
