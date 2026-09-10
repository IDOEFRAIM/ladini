"""Purge des redondances d'état (2026-09-08) — verrouille le fix, pas
seulement le comportement de surface.

Contexte réel : `state["role"]` (doublon de `state["user_role"]`) était écrit
UNE SEULE fois par `role_guard.py`, comme défaut initial, puis JAMAIS mis à
jour ensuite — contrairement à `user_role`, rafraîchi à chaque chargement de
profil. `domain/model.py::DomainContext.from_state` lisait
`state.get("role") or state.get("user_role")` : un `or` qui privilégiait
TOUJOURS la valeur figée de `role_guard` sur la valeur réellement à jour —
un buyer dont le rôle de session a été résolu APRÈS le tour d'entrée (le
cas courant, `role_guard` tourne avant `input_normalizer`/le chargement de
profil) aurait vu `DomainContext.role` rester bloqué sur le défaut
("PRODUCER") pour toute la conversation, silencieusement, sans qu'aucun
test ne le détecte (les deux valeurs sont identiques au 1er tour).

`working_memory.locked_intent` était un doublon exact de
`working_memory.active_goal` — audité, AUCUNE divergence trouvée sur aucun
des ~16 sites d'écriture du repo — mais lu via une chaîne `or` recopiée
indépendamment dans 9 fichiers, un risque de dérive futur pur (aucune
valeur ajoutée, juste une seconde clé à tenir synchronisée)."""

from __future__ import annotations

from ladini.graphs.agents.market_coach.core.state import resolve_current_goal
from ladini.graphs.agents.market_coach.domain.model import DomainContext


class TestRoleFieldNoLongerShadowsUserRole:
    def test_domain_context_uses_user_role_even_when_a_stale_role_key_lingers(self):
        """Reproduction directe du bug réel : un vieux checkpoint (ou un
        appelant legacy) porte encore une clé `role` figée sur une valeur
        PÉRIMÉE — `user_role`, la seule source désormais lue, doit gagner."""
        state = {
            "role": "PRODUCER",  # valeur périmée d'un ancien checkpoint/appelant
            "user_role": "BUYER",  # valeur réelle, à jour
            "user_id": "u1",
            "user_phone": "+22670000000",
        }
        ctx = DomainContext.from_state(state)
        assert ctx.role == "BUYER"

    def test_domain_context_falls_back_cleanly_when_user_role_is_absent(self):
        ctx = DomainContext.from_state({"user_phone": "+22670000000"})
        assert ctx.role is None


class TestRoleGuardNoLongerWritesTheRedundantKey:
    def test_role_guard_only_sets_user_role(self):
        from ladini.graphs.agents.market_coach.nodes.role_guard import (
            make_role_guard,
        )
        from tests.conftest import run

        guard = make_role_guard("PRODUCER")
        patch = run(guard({}, None))
        assert patch == {"user_role": "PRODUCER"}
        assert "role" not in patch

    def test_role_guard_does_not_overwrite_an_already_set_user_role(self):
        from ladini.graphs.agents.market_coach.nodes.role_guard import (
            make_role_guard,
        )
        from tests.conftest import run

        guard = make_role_guard("PRODUCER")
        patch = run(guard({"user_role": "BUYER"}, None))
        assert patch == {}


class TestResolveCurrentGoalCentralization:
    """`core/state.py::resolve_current_goal` — point de résolution UNIQUE qui
    remplace la chaîne `current_goal or working_memory.active_goal or
    working_memory.locked_intent` auparavant recopiée dans 9 fichiers."""

    def test_prefers_current_goal_when_present(self):
        state = {
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "working_memory": {"active_goal": "BUYER_ADD_TO_CART"},
        }
        assert resolve_current_goal(state) == "SALES_PUBLISH_PRODUCT"

    def test_falls_back_to_working_memory_active_goal_when_current_goal_is_cleared(self):
        """Le cas réel que ce mécanisme sert : `current_goal` est remis à
        `None` entre certains tours (post_response_cleanup) tant qu'aucun
        tunnel/confirmation n'est explicitement en attente — la mémoire
        tampon dans working_memory doit alors prendre le relais."""
        state = {
            "current_goal": None,
            "working_memory": {"active_goal": "PROCUREMENT_CREATE_REQUEST"},
        }
        assert resolve_current_goal(state) == "PROCUREMENT_CREATE_REQUEST"

    def test_returns_none_when_nothing_is_active(self):
        assert resolve_current_goal({}) is None
        assert resolve_current_goal({"working_memory": {}}) is None

    def test_tolerates_a_non_dict_working_memory(self):
        """Défense : `working_memory` corrompu/mal typé ne doit jamais lever."""
        assert resolve_current_goal({"working_memory": None}) is None
        assert resolve_current_goal({"working_memory": "oops"}) is None

    def test_tolerates_a_non_dict_state(self):
        assert resolve_current_goal(None) is None  # type: ignore[arg-type]
