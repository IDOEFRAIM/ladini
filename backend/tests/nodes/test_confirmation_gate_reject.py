"""`nodes/confirmation_gate.py` — décision d'approbation avant écriture DB.

Priorité à la régression production (2026-08) : un REJECT ("non") pendant la
confirmation d'un appel d'offres (PROCUREMENT_CREATE_REQUEST) effaçait tout
le brouillon (produit/quantité/prix), empêchant toute correction — le tour
suivant ("plafond 400") arrivait sans contexte et tombait sur le message
générique "je n'ai pas bien saisi". Le fix est scopé : SEUL le REJECT change
de comportement pour ce goal ; CONFIRM (et donc l'écriture réelle via
l'exécuteur générique) est totalement inchangé.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import confirmation_gate
from tests.conftest import make_state, run


def gate(**overrides):
    return run(confirmation_gate(make_state(**overrides), None))


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
    """Minimal LLM double matching the `llm.chat.completions.create(...)`
    shape `_llm_deviation_reply` expects (see tests/conftest.py::ScriptedLLM
    for the JSON-mode variant — this one just returns free text)."""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        return _Completion(self._text)


class _StubRuntimeWithLLM:
    def __init__(self, text: str) -> None:
        self.llm = _StubLLM(text)
        self.model_answer = "test-model"


class _CapturingStubLLM(_StubLLM):
    """Comme `_StubLLM`, mais garde le dernier `messages=` reçu — pour
    vérifier CE QUE le prompt contenait réellement (pas seulement ce que le
    LLM a renvoyé)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return super().create(**kwargs)


class _CapturingStubRuntimeWithLLM:
    def __init__(self, text: str) -> None:
        self.llm = _CapturingStubLLM(text)
        self.model_answer = "test-model"


# =====================================================================
# REJECT — comportement générique (goals NON dans l'allowlist)
# =====================================================================

class TestGenericRejectStillFullyResets:
    def test_reject_during_confirmation_wipes_the_goal_for_ordinary_goals(self):
        """Comportement inchangé pour tout goal hors de l'allowlist — ne pas
        régresser silencieusement les nombreux autres flows qui dépendent de
        ce reset total sur REJECT (vente, panier, stock...)."""
        result = gate(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "mais", "price": 250, "quantity": 100},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "CLARIFICATION"

    def test_stock_goal_reject_also_fully_resets(self):
        result = gate(
            current_goal="STOCK_ADJUST",
            transaction_payload={"stock_id": "s1", "quantity": 10},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}


# =====================================================================
# REJECT — PROCUREMENT_CREATE_REQUEST (brouillon préservé)
# =====================================================================

class TestProcurementRejectPreservesTheDraft:
    def test_reject_does_not_clear_current_goal(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert "current_goal" not in result, "ne doit pas être touché — préservé via merge_dict"

    def test_reject_does_not_reset_transaction_payload(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert "transaction_payload" not in result

    def test_reject_clears_only_confirmation_bookkeeping(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            confirmation_summary="ancien récap",
            interpreted_event="REJECT",
        )
        assert result["waiting_for_confirmation"] is False
        assert result["confirmation_summary"] is None
        assert result["expected_input"] == "NONE"
        assert result["status"] == "PLANNING"
        assert result["is_certified"] is False
        assert result["execution_authorized"] is False

    def test_reject_invites_a_correction_in_the_message(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert "modifier" in result["final_response"]
        assert "annuler" in result["final_response"]


# =====================================================================
# CONFIRM — chemin inchangé pour TOUS les goals, y compris procurement
# =====================================================================

class TestConfirmPathIsUntouched:
    def test_procurement_confirm_still_authorizes_execution_normally(self):
        """Le fix ne touche QUE REJECT — CONFIRM (et donc l'écriture réelle
        via l'exécuteur générique, ex. create_auction) doit rester identique."""
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="CONFIRM",
        )
        assert result["is_certified"] is True
        assert result["execution_authorized"] is True
        assert result["status"] == "EXECUTING"

    def test_first_pass_raises_a_normal_confirmation_summary(self):
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500, "unit": "TONNE"},
        )
        assert result["waiting_for_confirmation"] is True
        assert result["response_strategy"] == "CONFIRMATION"
        assert "carottes" in result["confirmation_summary"]


# =====================================================================
# BOUT-EN-BOUT — routage du tour suivant après un REJECT préservé
# =====================================================================

