"""`input_interpreter` — garanties de performance (clôture Bloc 1, mandat
§33) :

    - une interaction STRUCTURÉE (clic bouton/liste WhatsApp, réponse
      numérique/confirmation déterministe) coûte ZÉRO appel LLM ;
    - un texte libre qui exige une vraie interprétation coûte AU PLUS UN
      appel LLM ;
    - `unknown_reason=TECHNICAL_FAILURE` ne déclenche jamais un second
      appel Gateway dans `clarification_node` (déjà verrouillé par
      `tests/unit/test_clarification_node.py::
      TestTechnicalFailureSkipsASecondWastedLlmCall` — non dupliqué ici).

Utilise `ForbiddenLLM` (lève si appelé) pour les chemins qui NE DOIVENT
JAMAIS toucher le LLM, et un compteur d'appels pour le chemin texte libre.
"""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run


class TestStructuredInteractionsAreZeroToken:
    @pytest.mark.parametrize("payload,expected_event", [
        ("CONFIRM", "CONFIRM"),
        ("REJECT", "REJECT"),
        ("2", "SELECTION"),
        ("some-uuid-value", "SELECTION"),
    ])
    def test_interactive_selection_never_calls_the_llm(self, payload, expected_event):
        """Un clic bouton/liste WhatsApp (`interactive_selection`) est déjà
        désambiguïsé côté client — zéro token, quel que soit le rôle."""
        interp = make_input_interpreter("BUYER")
        state = make_state(interactive_selection=payload, user_role="BUYER")
        rt = StubRuntime(llm=ForbiddenLLM())
        result = run(interp(state, rt))
        assert result["interpreted_event"] == expected_event

    @pytest.mark.parametrize("text,expected_input,extra", [
        ("oui", "CONFIRMATION", {}),
        ("non", "CONFIRMATION", {}),
        ("2", "SELECTION", {"expected_candidates": ["Option A", "Option B"]}),
        ("200 kg", "QUANTITY", {}),
        ("250 fcfa", "PRICE", {}),
    ])
    def test_deterministic_free_text_fast_path_never_calls_the_llm(
        self, text, expected_input, extra
    ):
        """Un message texte encore déterministe (confirmation par mot-clé,
        index numérique, réponse chiffrée à un slot) est résolu par le
        fast-path AVANT tout appel LLM — même avec un LLM disponible sur le
        runtime (le fast-path a priorité, voir `interpreter/routing.py`)."""
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text=text,
            expected_input=expected_input,
            user_role="BUYER",
            **extra,
        )
        rt = StubRuntime(llm=ForbiddenLLM())
        result = run(interp(state, rt))
        assert result["interpreted_event"] != "UNKNOWN" or expected_input == "NONE"


