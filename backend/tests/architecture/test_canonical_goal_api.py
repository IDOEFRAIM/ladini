"""API canonique du goal (Phase 2 hardening, commit 6, mandat §18).

`core/state.py::resolve_current_goal` est le point de LECTURE canonique unique
(pré-existant). Ce module ajoute son symétrique en ÉCRITURE pour la
représentation de secours `working_memory.active_goal` :
`core/state.py::lock_goal` (SET/TRANSITION) et `clear_goal_lock` (CLEAR).

Portée délibérément étroite (mandat §22, "ne bascule pas tout d'un coup") :
seuls les DEUX propriétaires critiques identifiés par l'audit sont couverts
ici — `goal_planner` (seul propriétaire déclaré de `current_goal`, et
jusqu'ici seul à réimplémenter localement le verrou `active_goal`) et
`cognitive.py` (qui ne décide jamais du goal, mais peut le RÉAFFIRMER dans le
canal primaire lors d'une récupération de tunnel). Les ~25 autres sites
d'écriture directe de `current_goal` répartis dans `flows/*.py` et
`graph_builder.py` restent HORS PÉRIMÈTRE de ce commit (big-bang interdit) —
dette connue, non traitée ici.
"""
from __future__ import annotations

import ast
import inspect

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    DISAMBIGUATION_MENU_GOAL_SHIM,
)
from ladini.graphs.agents.market_coach.core.state import clear_goal_lock, lock_goal
from ladini.graphs.agents.market_coach.interpreter import goal_planner
from ladini.graphs.agents.market_coach.nodes import cognitive


class TestCanonicalGoalWriteAPI:
    """Comportement direct de `lock_goal`/`clear_goal_lock`."""

    def test_lock_goal_sets_active_goal_and_defaults_step_index(self):
        wm = lock_goal({}, "CREATE_RECURRING_NEED")
        assert wm["active_goal"] == "CREATE_RECURRING_NEED"
        assert wm["step_index"] == 0

    def test_lock_goal_reaffirming_the_same_goal_preserves_step_index(self):
        """Un tour qui confirme un goal déjà actif ne doit pas remettre sa
        progression à zéro (`setdefault`, pas d'écrasement inconditionnel)."""
        wm = lock_goal({"active_goal": "CREATE_RECURRING_NEED", "step_index": 3}, "CREATE_RECURRING_NEED")
        assert wm["step_index"] == 3

    def test_lock_goal_switching_goal_overwrites_active_goal_but_keeps_step_index_semantics(self):
        wm = lock_goal({"active_goal": "OLD_GOAL", "step_index": 3}, "NEW_GOAL")
        assert wm["active_goal"] == "NEW_GOAL"
        # `setdefault` : step_index d'un AUTRE goal survit tel quel — un vrai
        # reset de progression passe par `clear_goal_lock` avant `lock_goal`.
        assert wm["step_index"] == 3

    def test_lock_goal_never_locks_the_disambiguation_menu_shim(self):
        wm = lock_goal({"active_goal": "REAL_GOAL"}, DISAMBIGUATION_MENU_GOAL_SHIM)
        assert wm["active_goal"] == "REAL_GOAL"  # inchangé, jamais écrasé par le shim

    def test_lock_goal_with_no_goal_leaves_working_memory_untouched(self):
        wm = lock_goal({"unrelated": 1}, None)
        assert wm == {"unrelated": 1}

    def test_clear_goal_lock_uses_delete_semantics_not_pop(self):
        """Bug B1/commit C2 : un `.pop()` avant un patch `merge_dict` est un
        no-op silencieux (l'ABSENCE d'une clé veut dire "inchangé"). Le clear
        canonique doit mettre le sentinel DELETE, jamais retirer la clé."""
        from ladini.agents.reducers import DELETE

        wm = clear_goal_lock({"active_goal": "X", "step_index": 2, "other": 1})
        assert wm["active_goal"] == DELETE
        assert wm["step_index"] == DELETE
        assert wm["other"] == 1  # non concerné, laissé intact

    def test_lock_goal_and_clear_goal_lock_never_mutate_their_input(self):
        original = {"active_goal": "X", "step_index": 1}
        lock_goal(original, "Y")
        clear_goal_lock(original)
        assert original == {"active_goal": "X", "step_index": 1}


