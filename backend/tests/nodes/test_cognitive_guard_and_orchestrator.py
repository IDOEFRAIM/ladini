"""`nodes/cognitive.py` — garde-fou cognitif (protection de tunnel, abandon
après N échecs, report d'entités stables).

(2026-09-08, refonte responsabilités des nœuds d'entrée, mandat §8) :
`cognitive_orchestrator` a été SUPPRIMÉ (nœud ET fonction) — sa
classification perceive/think/decide/act/observe/reason ne pilotait AUCUNE
transition réelle du graphe (voir `core/graph_builder.py` et
`tests/architecture/test_cognitive_decisions_are_consumed_or_removed.py`,
qui verrouillait déjà cette preuve). `TestCognitiveOrchestrator`
ci-dessous a été retirée en conséquence — ce n'est pas une régression non
couverte, c'est la suppression documentée d'un test sur du code
intentionnellement supprimé.

(2026-09-08, revue de validation du bloc refondu, même jour) :
`_should_trigger_disambiguation` supprimée à son tour — trouvée SANS
AUCUN appelant en production (seul `cognitive_orchestrator`, déjà mort,
l'appelait) lors de l'audit exhaustif du périmètre `disambiguation_candidate`
(voir docstring de `cognitive_guard`). `TestShouldTriggerDisambiguation`
retirée pour la même raison que `TestCognitiveOrchestrator` ci-dessus."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.nodes.cognitive import (
    _build_proactive_hint,
    _entity_carry_forward,
    cognitive_guard,
)
from tests.conftest import make_state, run


# =====================================================================
# _build_proactive_hint
# =====================================================================

class TestBuildProactiveHint:
    def test_none_without_a_goal(self):
        assert _build_proactive_hint(None, {"pct": 50}, {}) is None

    def test_none_without_progress(self):
        assert _build_proactive_hint("SALES_PUBLISH_PRODUCT", None, {}) is None

    def test_zero_percent_announces_new_operation(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 0, "remaining": ["price"]}, {})
        assert "Nouvelle opération" in hint

    def test_hundred_percent_announces_ready_for_confirmation(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 100, "remaining": []}, {})
        assert "confirmation" in hint

    def test_one_remaining_field_names_it(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 66, "remaining": ["price"]}, {})
        assert "Plus qu'une info" in hint

    def test_multiple_remaining_fields_shows_percentage(self):
        hint = _build_proactive_hint("SALES_PUBLISH_PRODUCT", {"pct": 33, "remaining": ["price", "quantity"]}, {})
        assert "33%" in hint
        assert "2" in hint


# =====================================================================
# _entity_carry_forward
# =====================================================================

class TestEntityCarryForward:
    def test_none_outside_a_tunnel(self):
        state = make_state(stable_entities={"product": "mais"}, extracted_entities={})
        assert _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", False) is None

    def test_none_without_a_current_goal(self):
        state = make_state(stable_entities={"product": "mais"}, extracted_entities={})
        assert _entity_carry_forward(state, None, True) is None

    def test_carries_missing_product_unit_zone_from_stable(self):
        state = make_state(
            stable_entities={"product": "mais", "unit": "KG", "zone_name": "Ouaga"},
            extracted_entities={},
        )
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True)
        assert result == {"product": "mais", "unit": "KG", "zone_name": "Ouaga"}

    def test_does_not_overwrite_already_extracted_entities(self):
        state = make_state(
            stable_entities={"product": "mais"},
            extracted_entities={"product": "riz"},
        )
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True)
        assert result is None or result.get("product") == "riz"

    def test_none_when_nothing_to_carry(self):
        state = make_state(stable_entities={}, extracted_entities={"product": "mais"})
        assert _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True) is None

    def test_none_on_reject_event_even_with_carryable_stable_entities(self):
        # (2026-09-30, Étape 5, incident réel) : un « annule »/« non » nu ne doit jamais
        # récupérer product/unit depuis stable_entities — sinon un CANCEL sans contenu se
        # lit, en aval, comme un refus PORTEUR de valeurs (UpdateDraft au lieu de Cancel).
        state = make_state(
            stable_entities={"product": "lait", "unit": "LITRE"},
            extracted_entities={},
        )
        assert _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True, "REJECT") is None

    def test_none_on_cancel_event_even_with_carryable_stable_entities(self):
        state = make_state(
            stable_entities={"product": "lait", "unit": "LITRE"},
            extracted_entities={},
        )
        assert _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True, "CANCEL") is None

    def test_still_carries_on_answer_event(self):
        # Non-régression (mandat §18) : le carry-forward doit continuer à fonctionner
        # tant que le flow n'est pas terminé/annulé (ex: "500" répond à PRICE, product
        # doit rester disponible).
        state = make_state(
            stable_entities={"product": "miel", "unit": "KG"},
            extracted_entities={},
        )
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True, "ANSWER")
        assert result == {"product": "miel", "unit": "KG"}

    def test_default_event_argument_preserves_prior_behavior(self):
        state = make_state(
            stable_entities={"product": "mais", "unit": "KG", "zone_name": "Ouaga"},
            extracted_entities={},
        )
        result = _entity_carry_forward(state, "SALES_PUBLISH_PRODUCT", True)
        assert result == {"product": "mais", "unit": "KG", "zone_name": "Ouaga"}


class TestShouldTriggerDisambiguationRemoved:
    """Preuve négative directe (revue de validation) : la fonction ne doit
    plus exister EN CODE — pas seulement inutilisée mais encore présente
    en dormance (même discipline que `should_replan`,
    tests/architecture/test_cognitive_decisions_are_consumed_or_removed.py)."""

    def test_should_trigger_disambiguation_does_not_exist_anymore(self):
        import ladini.graphs.agents.market_coach.nodes.cognitive as mod
        assert not hasattr(mod, "_should_trigger_disambiguation")


# =====================================================================
# cognitive_guard
# =====================================================================

class TestCognitiveGuardInterruption:
    def test_new_task_with_different_intent_and_high_confidence_suspends_the_current_goal(self):
        """(2026-09-08, mandat §7 "amélioration obligatoire") : l'interruption
        exige désormais une confiance suffisante — voir le test symétrique
        `test_new_task_with_different_intent_but_low_confidence_does_not_interrupt`
        ci-dessous pour le cas AVANT correctif (interrompait aveuglément)."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.95,
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "INTERRUPTION"
        assert result["cognitive_decision"]["action"] == "INTERRUPT_ACTIVE_GOAL"

    def test_new_task_with_different_intent_but_low_confidence_does_not_interrupt(self):
        """Bug réel corrigé (2026-09-08) : AVANT ce correctif, cette
        interruption ne vérifiait AUCUNE confiance — une intention
        concurrente classée à confiance quasi nulle cassait quand même un
        tunnel fiable en cours (mandat §7 : "une intention concurrente
        faible ne doit pas casser un tunnel fiable")."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.2,
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result.get("interpreted_event") != "INTERRUPTION"
        assert result["cognitive_decision"]["action"] != "INTERRUPT_ACTIVE_GOAL"
        # L'intention concurrente reste journalisée pour observabilité /
        # future désambiguïsation, même si elle ne casse pas le tunnel.
        sources = [c["intent"] for c in result["intent_competition"]]
        assert "BUYER_REQUEST" in sources

    def test_new_task_with_the_same_intent_as_current_goal_does_not_interrupt(self):
        """(revue de validation, scénario C du mandat) : confiance HAUTE
        explicite — la non-interruption doit venir du fait que l'intention
        est la MÊME que le goal actif, pas d'un repli sur le seuil de
        confiance par défaut (sinon ce test ne prouverait rien de plus que
        le test "low_confidence" ci-dessous)."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            interpreter_confidence=0.97,
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result.get("interpreted_event") != "INTERRUPTION"

    def test_new_task_with_unknown_intent_does_not_interrupt(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
            interpreter_confidence=0.97,
            expected_input="QUANTITY",
        )
        result = run(cognitive_guard(state, None))
        assert result.get("interpreted_event") != "INTERRUPTION"


