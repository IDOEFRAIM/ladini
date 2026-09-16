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
