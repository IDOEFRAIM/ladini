"""Buyer-side pricing_tiers selection (2026-08-30) — see `domain/pricing_tiers.py`.

Before this, a buyer had NO way to select a tier at all: `add_to_cart_with_ref`
always resolved against the flat `Product.price`/`Product.unit`, `OrderItem`
had no tier reference, and stock was always decremented by the raw requested
quantity regardless of which packaging was actually purchased.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
)
from tests.conftest import StubRuntime, make_state, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


_TIERS = [
    {
        "tier_id": "tier-5l",
        "quantity": 5.0,
        "unit": "L",
        "price": 500.0,
        "packaging": "bidon",
        "base_unit_quantity": 5.0,
        "min_order_quantity": 1,
    },
    {
        "tier_id": "tier-10l",
        "quantity": 10.0,
        "unit": "L",
        "price": 900.0,
        "packaging": "bidon",
        "base_unit_quantity": 10.0,
        "min_order_quantity": 1,
    },
]


def _tiered_vendor():
    return {
        "product_id": "P-LAIT",
        "name": "lait",
        "price": 500.0,
        "unit": "LITRE",
        "vendor_name": "jojo",
        "producer_id": "PR-JOJO",
        "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": _TIERS,
    }


class TestAddToCartWithTierId:
    def test_no_tier_id_behaves_exactly_as_before(self):
        """Regression guard: a product WITHOUT tiers (or tier_id omitted)
        must produce byte-identical output to the pre-refactor flat path."""
        svc = CartDomainService(rt())
        vendor = _tiered_vendor()
        vendor["pricing_tiers"] = None
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001", "lait", 50, vendor, [], make_state()
            )
        )
        line = result["active_cart"][-1]
        assert line["quantity"] == 50.0
        assert "tier_id" not in line
        assert "base_unit_quantity" not in line

    def test_valid_tier_id_computes_pack_price_and_base_unit_quantity(self):
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001",
                "lait",
                3,  # 3 packs of the 10L tier
                _tiered_vendor(),
                [],
                make_state(),
                tier_id="tier-10l",
            )
        )
        line = result["active_cart"][-1]
        assert line["tier_id"] == "tier-10l"
        assert line["quantity"] == 3  # pack count, buyer-facing
        assert line["unit"] == "L"
        assert line["packaging"] == "bidon"
        assert line["base_unit_quantity"] == 30.0  # 3 x 10L, for stock debit
        assert line["price"] == 900.0  # per-pack price
        assert line["line_total"] == 2700.0  # 3 x 900

    def test_unknown_tier_id_is_rejected_cleanly(self):
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001",
                "lait",
                2,
                _tiered_vendor(),
                [],
                make_state(),
                tier_id="not-a-real-tier",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "introuvable" in result["final_response"]

    def test_min_order_quantity_threshold_is_enforced(self):
        vendor = _tiered_vendor()
        vendor["pricing_tiers"] = [
            {**_TIERS[1], "min_order_quantity": 2},
        ]
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001", "lait", 1, vendor, [], make_state(),
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "minimale" in result["final_response"]

    def test_incompatible_buyer_unit_is_rejected_not_silently_ignored(self):
        """Audit 2026-09-01, Faille A : un acheteur qui répond "3 kg" sur un
        palier vendu en "bidon de 10 L" ne doit jamais voir son "kg"
        silencieusement disparaître — même garde que la branche sans
        palier (conversion universelle KG<->TONNE seulement, sinon on
        redemande explicitement)."""
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001",
                "lait",
                3,
                _tiered_vendor(),
                [],
                make_state(),
                buyer_unit="KG",
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "pas en kg" in result["final_response"].lower()

    def test_tier_content_unit_is_rejected_never_read_as_a_pack_count(self):
        """Audit 2026-09-01 — remplace l'ancien
        `test_compatible_buyer_unit_still_adds_to_cart`, qui verrouillait le
        comportement à l'origine de l'incident : "3 litre" y était accepté
        comme 3 PAQUETS de 10 L. Le même chemin transformait "30 litres" en 30
        bidons (300 L, 27 000 FCFA) sur la question "combien de bidons de
        10 L ?" — le garde historique ne pouvait pas l'attraper puisque `L` et
        `litre` normalisent vers la MÊME unité que le palier.

        Un nombre de paquets est SANS DIMENSION : une unité de mesure signifie
        que l'acheteur a répondu une quantité totale, pas un nombre de paquets.
        On refuse et on redemande — jamais de conversion silencieuse
        30 L → 3 bidons."""
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001",
                "lait",
                30,
                _tiered_vendor(),
                [],
                make_state(),
                buyer_unit="litre",
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(result)) == "QUANTITY"
        assert "active_cart" not in result, "rien ne doit entrer au panier"
        body = result["final_response"].lower()
        assert "quantité totale" in body
        assert "combien de" in body

    def test_bare_pack_count_still_adds_to_cart(self):
        """Le format attendu (un nombre nu) reste évidemment accepté — le
        garde ci-dessus ne doit pas fermer le chemin nominal."""
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001",
                "lait",
                3,
                _tiered_vendor(),
                [],
                make_state(),
                buyer_unit=None,
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "COMPLETED"
        line = result["active_cart"][-1]
        assert line["tier_id"] == "tier-10l"
        assert line["quantity"] == 3
        assert line["base_unit_quantity"] == 30.0

    def test_packaging_word_counts_as_a_pack_count(self):
        """"3 bidons" EST un nombre de paquets — le mot de conditionnement ne
        doit jamais être confondu avec une unité de mesure."""
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001",
                "lait",
                3,
                _tiered_vendor(),
                [],
                make_state(),
                buyer_unit="bidons",
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "COMPLETED"
        assert result["active_cart"][-1]["quantity"] == 3

    def test_non_integer_pack_count_is_rejected_not_truncated(self):
        """Audit 2026-09-01, Faille B : "2.5" bidons doit être rejeté
        explicitement, jamais tronqué en silence vers 2."""
        svc = CartDomainService(rt())
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001", "lait", 2.5, _tiered_vendor(), [], make_state(),
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "entier" in result["final_response"].lower()

    def test_stock_check_uses_base_unit_quantity_not_pack_count(self):
        """The stock-availability check must be against the BASE unit total
        (30L for 3 packs of 10L), never the raw pack count (3) — otherwise a
        product with only 20L left would wrongly appear available for a
        30L-equivalent order."""
        svc = CartDomainService(
            rt(
                responses={
                    "validate_stock_availability_atomic": {
                        "status": "error",
                        "reason": "insufficient_stock",
                        "available_quantity": 20.0,
                        "message": "Stock insuffisant",
                    }
                }
            )
        )
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001", "lait", 3, _tiered_vendor(), [], make_state(),
                tier_id="tier-10l",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "insuffisant" in result["final_response"].lower()


class TestTierSelectionContextIsARealStateChannel:
    """Incident réel (2026-08-30) : `tier_selection_context` était renvoyé
    par cart.py mais jamais déclaré dans `flows/buyer/state.py::BuyerContext`
    — LangGraph ne persiste QUE les clés déclarées comme channels entre deux
    tours. Résultat en prod : le menu de paliers s'affichait, mais la
    réponse ("2") du tour suivant ne trouvait plus AUCUN contexte actif —
    le tour partait dans une direction complètement différente (résurgence
    d'un goal producteur suspendu) au lieu de résoudre le palier choisi.
    Ce test verrouille juste la déclaration ; le comportement multi-tour
    est déjà couvert par TestCartManagementTierMenu ci-dessous."""

    def test_tier_selection_context_is_declared_alongside_vendor_selection_context(self):
        from ladini.graphs.agents.market_coach.flows.buyer.state import (
            BuyerContext,
        )

        assert "tier_selection_context" in BuyerContext.__annotations__
        assert "vendor_selection_context" in BuyerContext.__annotations__

    def test_tier_selection_context_is_registered_durable_in_state_profile(self):
        """Real bug found live (2026-08-30): declaring the LangGraph channel
        (test above) wasn't enough — a THIRD, separate registry
        (`core/state_profile.py`, consumed by the checkpointer's pruning
        logic) also has to know a field is DURABLE, or it never survives a
        turn boundary in production even though the channel itself is
        correctly declared. Same fix `vendor_selection_context` already
        needed."""
        from ladini.graphs.agents.market_coach.core.state_profile import (
            FieldLifecycle,
            get_field_spec,
        )

        spec = get_field_spec("tier_selection_context")
        assert spec is not None, "tier_selection_context must be registered in state_profile"
        assert spec.lifecycle == FieldLifecycle.DURABLE


class TestCartManagementTierMenu:
    """Full node-level test: a buyer resolving a tiered product through
    `cart_management` must see a tier menu before anything is added, then
    resolving the SAME turn's selection must add the chosen tier."""

    def test_tiered_single_vendor_shows_menu_before_adding(self):
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="ANSWER",
            transaction_payload={"product": "lait", "quantity": 3},
            user_phone="+22670000001",
        )
        # Force the single-vendor path via a stubbed search result.
        import ladini.graphs.agents.market_coach.flows.buyer.cart as cart_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_tiered_vendor()], False

        orig = cart_mod.CartDomainService.resolve_product_vendors
        cart_mod.CartDomainService.resolve_product_vendors = _fake_resolve_vendors
        try:
            result = run(cart_management(state, rt()))
        finally:
            cart_mod.CartDomainService.resolve_product_vendors = orig

        assert result["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert "conditionnements" in result["final_response"]
        assert "tier_selection_context" in result
        assert result["tier_selection_context"]["tiers"] == _TIERS
        # Real incident (2026-08-30): without explicitly claiming the
        # mapping-kind slot and clearing any stale menu snapshot, a bare
        # numeric reply could get silently resolved against an EARLIER,
        # unrelated menu in the same conversation before cart_management
        # ever saw it — see test_memory_stale_menu_snapshot.py for the
        # mechanism this closes.
        assert result["working_memory"]["available_mapping_kind"] == "pricing_tier"
        assert result["working_memory"]["menu_snapshot_id"] is None
        assert result["available_mapping"] == {}

    def test_prefetched_vendors_skip_the_redundant_search_products_call(self):
        """`buyer_request_resolver` already resolved the vendors this turn and
        forwards them as `_prefetched_vendors` — `cart_management` must reuse
        them instead of a second identical `search_products` (efficiency
        finding). With no `search_products` stubbed and `resolve_product_vendors`
        rigged to fail, the tier menu still renders iff the prefetch is honored."""
        import ladini.graphs.agents.market_coach.flows.buyer.cart as cart_mod

        async def _boom_resolve_vendors(self, phone, product_name):
            raise AssertionError("resolve_product_vendors must not be called when prefetched")

        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="ANSWER",
            transaction_payload={"product": "lait"},
            user_phone="+22670000001",
        )
        state["_prefetched_vendors"] = [_tiered_vendor()]
        orig = cart_mod.CartDomainService.resolve_product_vendors
        cart_mod.CartDomainService.resolve_product_vendors = _boom_resolve_vendors
        try:
            result = run(cart_management(state, rt()))
        finally:
            cart_mod.CartDomainService.resolve_product_vendors = orig

        assert result["status"] == "WAITING_INPUT"
        assert "conditionnements" in result["final_response"]
        assert result["tier_selection_context"]["tiers"] == _TIERS

    def test_second_turn_resolves_the_chosen_tier_and_asks_for_pack_count(self):
        """The exact reported live scenario: menu shown on turn 1 (quantity
        40 already known from BEFORE the tier menu existed), buyer replies
        with the tier's index on turn 2 — must resolve to that tier, but
        (2026-09-01, Option 1) must NOT silently reuse the pre-tier
        quantity=40 as a pack count (that would be 40 bidons de 10L = 400L,
        a real incident) — must ask explicitly how many packs instead."""
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="ANSWER",
            transaction_payload={"product": "lait", "quantity": 40, "selection_index": "2"},
            vendor_selection_context={
                "product": "lait",
                "vendors": [_tiered_vendor()],
                "chosen_vendor": _tiered_vendor(),
                "requested_quantity": None,
                "requested_unit": "LITRE",
                "available_mapping_kind": "product_vendor",
            },
            tier_selection_context={
                "product_id": "P-LAIT",
                "product_name": "lait",
                "tiers": _TIERS,
            },
            user_phone="+22670000001",
        )
        result = run(cart_management(state, rt()))
        assert result["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(result)) == "QUANTITY"
        assert "10.0 L" in result["final_response"]
        assert result["vendor_selection_context"]["resolved_tier_id"] == "tier-10l"

    def test_third_turn_completes_with_the_freshly_given_pack_count(self):
        """Follow-up to the test above: once the buyer actually answers
        "how many packs", the order must complete with THAT number, never
        the stale pre-tier quantity."""
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="ANSWER",
            transaction_payload={"product": "lait", "quantity": 3},
            extracted_entities={"quantity": 3},
            vendor_selection_context={
                "product": "lait",
                "vendors": [_tiered_vendor()],
                "chosen_vendor": _tiered_vendor(),
                "requested_quantity": None,
                "requested_unit": "LITRE",
                "available_mapping_kind": "product_vendor",
                "resolved_tier_id": "tier-10l",
            },
            tier_selection_context=None,
            user_phone="+22670000001",
        )
        result = run(cart_management(state, rt()))
        assert result["status"] == "COMPLETED"
        line = result["active_cart"][-1]
        assert line["tier_id"] == "tier-10l"
        assert line["quantity"] == 3
        assert line["base_unit_quantity"] == 30.0  # 3 x 10L

    def test_second_turn_resolves_even_if_the_fresh_search_returns_a_different_product_id(self):
        """Real live bug (2026-08-30): `vendor_selection_context` never gets
        re-seeded on the 'quantity already known' path (it's only seeded by
        the 'ask for quantity' branch), so every subsequent turn re-runs
        `resolve_product_vendors` fresh. If that fresh search's tied
        ordering (e.g. multiple test products sharing the same price) ever
        returns a DIFFERENT row than the one that showed the tier menu, a
        `product_id`-matching resolver silently fails to consume the
        selection and re-shows the same menu forever. The fix trusts
        `tier_selection_context`'s own stored tier list instead of
        re-matching by product_id — must resolve correctly even when the
        freshly re-searched vendor is a DIFFERENT product row entirely.

        (2026-08-31) The staleness guard added for the "different PRODUCT
        leaking in" incident (see test_tier_selection_stale_product_leak.py)
        compares by `product_name`, never `product_id` — precisely so this
        exact drift case keeps working: same product name "lait", just a
        different row/ID from a repeated search."""
        drifted_vendor = _tiered_vendor()
        drifted_vendor["product_id"] = "P-LAIT-DIFFERENT-ROW"  # simulates search drift
        import ladini.graphs.agents.market_coach.flows.buyer.cart as cart_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [drifted_vendor], False

        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="ANSWER",
            transaction_payload={"product": "lait", "quantity": 40, "selection_index": "2"},
            tier_selection_context={
                "product_id": "P-LAIT",  # the ORIGINAL product_id, now stale
                "product_name": "lait",  # what the staleness guard actually checks
                "tiers": _TIERS,
            },
            user_phone="+22670000001",
        )
        orig = cart_mod.CartDomainService.resolve_product_vendors
        cart_mod.CartDomainService.resolve_product_vendors = _fake_resolve_vendors
        try:
            result = run(cart_management(state, rt()))
        finally:
            cart_mod.CartDomainService.resolve_product_vendors = orig

        assert result["status"] == "COMPLETED"
        line = result["active_cart"][-1]
        assert line["tier_id"] == "tier-10l"
        assert line["base_unit_quantity"] == 400.0