class TestCognitiveGuardUnknownInTunnel:
    def test_low_retry_count_triggers_recovery(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UNKNOWN",
            expected_input="QUANTITY",
            retry_count=0,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "recover_active_tunnel"
        assert result["response_strategy"] == "RECOVERY"
        assert result["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_max_retries_abandons_the_tunnel_with_a_full_reset(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UNKNOWN",
            expected_input="QUANTITY",
            retry_count=2,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "abandon_tunnel_max_retries"
        assert result["current_goal"] is None
        assert result["transaction_payload"] == {"__reset__": True}
        assert result["response_strategy"] == "CLARIFICATION"

    def test_max_retries_abandon_also_clears_stale_producer_mini_state_machines(self):
        """Chantier résilience 2026-08 : `bid_phase`/`update_phase` (mini
        machines à états auto-suffisantes, flows/producer/auctions.py +
        flow.py) vivent exclusivement dans working_memory, jamais touché par
        ce reset avant ce fix — un abandon en plein milieu d'une confirmation
        d'offre laissait `bid_phase="CONFIRM"` stale, et relancer le même
        goal peu après pouvait réafficher un récap périmé au lieu de
        redémarrer proprement."""
        state = make_state(
            current_goal="MARKET_BROWSE_REQUESTS",
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",
            retry_count=2,
            working_memory={
                "bid_phase": "CONFIRM",
                "pending_bid_auction": "a1",
                "pending_bid_price": 250,
                "update_phase": "COLLECT",
                "update_cycle_id": "c1",
                "auction_brief": {"a1": {"product": "mais"}},  # doit survivre
            },
        )
        result = run(cognitive_guard(state, None))
        wm = result["working_memory"]
        assert wm["bid_phase"] is None
        assert wm["pending_bid_auction"] is None
        assert wm["pending_bid_price"] is None
        assert wm["update_phase"] is None
        assert wm["update_cycle_id"] is None
        assert wm["auction_brief"] == {"a1": {"product": "mais"}}

    def test_max_retries_abandon_also_clears_stale_buyer_gps_stage_flags(self):
        """Même bug, côté acheteur (incident réel +22601479800, 2026-09-02) :
        `preorder_workflow.gps_stage`/`gps_default` (flows/buyer/preorder.py)
        et `working_memory.winner_gps_stage` (flows/buyer/order_tracking.py)
        sont la même famille de mini-état auto-suffisant que
        `bid_phase`/`update_phase` ci-dessus, mais vivaient hors de portée du
        reset : `pending_interaction` (PROVIDE_LOCATION) était bien effacé à
        l'abandon, mais `gps_stage=True` restait collé (merge_dict ne s'auto-
        efface jamais). Reprendre le même goal plus tard retombait alors sur
        `resolve_gps_stage` sans aucun `pending_interaction` actif pour le
        justifier — exactement la désynchronisation observée en prod."""
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )

        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            interpreted_event="UNKNOWN",
            retry_count=2,
            working_memory={"winner_gps_stage": True},
            preorder_workflow={
                "phase": "PREORDER_DRAFTED",
                "preorder_id": "abc123",
                "gps_stage": True,
                "gps_default": {"lat": 12.35, "lon": -1.5},
            },
            **set_pending_interaction(
                InteractionKind.PROVIDE_LOCATION, context_ref="confirmation"
            ),
        )
        result = run(cognitive_guard(state, None))

        assert result["cognitive_decision"]["action"] == "abandon_tunnel_max_retries"
        assert result["working_memory"]["winner_gps_stage"] is None
        assert result["preorder_workflow"]["gps_stage"] is None
        assert result["preorder_workflow"]["gps_default"] is None
        assert result["pending_interaction"] is None

    def test_unknown_event_outside_a_tunnel_does_not_trigger_this_branch(self):
        """(2026-09-08, correction topologique du bloc conversationnel) :
        ce scénario ne déclenche toujours PAS la branche recovery/abandon
        (`in_tunnel` est faux, `current_goal` absent) — mais il n'est plus
        classé en `"continue"` muet : `event=UNKNOWN` + rien en attente +
        aucun goal actif est EXACTEMENT la condition de clarification
        générique (ex-1ère clause de `clarification_node::
        needs_clarification`), désormais décidée ici. Voir mandat §8."""
        state = make_state(current_goal=None, interpreted_event="UNKNOWN", expected_input="NONE")
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "CLARIFY"

    def test_a_shared_location_in_tunnel_is_not_treated_as_an_unknown_event(self):
        """Bug réel (2026-08-13) : un partage de position WhatsApp natif n'a
        pas de texte à classifier — l'interprète renvoie event=UNKNOWN pour
        ces tours. Avant ce fix, ÇA incrémentait retry_count et forçait
        response_strategy=RECOVERY, qui court-circuite tout le graphe
        directement vers la réponse (nodes/routing `_route_after_clarification`)
        SANS jamais atteindre le resolver — un point GPS valide au stade
        `finalize_winner`/`create_preorder` tombait donc systématiquement sur
        le message générique "Je n'ai pas bien saisi", même après N tentatives.
        Voir [[gps-delivery-burkina-faso-2026-08]]."""
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",
            retry_count=0,
            location_shared=True,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"
        assert result.get("response_strategy") != "RECOVERY"
        assert "current_goal" not in result, "le tunnel ne doit pas être touché"

    def test_a_shared_location_never_triggers_tunnel_abandonment_even_at_max_retries(self):
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",
            retry_count=5,
            location_shared=True,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"
        assert result.get("response_strategy") != "CLARIFICATION"


