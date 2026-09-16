"""`nodes/confirmation_gate.py` — décision d'approbation avant écriture DB.

Historique (2026-08) : un REJECT ("non") pendant la confirmation d'un appel
d'offres (PROCUREMENT_CREATE_REQUEST) effaçait tout le brouillon
(produit/quantité/prix), empêchant toute correction. Corrigé une 1re fois de
façon scopée (préserver `transaction_payload` pour ce seul goal), puis
remplacé de fond en comble (2026-09-03, refonte transactionnelle) : ce goal
délègue désormais entièrement à un `ProcurementDraft` canonique et versionné
(domain/procurement_draft.py) — voir `TestProcurementRejectPreservesTheDraft`
et `TestConfirmPathAuthorizesExecution` ci-dessous pour le comportement
actuel. Idem pour `SALES_PUBLISH_PRODUCT` depuis 2026-09-04 (migration
SALES, `domain/sales_publish_draft.py`). Les goals PAS ENCORE migrés
(`SALES_UPDATE_PRODUCT`, panier, stock...) restent inchangés, couverts par
`TestGenericRejectStillFullyResets`.
"""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
)
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import confirmation_gate
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
            current_goal="SALES_UPDATE_PRODUCT",
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
            current_goal="STOCK_REGISTER_HARVEST",
            transaction_payload={"product": "mais", "quantity": 10},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
        )
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}


# =====================================================================
# REJECT — PROCUREMENT_CREATE_REQUEST (2026-09-03, refonte transactionnelle)
#
# `confirmation_gate` ne gère plus ce goal via le mécanisme générique
# confirmation_summary/transaction_payload — il DÉLÈGUE entièrement à
# `domain/procurement_draft.py` (voir `_DRAFT_BASED_CONFIRMATION_GOALS` dans
# nodes/confirmation_gate.py). Le brouillon est maintenant un
# `ProcurementDraft` canonique, versionné : un REJECT pendant la
# confirmation ("non") ne touche JAMAIS aux champs du draft — seule la
# cible de confirmation (`PendingInteraction.target`) tombe, invitant une
# correction. Remplace les anciens tests qui vérifiaient la préservation de
# `transaction_payload` par le mécanisme générique (root cause exacte de
# l'incident `quantity_display` figé : deux représentations indépendantes
# du même fait métier — voir domain/procurement_draft.py, docstring).
# =====================================================================

class TestProcurementRejectPreservesTheDraft:
    def _draft_confirmation_state(self, **overrides):
        draft = ProcurementDraft.new(
            draft_id="d1", product="carottes", quantity=500, unit="KG", price=300
        )
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=draft.to_dict(),
            interpreted_event="REJECT",
            **overrides,
        )
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": draft.draft_id, "draft_version": draft.version},
        }
        return state, draft

    def test_reject_does_not_clear_current_goal(self):
        state, _ = self._draft_confirmation_state()
        result = run(confirmation_gate(state, None))
        assert "current_goal" not in result, "ne doit pas être touché — préservé via merge_dict"

    def test_reject_preserves_every_draft_field_unchanged(self):
        state, draft = self._draft_confirmation_state()
        result = run(confirmation_gate(state, None))
        preserved = ProcurementDraft.from_dict(result["procurement_draft"])
        assert preserved.product == draft.product
        assert preserved.quantity == draft.quantity
        assert preserved.price == draft.price
        assert preserved.version == draft.version, "un REJECT ne bump jamais la version"

    def test_reject_clears_only_the_confirmation_target(self):
        state, _ = self._draft_confirmation_state()
        result = run(confirmation_gate(state, None))
        assert to_tunnel_category(get_pending_interaction(result)) == "NONE"
        assert result["status"] == "PLANNING"

    def test_reject_invites_a_correction_in_the_message(self):
        state, _ = self._draft_confirmation_state()
        result = run(confirmation_gate(state, None))
        assert "modifier" in result["final_response"]
        assert "annuler" in result["final_response"]


# =====================================================================
# CONFIRM — draft canonique, y compris la création de la 1re version
# =====================================================================

