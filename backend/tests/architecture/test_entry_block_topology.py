"""Revue de validation du bloc refondu (2026-09-08) — mandat §17/§18 :
preuve du graphe COMPILÉ réel (introspection `graph.get_graph()`, pas une
réimplémentation ni une lecture de source) que :

    - `role_guard` n'existe plus comme nœud ;
    - `cognitive_orchestrator` n'existe plus comme nœud ;
    - `input_normalizer` est bien le point d'entrée, avant
      `security_moderation` ;
    - `session_bootstrap` est désormais un nœud décisionnel APRÈS
      `security_moderation` (ALLOW), pas un simple relais fixe avant
      `input_normalizer` (correction topologique 2026-09-08 : un profil
      utilisateur ne doit jamais être chargé avant que l'entrée soit
      canonicalisée et jugée sûre) ;
    - le sous-graphe d'entrée a EXACTEMENT la forme documentée dans le
      rapport de revue.

Les deux variantes compilées (PRODUCER/BUYER, `GraphFactory`) sont
testées tant qu'elles coexistent (dette documentée : deux graphes compilés
par rôle, devenue vestigiale mais pas démantelée dans ce chantier)."""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph


def _compiled_edges(role: str):
    graph = build_graph(role=role)
    real_graph = graph.get_graph()
    nodes = set(real_graph.nodes.keys())
    edges = {(e.source, e.target, e.data) for e in real_graph.edges}
    return nodes, edges


class TestRoleGuardAndCognitiveOrchestratorAreGone:
    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_role_guard_is_not_a_node(self, role):
        nodes, _ = _compiled_edges(role)
        assert "role_guard" not in nodes

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_cognitive_orchestrator_is_not_a_node(self, role):
        nodes, _ = _compiled_edges(role)
        assert "cognitive_orchestrator" not in nodes

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_session_bootstrap_is_a_node(self, role):
        nodes, _ = _compiled_edges(role)
        assert "session_bootstrap" in nodes


class TestEntryPointIsInputNormalizer:
    """(2026-09-08, correction topologique) : `input_normalizer` est
    désormais le point d'entrée réel — `session_bootstrap` n'est plus
    exécuté avant la normalisation/sécurité, il est déplacé APRÈS
    `security_moderation` (branche ALLOW uniquement)."""

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_start_edge_targets_input_normalizer(self, role):
        _, edges = _compiled_edges(role)
        assert ("__start__", "input_normalizer", None) in edges

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_start_edge_no_longer_targets_session_bootstrap(self, role):
        _, edges = _compiled_edges(role)
        assert ("__start__", "session_bootstrap", None) not in edges

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_input_normalizer_leads_to_security_moderation(self, role):
        _, edges = _compiled_edges(role)
        assert ("input_normalizer", "security_moderation", None) in edges

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_session_bootstrap_no_longer_leads_unconditionally_to_input_normalizer(
        self, role
    ):
        _, edges = _compiled_edges(role)
        assert ("session_bootstrap", "input_normalizer", None) not in edges


class TestEntryBlockShapeMatchesTheReviewReport:
    """Un seul test par arête documentée dans le rapport de revue — casse
    bruyamment si la topologie dérive silencieusement d'un futur commit."""

    EXPECTED_EDGES = {
        ("input_normalizer", "security_moderation", None),
        ("security_moderation", "session_bootstrap", "to_bootstrap"),
        ("security_moderation", "response_strategy", "to_strategy"),
        ("session_bootstrap", "input_interpreter", "to_interpreter"),
        ("session_bootstrap", "onboarding_node", "to_onboarding"),
        ("session_bootstrap", "response_strategy", "to_strategy"),
        ("input_interpreter", "cognitive_guard", "to_cognitive"),
        ("input_interpreter", "memory_update", "to_memory_fast"),
    }
    # NB : la frontière de CE fichier s'arrête à `input_interpreter` (le
    # "bloc d'entrée" audité par ce chantier). Les arêtes du bloc
    # CONVERSATIONNEL (`cognitive_guard`/`clarification_node`/
    # `semantic_disambiguation`) — corrigé dans un chantier SÉPARÉ le même
    # jour — sont verrouillées dans
    # `tests/architecture/test_cognitive_decisions_are_consumed_or_removed.py::
    # TestCognitiveGuardHasTheFullConversationalRouting`, pas ici : les
    # dupliquer aux deux endroits aurait recréé exactement le risque de
    # dérive que ces tests d'architecture existent pour éliminer.

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_every_expected_entry_block_edge_is_present(self, role):
        _, edges = _compiled_edges(role)
        missing = self.EXPECTED_EDGES - edges
        assert not missing, f"arêtes attendues absentes du graphe compilé : {missing}"

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_session_bootstrap_has_exactly_three_outgoing_edges(self, role):
        """`session_bootstrap` est désormais un vrai nœud décisionnel
        (READY/ONBOARDING/UNAVAILABLE) — verrouille qu'aucune 4e branche
        fantôme n'est réintroduite par erreur."""
        _, edges = _compiled_edges(role)
        outgoing = {e for e in edges if e[0] == "session_bootstrap"}
        assert outgoing == {
            ("session_bootstrap", "input_interpreter", "to_interpreter"),
            ("session_bootstrap", "onboarding_node", "to_onboarding"),
            ("session_bootstrap", "response_strategy", "to_strategy"),
        }