class TestCognitiveGuardEntityCarryAndProgress:
    def test_carried_entities_are_reflected_in_updates_and_decision(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="ANSWER",
            stable_entities={"product": "mais"},
            extracted_entities={},
        )
        result = run(cognitive_guard(state, None))
        assert result["extracted_entities"]["product"] == "mais"
        assert result["cognitive_decision"]["entity_carry_forward"] is True

    def test_progress_and_proactive_hint_are_computed_for_a_known_goal(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="ANSWER",
            transaction_payload={},
        )
        result = run(cognitive_guard(state, None))
        assert "conversation_progress" in result
        assert result["cognitive_decision"]["action"] == "CONTINUE_ACTIVE_GOAL"

    def test_lexical_disambiguation_candidates_are_added_to_competition(self, monkeypatch):
        """(2026-09-08, clôture Bloc 1, mandat §21/§22) : `options` est
        désormais la SEULE source des intents candidats — `entry["candidates"]`
        (retiré du catalogue `INTENT_DISAMBIGUATION`) ne serait plus lu même
        s'il était présent ici."""
        import ladini.graphs.agents.market_coach.nodes.cognitive as mod

        monkeypatch.setattr(
            mod, "_detect_disambiguation_candidates",
            lambda text, role: {
                "id": "trigger1",
                "options": [("INTENT_A", "Option A"), ("INTENT_B", "Option B")],
            },
        )
        state = make_state(interpreted_event="NEW_TASK", detected_intent="UNKNOWN")
        result = run(cognitive_guard(state, None))
        sources = [c["source"] for c in result["intent_competition"]]
        assert "lexical_disambiguation" in sources
        intents = {c["intent"] for c in result["intent_competition"] if c["source"] == "lexical_disambiguation"}
        assert intents == {"INTENT_A", "INTENT_B"}

    def test_default_action_is_start_or_plan_goal_with_no_special_context(self):
        """(2026-09-08, correction topologique) : renommé — un NEW_TASK
        sans goal actif, sans ambiguïté ni clarification à faire, est
        désormais classé START_OR_PLAN_GOAL (ex-"continue" muet)."""
        state = make_state(current_goal=None, interpreted_event="NEW_TASK", detected_intent="UNKNOWN")
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "START_OR_PLAN_GOAL"


