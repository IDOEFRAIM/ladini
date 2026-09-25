"""Cycle de vie de `PendingInteraction` (Phase 2 hardening, commit 7, mandat §7).

Voir `core/pending_interaction.py::InteractionStatus` pour pourquoi ces 4 transitions
terminales (RESOLVED/CANCELLED/EXPIRED/REPLACED, "SUPERSEDED" au sens du mandat) sont
enforcées par REMPLACEMENT de l'interaction entière (reducer `replace_value`), jamais par un
`.status` réécrit sur le même objet — ce module teste directement CE mécanisme de remplacement,
au niveau le plus bas (fonctions pures), pour que l'invariant survive même si un futur commit
refactore les flows qui l'utilisent."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    get_pending_interaction,
    replace_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)


def _active_state(**extra) -> dict:
    return {
        "current_goal": "CREATE_RECURRING_NEED",
        **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
        **extra,
    }


class TestConsumedNeverReactivates:
    """RESOLVED : une interaction traitée avec succès ne doit jamais réapparaître ACTIVE."""

    def test_resolve_pending_interaction_clears_it_to_none(self):
        state = _active_state()
        state.update(resolve_pending_interaction())
        assert get_pending_interaction(state).kind == InteractionKind.NONE

    def test_a_resolved_interaction_stays_none_across_repeated_reads(self):
        """Pas de résurrection au second appel — `get_pending_interaction` reste une fonction
        pure du state donné, jamais un curseur qui avancerait tout seul."""
        state = _active_state()
        state.update(resolve_pending_interaction())
        assert get_pending_interaction(state).kind == InteractionKind.NONE
        assert get_pending_interaction(state).kind == InteractionKind.NONE


class TestCancelledNeverReactivates:
    """CANCELLED : même garantie que RESOLVED, mais sur le chemin explicite d'annulation
    ("annule"/"laisse tomber", politique C4)."""

    def test_clear_pending_interaction_removes_it(self):
        state = _active_state()
        state.update(clear_pending_interaction("user_cancelled"))
        assert get_pending_interaction(state).kind == InteractionKind.NONE

    def test_a_cancelled_interaction_cannot_be_resurrected_by_re_reading_stale_data(self):
        """Le patch EST la nouvelle vérité — relire un ANCIEN dict `pending_interaction`
        indépendamment de state (une erreur d'implémentation plausible) ne doit jamais être
        fait : ce test documente que `state["pending_interaction"]` lui-même vaut bien `None`
        après le patch, pas seulement que le RÉSOLVEUR ment dessus."""
        state = _active_state()
        patch = clear_pending_interaction("user_cancelled")
        state.update(patch)
        assert state["pending_interaction"] is None


class TestExpiredNeverBlocksANewTask:
    """EXPIRED : voir `tests/unit/test_pending_interaction_ttl.py` pour la frontière TTL
    précise ; ce test-ci vérifie l'invariant du mandat au niveau du state complet — un pending
    expiré n'apparaît JAMAIS comme `already_expecting`/`in_tunnel` pour le reste du moteur."""

    def test_an_expired_pending_resolves_as_none_kind(self):
        state = _active_state()
        created_at = state["pending_interaction"]["created_at"]
        resolved = get_pending_interaction(state, now=created_at + 1801.0)
        assert resolved.kind == InteractionKind.NONE

    def test_expiry_does_not_mutate_the_persisted_state(self):
        """`get_pending_interaction` reste une LECTURE pure — l'expiration ne doit jamais
        écrire silencieusement dans `state["pending_interaction"]` (qui resterait sinon un
        second endroit où la même information est représentée deux fois, avant/après lecture)."""
        state = _active_state()
        created_at = state["pending_interaction"]["created_at"]
        original = dict(state["pending_interaction"])
        get_pending_interaction(state, now=created_at + 1801.0)
        assert state["pending_interaction"] == original


class TestReplacedSupersededNeverResumes:
    """REPLACED/SUPERSEDED : remplacer une interaction par une AUTRE ne doit jamais laisser la
    première réapparaître — `replace_pending_interaction` écrase entièrement (`replace_value`),
    jamais une fusion des deux."""

    def test_replacing_a_pending_interaction_leaves_no_trace_of_the_old_one(self):
        state = _active_state()
        old_field = get_pending_interaction(state).field
        state.update(
            replace_pending_interaction(
                InteractionKind.ENTER_FIELD, goal="CREATE_RECURRING_NEED", field_name="quantity"
            )
        )
        resolved = get_pending_interaction(state)
        assert resolved.field == "quantity" and resolved.field != old_field
        assert resolved.kind == InteractionKind.ENTER_FIELD

    def test_a_fresh_set_pending_interaction_also_fully_replaces_the_previous_one(self):
        """`set_pending_interaction`/`replace_pending_interaction` sont sémantiquement
        identiques (même reducer `replace_value`) — les deux noms existent pour la lisibilité
        des call sites, jamais pour un comportement différent (voir leur docstring)."""
        state = _active_state()
        state.update(
            set_pending_interaction(InteractionKind.CONFIRM_ACTION, goal="CREATE_RECURRING_NEED")
        )
        resolved = get_pending_interaction(state)
        assert resolved.kind == InteractionKind.CONFIRM_ACTION
        assert resolved.field is None, "le champ ENTER_FIELD précédent ne doit laisser aucune trace"


class TestNoIncompatibleInteractionSurvivesSilently:
    """"une seule interaction incompatible ne doit pas rester active silencieusement après
    changement de tâche" (mandat §7) — `goal_planner._purge_transaction_state()` efface
    `pending_interaction` au même titre que les drafts sur un VRAI changement de goal (voir
    `interpreter/goal_planner.py`, appelée depuis chacune des règles NEW_TASK/RESUME)."""

    def test_purge_transaction_state_is_wired_to_clear_the_pending_interaction(self):
        import inspect

        from ladini.graphs.agents.market_coach.interpreter import goal_planner

        source = inspect.getsource(goal_planner)
        assert 'clear_pending_interaction("goal_changed")' in source, (
            "un changement de goal doit explicitement effacer pending_interaction — sinon une "
            "interaction du goal abandonné peut fuiter dans le nouveau parcours"
        )
