"""P1-1 / Test F du mandat (audit architectural 2026-09-08) — « toute
décision produite doit avoir au moins un consommateur ; sinon, la
supprimer ».

Verdict tranché sur PREUVE (pas supposé) pour `cognitive_orchestrator` :
- `cognitive_decision.action` (écrit par `cognitive_guard`) EST consommé —
  `interpreter/strategy.py` et `nodes/clarification.py` branchent
  explicitement dessus (`recover_active_tunnel`, `abandon_tunnel_max_retries`).
- `cognitive_decision.phase`/`next_step`/`reason` (écrits par
  `cognitive_orchestrator`) ne pilotent AUCUNE transition — le seul edge
  conditionnel après ce nœud (`_route_after_cognitive`) lit
  `is_onboarding`, posé par un AUTRE nœud, sans rapport. Assumés comme
  télémétrie de diagnostic (option (a) du mandat), documentés comme tels.
- `should_replan` (dérivé de `next_step`, sans lecteur NULLE PART, pas même
  diagnostique) a été supprimé plutôt que gardé comme pseudo-décision.

Ce fichier verrouille la preuve elle-même — si un futur commit ajoute un
lecteur RÉEL de `next_step`/`phase`/`reason` quelque part dans le graphe
(les rendant enfin décisionnels), ce test doit être mis à jour pour le
documenter ; s'il continue de passer alors qu'un tel lecteur existe, il a
raté la régression qu'il existe pour attraper."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "ladini" / "graphs" / "agents" / "market_coach"


def _iter_py_files():
    return [p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _file_has_live_code_reference(path: Path, needle: str) -> bool:
    """True si `needle` apparaît en CODE (pas seulement dans un
    commentaire/docstring expliquant sa suppression — ce fichier de test
    lui-même, et les commentaires laissés dans state.py/cognitive.py comme
    trace de la décision P1-1, en contiennent légitimement)."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:  # pragma: no cover
        return False
    if needle not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:  # pragma: no cover
        return needle in source
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if needle in node.value and _looks_like_identifier_use(node.value, needle):
                return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            rendered = getattr(node, "id", None) or getattr(node, "attr", None)
            if rendered == needle:
                return True
    return False


def _looks_like_identifier_use(string_value: str, needle: str) -> bool:
    # Une clé de dict ("should_replan": ...) ou un state.get("should_replan")
    # apparaissent comme une constante string EXACTEMENT égale au champ —
    # jamais noyée dans une phrase de commentaire/docstring en français.
    return string_value.strip() == needle


class TestShouldReplanHasNoReaderAnywhere:
    def test_should_replan_does_not_exist_in_source_anymore(self):
        """`should_replan` doit être totalement absent EN CODE — écrivain
        ET lecteur — pas seulement inutilisé mais encore présent en
        dormance. Les commentaires documentant sa suppression (ce fichier
        inclus) sont légitimes et ignorés."""
        offenders = [
            str(p.relative_to(SRC_ROOT))
            for p in _iter_py_files()
            if _file_has_live_code_reference(p, "should_replan")
        ]
        assert offenders == [], (
            f"'should_replan' réapparaît en CODE dans : {offenders} — soit "
            f"un vrai lecteur a été ajouté (dans ce cas, documente-le et "
            f"retire ce test), soit c'est une résurgence accidentelle du "
            f"champ supprimé"
        )


class TestCognitiveOrchestratorFieldsHaveNoRoutingReader:
    """Preuve négative directe : aucun edge conditionnel du graphe ne lit
    `next_step`/`phase` (les clés propres à `cognitive_orchestrator`, par
    opposition à `action`, propre à `cognitive_guard` et RÉELLEMENT
    consommée).

    (2026-09-08, correction topologique du bloc d'entrée) : `_route_after_
    cognitive` a été SUPPRIMÉE — `is_onboarding` n'était déjà son SEUL
    signal (preuve figée par ce test avant correctif), et cette
    responsabilité a été déplacée vers `session_bootstrap`/
    `_route_after_session_bootstrap` (qui tranche AVANT que
    `input_interpreter`/`cognitive_guard` ne s'exécutent). `cognitive_guard`
    n'a donc plus qu'une seule suite réelle — `clarification_node`, un edge
    FIXE, plus de conditional_edges à sortie unique (même discipline que
    P2-2 pour `_route_after_executor`, déjà supprimé pour la même raison).
    `TestNoConditionalRoutingAfterCognitiveGuardAnymore` ci-dessous verrouille
    cette suppression."""

    def test_action_from_cognitive_guard_is_the_only_consumed_key(self):
        """Non-régression positive : `action` (l'AUTRE moitié du même
        channel `cognitive_decision`, écrite par `cognitive_guard`) reste
        bien consommée — la suppression de `should_replan` ne doit jamais
        avoir emporté ce mécanisme réellement utile avec elle."""
        import inspect

        from ladini.graphs.agents.market_coach.interpreter import strategy
        from ladini.graphs.agents.market_coach.nodes import clarification

        strategy_src = inspect.getsource(strategy.response_strategy)
        clarification_src = inspect.getsource(clarification.clarification_node)

        assert "cognitive_action" in strategy_src
        assert "abandon_tunnel_max_retries" in strategy_src
        assert "cognitive_action" in clarification_src
        # (2026-09-08, clôture Bloc 1, mandat §16) : `clarification_node`
        # compare désormais la constante centralisée `ConversationAction.
        # ABANDON_ACTIVE_GOAL` (valeur littérale inchangée,
        # "abandon_tunnel_max_retries" — voir core/conversation_decision.py)
        # plutôt que la chaîne nue. `response_strategy.py`, hors périmètre,
        # garde la comparaison littérale historique.
        assert "ConversationAction.ABANDON_ACTIVE_GOAL" in clarification_src
        from ladini.graphs.agents.market_coach.core.conversation_decision import (
            ConversationAction,
        )

        assert ConversationAction.ABANDON_ACTIVE_GOAL == "abandon_tunnel_max_retries"


