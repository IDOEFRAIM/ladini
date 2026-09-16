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
    _cart_pending_signal,
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


class TestCartPendingIsStateContextNeverAKeywordShortcut:
    """Incident réel (2026-09-13, WhatsApp production, TROIS occurrences :
    "okay", puis "je suis d'accord", puis "je valide" — tous après affichage
    du panier, `BUYER_VIEW_CART`, qui ne pose aucun `PendingInteraction` de
    type CONFIRM_ACTION, voir `services/domain/cart_service.py`).

    Deux correctifs successifs se sont révélés être la MAUVAISE approche :
    (1) `_cart_pending_signal` gardait `role_up == "BUYER"` — le RÔLE PAR
    DÉFAUT du graphe compilé pour cette conversation (`orchestrator.py`,
    dérivé du `workspace_type`/profil DB), pas le contexte réel de la
    conversation ; corrigé, le signal ne dépend plus que de l'ÉTAT
    (`active_cart` non vide + `preorder_workflow.phase == "CART"`).
    (2) une tentative d'étendre le fast-path déterministe
    (`_interpret_fast_path`) à ce signal — décision produit EXPLICITEMENT
    REJETÉE ensuite : « à vouloir tout prédire [tous les synonymes/graphies
    d'accord libre], on ne pourra pas s'en sortir ». Le vocabulaire humain
    d'accord/refus est infini ("okay", "je valide", "ça marche", "vas-y",
    "nickel"...) — coder une liste fermée pour CE signal-là est un jeu perdu
    d'avance, contrairement à CONFIRM_ACTION (`expected == "CONFIRMATION"`)
    où le fast-path reste scopé à un vocabulaire déjà documenté ET testé
    comme filet de fiabilité pour le non-déterminisme MoE de Groq — PAS une
    tentative de couvrir tous les cas.

    Architecture retenue : `_cart_pending_signal` reste calculé et transmis
    aux DEUX prompts LLM (legacy + `new_task_v2`) comme CONTEXTE D'ÉTAT
    factuel ("il y a un panier non vide en attente") — c'est le LLM, jamais
    Python, qui juge si le texte de CE tour constitue un accord ou un refus.
    `_interpret_fast_path` ne doit JAMAIS intercepter sur ce signal."""

    def _cart_pending_state(self, text: str, **overrides) -> dict:
        base = dict(
            current_goal=None,
            normalized_text=text,
            active_cart=[{"product": "oignons", "quantity": 75, "price": 225}],
            preorder_workflow={"phase": "CART"},
        )
        base.update(overrides)
        return make_state(**base)

    def test_cart_pending_signal_is_true_regardless_of_the_graph_default_role(self):
        """Le signal d'état reste correct (role_up n'existe même plus comme
        paramètre) — c'est la partie du correctif (1) qui reste valide."""
        state = self._cart_pending_state("peu importe le texte", user_role="PRODUCER")
        assert _cart_pending_signal(state) is True

    def test_cart_pending_signal_is_false_without_an_active_cart(self):
        state = self._cart_pending_state("peu importe le texte", active_cart=[])
        assert _cart_pending_signal(state) is False

    @pytest.mark.parametrize(
        "text",
        ["okay", "je suis d'accord", "je valide", "ça marche", "nickel", "vas-y"],
    )
    def test_the_fast_path_never_intercepts_on_cart_pending_alone(self, text):
        """Contrat central de ce chantier : quel que soit le texte (y
        compris un vocabulaire d'accord parfaitement clair pour un humain),
        `_interpret_fast_path` ne doit JAMAIS trancher CONFIRM/REJECT sur la
        seule base de `cart_pending` — cette décision appartient au LLM
        (`new_task_v2`, via `NewTaskPromptContext.cart_pending`), qui reçoit
        déjà ce contexte (voir `build_new_task_user_prompt`)."""
        state = self._cart_pending_state(text)
        result = _interpret_fast_path(state, text)
        assert result is None, (
            f"{text!r} a été intercepté par le fast-path déterministe alors "
            "que la décision CONFIRM/REJECT doit revenir au LLM pour ce "
            "signal (cart_pending) — voir la docstring de cette classe."
        )


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