class TestPostRejectRoutingRoundTrip:
    """Vérifie que l'état produit par le REJECT "doux" retombe bien sur
    `buyer_request_resolver` (via `to_resolver`) au tour suivant plutôt que
    d'être court-circuité vers une réponse générique — c'est précisément le
    routage qui manquait dans le bug production."""

    def test_router_sends_the_soft_rejected_state_back_to_the_resolver(self):
        from agriconnect.graphs.agents.market_coach.core.router import get_domain_router

        soft_rejected_state = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
            working_memory={"active_goal": "PROCUREMENT_CREATE_REQUEST", "locked_intent": "PROCUREMENT_CREATE_REQUEST"},
        )
        # Simule le merge_dict du reducer : les champs non retournés par le
        # patch (current_goal, transaction_payload, working_memory) survivent
        # tels quels depuis l'état d'entrée.
        next_turn_state = {
            "current_goal": "PROCUREMENT_CREATE_REQUEST",
            "transaction_payload": {"product": "carottes", "price": 300, "quantity": 500},
            "working_memory": {"active_goal": "PROCUREMENT_CREATE_REQUEST", "locked_intent": "PROCUREMENT_CREATE_REQUEST"},
            "missing_fields": [],
            "interpreted_event": "UPDATE",  # l'utilisateur corrige le prix
            **soft_rejected_state,
        }
        router = get_domain_router()
        assert router.decide(next_turn_state) == "to_resolver"

    def test_buyer_request_resolver_re_escalates_with_a_corrected_price(self):
        """Le tour suivant (une correction de prix) doit ré-entrer dans le
        formulaire d'appel d'offres au lieu d'être traité comme un nouveau
        message sans contexte."""
        from agriconnect.graphs.agents.market_coach.flows.buyer.procurement import buyer_request_resolver
        from tests.conftest import StubRuntime

        state = make_state(
            user_phone="+2260",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            active_form="AUCTION_CREATE",
            form_data={"product": "carottes", "unit": "KG"},
            transaction_payload={"product": "carottes", "price": 50_000_000, "quantity": 500},
            working_memory={"active_goal": "PROCUREMENT_CREATE_REQUEST", "locked_intent": "PROCUREMENT_CREATE_REQUEST"},
        )
        result = run(buyer_request_resolver(state, StubRuntime()))
        assert result["active_form"] == "AUCTION_CREATE"
        assert result["transaction_payload"]["product"] == "carottes"


# =====================================================================
# NI CONFIRM NI REJECT — un écart (question/correction/remarque) ne doit
# plus faire répéter le même récap mot pour mot en boucle (bug réel
# 2026-08-14, même défaut d'adaptivité que l'onboarding avant
# [[onboarding-adaptive-questions-2026-08]]).
# =====================================================================

class TestDeviationDuringConfirmationGetsAnAdaptiveReply:
    def test_a_question_during_confirmation_gets_an_llm_generated_note(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "mais", "price": 250, "quantity": 100},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            confirmation_summary="Vente de mais — 100 kg à 250 FCFA/kg",
            interpreted_event="UNKNOWN",
            normalized_text="comment ça une nouvelle exploitation",
        )
        runtime = _StubRuntimeWithLLM("Ce récap concerne juste ta déclaration de vente, pas une nouvelle exploitation.")
        result = run(confirmation_gate(state, runtime))
        assert result["confirmation_deviation_note"] == (
            "Ce récap concerne juste ta déclaration de vente, pas une nouvelle exploitation."
        )
        # Le récap lui-même doit rester présent (pas remplacé) — l'écart
        # s'affiche EN PLUS, pas à la place.
        assert "mais" in result["confirmation_summary"]
        assert result["response_strategy"] == "CONFIRMATION"

    def test_no_llm_available_falls_back_to_the_historical_silent_repeat(self):
        """Filet de sécurité : sans client LLM (mc_runtime=None, comme dans
        tout le reste de ce fichier), on retombe sur le comportement
        historique — pas de note, juste le récap réaffiché."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "mais", "price": 250, "quantity": 100},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="UNKNOWN",
            normalized_text="comment ça",
        )
        result = run(confirmation_gate(state, None))
        assert result.get("confirmation_deviation_note") is None
        assert result["response_strategy"] == "CONFIRMATION"

    def test_the_llm_prompt_uses_the_fresh_payload_not_the_stale_stored_summary(self):
        """Bug réel confirmé (2026-08-16) : une correction ("non non c'est 35
        unité") était déjà appliquée à `transaction_payload` par les nœuds
        amont avant ce nœud, donc le récap final affiché à l'utilisateur
        reflétait bien la correction — mais la note LLM était construite à
        partir de `state["confirmation_summary"]` (le résumé STOCKÉ du tour
        PRÉCÉDENT, donc encore "35 KG"), pas du payload à jour. Le LLM
        répondait alors "pouvez-vous reformuler ?" juste avant d'afficher un
        récap DÉJÀ corrigé. Voir
        [[precommande-architecture-consolidation-2026-08]] Round 8."""
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "champignons", "quantity": 35, "unit": "UNITE", "price": 950},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            # Résumé STOCKÉ du tour précédent — volontairement PAS à jour,
            # pour prouver que le prompt LLM n'en dépend plus.
            confirmation_summary="Lancement d'un appel d'offres pour 35 KG de champignons au prix plafond de 950 FCFA/KG.",
            interpreted_event="UNKNOWN",
            normalized_text="non non c est 35 unite",
        )
        runtime = _CapturingStubRuntimeWithLLM("Noté, c'est corrigé.")
        run(confirmation_gate(state, runtime))

        prompt_text = str(runtime.llm.last_kwargs)
        assert "UNITE" in prompt_text
        assert "35 KG" not in prompt_text

    def test_render_confirmation_places_the_note_before_the_recap(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import RenderContext

        state = {
            "confirmation_summary": "Vente de mais — 100 kg à 250 FCFA/kg",
            "confirmation_deviation_note": "Je comprends ta question, laisse-moi t'expliquer.",
        }
        ctx = RenderContext(
            state=state, mc_runtime=None, strategy="CONFIRMATION", status="WAITING_CONFIRMATION",
            goal="SALES_PUBLISH_PRODUCT", payload={}, salutation="",
        )
        result = run(render_confirmation(ctx))
        text = result["final_response"]
        assert text.index("Je comprends") < text.index("Voici le récapitulatif")
