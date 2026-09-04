"""`nodes/memory.py`'s generic `mapping_kind` selection-resolution machinery
— real incident (2026-08-30), see [[pricing-tiers-catalog-listing-display-2026-08]].

A buyer replying to the NEW tier-selection menu ("2") had that reply
silently resolved against a STALE `menu_snapshot_id`/`available_mapping_kind`
left over from an earlier, unrelated menu shown earlier in the SAME
conversation (e.g. a stock/vendor list). That stale resolution path popped
`selection_index` from the payload before `cart_management` ever got to read
it — the tier menu re-displayed forever, no error anywhere, because nothing
downstream knew a numeric reply had even been "consumed" by the wrong menu.

Root cause: `_interpret_fast_path`/interpreter set `selection_index`
correctly every time; the loss happened INSIDE `memory_update`'s generic
selection-resolution block, which only protects `selection_index` from being
popped for `mapping_kind == "product_vendor"` — every other kind (including
"no kind at all, but a truthy stale snapshot") got it popped unconditionally
once ANY snapshot happened to resolve the same index to SOMETHING.

Fix: `"pricing_tier"` is now an equally protected `mapping_kind` (memory.py),
and `cart.py`'s tier-menu responses explicitly claim it (overwriting
whatever was there) and clear the stale snapshot/mapping every time they
show the menu — so by the time the buyer's reply is processed, there is
nothing stale left to misresolve against.
"""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from agriconnect.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)
from tests.conftest import StubRuntime, make_state, run


class TestPricingTierMappingKindIsProtected:
    def test_selection_index_survives_even_with_a_resolvable_stale_snapshot(self):
        """The exact live failure mode: a snapshot from an EARLIER, unrelated
        menu (kind='stock') still resolves index '2' to SOMETHING. Before the
        fix, memory.py didn't know `mapping_kind='pricing_tier'` should be
        protected the same way `product_vendor` is, so it popped
        `selection_index` after "helpfully" resolving it against the wrong
        menu entirely."""
        stale_snapshot = menu_snapshot_store.save(
            "session-live-repro",
            {"1": "stale-stock-id-1", "2": "stale-stock-id-2"},
            kind="stock",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id="session-live-repro",
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait", "quantity": 40},
            # This is the crux: mapping_kind is now "pricing_tier" (what
            # cart.py's tier menu sets), but a stale snapshot from an OLDER
            # "stock" menu is still sitting in menu_snapshot_id from before
            # cart.py started clearing it — the fix must protect
            # selection_index regardless of what that stale snapshot resolves to.
            working_memory={
                "available_mapping_kind": "pricing_tier",
                "menu_snapshot_id": stale_snapshot.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        assert result["transaction_payload"].get("selection_index") == 2, (
            "selection_index was popped/lost even though mapping_kind='pricing_tier' "
            "should protect it exactly like 'product_vendor' does"
        )

    def test_unprotected_mapping_kind_still_loses_selection_index_to_a_stale_snapshot(self):
        """Documents the OTHER half of the real incident: if `cart.py` had
        NOT started claiming `available_mapping_kind='pricing_tier'` (i.e. if
        the stale value from an earlier menu were still sitting there
        unclaimed), the generic resolver DOES still silently consume
        `selection_index` — proving the fix must actively CLAIM the kind
        every time the tier menu is shown, not just add it to the protected
        set. If this test ever starts failing, it means the protection
        became unconditional (a different, larger footgun), not that this
        specific bug reproduction stopped applying."""
        stale_snapshot = menu_snapshot_store.save(
            "session-live-repro-2",
            {"1": "stale-stock-id-1", "2": "stale-stock-id-2"},
            kind="stock",
        )
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="BUYER_ADD_TO_CART",
            detected_intent="UNKNOWN",
            session_id="session-live-repro-2",
            extracted_entities={"selection_index": 2},
            transaction_payload={"product": "lait", "quantity": 40},
            working_memory={
                # Stale kind from the OLDER menu, never overwritten — this is
                # what happened live BEFORE cart.py started claiming
                # "pricing_tier" on every tier-menu response.
                "available_mapping_kind": "stock",
                "menu_snapshot_id": stale_snapshot.menu_id,
            },
        )
        result = run(memory_update(st, StubRuntime()))
        assert "selection_index" not in result["transaction_payload"], (
            "this documents the bug mechanism itself — an unprotected/stale "
            "mapping_kind DOES lose selection_index to an unrelated snapshot"
        )
        # And it gets resolved into the WRONG field for the WRONG entity kind.
        assert result["transaction_payload"].get("stock_id") == "stale-stock-id-2"
