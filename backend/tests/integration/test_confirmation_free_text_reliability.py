"""Validation réelle post-refonte (2026-09-02) — incident rapporté : un
utilisateur répondant "okay" à un récapitulatif de précommande recevait le
récap répété au lieu d'être confirmé ; "je confirme" fonctionnait. Root
cause : le LLM est la source PRIMAIRE de classification CONFIRM/REJECT pour
du texte libre, et son non-déterminisme connu (même phrase, même
température=0.0, verdict qui varie d'un appel Groq à l'autre — voir mémoire
projet) en faisait une fiabilité insuffisante pour LE tour le plus critique
de la conversation (validation d'une commande).

Fix : `interpreter/routing.py::_interpret_fast_path` porte désormais un
filet déterministe — vocabulaire FERMÉ, égalité stricte sur texte normalisé,
repris mot pour mot du prompt LLM lui-même (§ CONFIRM/REJECT) — donc pas une
"nouvelle règle" mais une garantie que ce que le prompt promet déjà au LLM
est vrai à 100% pour ce vocabulaire précis. Gated sur
`PendingInteraction.kind == CONFIRM_ACTION` (source canonique), jamais sur
`expected_input` directement (mandat de refonte antérieur, "no legacy
shim")."""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _interpret_fast_path,
)
from tests.conftest import make_state


def _confirmation_state(text: str) -> dict:
    return make_state(
        current_goal="BUYER_PREORDER_INIT",
        normalized_text=text,
        confirmation_summary="Récapitulatif : 50 kg de mais — 25000 FCFA",
        transaction_payload={"product": "mais", "quantity": 50, "price": 500},
        **set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, context_ref="confirmation"
        ),
    )


