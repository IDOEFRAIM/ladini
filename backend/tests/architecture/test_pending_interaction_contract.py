"""Tests de contrat — `core/pending_interaction.py` (refonte architecturale
2026-09-02, G-1/G-2).

Verrouille les 5 invariants du mandat de refonte + les 10 tests de contrat
demandés (§46) applicables à ce module. Style conforme à `tests/architecture/`
(invariants structurels anti-dérive) et à la maison (`make_state`, `run`,
`TestXxx` + méthodes longues descriptives)."""

from __future__ import annotations

from tests.conftest import make_state, run

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    CART_TUNNEL_KINDS,
    InteractionKind,
    check_invariants,
    clear_pending_interaction,
    get_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)


class TestOnlyOneInteractionActiveAtATime:
    """Test 1 (§46) : à tout instant, une seule interaction canonique
    active — `set_pending_interaction` REMPLACE toujours intégralement,
    jamais une fusion résiduelle de l'ancienne (reducer `replace_value`,
    pas `merge_dict` — voir core/state.py)."""

    def test_setting_a_new_interaction_fully_replaces_the_previous_one(self):
        first = set_pending_interaction(
            InteractionKind.ENTER_FIELD, goal="SALES_PUBLISH_PRODUCT", field_name="price"
        )
        second = set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, goal="SALES_PUBLISH_PRODUCT", context_ref="confirmation"
        )
        state = make_state()
        state.update(first)
        state.update(second)  # simule deux nodes écrivant dans le même tour

        resolved = get_pending_interaction(state)
        assert resolved.kind == InteractionKind.CONFIRM_ACTION
        assert resolved.field is None  # aucune trace de "price" ne survit


class TestCartTunnelTakesPriorityOverPersistedField:
    """Un tunnel panier vivant (dérivé de vendor/tier_selection_context)
    gagne TOUJOURS sur l'état persisté explicite — jamais périmable."""

    def test_a_live_tier_menu_wins_over_a_stale_persisted_confirmation(self):
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            tier_selection_context={
                "tiers": [
                    {"tier_id": "t1", "quantity": 5, "unit": "LITRE", "price": 2500},
                    {"tier_id": "t2", "quantity": 10, "unit": "LITRE", "price": 4500},
                ]
            },
            **set_pending_interaction(
                InteractionKind.CONFIRM_ACTION, goal="BUYER_ADD_TO_CART", context_ref="confirmation"
            ),
        )

        resolved = get_pending_interaction(state)

        assert resolved.kind == InteractionKind.SELECT_PRICING_TIER
        assert resolved.kind in CART_TUNNEL_KINDS


class TestStateTransitionInvalidatesWhatIsNoLongerRelevant:
    """Test 2 (§46) : SELECT_PRODUCER -> SELECT_PRICING_TIER invalide ce qui
    n'est plus pertinent (ici : une fois un vendeur choisi, la liste de
    producteurs candidats ne doit plus être ce que le résolveur rapporte)."""

    def test_choosing_a_producer_moves_the_resolved_kind_to_pricing_tier(self):
        state_before = make_state(
            vendor_selection_context={
                "vendors": [
                    {"producer_id": "p1", "vendor_name": "A", "price": 250, "unit": "KG"},
                    {"producer_id": "p2", "vendor_name": "B", "price": 260, "unit": "KG"},
                ]
            }
        )
        assert get_pending_interaction(state_before).kind == InteractionKind.SELECT_PRODUCER

        state_after = make_state(
            vendor_selection_context={
                "chosen_vendor": {
                    "producer_id": "p1",
                    "pricing_tiers": [
                        {"tier_id": "t1", "quantity": 5, "unit": "LITRE", "price": 2500}
                    ],
                }
            }
        )
        resolved = get_pending_interaction(state_after)
        assert resolved.kind == InteractionKind.SELECT_PRICING_TIER
        assert resolved.kind != InteractionKind.SELECT_PRODUCER


class TestStaleTierContextNeverSurvivesAProductChange:
    """Test 3 (§46) — matérialise G-2 : après reset (goal_planner /
    memory.py::clear_vendor_ctx, désormais symétriques), l'ancien contexte
    palier est absent, jamais réutilisé pour un nouveau produit."""

    def test_a_reset_tier_context_is_never_read_as_a_live_pending_interaction(self):
        state = make_state(
            tier_selection_context=None,  # ce que memory.py/goal_planner.py écrivent désormais
            vendor_selection_context={"__reset__": True},
        )
        resolved = get_pending_interaction(state)
        assert resolved.kind != InteractionKind.SELECT_PRICING_TIER
        assert resolved.kind != InteractionKind.ENTER_PACKAGE_COUNT