class TestConfirmPathAuthorizesExecution:
    def test_procurement_confirm_authorizes_execution(self, monkeypatch):
        # `claim_once` réel tape Redis (SET-NX) — un draft_id/version déjà
        # réclamé par un run de test antérieur (ou un autre test de ce
        # fichier) y apparaîtrait comme déjà confirmé. Forcé à toujours
        # gagner ici : ce test vérifie la TRANSITION D'ÉTAT du nœud, pas le
        # claim lui-même (voir tests/architecture/
        # test_procurement_draft_transactional_contract.py pour
        # l'idempotence/la concurrence, qui contrôlent `claim` explicitement).
        import ladini.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)
        draft = ProcurementDraft.new(
            draft_id="d1", product="carottes", quantity=500, unit="KG", price=300
        )
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=draft.to_dict(),
            interpreted_event="CONFIRM",
        )
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": draft.draft_id, "draft_version": draft.version},
        }
        result = run(confirmation_gate(state, None))
        assert result["is_certified"] is True
        assert result["execution_authorized"] is True
        assert result["status"] == "EXECUTING"
        assert result["transaction_payload"]["product"] == "carottes"

    def test_bootstrap_creates_draft_v1_from_the_completed_collection_payload(self):
        """1re fois que ce goal atteint ce nœud, `transaction_payload`
        complet, aucun `procurement_draft` encore posé : la v1 canonique est
        créée ICI — remplace l'ancien `_build_confirmation_summary(goal,
        payload)` générique pour ce goal précis."""
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500, "unit": "TONNE"},
        )
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"
        assert result["response_strategy"] == "CONFIRMATION"
        assert "carottes" in result["final_response"]
        draft = ProcurementDraft.from_dict(result["procurement_draft"])
        assert draft.version == 1
        assert draft.product == "carottes"

    def test_bootstrap_is_a_no_op_while_fields_are_still_missing(self):
        """Tant que la collecte n'est pas complète, ce nœud ne crée aucun
        draft — le mécanisme générique de collecte (validator/
        ASK_MISSING_FIELD) gère ce tour exactement comme avant. Seule
        exception (2026-09-03, chaos I1/I2) : `execution_authorized`/
        `is_certified` sont explicitement forcés à False dans le patch —
        défense contre un état amont hostile qui les aurait forgés à True
        pendant que la collecte est encore en cours (voir
        `tests/chaos/test_state_machine_invariants.py::
        test_no_write_executes_without_explicit_confirm_draft_based_goal`)."""
        result = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes"},  # price/quantity/unit manquants
        )
        assert result == {"execution_authorized": False, "is_certified": False}

    def test_the_rendered_summary_can_never_be_stale_by_construction(self):
        """Remplace l'ancien test de régression 'le prompt LLM utilise le
        payload frais, pas le résumé stocké périmé' : ce bug de classe
        entière est désormais IMPOSSIBLE pour ce goal — il n'existe plus
        de `confirmation_summary` stocké séparément à désynchroniser,
        `render_summary()` est TOUJOURS recalculé depuis le draft courant
        (mandat §8 : « confirmation_summary = render(draft) »)."""
        draft_v1 = ProcurementDraft.new(
            draft_id="d1", product="champignons", quantity=35, unit="UNITE", price=950
        )
        draft_v2 = draft_v1.with_updates(quantity=40)
        assert draft_v1.render_summary() != draft_v2.render_summary()
        assert "35" in draft_v1.render_summary()
        assert "40" in draft_v2.render_summary()
        assert "35" not in draft_v2.render_summary()


# =====================================================================
# BOUT-EN-BOUT — routage du tour suivant après un REJECT préservé
# =====================================================================