class TestGoalPlannerDelegatesToTheCanonicalAPI:
    """`goal_planner` est le propriétaire déclaré de `current_goal` (docstring
    `core/state.py`) — mais son verrou `working_memory.active_goal` ne doit
    plus être une réimplémentation locale : il doit passer par l'API
    canonique, pour qu'il n'existe qu'UN SEUL endroit qui sait comment cette
    représentation est posée/effacée."""

    def test_lock_and_clear_helpers_call_the_canonical_functions(self):
        source = inspect.getsource(goal_planner)
        assert "lock_goal(working" in source
        assert "clear_goal_lock(working" in source

    def test_goal_planner_no_longer_reimplements_the_lock_body_locally(self):
        """Garde de non-régression : si un futur patch réintroduit
        `wm["active_goal"] = ...` en dur dans ce fichier (au lieu d'appeler
        `lock_goal`), c'est une seconde représentation concurrente qui
        réapparaît silencieusement."""
        source = inspect.getsource(goal_planner)
        assert 'wm["active_goal"]' not in source
        assert 'wm.setdefault("step_index"' not in source


class TestCognitiveGuardNeverBypassesTheCanonicalRepresentation:
    """`cognitive_guard` (`nodes/cognitive.py`) ne DÉCIDE jamais du goal — il
    peut seulement RÉAFFIRMER, dans le canal primaire `current_goal`, la
    valeur déjà résolue par `resolve_current_goal` (cas RECOVER_ACTIVE_GOAL,
    quand `current_goal` était retombé à `None` entre deux tours). Il ne doit
    JAMAIS toucher `working_memory["active_goal"]` directement : cette
    représentation de secours reste la propriété exclusive de `goal_planner`,
    via l'API canonique."""

    def test_cognitive_guard_never_writes_the_working_memory_goal_lock_directly(self):
        """AST, pas une recherche de sous-chaîne : le mot `active_goal`
        apparaît légitimement dans des COMMENTAIRES explicatifs de ce fichier
        (ex. sur `resolve_current_goal`) — seule une écriture réelle (clé de
        dict ou assignation de subscript) doit faire échouer ce test."""
        tree = ast.parse(inspect.getsource(cognitive))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key_node in node.keys:
                    if isinstance(key_node, ast.Constant) and key_node.value == "active_goal":
                        offenders.append(("dict-literal", key_node.lineno))
            if isinstance(node, ast.Subscript):
                sl = node.slice
                if isinstance(sl, ast.Constant) and sl.value == "active_goal":
                    offenders.append(("subscript", node.lineno))
        assert offenders == [], offenders

    def test_cognitive_guard_writes_current_goal_at_most_once_and_only_as_a_reaffirmation(self):
        """Un SEUL site écrit `current_goal` dans ce fichier, et la valeur
        écrite doit être exactement la variable locale peuplée par
        `resolve_current_goal(state)` — jamais une valeur inventée
        indépendamment, ce qui reviendrait à une seconde autorité de
        décision du goal (violerait le contrat `cognitive_guard` seul
        propriétaire de la décision d'INTERRUPTION, pas du GOAL lui-même)."""
        tree = ast.parse(inspect.getsource(cognitive))
        sites = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key_node, value_node in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key_node, ast.Constant)
                        and key_node.value == "current_goal"
                    ):
                        sites.append(value_node)
        assert len(sites) == 1, "current_goal doit rester écrit à un seul endroit dans cognitive.py"
        (value_node,) = sites
        assert isinstance(value_node, ast.Name) and value_node.id == "current_goal", (
            "cognitive.py ne doit réaffirmer que la valeur résolue par "
            "resolve_current_goal(state), jamais une valeur littérale ou "
            "recalculée indépendamment"
        )
