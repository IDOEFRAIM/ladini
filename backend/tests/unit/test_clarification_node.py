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


class TestClarificationNodeTriggerConditions:
    def test_out_of_scope_without_a_tunnel_triggers_the_llm(self):
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Salut ! Je peux t'aider à vendre ou acheter.")
        result = run(clarification_node({
            "interpreted_event": "OUT_OF_SCOPE", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "bonjour ça va ?",
            "user_role": "PRODUCER",
        }, rt))
        assert result["final_response"] == "Salut ! Je peux t'aider à vendre ou acheter."
        assert result["response_strategy"] == "CLARIFICATION"

    def test_a_stray_reject_without_a_tunnel_now_triggers_the_llm_too(self):
        """Bug réel (2026-08-14) : un "non" sans rien en attente tombait sur
        le fallback générique sans jamais essayer une réponse contextuelle —
        contrairement à OUT_OF_SCOPE/UNKNOWN dans le même contexte."""
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Pas de souci ! Dis-moi ce que tu veux faire.")
        result = run(clarification_node({
            "interpreted_event": "REJECT", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "non",
            "user_role": "BUYER",
        }, rt))
        assert result["final_response"] == "Pas de souci ! Dis-moi ce que tu veux faire."

    def test_a_reject_with_an_active_tunnel_does_not_trigger_this_node(self):
        """Volontaire : quand un goal est actif, confirmation_gate.py /
        render_recovery gèrent déjà ce REJECT avec leur propre logique
        adaptée — ce nœud ne doit pas s'en mêler (double traitement)."""
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "REJECT", "expected_input": "CONFIRMATION",
            "current_goal": "SALES_PUBLISH_PRODUCT", "normalized_text": "non",
            "user_role": "PRODUCER",
        }, rt))
        assert result == {}

    def test_an_unknown_event_with_an_active_tunnel_does_not_trigger_this_node(self):
        """Volontaire : couvert par render_ask_missing_field/render_recovery."""
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "PRICE",
            "current_goal": "PROCUREMENT_CREATE_REQUEST", "normalized_text": "je sais pas trop",
            "user_role": "BUYER",
        }, rt))
        assert result == {}

    def test_abandoned_tunnel_triggers_the_llm_regardless_of_event(self):
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("On repart de zéro, dis-moi ce dont tu as besoin.")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "PRICE",
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "cognitive_decision": {"action": "abandon_tunnel_max_retries"},
            "normalized_text": "bla bla", "user_role": "PRODUCER",
        }, rt))
        assert result["final_response"] == "On repart de zéro, dis-moi ce dont tu as besoin."

    def test_a_confirm_event_never_triggers_this_node(self):
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître")
        result = run(clarification_node({
            "interpreted_event": "CONFIRM", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "oui",
            "user_role": "BUYER",
        }, rt))
        assert result == {}

    def test_no_llm_available_returns_an_empty_patch_not_a_crash(self):
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = type("RT", (), {"llm": None})()
        result = run(clarification_node({
            "interpreted_event": "REJECT", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "non",
            "user_role": "BUYER",
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
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("ne devrait jamais apparaître — le LLM ne doit pas être appelé")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "je veux voir les enchères",
            "user_role": "PRODUCER", "unknown_reason": "TECHNICAL_FAILURE",
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
        from agriconnect.graphs.agents.market_coach.nodes.clarification import clarification_node
        rt = _runtime("Salut ! Je peux t'aider à vendre ou acheter.")
        result = run(clarification_node({
            "interpreted_event": "UNKNOWN", "expected_input": "NONE",
            "current_goal": None, "normalized_text": "bonjour ça va ?",
            "user_role": "PRODUCER", "unknown_reason": "AMBIGUOUS",
        }, rt))
        assert result["final_response"] == "Salut ! Je peux t'aider à vendre ou acheter."
        assert rt.llm.calls == 1
