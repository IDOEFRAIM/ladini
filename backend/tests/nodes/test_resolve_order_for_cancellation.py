"""`_resolve_order_for_cancellation` — résolution de QUELLE commande le
producteur annule (Phase 5, décision produit #1).

Même gabarit que `test_resolve_order_for_delivery_payment.py` : ce flux en
est le pendant « je ne peux pas honorer », et partage volontairement le
même jeu de candidates (`CONFIRMED` + `payment_status=PENDING`) — une
commande escrow ne relève d'aucun des deux chemins."""
from __future__ import annotations

from tests.conftest import StubRuntime, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _order(order_id, *, payment_status="PENDING", **overrides):
    base = {
        "order_id": order_id,
        "reference": order_id[:8].upper(),
        "status": "CONFIRMED",
        "payment_status": payment_status,
        "total_amount": 15000.0,
        "currency": "XOF",
        "buyer_name": "Acheteur Test",
    }
    base.update(overrides)
    return base


def _resolver():
    from agriconnect.graphs.agents.market_coach.flows.producer.flow import (
        _resolve_order_for_cancellation,
    )

    return _resolve_order_for_cancellation


class TestResolveOrderForCancellation:
    def test_no_phone_returns_error(self):
        assert run(_resolver()(rt(), "", {}))["status"] == "ERROR"

    def test_nothing_cancellable_is_a_clean_error(self):
        runtime = rt({"get_producer_orders": {"status": "success", "data": []}})
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert "no_cancellable_order" in result["validation_errors"]

    def test_escrow_orders_are_never_cancellable_through_this_path(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("escrow-1", payment_status="ESCROWED")],
                }
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert "no_cancellable_order" in result["validation_errors"]

    def test_single_candidate_is_auto_selected(self):
        runtime = rt(
            {"get_producer_orders": {"status": "success", "data": [_order("ord-1")]}}
        )
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "ord-1"

    def test_several_candidates_never_pick_implicitly(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("ord-1"), _order("ord-2")],
                }
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert result["status"] == "WAITING_INPUT"
        assert result["response_strategy"] == "SELECTION_MENU"
        assert "transaction_payload" not in result
        assert result["available_mapping"] == {"1": "ord-1", "2": "ord-2"}

    def test_selection_index_resolves_the_right_order(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("ord-1"), _order("ord-2")],
                }
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {"selection_index": 2}))
        assert result["transaction_payload"]["order_id"] == "ord-2"
        assert "selection_index" not in result["transaction_payload"]

    def test_out_of_range_selection_re_displays_the_menu(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("ord-1"), _order("ord-2")],
                }
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {"selection_index": 42}))
        assert result["status"] == "WAITING_INPUT"