class TestUpdateFieldTunnelIsZeroTokenWhenDeterministicallyParseable:
    """(2026-09-19, incident réel) — `field_name="update_field"`
    (SALES_UPDATE_PRODUCT / PRODUCTION_UPDATE_FUTURE /
    PROCUREMENT_UPDATE_REQUEST : "quel champ modifier ?") résout à la
    catégorie "UPDATE_FIELD", absente de `SLOT_FILLING_INPUTS` — elle ne
    route donc JAMAIS vers le micro-prompt spécialisé ACTIVE_SLOT
    (`state_router.py`), et retombait entièrement sur la classification LLM
    générique (NEW_TASK), qui a réellement classé "prix 485000 fcfa et la
    quantite est maintenant de 95" comme UNKNOWN en production — déclenchant
    `cognitive_guard` (accusé de réception SANS appliquer la correction),
    puis, au message suivant, l'abandon complet du tunnel (contexte perdu,
    réponse générique "je n'ai pas compris"). `_fast_path_update_field_
    correction` intercepte maintenant ce cas AVANT tout appel LLM."""

    def test_the_exact_incident_message_is_resolved_without_any_llm_call(self):
        """Message RÉEL de l'incident : correction combinée prix+quantité."""
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="prix 485000 fcfa et la quantite est maintenant de 95",
            expected_input="UPDATE_FIELD",
            current_goal="SALES_UPDATE_PRODUCT",
            user_role="PRODUCER",
        )
        rt = StubRuntime(llm=ForbiddenLLM())
        result = run(interp(state, rt))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"]["price"] == 485000.0
        assert result["extracted_entities"]["quantity"] == 95.0

    def test_the_follow_up_bare_price_message_is_also_resolved_without_llm(self):
        """2e tour de l'incident : une fois le contexte perdu en prod, ce
        message — pourtant dans le format d'exemple donné PAR LE BOT
        lui-même ("prix 400") — tombait en UNKNOWN générique. Doit
        maintenant rester une simple ANSWER déterministe, tunnel préservé."""
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="prix 485000 fcfa",
            expected_input="UPDATE_FIELD",
            current_goal="SALES_UPDATE_PRODUCT",
            user_role="PRODUCER",
        )
        rt = StubRuntime(llm=ForbiddenLLM())
        result = run(interp(state, rt))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"]["price"] == 485000.0

    def test_production_cycle_update_variant_is_also_covered(self):
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="quantite 500 kg",
            expected_input="UPDATE_FIELD",
            current_goal="PRODUCTION_UPDATE_FUTURE",
            user_role="PRODUCER",
        )
        rt = StubRuntime(llm=ForbiddenLLM())
        result = run(interp(state, rt))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"]["quantity"] == 500.0

    def test_buyer_procurement_request_update_variant_is_also_covered(self):
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text="prix 5000",
            expected_input="UPDATE_FIELD",
            current_goal="PROCUREMENT_UPDATE_REQUEST",
            user_role="BUYER",
        )
        rt = StubRuntime(llm=ForbiddenLLM())
        result = run(interp(state, rt))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"]["price"] == 5000.0

    def test_a_precise_pricing_tier_correction_still_goes_through_the_llm(self):
        """Garde de sécurité : ce fast-path n'a pas accès aux tarifs RÉELS du
        produit (`current_tiers`, chargés en DB par flow.py) — une
        correction qui a la FORME d'un palier précis doit continuer à
        passer par le chemin existant (ici : classification LLM), jamais
        être devinée comme un prix scalaire plat (régression du bug déjà
        corrigé le 2026-09-14 côté flow.py)."""
        interp = make_input_interpreter("PRODUCER")
        llm = ScriptedLLM({
            "disposition": "UPDATE",
            "intent": "SALES_UPDATE_PRODUCT",
            "confidence": 0.9,
            "entities": {"pricing_tiers": [{"quantity": 20, "unit": "L", "price": 70000}]},
        })
        rt = StubRuntime(llm=llm)
        state = make_state(
            normalized_text="prix bidon de 20 L a 70000 fcfa",
            expected_input="UPDATE_FIELD",
            current_goal="SALES_UPDATE_PRODUCT",
            user_role="PRODUCER",
        )
        run(interp(state, rt))
        assert llm.calls >= 1, "une correction de palier précis doit toujours consulter le LLM, pas être devinée"

    def test_an_unrecognized_goal_sharing_the_update_field_sentinel_falls_back_unchanged(self):
        """Un futur mini-flow qui réutiliserait `field_name="update_field"`
        sans être câblé dans `_fast_path_update_field_correction` ne doit
        jamais planter — juste retomber sur le comportement existant."""
        interp = make_input_interpreter("PRODUCER")
        llm = ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "UNKNOWN",
            "confidence": 0.5,
            "entities": {},
        })
        rt = StubRuntime(llm=llm)
        state = make_state(
            normalized_text="prix 400",
            expected_input="UPDATE_FIELD",
            current_goal="SOME_FUTURE_UPDATE_GOAL",
            user_role="PRODUCER",
        )
        result = run(interp(state, rt))
        assert result is not None  # ne doit jamais lever


class TestFreeTextCostsAtMostOneLlmCall:
    def test_a_genuine_free_text_message_calls_the_llm_exactly_once(self):
        interp = make_input_interpreter("PRODUCER")
        llm = ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.95,
            "entities": {"product": "mais"},
        })
        rt = StubRuntime(llm=llm)
        state = make_state(
            normalized_text="je voudrais mettre en vente du mais que j'ai recolte",
            expected_input="NONE",
            user_role="PRODUCER",
        )
        result = run(interp(state, rt))
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert llm.calls == 1

    def test_llm_outage_falls_back_to_the_legacy_interpreter_once(self):
        """(2026-09-13, Incrément F) : ce test documentait auparavant "UNE
        panne = UN SEUL essai" quand NEW_TASK n'avait pas encore de
        micro-prompt propre — la route NEW_TASK utilisait alors DIRECTEMENT
        l'interpréteur unifié legacy, sans personne en amont vers qui
        retomber. Depuis `new_task_micro.py`, une panne infrastructurelle du
        micro-prompt (spec §57) retombe explicitement sur ce MÊME
        interpréteur unifié legacy comme filet de sécurité — exactement le
        même principe déjà appliqué à SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION
        (voir `test_selection_microprompt.py::
        test_legacy_fallback_is_traced_in_the_gateway_extra_metadata`). Un
        total-outage Gateway coûte donc désormais 2 tentatives (micro-prompt
        + legacy), toutes deux ratées, jamais une boucle locale de retry —
        chaque tentative reste un essai UNIQUE, pas de ré-essai en interne."""
        interp = make_input_interpreter("PRODUCER")

        class _CrashingLLM:
            def __init__(self):
                self.calls = 0

            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kwargs):
                self.calls += 1
                raise RuntimeError("gateway down")

        llm = _CrashingLLM()
        rt = StubRuntime(llm=llm)
        state = make_state(
            normalized_text="un message quelconque",
            expected_input="NONE",
            user_role="PRODUCER",
        )
        result = run(interp(state, rt))
        assert result["interpreted_event"] == "UNKNOWN"
        assert llm.calls == 2
