"""`nodes/clarification.py::clarification_node` — décide QUAND tenter une
réponse LLM adaptative plutôt que de laisser le fallback générique
("Je n'ai pas bien saisi...") s'afficher. Élargi (2026-08-14) pour couvrir
REJECT hors tunnel, après avoir découvert que `render_clarification`
(nodes/rendering/feedback.py) ignorait systématiquement tout ce que ce nœud
produisait — voir [[precommande-architecture-consolidation-2026-08]]."""
from __future__ import annotations

from tests.conftest import run


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        return _Completion(self._text)


def _runtime(text: str = "Bien sûr, je t'aide !") -> object:
    llm = _StubLLM(text)
    return type("RT", (), {"llm": llm, "model_answer": "test-model"})()


class TestClarificationNodeDoesNotRecomputeDisambiguationPolicy:
    """(2026-09-08, correction topologique du bloc conversationnel, mandat
    §15) : `clarification_node` ne décide plus lui-même s'il faut
    désambiguïser — cette précédence (DISAMBIGUATE gagne sur CLARIFY quand
    les deux seraient éligibles) est maintenant arbitrée UNE fois, en
    amont, par `cognitive_guard` (voir `nodes/cognitive.py::
    _classify_nominal_action`) : les deux actions sont mutuellement
    exclusives AVANT même que ce nœud ne soit invoqué. `clarification_node`
    n'a donc plus besoin de consulter `disambiguation_candidate` pour
    s'effacer — il fait simplement confiance à `cognitive_decision.action`."""

    def test_module_no_longer_imports_detect_disambiguation_candidates(self):
        import ladini.graphs.agents.market_coach.nodes.clarification as mod
        assert not hasattr(mod, "_detect_disambiguation_candidates")

    def test_a_bare_disambiguation_candidate_no_longer_makes_this_node_defer(self):
        """Non-régression du RETRAIT (mandat §15) : avant ce correctif, la
        seule PRÉSENCE de `disambiguation_candidate` dans l'état suffisait
        à faire taire ce nœud, même si `cognitive_guard` avait par ailleurs
        décidé CLARIFY (ex: le candidat provient d'un tour précédent resté
        dans l'état). Ce garde était une SECONDE policy de précédence,
        redondante avec celle — maintenant unique — de `cognitive_guard`.
        Preuve : avec `cognitive_decision.action == "CLARIFY"` ET un
        `disambiguation_candidate` présent, ce nœud RENDS bel et bien
        (cognitive_guard ne pose jamais les deux à la fois en pratique,
        mais ce nœud ne doit plus s'appuyer là-dessus pour décider)."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Salut ! Je peux t'aider.")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "j'ai des tomates",
            "user_role": "PRODUCER",
            "cognitive_decision": {"action": "CLARIFY"},
            "disambiguation_candidate": {"id": "trigger1", "candidates": ["A", "B"]},
        }, rt))
        assert result["final_response"] == "Salut ! Je peux t'aider."
        assert rt.llm.calls == 1

    def test_no_cognitive_decision_at_all_is_a_safe_no_op(self):
        """Garde-fou de compatibilité restant (mandat §15, "garder
        uniquement les garde-fous indispensables") : sans
        `cognitive_decision` du tout (checkpoint pré-déploiement, appel
        direct hors graphe), ce nœud ne devine rien et ne rend rien."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître — le LLM ne doit pas être appelé")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "j'ai des tomates",
            "user_role": "PRODUCER",
            "disambiguation_candidate": {"id": "trigger1", "candidates": ["A", "B"]},
        }, rt))
        assert result == {}
        assert rt.llm.calls == 0