class TestAmbiguousReplyNeverBecomesConfirmation:
    """Test 4 (§46) : expected=SELECT_PRICING_TIER, input="celui de 10 l" —
    ne devient JAMAIS CONFIRMATION. Reproduit exactement le bug rapporté :
    un `pending_interaction` CONFIRM_ACTION périmé d'un tour antérieur ne
    doit jamais l'emporter tant qu'un menu palier est encore vivant."""

    def test_a_live_tier_menu_beats_a_stale_confirm_action_regardless_of_order(self):
        stale_confirm = set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, goal="BUYER_ADD_TO_CART", context_ref="confirmation"
        )
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="UNKNOWN",  # le LLM n'a pas produit d'agent_action pour "celui de 10 l"
            tier_selection_context={
                "tiers": [
                    {"tier_id": "t1", "quantity": 5, "unit": "LITRE", "price": 2500},
                    {"tier_id": "t2", "quantity": 10, "unit": "LITRE", "price": 4500},
                ]
            },
            **stale_confirm,
        )

        resolved = get_pending_interaction(state)

        assert resolved.kind != InteractionKind.CONFIRM_ACTION
        assert resolved.kind == InteractionKind.SELECT_PRICING_TIER


class TestUnknownNeverSilentlyBypasses:
    """Test 5 (§46) — au niveau routeur, voir aussi
    tests/architecture/... (core/tunnel_manager.py::is_cart_routeable). Ici :
    le résolveur canonique lui-même ne peut jamais transformer un tunnel
    vivant en confirmation à cause d'un UNKNOWN — testé directement via
    `core/tunnel_manager.py::TunnelManager.is_cart_routeable`."""

    def test_a_live_selection_tunnel_forces_cart_routing_even_with_missing_fields(self):
        from agriconnect.graphs.agents.market_coach.core.tunnel_manager import (
            tunnel_manager,
        )

        # Sans le fix (G-1) : missing_fields non vide + WAITING_INPUT bloquait
        # systématiquement le routage vers cart_management.
        routeable_without_tunnel = tunnel_manager.is_cart_routeable(
            "WAITING_INPUT", ["quantity"], selection_tunnel_active=False
        )
        routeable_with_tunnel = tunnel_manager.is_cart_routeable(
            "WAITING_INPUT", ["quantity"], selection_tunnel_active=True
        )

        assert routeable_without_tunnel is False
        assert routeable_with_tunnel is True


class TestConfirmationCoherence:
    """Test 6 (§46) : impossible d'avoir pending_interaction=NONE tout en
    affichant une confirmation — vérifié à la fois côté résolveur et côté
    invariants explicites."""

    def test_no_pending_interaction_never_reports_confirm_action(self):
        state = make_state()
        assert get_pending_interaction(state).kind == InteractionKind.NONE

    def test_check_invariants_flags_a_confirm_action_without_any_context(self):
        state = make_state(
            **set_pending_interaction(
                InteractionKind.CONFIRM_ACTION, goal="SALES_PUBLISH_PRODUCT", context_ref="confirmation"
            )
        )
        # Ni confirmation_summary ni transaction_payload — contexte incohérent.
        violations = check_invariants(state)
        assert violations
        assert any("CONFIRM_ACTION" in v for v in violations)

    def test_check_invariants_is_satisfied_when_confirmation_summary_is_present(self):
        state = make_state(
            confirmation_summary="Récapitulatif...",
            **set_pending_interaction(
                InteractionKind.CONFIRM_ACTION, goal="SALES_PUBLISH_PRODUCT", context_ref="confirmation"
            ),
        )
        assert check_invariants(state) == []


class TestResolvedInteractionNeverStaysActive:
    """Invariant 5 : une interaction résolue ne doit jamais rester active au
    tour suivant."""

    def test_resolve_pending_interaction_returns_none_not_a_resolved_status(self):
        patch = resolve_pending_interaction()
        assert patch == {"pending_interaction": None}

    def test_clear_pending_interaction_also_returns_none(self):
        patch = clear_pending_interaction("any_reason")
        assert patch == {"pending_interaction": None}


class TestSerializationRoundTrip:
    def test_a_confirm_action_survives_a_to_dict_from_dict_round_trip(self):
        from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
            PendingInteraction,
        )

        original = PendingInteraction(
            kind=InteractionKind.ENTER_FIELD,
            goal="SALES_PUBLISH_PRODUCT",
            field="price",
            candidates=("a", "b"),
        )
        restored = PendingInteraction.from_dict(original.to_dict())

        assert restored.kind == original.kind
        assert restored.goal == original.goal
        assert restored.field == original.field
        assert restored.candidates == original.candidates

    def test_a_corrupted_or_unknown_kind_degrades_to_none_rather_than_crashing(self):
        from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
            PendingInteraction,
        )

        restored = PendingInteraction.from_dict({"kind": "SOMETHING_THAT_NO_LONGER_EXISTS"})
        assert restored.kind == InteractionKind.NONE
