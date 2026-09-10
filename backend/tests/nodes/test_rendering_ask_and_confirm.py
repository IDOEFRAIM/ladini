"""Derniers trous de couverture dans `nodes/rendering/ask.py` et `confirm.py`
(le reste de ces fichiers est déjà couvert ailleurs — seules les branches
encore manquantes sont ciblées ici)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.nodes.rendering.common import RenderContext
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
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_onboarding
        c = ctx(onboarding_prompt=None)
        result = run(render_onboarding(c))
        assert "Bienvenue sur Ladini" in result["final_response"]

    def test_precomputed_prompt_is_reused(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_onboarding
        c = ctx(onboarding_prompt="Question personnalisée")
        result = run(render_onboarding(c))
        assert result["final_response"] == "Question personnalisée"


class TestRenderAskMissingField:
    def test_no_goal_asks_generic_intent_question(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(current_goal=None, final_response=None)
        result = run(render_ask_missing_field(c))
        assert "vendre, acheter" in result["final_response"]

    def test_candidates_present_renders_a_list_menu_component(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            final_response=None,
            missing_fields=["unit"],
            expected_candidates=["KG", "TONNE", "SAC"],
        )
        result = run(render_ask_missing_field(c))
        assert result["ag_ui_component"]["id"] == ["ag_ui", "ListMenu"]

    def test_no_candidates_renders_a_form_input_component(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", final_response=None, missing_fields=["price"])
        result = run(render_ask_missing_field(c))
        assert result["ag_ui_component"]["id"] == ["ag_ui", "FormInputComponent"]

    def test_progress_prefixes_the_question_with_a_step_counter(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT", final_response=None,
            missing_fields=["price"],
            conversation_progress={"total": 3, "filled": 1, "remaining": ["price", "quantity"]},
        )
        result = run(render_ask_missing_field(c))
        assert result["final_response"].startswith("[2/3]")

    def test_precomputed_final_response_is_reused_without_llm_call(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", final_response="Déjà calculé")
        result = run(render_ask_missing_field(c))
        assert result["final_response"] == "Déjà calculé"


class TestTechnicalFailureNeverFakesAnAcknowledgement:
    """Incident réel (2026-09-08, log de production) :

        User  : « j'ai 4000 poulets »   (tunnel SALES_PUBLISH_PRODUCT,
                                         champ manquant = quantité)
        Agent : « Merci pour l'information, vous avez indiqué 4000 poulets 🙏
                  […] J'ai juste besoin de quantité disponible »

    L'interpréteur était INDISPONIBLE ce tour-là (429 Groq, tous candidats
    épuisés → `routing.py` force UNKNOWN avec `raw_analysis.path="llm_crash"`
    → `unknown_reason=TECHNICAL_FAILURE`). Le « 4000 » n'a donc JAMAIS été
    extrait ni stocké — mais la note de déviation, générée par un SECOND
    appel LLM sur le texte brut, l'a poliment acquitté... juste avant que le
    formulaire redemande cette même quantité. L'agent affirmait avoir
    enregistré ce qu'il venait de perdre."""

    def _deviation_spy(self, monkeypatch, returns="NOTE_LLM_ADAPTATIVE"):
        import ladini.graphs.agents.market_coach.nodes.rendering.ask as ask_mod

        calls = []

        async def _fake_deviation(runtime, user_text, context_hint, **kw):
            calls.append(user_text)
            return returns

        monkeypatch.setattr(ask_mod, "llm_deviation_reply", _fake_deviation)
        return calls

    def test_technical_failure_says_so_honestly_and_calls_no_llm(self, monkeypatch):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import (
            render_ask_missing_field,
        )

        calls = self._deviation_spy(monkeypatch)
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            final_response=None,
            missing_fields=["quantity"],
            interpreted_event="UNKNOWN",
            unknown_reason="TECHNICAL_FAILURE",
            normalized_text="j ai 4000 poulets",
        )
        result = run(render_ask_missing_field(c))

        assert calls == [], (
            "aucun second appel LLM ne doit être tenté : la Gateway vient "
            "d'échouer sur CE tour"
        )
        assert "NOTE_LLM_ADAPTATIVE" not in result["final_response"]
        assert "n'a donc pas été pris en compte" in result["final_response"], (
            "l'agent doit dire honnêtement que le message n'a pas été traité"
        )
        assert "Merci pour l'information" not in result["final_response"]

    def test_semantic_unknown_still_gets_its_adaptive_note(self, monkeypatch):
        """Non-régression : quand le LLM a RÉELLEMENT tourné et n'a
        sincèrement pas su classer le message, la note adaptative reste le
        bon comportement — c'est le cas pour lequel ce bloc existe."""
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import (
            render_ask_missing_field,
        )

        calls = self._deviation_spy(monkeypatch)
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            final_response=None,
            missing_fields=["quantity"],
            interpreted_event="UNKNOWN",
            unknown_reason="AMBIGUOUS",
            normalized_text="vous me tiendrez informé ?",
        )
        result = run(render_ask_missing_field(c))

        assert calls == ["vous me tiendrez informé ?"]
        assert "NOTE_LLM_ADAPTATIVE" in result["final_response"]
        assert "pas été pris en compte" not in result["final_response"]

    def test_an_unknown_event_gets_an_adaptive_note_before_the_question(self):
        """Bug réel (2026-08-14) : ce nœud générique rend TOUT champ manquant
        pour TOUS les goals (appel d'offres, vente, stock...) — "Quels sont
        les prix disponibles ?" pendant une collecte de prix rejouait juste
        la question du champ suivant, en ignorant complètement la question
        posée. Voir [[precommande-architecture-consolidation-2026-08]]."""
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field

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
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field as _r
        result = run(_r(c))
        assert result["final_response"].startswith("Les prix varient selon la saison")

    def test_a_clean_answer_event_does_not_trigger_a_second_llm_call(self):
        """Non-régression : le chemin normal (event=ANSWER) appelle le LLM
        UNE seule fois (génération de la question du champ suivant) — pas
        d'appel supplémentaire de reconnaissance d'écart."""
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import render_ask_missing_field

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
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question
        result = run(generate_llm_question(None, "SALES_PUBLISH_PRODUCT", "price", "prix", {}))
        assert "prix" in result

    def test_llm_success_returns_its_content(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question

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
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question

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
        from ladini.graphs.agents.market_coach.nodes.rendering.ask import generate_llm_question
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


def _confirm_pending(goal):
    """Miroir de ce que `confirmation_gate.py` pose réellement (voir
    `core/pending_interaction.py::set_pending_interaction`) — depuis la
    refonte 2026-09-02, `render_confirmation` n'affiche plus RIEN sans ce
    signal canonique, même si `confirmation_summary`/`current_goal` sont
    renseignés (§ Invariant 2 : "CONFIRM_ACTION implique un contexte
    cohérent", vérifié par `check_invariants`)."""
    from ladini.graphs.agents.market_coach.core.pending_interaction import (
        InteractionKind,
        set_pending_interaction,
    )

    return set_pending_interaction(
        InteractionKind.CONFIRM_ACTION, goal=goal, context_ref="confirmation"
    )["pending_interaction"]


class TestRenderConfirmation:
    def test_no_goal_asks_what_to_confirm(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(current_goal=None)
        result = run(render_confirmation(c))
        assert "récapitulatif" in result["final_response"]

    def test_no_pending_confirmation_is_never_rendered_even_with_a_goal_and_summary(self):
        """(2026-09-02, G-1) Invariant 6 du mandat refonte : impossible
        d'avoir pending_interaction=NONE ET un rendu de confirmation
        affiché — même si goal/confirmation_summary sont renseignés (ex:
        périmés d'un tour antérieur), sans le signal canonique CONFIRM_ACTION
        ce rendu ne doit JAMAIS afficher le récap tel quel."""
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary="Vente de mais\nPrix: 250 FCFA/KG",
            confirmation_summary_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary_payload={"product": "mais"},
            transaction_payload={"product": "mais"},
            # pending_interaction volontairement absent.
        )
        result = run(render_confirmation(c))
        assert "Vente de mais" not in result["final_response"]
        assert result["response_strategy"] == "CLARIFICATION"

    def test_uses_precomputed_confirmation_summary(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        payload = {"product": "mais"}
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary="Vente de mais\nPrix: 250 FCFA/KG",
            confirmation_summary_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary_payload=payload,
            transaction_payload=payload,
            pending_interaction=_confirm_pending("SALES_PUBLISH_PRODUCT"),
        )
        result = run(render_confirmation(c))
        assert "Vente de mais" in result["final_response"]
        assert result["ag_ui_component"]["id"] == ["ag_ui", "QuickReplies"]
        buttons = result["ag_ui_component"]["kwargs"]["buttons"]
        assert [b["id"] for b in buttons] == ["CONFIRM", "REJECT"]

    def test_builds_a_summary_when_none_precomputed(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary=None,
            transaction_payload={"product": "mais", "price": 250, "quantity": 100, "unit": "KG"},
            pending_interaction=_confirm_pending("SALES_PUBLISH_PRODUCT"),
        )
        result = run(render_confirmation(c))
        assert result["final_response"]
        assert "Confirmez-vous" in result["final_response"]

    def test_preorder_recap_is_rendered_from_the_draft_not_the_generic_fallback(self):
        """Incident réel (2026-09-09) : « je veux 50 kg » → panier → « okay »
        affichait « Voici le récapitulatif :\\nConfirmez-vous cette opération ?
        \\n\\nConfirmez-vous ? » — aucun détail. `build_confirmation_summary`
        n'a pas de gabarit `BUYER_PREORDER_*` et le récap vit dans
        `PreorderDraft`, pas dans `transaction_payload`. `render_confirmation`
        doit rendre `draft.render_summary()`."""
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        draft = {
            "draft_id": "d-1", "version": 1, "status": "DRAFT",
            "items": [{
                "name": "riz", "quantity": 50.0, "unit": "KG",
                "price": 20000.0, "producer_id": "p-1",
            }],
            "total_amount": 1000000.0, "currency": "XOF",
        }
        c = ctx(
            current_goal="BUYER_PREORDER_INIT",
            preorder_draft=draft,
            transaction_payload={},
            pending_interaction=_confirm_pending("BUYER_PREORDER_INIT"),
        )
        result = run(render_confirmation(c))
        fr = result["final_response"]
        assert "riz" in fr
        assert "50" in fr and "KG" in fr
        assert "1000000" in fr
        assert "Confirmez-vous cette opération ?" not in fr  # pas le fallback vide
        assert result["ag_ui_component"]["id"] == ["ag_ui", "QuickReplies"]

    def test_a_stale_summary_from_a_different_payload_is_rejected_and_rebuilt(self):
        """Incident réel (2026-08-27) : un acheteur commandait des chèvres,
        mais la confirmation affichée parlait de "35 KG de champignons" — un
        résumé PÉRIMÉ d'une tentative abandonnée plus tôt, jamais invalidé
        car le GOAL (BUYER_PREORDER_INIT) était identique pour les deux
        tentatives (seul le produit changeait). Le résumé stocké ne doit
        être honoré que si le payload ayant servi à le construire correspond
        EXACTEMENT au payload courant."""
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(
            current_goal="BUYER_PREORDER_INIT",
            confirmation_summary="Confirmez-vous cette opération pour 35 KG de *champignons* ?",
            confirmation_summary_goal="BUYER_PREORDER_INIT",
            confirmation_summary_payload={"product": "champignons", "quantity": 35, "unit": "KG"},
            transaction_payload={"product": "chevres", "quantity": 21, "unit": "UNITE", "price": 55000},
            pending_interaction=_confirm_pending("BUYER_PREORDER_INIT"),
        )
        result = run(render_confirmation(c))
        assert "champignons" not in result["final_response"]
        assert "chevres" in result["final_response"]

    def test_a_stale_summary_with_a_different_goal_is_also_rejected(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        c = ctx(
            current_goal="SALES_PUBLISH_PRODUCT",
            confirmation_summary="Vente de mais\nPrix: 250 FCFA/KG",
            confirmation_summary_goal="PROCUREMENT_CREATE_REQUEST",
            confirmation_summary_payload={"product": "mais"},
            transaction_payload={"product": "riz", "price": 300, "quantity": 50, "unit": "KG"},
            pending_interaction=_confirm_pending("SALES_PUBLISH_PRODUCT"),
        )
        result = run(render_confirmation(c))
        assert "mais" not in result["final_response"]
        assert "riz" in result["final_response"]