class TestClarificationNodeTriggerConditions:
    """(2026-09-08, correction topologique du bloc conversationnel, mandat
    §15) : ce nœud ne décide plus lui-même « faut-il clarifier ? » — il
    fait confiance à `cognitive_decision.action`, posé par `cognitive_guard`
    (voir sa docstring). Les scénarios ci-dessous qui attendent un VRAI
    rendu passent donc désormais explicitement `cognitive_decision:
    {"action": "CLARIFY"}` — exactement ce que `cognitive_guard` aurait
    décidé pour ces mêmes états (event ∈ {OUT_OF_SCOPE, UNKNOWN, REJECT},
    rien en attente, aucun goal actif ; voir
    `nodes/cognitive.py::_classify_nominal_action`). Les scénarios qui
    n'attendent AUCUN rendu (goal actif, event CONFIRM...) n'ont
    volontairement PAS ce champ : `cognitive_guard` ne déciderait jamais
    CLARIFY pour eux, donc ce nœud ne serait jamais invoqué dans le graphe
    réel — le garde-fou de compatibilité (action non reconnue -> `{}`)
    couvre ce cas défensivement, sans recalcul."""

    def test_out_of_scope_without_a_tunnel_triggers_the_llm(self):
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Salut ! Je peux t'aider à vendre ou acheter.")
        result = run(clarification_node({
            "interpreted_event": "OUT_OF_SCOPE", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "bonjour ça va ?",
            "user_role": "PRODUCER",
            "cognitive_decision": {"action": "CLARIFY"},
        }, rt))
        assert result["final_response"] == "Salut ! Je peux t'aider à vendre ou acheter."
        assert result["response_strategy"] == "CLARIFICATION"

    def test_a_stray_reject_without_a_tunnel_now_triggers_the_llm_too(self):
        """Bug réel (2026-08-14) : un "non" sans rien en attente tombait sur
        le fallback générique sans jamais essayer une réponse contextuelle —
        contrairement à OUT_OF_SCOPE/UNKNOWN dans le même contexte."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Pas de souci ! Dis-moi ce que tu veux faire.")
        result = run(clarification_node({
            "interpreted_event": "REJECT", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "non",
            "user_role": "BUYER",
            "cognitive_decision": {"action": "CLARIFY"},
        }, rt))
        assert result["final_response"] == "Pas de souci ! Dis-moi ce que tu veux faire."

    def test_a_reject_with_an_active_tunnel_does_not_trigger_this_node(self):
        """Volontaire : quand un goal est actif, confirmation_gate.py /
        render_recovery gèrent déjà ce REJECT avec leur propre logique
        adaptée — `cognitive_guard` ne déciderait jamais CLARIFY ici (un
        goal est actif), donc ce nœud ne serait jamais invoqué en pratique ;
        prouvé ici en l'appelant SANS `cognitive_decision` (garde-fou de
        compatibilité, pas une double policy)."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "REJECT", "expected_input": "CONFIRMATION",
            "current_goal": "SALES_PUBLISH_PRODUCT", "normalized_text": "non",
            "user_role": "PRODUCER",
        }, rt))
        assert result == {}

    def test_an_unknown_event_with_an_active_tunnel_does_not_trigger_this_node(self):
        """Volontaire : couvert par render_ask_missing_field/render_recovery."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "PRICE",
            "current_goal": "PROCUREMENT_CREATE_REQUEST", "normalized_text": "je sais pas trop",
            "user_role": "BUYER",
        }, rt))
        assert result == {}

    def test_abandoned_tunnel_triggers_the_llm_regardless_of_event(self):
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("On repart de zéro, dis-moi ce dont tu as besoin.")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "PRICE",
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "cognitive_decision": {"action": "abandon_tunnel_max_retries"},
            "normalized_text": "bla bla", "user_role": "PRODUCER",
        }, rt))
        assert result["final_response"] == "On repart de zéro, dis-moi ce dont tu as besoin."

    def test_recover_active_tunnel_is_always_a_no_op_here(self):
        """(2026-09-08, correction topologique) : preuve directe du
        pass-through documenté dans `clarification_node` — la relance
        RECOVERY est entièrement rendue par `response_strategy.py` depuis
        `cognitive_action` seul, ce nœud n'a RIEN à produire pour cette
        action, même avec un LLM disponible et prêt à répondre."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "QUANTITY",
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "cognitive_decision": {"action": "recover_active_tunnel"},
            "normalized_text": "bla bla", "user_role": "PRODUCER",
        }, rt))
        assert result == {}
        assert rt.llm.calls == 0

    def test_a_confirm_event_never_triggers_this_node(self):
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "CONFIRM", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "oui",
            "user_role": "BUYER",
        }, rt))
        assert result == {}

    def test_no_llm_available_returns_an_empty_patch_not_a_crash(self):
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = type("RT", (), {"llm": None})()
        result = run(clarification_node({
            "interpreted_event": "REJECT", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "non",
            "user_role": "BUYER",
            "cognitive_decision": {"action": "CLARIFY"},
        }, rt))
        assert result == {}


class TestTechnicalFailureSkipsASecondWastedLlmCall:
    """Incident réel (2026-09-05) : « je veux voir les enchères » → Gateway
    épuisée dans `input_interpreter` (2 candidats 401, repli refusé) →
    UNKNOWN → `clarification_node` retentait EXACTEMENT les mêmes providers
    → mêmes échecs → réponse générique. Corrigé : `unknown_reason=
    TECHNICAL_FAILURE` (posé par `input_interpreter`/`InterpreterResult`)
    court-circuite le second appel — repli déterministe honnête à la place."""

    def test_technical_failure_never_calls_the_llm_and_returns_an_honest_degraded_message(self):
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître — le LLM ne doit pas être appelé")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "je veux voir les enchères",
            "user_role": "PRODUCER", "unknown_reason": "TECHNICAL_FAILURE",
            "cognitive_decision": {"action": "CLARIFY"},
        }, rt))
        assert result["response_strategy"] == "CLARIFICATION"
        assert "indisponible" in result["final_response"].lower()
        # Jamais le mensonge "je n'ai pas bien saisi" pour une panne
        # d'infrastructure (§12/§15 du brief incident) :
        assert "bien saisi" not in result["final_response"].lower()
        assert rt.llm.calls == 0, "le LLM ne doit JAMAIS être appelé une 2e fois pour rien"

    def test_a_genuine_ambiguous_unknown_still_calls_the_llm_as_before(self):
        """Non-régression : seul `TECHNICAL_FAILURE` court-circuite l'appel —
        une vraie ambiguïté de contenu (`AMBIGUOUS`, ou absent) garde son
        comportement adaptatif existant."""
        from ladini.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Salut ! Je peux t'aider à vendre ou acheter.")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "bonjour ça va ?",
            "user_role": "PRODUCER", "unknown_reason": "AMBIGUOUS",
            "cognitive_decision": {"action": "CLARIFY"},
        }, rt))
        assert result["final_response"] == "Salut ! Je peux t'aider à vendre ou acheter."
        assert rt.llm.calls == 1
