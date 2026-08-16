"""Derniers trous de couverture dans `nodes/rendering/ask.py` et `confirm.py`
(le reste de ces fichiers est déjà couvert ailleurs — seules les branches
encore manquantes sont ciblées ici)."""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import RenderContext
from tests.conftest import make_state, run


def ctx(**state_overrides):
    state = make_state(**state_overrides)
    return RenderContext(
        state=state, mc_runtime=None,
        strategy=str(state.get("response_strategy") or ""),
        status=str(state.get("status") or ""),
        goal=state.get("current_goal"),
        salutation="",
        payload=state.get("transaction_payload") or {},
    )


class TestRenderOnboarding:
    def test_default_prompt_when_none_precomputed(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_onboarding
        c = ctx(onboarding_prompt=None)
        result = run(render_onboarding(c))
        assert "Bienvenue sur AgriConnect" in result["final_response"]

    def test_precomputed_prompt_is_reused(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_onboarding
        c = ctx(onboarding_prompt="Question personnalisée")
        result = run(render_onboarding(c))
        assert result["final_response"] == "Question personnalisée"


class TestRenderAskMissingField:
    def test_no_goal_asks_generic_intent_question(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(current_goal=None, final_response=None)
        result = run(render_ask_missing_field(c))
        assert "vendre, acheter" in result["final_response"]

    def test_candidates_present_renders_a_list_menu_component(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            final_response=None,
            missing_fields=["unit"],
            expected_candidates=["KG", "TONNE", "SAC"],
        )
        result = run(render_ask_missing_field(c))
        assert result["ag_ui_component"]["id"] == ["ag_ui", "ListMenu"]

    def test_no_candidates_renders_a_form_input_component(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", final_response=None, missing_fields=["price"])
        result = run(render_ask_missing_field(c))
        assert result["ag_ui_component"]["id"] == ["ag_ui", "FormInputComponent"]

    def test_progress_prefixes_the_question_with_a_step_counter(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT", final_response=None,
            missing_fields=["price"],
            conversation_progress={"total": 3, "filled": 1, "remaining": ["price", "quantity"]},
        )
        result = run(render_ask_missing_field(c))
        assert result["final_response"].startswith("[2/3]")

    def test_precomputed_final_response_is_reused_without_llm_call(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", final_response="Déjà calculé")
        result = run(render_ask_missing_field(c))
        assert result["final_response"] == "Déjà calculé"

    def test_an_unknown_event_gets_an_adaptive_note_before_the_question(self):
        """Bug réel (2026-08-14) : ce nœud générique rend TOUT champ manquant
        pour TOUS les goals (appel d'offres, vente, stock...) — "Quels sont
        les prix disponibles ?" pendant une collecte de prix rejouait juste
        la question du champ suivant, en ignorant complètement la question
        posée. Voir [[precommande-architecture-consolidation-2026-08]]."""
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field

        class _Msg:
            content = "Les prix varient selon la saison, je n'ai pas de liste fixe à te donner."

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _LLM:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        return _Completion()

        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST", final_response=None,
            missing_fields=["price"], interpreted_event="OUT_OF_SCOPE",
            normalized_text="Quels sont les prix disponibles?",
        )
        runtime = type("RT", (), {"llm": _LLM(), "model_answer": "test-model"})()
        c = RenderContext(
            state=state, mc_runtime=runtime,
            strategy="ASK_MISSING_FIELD", status="", goal="PROCUREMENT_CREATE_REQUEST",
            salutation="", payload={},
        )
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field as _r
        result = run(_r(c))
        assert result["final_response"].startswith("Les prix varient selon la saison")

    def test_a_clean_answer_event_does_not_trigger_a_second_llm_call(self):
        """Non-régression : le chemin normal (event=ANSWER) appelle le LLM
        UNE seule fois (génération de la question du champ suivant) — pas
        d'appel supplémentaire de reconnaissance d'écart."""
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field

        calls = {"n": 0}

        class _Msg:
            content = "Quelle quantité ?"

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _CountingLLM:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        calls["n"] += 1
                        return _Completion()

        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT", final_response=None,
            missing_fields=["price"], interpreted_event="ANSWER",
            normalized_text="200 kg",
        )
        runtime = type("RT", (), {"llm": _CountingLLM(), "model_answer": "test-model"})()
        c = RenderContext(
            state=state, mc_runtime=runtime,
            strategy="ASK_MISSING_FIELD", status="", goal="SALES_PUBLISH_PRODUCT",
            salutation="", payload={},
        )
        result = run(render_ask_missing_field(c))
        assert result["final_response"]
        assert calls["n"] == 1


class TestGenerateLlmQuestion:
    def test_no_llm_on_runtime_returns_the_fallback(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question
        result = run(generate_llm_question(None, "SALES_PUBLISH_PRODUCT", "price", "prix", {}))
        assert "prix" in result

    def test_llm_success_returns_its_content(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question

        class _Msg:
            content = "Quel est le prix par kilo ?"

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _LLM:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        return _Completion()

        runtime = type("RT", (), {"llm": _LLM(), "model_answer": "test-model"})()
        result = run(generate_llm_question(runtime, "SALES_PUBLISH_PRODUCT", "price", "prix", {}))
        assert result == "Quel est le prix par kilo ?"

    def test_llm_exception_falls_back(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question

        class _LLM:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        raise RuntimeError("groq down")

        runtime = type("RT", (), {"llm": _LLM(), "model_answer": "test-model"})()
        result = run(generate_llm_question(runtime, "SALES_PUBLISH_PRODUCT", "price", "prix", {}))
        assert "prix" in result

    def test_last_field_hint_and_progress_context_included(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question
        captured = {}

        class _Msg:
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _LLM:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        captured["prompt"] = kwargs["messages"][0]["content"]
                        return _Completion()

        runtime = type("RT", (), {"llm": _LLM(), "model_answer": "test-model"})()
        state = {"conversation_progress": {"filled": 2, "total": 3, "remaining": ["price"]}, "user_name": "Awa"}
        run(generate_llm_question(runtime, "SALES_PUBLISH_PRODUCT", "price", "prix", {"product": "mais"}, state=state))
        assert "DERNIÈRE info" in captured["prompt"]
        assert "Awa" in captured["prompt"]


class TestRenderConfirmation:
    def test_no_goal_asks_what_to_confirm(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(current_goal=None)
        result = run(render_confirmation(c))
        assert "confirmer" in result["final_response"]

    def test_uses_precomputed_confirmation_summary(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", confirmation_summary="Vente de mais\nPrix: 250 FCFA/KG")
        result = run(render_confirmation(c))
        assert "Vente de mais" in result["final_response"]
        assert result["ag_ui_component"]["id"] == ["ag_ui", "FormConfirmation"]

    def test_builds_a_summary_when_none_precomputed(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary=None,
            transaction_payload={"product": "mais", "price": 250, "quantity": 100, "unit": "KG"},
        )
        result = run(render_confirmation(c))
        assert result["final_response"]
        assert "Confirmez-vous" in result["final_response"]