class TestPostRejectRoutingRoundTrip:
    """Vérifie que l'état produit par le REJECT "doux" retombe bien sur
    `buyer_request_resolver` (via `to_resolver`) au tour suivant plutôt que
    d'être court-circuité vers une réponse générique — c'est précisément le
    routage qui manquait dans le bug production."""

    def test_router_sends_the_soft_rejected_state_back_to_the_resolver(self):
        from ladini.graphs.agents.market_coach.core.router import get_domain_router

        soft_rejected_state = gate(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={"product": "carottes", "price": 300, "quantity": 500},
            expected_input="CONFIRMATION",
            waiting_for_confirmation=True,
            interpreted_event="REJECT",
            working_memory={"active_goal": "PROCUREMENT_CREATE_REQUEST"},
        )
        # Simule le merge_dict du reducer : les champs non retournés par le
        # patch (current_goal, transaction_payload, working_memory) survivent
        # tels quels depuis l'état d'entrée.
        next_turn_state = {
            "current_goal": "PROCUREMENT_CREATE_REQUEST",
            "transaction_payload": {"product": "carottes", "price": 300, "quantity": 500},
            "working_memory": {"active_goal": "PROCUREMENT_CREATE_REQUEST"},
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
        from ladini.graphs.agents.market_coach.flows.buyer.procurement import buyer_request_resolver
        from tests.conftest import StubRuntime

        state = make_state(
            user_phone="+2260",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            active_form="AUCTION_CREATE",
            form_data={"product": "carottes", "unit": "KG"},
            transaction_payload={"product": "carottes", "price": 50_000_000, "quantity": 500},
            working_memory={"active_goal": "PROCUREMENT_CREATE_REQUEST"},
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
            current_goal="SALES_UPDATE_PRODUCT",
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
            current_goal="SALES_UPDATE_PRODUCT",
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
        [[precommande-architecture-consolidation-2026-08]] Round 8.

        (2026-09-03) : ce goal était `PROCUREMENT_CREATE_REQUEST` à
        l'origine — migré vers `SALES_PUBLISH_PRODUCT` (même mécanisme
        générique confirmation_summary/transaction_payload) car
        PROCUREMENT_CREATE_REQUEST délègue désormais à un
        `ProcurementDraft` canonique où cette classe de bug est devenue
        structurellement impossible.

        (2026-09-04, migration SALES) : re-migré vers `SALES_UPDATE_PRODUCT`
        — `SALES_PUBLISH_PRODUCT` délègue À SON TOUR désormais à un
        `SalesPublishDraft` canonique (même garantie structurelle que
        PROCUREMENT, voir `test_sales_publish_draft_anti_regression.py`) —
        ce test-ci continue de verrouiller la garantie pour le mécanisme
        générique, toujours utilisé par les goals pas encore migrés
        (`SALES_UPDATE_PRODUCT`, `PRODUCTION_DECLARE_FUTURE`, panier, stock...)."""
        state = make_state(
            current_goal="SALES_UPDATE_PRODUCT",
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
        from ladini.graphs.agents.market_coach.nodes.rendering.confirm import render_confirmation
        from ladini.graphs.agents.market_coach.nodes.rendering.common import RenderContext
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )

        state = {
            "confirmation_summary": "Vente de mais — 100 kg à 250 FCFA/kg",
            "confirmation_summary_goal": "SALES_UPDATE_PRODUCT",
            "confirmation_summary_payload": {},
            "confirmation_deviation_note": "Je comprends ta question, laisse-moi t'expliquer.",
            # (2026-09-02) Signal canonique requis par render_confirmation
            # depuis la refonte G-1 — voir core/pending_interaction.py.
            "pending_interaction": set_pending_interaction(
                InteractionKind.CONFIRM_ACTION,
                goal="SALES_UPDATE_PRODUCT",
                context_ref="confirmation",
            )["pending_interaction"],
        }
        ctx = RenderContext(
            state=state, mc_runtime=None, strategy="CONFIRMATION", status="WAITING_CONFIRMATION",
            goal="SALES_UPDATE_PRODUCT", payload={}, salutation="",
        )
        result = run(render_confirmation(ctx))
        text = result["final_response"]
        assert text.index("Je comprends") < text.index("Voici le récapitulatif")
        assert "Vente de mais" in text


# =====================================================================
# Incident réel (2026-08-27) : "je commande des chèvres, la confirmation
# parle de champignons" — `confirmation_summary` doit toujours être posé
# avec le GOAL et le PAYLOAD ayant servi à le construire, pour que
# `nodes/rendering/confirm.py::render_confirmation` puisse détecter un
# résumé périmé (voir test_rendering_ask_and_confirm.py pour le rendu).
# =====================================================================

class TestConfirmationGateStampsSummaryProvenance:
    def test_a_fresh_confirmation_stamps_the_goal_and_payload_alongside_the_summary(self):
        payload = {"product": "chevres", "quantity": 21, "unit": "UNITE", "price": 55000}
        result = gate(
            current_goal="BUYER_PREORDER_INIT",
            transaction_payload=payload,
            expected_input="CONFIRMATION",
            waiting_for_confirmation=False,
            interpreted_event="NEW_TASK",
        )
        assert result["confirmation_summary_goal"] == "BUYER_PREORDER_INIT"
        assert result["confirmation_summary_payload"] == payload
        assert "confirmation_summary" in result and result["confirmation_summary"]