class TestFreeTextConfirmationIsDeterministic:
    @pytest.mark.parametrize(
        "text",
        [
            "okay", "Okay", "OKAY", "oui", "Oui", "ok", "d'accord", "daccord",
            "je confirme", "je suis d'accord", "c'est bon", "parfait",
            "oui.", "oui !", "  oui  ",
        ],
    )
    def test_a_confirmation_word_always_resolves_to_confirm(self, text):
        state = _confirmation_state(text)
        result = _interpret_fast_path(state, text)
        assert result is not None, (
            f"{text!r} pendant CONFIRM_ACTION doit être résolu déterministe, "
            "pas laissé au LLM (non-déterminisme MoE connu)"
        )
        assert result["interpreted_event"] == "CONFIRM"
        assert result["interpreter_confidence"] >= 0.9

    @pytest.mark.parametrize("text", ["non", "annule", "stop", "je refuse"])
    def test_a_rejection_word_always_resolves_to_reject(self, text):
        state = _confirmation_state(text)
        result = _interpret_fast_path(state, text)
        assert result is not None
        assert result["interpreted_event"] == "REJECT"

    def test_a_real_deviation_is_not_swallowed_by_the_fast_path(self):
        """Non-régression : une vraie question/déviation pendant la
        confirmation continue d'être laissée au LLM (le filet ne couvre QUE
        le vocabulaire fermé, jamais une phrase plus longue ou ambiguë)."""
        state = _confirmation_state("attends c'est combien déjà le prix ?")
        result = _interpret_fast_path(state, "attends c'est combien déjà le prix ?")
        assert result is None

    def test_the_word_confirmation_alone_does_not_falsely_match_a_longer_sentence(self):
        """Garde-fou anti-faux-positif : "oui" DANS une phrase plus longue
        (ex: correction, hésitation) ne doit jamais matcher — seulement le
        texte normalisé EXACT."""
        state = _confirmation_state("oui mais en fait je voudrais changer la quantité")
        result = _interpret_fast_path(
            state, "oui mais en fait je voudrais changer la quantité"
        )
        assert result is None

    def test_outside_confirm_action_the_same_words_are_not_intercepted(self):
        """Le filet est strictement scopé à CONFIRM_ACTION — "oui" pendant un
        tout autre pending_interaction (ex: un champ générique en attente)
        ne doit jamais être détourné vers CONFIRM."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            normalized_text="oui",
            **set_pending_interaction(
                InteractionKind.ENTER_FIELD, field_name="price"
            ),
        )
        result = _interpret_fast_path(state, "oui")
        assert result is None


class TestConfirmationWordsAreContextOwned:
    """Mandat de nettoyage final §14 : "oui" ne doit JAMAIS devenir
    CONFIRM_ACTION indépendamment du contexte — le contexte
    (`PendingInteraction`) reste seul propriétaire du sens. Une tier-tunnel
    ou une étape GPS active ont chacune leur PROPRE résolution pour "oui" —
    ce filet ne doit ni les court-circuiter, ni y être actif du tout."""

    @pytest.mark.parametrize("word", ["oui", "okay", "d'accord", "je confirme"])
    def test_confirmation_words_are_never_intercepted_during_pricing_tier_selection(
        self, word
    ):
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text=word,
            tier_selection_context={
                "tiers": [
                    {"tier_id": "t5", "quantity": 5, "unit": "LITRE", "price": 2500},
                    {"tier_id": "t10", "quantity": 10, "unit": "LITRE", "price": 4500},
                ]
            },
        )
        result = _interpret_fast_path(state, word)
        assert result is None or result["interpreted_event"] != "CONFIRM"

    @pytest.mark.parametrize("word", ["oui", "okay", "d'accord", "je confirme"])
    def test_confirmation_words_are_never_intercepted_while_entering_a_quantity(
        self, word
    ):
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text=word,
            **set_pending_interaction(
                InteractionKind.ENTER_QUANTITY, field_name="quantity"
            ),
        )
        result = _interpret_fast_path(state, word)
        assert result is None or result["interpreted_event"] != "CONFIRM"

    @pytest.mark.parametrize("word", ["oui", "okay", "d'accord", "je confirme"])
    def test_confirmation_words_are_never_intercepted_during_provide_location(
        self, word
    ):
        """PROVIDE_LOCATION a sa PROPRE lecture de "oui" (accepter le point
        GPS habituel — voir gps_delivery_gate.py::resolve_gps_stage,
        `is_yes`) : le filet CONFIRM_ACTION ne doit jamais interférer."""
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            normalized_text=word,
            **set_pending_interaction(
                InteractionKind.PROVIDE_LOCATION, context_ref="confirmation"
            ),
        )
        result = _interpret_fast_path(state, word)
        assert result is None or result["interpreted_event"] != "CONFIRM"


class TestFastPathSharesTheExactSameContractAsTheLlm:
    """Mandat de nettoyage final §12-13 : le fast-path ne doit PAS
    directement muter `PendingInteraction`/`ConversationState` — il doit
    produire la MÊME forme qu'un résultat LLM, passée ensuite par LE MÊME
    point de conversion (`InterpreterResult.from_legacy_dict`)."""

    def test_the_fast_path_dict_never_touches_pending_interaction_or_state_keys(self):
        state = _confirmation_state("oui")
        result = _interpret_fast_path(state, "oui")
        assert result is not None
        # Seules les clés de RÉSULTAT D'INTERPRÉTATION — jamais une mutation
        # directe de l'état conversationnel (pending_interaction, working_memory,
        # current_goal, transaction_payload...).
        assert set(result.keys()) <= {
            "interpreted_event",
            "detected_intent",
            "interpreter_confidence",
            "extracted_entities",
            "raw_analysis",
        }

    def test_the_fast_path_result_converts_through_interpreter_result_identically_to_an_llm_dict(
        self,
    ):
        from ladini.graphs.agents.market_coach.interpreter.interpreter_result import (
            InterpreterResult,
        )

        state = _confirmation_state("oui")
        fast_raw = _interpret_fast_path(state, "oui")
        llm_raw = {
            "interpreted_event": "CONFIRM",
            "detected_intent": "BUYER_PREORDER_INIT",
            "interpreter_confidence": 0.87,
            "extracted_entities": {},
            "raw_analysis": {"path": "llm"},
        }
        fast_patch = InterpreterResult.from_legacy_dict(fast_raw).to_state_patch()
        llm_patch = InterpreterResult.from_legacy_dict(llm_raw).to_state_patch()
        assert set(fast_patch.keys()) == set(llm_patch.keys())
        assert fast_patch["interpreted_event"] == llm_patch["interpreted_event"] == "CONFIRM"