class TestCognitiveGuardRoutingHistory:
    """Verrouille les DEUX corrections topologiques successives du
    2026-09-08 sur `cognitive_guard` — même nœud, deux chantiers distincts,
    à ne pas confondre :

    1. Correction du bloc D'ENTRÉE (onboarding) : `_route_after_cognitive`
       (l'ORIGINAL, qui ne lisait QUE `is_onboarding`) a été supprimé —
       cette décision est tranchée bien plus en amont par
       `session_bootstrap`, avant même que `cognitive_guard` ne s'exécute.
       Cette fonction reste et doit rester absente pour toujours (test
       ci-dessous).
    2. Correction du bloc CONVERSATIONNEL (CE chantier) : `cognitive_guard`
       a ENSUITE regagné un edge conditionnel RÉEL —
       `_route_after_cognitive_guard` (`nodes/routing.py`) — mais pour une
       raison structurellement différente : ce n'est plus l'onboarding qui
       le pilote, c'est la décision `ConversationDecision` complète
       (CLARIFY/DISAMBIGUATE/CONTINUE_ACTIVE_GOAL/START_OR_PLAN_GOAL/
       INTERRUPT_ACTIVE_GOAL/recover/abandon) — voir docstring de
       `cognitive_guard`. `TestCognitiveGuardHasTheFullConversationalRouting`
       ci-dessous verrouille ce nouveau contrat (qui remplace l'edge fixe
       introduit — puis retiré le même jour — par le 1er chantier)."""

    def test_route_after_cognitive_no_longer_exists(self):
        """L'ORIGINAL routeur onboarding-only reste mort — pas de résurgence."""
        from ladini.graphs.agents.market_coach.core import graph_builder

        assert not hasattr(graph_builder, "_route_after_cognitive")


class TestCognitiveGuardHasTheFullConversationalRouting:
    """(2026-09-08, correction topologique du bloc CONVERSATIONNEL,
    complétée à la clôture du Bloc 1) : `cognitive_guard` est le
    propriétaire unique de la décision de transition — verrouille que le
    graphe COMPILÉ a EXACTEMENT les 4 destinations documentées dans
    `nodes/routing.py::_route_after_cognitive_guard`, rien de plus (pas de
    5e branche fantôme)."""

    @pytest.mark.parametrize("role", ["PRODUCER", "BUYER"])
    def test_cognitive_guard_has_exactly_the_four_documented_destinations(self, role):
        """(2026-09-08, clôture Bloc 1, mandat §29) : une 4e destination a
        été ajoutée — RECOVER_ACTIVE_GOAL route directement vers
        `response_strategy`, `clarification_node` étant un no-op structurel
        prouvé pour cette action (voir `nodes/clarification.py`)."""
        from ladini.graphs.agents.market_coach.core.graph_builder import (
            build_graph,
        )

        graph = build_graph(role=role).get_graph()
        outgoing = {
            (e.source, e.target, e.data)
            for e in graph.edges
            if e.source == "cognitive_guard"
        }
        assert outgoing == {
            ("cognitive_guard", "goal_planner", "to_planner"),
            ("cognitive_guard", "semantic_disambiguation", "to_disambiguation"),
            ("cognitive_guard", "clarification_node", "to_clarification"),
            ("cognitive_guard", "response_strategy", "to_strategy"),
        }


class TestCognitiveOrchestratorHasNoLiveCodeAnywhere:
    """Revue de validation du bloc refondu (2026-09-08, même jour) — mandat
    §14 : "aucune registration dans le graph ; aucun edge ; aucun import
    mort ; aucun champ d'état uniquement destiné à ce node". Preuve
    négative directe, même mécanisme que `TestShouldReplanHasNoReaderAnywhere`
    ci-dessus (les commentaires qui DOCUMENTENT la suppression, dans ce
    fichier compris, ne sont pas de l'AST exécutable — invisibles à
    `ast.parse`, donc ignorés par construction, pas par une liste
    d'exceptions fragile)."""

    def test_cognitive_orchestrator_does_not_exist_in_source_anymore(self):
        offenders = [
            str(p.relative_to(SRC_ROOT))
            for p in _iter_py_files()
            if _file_has_live_code_reference(p, "cognitive_orchestrator")
        ]
        assert offenders == [], (
            f"'cognitive_orchestrator' réapparaît en CODE dans : {offenders}"
        )


class TestShouldTriggerDisambiguationHasNoLiveCodeAnywhere:
    """Même preuve pour `_should_trigger_disambiguation` — trouvée SANS
    AUCUN appelant en production lors de l'audit exhaustif du périmètre
    `disambiguation_candidate` (revue de validation, 2026-09-08) : seul
    `cognitive_orchestrator`, déjà mort, l'appelait. Supprimée plutôt que
    laissée en dormance (même discipline que `should_replan`)."""

    def test_should_trigger_disambiguation_does_not_exist_in_source_anymore(self):
        offenders = [
            str(p.relative_to(SRC_ROOT))
            for p in _iter_py_files()
            if _file_has_live_code_reference(p, "_should_trigger_disambiguation")
        ]
        assert offenders == [], (
            f"'_should_trigger_disambiguation' réapparaît en CODE dans : {offenders}"
        )
