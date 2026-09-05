"""Tests d'architecture — anti-dérive (mandat §40, Test C) : les points de
décision migrés vers `pending_interaction` ne doivent plus JAMAIS relire
`waiting_for_confirmation`/`state.get("expected_input")` comme AUTORITÉ.

Test par inspection de SOURCE plutôt que de comportement : le comportement
peut coïncidentiellement rester correct même si quelqu'un réintroduit une
lecture legacy en OR (comme c'était le cas avant cette refonte) — seule une
garantie structurelle empêche la régression silencieuse. Le SEUL endroit
autorisé à connaître `waiting_for_confirmation`/`expected_input` comme
signal de confirmation est `core/pending_interaction.py` lui-même (pont de
transition isolé et documenté, voir sa docstring)."""

from __future__ import annotations

import inspect


def _source(fn) -> str:
    return inspect.getsource(fn)


class TestConfirmationGateReadsOnlyPendingInteraction:
    def test_confirmation_gate_does_not_read_waiting_for_confirmation_as_a_condition(self):
        from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
            confirmation_gate,
        )

        src = _source(confirmation_gate)
        # Le champ peut encore être ÉCRIT (compat de lecture pour du code non
        # migré) mais plus jamais lu via `state.get("waiting_for_confirmation")`
        # ou `state.get("expected_input")` pour DÉCIDER quoi que ce soit.
        assert 'state.get("waiting_for_confirmation")' not in src
        assert 'state.get("expected_input")' not in src
        assert "get_pending_interaction(state).kind == InteractionKind.CONFIRM_ACTION" in src


class TestRouteAfterValidatorFallbackReadsOnlyPendingInteraction:
    def test_fallback_router_does_not_read_waiting_for_confirmation_as_a_condition(self):
        from agriconnect.graphs.agents.market_coach.interpreter.routing import (
            make_route_after_validator,
        )

        src = _source(make_route_after_validator)
        assert 'state.get("waiting_for_confirmation")' not in src


class TestGoalPlannerDerivesExpectedInputFromPendingInteractionOnly(object):
    def test_goal_planner_assigns_expected_input_exactly_once_from_the_resolver(self):
        from agriconnect.graphs.agents.market_coach.interpreter.goal_planner import (
            goal_planner,
        )

        src = _source(goal_planner)
        assert 'expected_input = to_tunnel_category(get_pending_interaction(state))' in src
        # Plus aucune lecture DIRECTE de `state.get("expected_input")` pour
        # initialiser la variable de décision locale.
        assert 'expected_input = state.get("expected_input")' not in src


class TestPricingTiersPackageCountGateUsesPendingInteractionKind:
    def test_pending_pack_count_tier_no_longer_compares_the_raw_string(self):
        from agriconnect.graphs.agents.market_coach.domain.tier_interaction import (
            pending_pack_count_tier,
        )

        src = _source(pending_pack_count_tier)
        assert 'state.get("expected_input")' not in src
        assert "InteractionKind.ENTER_PACKAGE_COUNT" in src
