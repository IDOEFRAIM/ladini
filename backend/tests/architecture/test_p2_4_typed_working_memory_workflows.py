"""P2-4 (audit architectural 2026-09-08) — mini machines à états
`working_memory` typées.

`bid_phase` (flows/producer/auctions.py), `update_phase`
(flows/producer/flow.py) et `winner_gps_stage` (flows/buyer/order_tracking.py)
sont des mini machines à états AUTO-SUFFISANTES, strictement internes à un
seul flow chacune — le mandat explicite décourage la conversion en
sous-graphe sans bénéfice démontré. Ce qui manquait réellement : un contrat
TYPÉ (`flows/producer/contexts.py::BidWorkflowState`/
`ProducerUpdateWorkflowState`, `flows/buyer/contexts.py::WinnerGpsWorkflowState`)
pour que `nodes/cognitive.py` (abandon de tunnel) n'ait plus besoin de
recopier à la main une liste littérale de clés internes à chaque domaine.

Ces tests prouvent : (1) le comportement de nettoyage à l'abandon de tunnel
est STRICTEMENT INCHANGÉ (mêmes clés qu'avant le refactor) ; (2) les classes
elles-mêmes lisent correctement `working_memory`."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.flows.buyer.contexts import (
    WinnerGpsWorkflowState,
)
from ladini.graphs.agents.market_coach.flows.producer.contexts import (
    BidWorkflowState,
    ProducerUpdateWorkflowState,
)
from tests.conftest import StubRuntime, make_state, run

_ORIGINAL_STALE_WM_KEYS = frozenset(
    {
        "bid_phase", "pending_bid_auction", "pending_bid_price",
        "pending_modify_bid",
        # Phase B2b (2026-09-28) : le prix CERTIFIÉ en cours de confirmation (montant + base + provenance) fait
        # partie de la même mini machine à états `bid_phase` — purgé avec elle à l'abandon du tunnel.
        "pending_bid_pricing",
        "update_phase", "update_cycle_id", "update_product_id",
        "update_pending",
        # Phase B2c.7 (2026-09-29) : lot relu (quantité/unité) + question de base du prix en attente —
        # même mini machine à états `update_phase`, purgés avec elle à l'abandon du tunnel.
        "update_listing", "update_price_pending",
        "winner_gps_stage",
    }
)


class TestTypedContractsMatchThePreRefactorKeySet:
    def test_union_of_declared_keys_is_unchanged(self):
        union = (
            BidWorkflowState.KEYS
            | ProducerUpdateWorkflowState.KEYS
            | WinnerGpsWorkflowState.KEYS
        )
        assert union == _ORIGINAL_STALE_WM_KEYS


class TestBidWorkflowState:
    def test_from_state_reads_working_memory(self):
        s = BidWorkflowState.from_state(
            make_state(working_memory={"bid_phase": "confirm"})
        )
        assert s.phase == "CONFIRM"
        assert s.is_active is True

    def test_inactive_when_no_phase(self):
        s = BidWorkflowState.from_state(make_state(working_memory={}))
        assert s.is_active is False

    def test_reset_patch_covers_all_declared_keys(self):
        s = BidWorkflowState.from_state(make_state())
        patch = s.reset_patch()
        assert set(patch) == BidWorkflowState.KEYS
        assert all(v is None for v in patch.values())


class TestProducerUpdateWorkflowState:
    def test_from_state_reads_working_memory(self):
        s = ProducerUpdateWorkflowState.from_state(
            make_state(working_memory={"update_phase": "select"})
        )
        assert s.phase == "SELECT"
        assert s.is_active is True

    def test_reset_patch_covers_all_declared_keys(self):
        s = ProducerUpdateWorkflowState.from_state(make_state())
        patch = s.reset_patch()
        assert set(patch) == ProducerUpdateWorkflowState.KEYS


class TestWinnerGpsWorkflowState:
    def test_is_active_reflects_flag(self):
        assert WinnerGpsWorkflowState.from_state(
            make_state(working_memory={"winner_gps_stage": True})
        ).is_active is True
        assert WinnerGpsWorkflowState.from_state(
            make_state(working_memory={})
        ).is_active is False

    def test_reset_patch_covers_declared_key(self):
        s = WinnerGpsWorkflowState.from_state(make_state())
        assert s.reset_patch() == {"winner_gps_stage": None}


class TestCognitiveGuardTunnelAbandonStillClearsTheSameKeys:
    """Test comportemental (pas juste unitaire de classe) : reproduit
    exactement le scénario de l'incident +22601479800 (bid_phase="CONFIRM"
    + winner_gps_stage=True + update_phase="COLLECT" simultanément — cas
    extrême mais couvre l'union complète) et vérifie que l'abandon de
    tunnel après max retries efface bien toutes les clés, ni plus ni
    moins qu'avant le refactor P2-4."""

    def test_all_stale_keys_cleared_on_tunnel_abandon(self):
        from ladini.graphs.agents.market_coach.nodes.cognitive import (
            cognitive_guard,
        )

        state = make_state(
            current_goal="SALES_UPDATE_PRODUCT",
            goal_status="IN_PROGRESS",
            retry_count=2,
            interpreted_event="UNKNOWN",
            pending_interaction={
                "kind": "CONFIRM_ACTION",
                "context_ref": "confirmation",
            },
            working_memory={
                "bid_phase": "CONFIRM",
                "pending_bid_auction": "a1",
                "pending_bid_price": 1000,
                "pending_modify_bid": "b1",
                "update_phase": "COLLECT",
                "update_cycle_id": "c1",
                "update_product_id": "p1",
                "update_pending": {"stock": 10},
                "winner_gps_stage": True,
                "some_unrelated_key": "must_survive",
            },
        )
        result = run(cognitive_guard(state, StubRuntime()))
        wm = result.get("working_memory") or {}
        for key in _ORIGINAL_STALE_WM_KEYS:
            assert wm.get(key) is None, f"{key} aurait dû être effacé à l'abandon du tunnel"
        assert wm.get("some_unrelated_key") == "must_survive"