# =====================================================================
# disambiguation_candidate — source unique consultée par clarification_node
# et semantic_disambiguation (mandat §9, 2026-09-08)
# =====================================================================

class TestDisambiguationCandidatePrecomputed:
    def test_no_lexical_match_yields_none(self):
        state = make_state(
            normalized_text="bonjour comment allez-vous",
            interpreted_event="NEW_TASK",
            detected_intent="UNKNOWN",
        )
        result = run(cognitive_guard(state, None))
        assert result["disambiguation_candidate"] is None

    def test_lexical_match_is_exposed_on_every_branch_not_only_continue(self, monkeypatch):
        """L'abandon de tunnel (max retries) passe par un chemin de retour
        anticipé différent de la branche "continue" — le candidat doit être
        posé AVANT toute branche, pas seulement sur le chemin nominal."""
        import ladini.graphs.agents.market_coach.nodes.cognitive as mod

        monkeypatch.setattr(
            mod, "_detect_disambiguation_candidates",
            lambda text, role: {
                "id": "trigger1",
                "options": [("INTENT_A", "Option A"), ("INTENT_B", "Option B")],
            },
        )
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UNKNOWN",
            expected_input="QUANTITY",
            retry_count=2,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "abandon_tunnel_max_retries"
        assert result["disambiguation_candidate"] == {
            "id": "trigger1",
            "options": [("INTENT_A", "Option A"), ("INTENT_B", "Option B")],
        }
