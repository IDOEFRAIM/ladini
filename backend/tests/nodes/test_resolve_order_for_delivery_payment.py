"""`flows/producer/flow.py::_resolve_order_for_delivery_payment` — résolution
de QUELLE commande le producteur vise pour la clôture paiement-à-la-livraison
(2026-09-04, F1). Même gabarit que `TestResolveStock`
(`tests/nodes/test_producer_flow_resolvers.py`), dont cette résolution
reprend directement le motif (auto-résolution / `selection_index` / menu
strict — jamais un choix implicite, mandat §20)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from tests.conftest import StubRuntime, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _order(order_id, *, status="CONFIRMED", payment_status="PENDING", **overrides):
    base = {
        "order_id": order_id,
        "reference": order_id[:8].upper(),
        "status": status,
        "payment_status": payment_status,
        "delivery_status": "PENDING",
        "total_amount": 15000.0,
        "currency": "XOF",
        "buyer_name": "Acheteur Test",
    }
    base.update(overrides)
    return base


class TestResolveOrderForDeliveryPayment:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        result = run(_resolve_order_for_delivery_payment(rt(), "", {}))
        assert result["status"] == "ERROR"

    def test_no_candidate_at_all_is_an_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt({"get_producer_orders": {"status": "success", "data": []}})
        result = run(_resolve_order_for_delivery_payment(runtime, "+22670000001", {}))
        assert "no_order_pending_payment_at_delivery" in result["validation_errors"]

    def test_escrow_orders_are_never_offered_as_candidates(self):
        """Une commande escrow (`payment_status="ESCROWED"`) revenant dans
        `get_producer_orders(status="CONFIRMED")` ne doit JAMAIS apparaître
        comme candidate — seul `verify_delivery_otp` la traite."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("escrow-order-1", payment_status="ESCROWED")],
                }
            }
        )
        result = run(_resolve_order_for_delivery_payment(runtime, "+22670000001", {}))
        assert "no_order_pending_payment_at_delivery" in result["validation_errors"]

    def test_single_candidate_autoresolves(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("order-1")],
                }
            }
        )
        result = run(_resolve_order_for_delivery_payment(runtime, "+22670000001", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "order-1"

    def test_multiple_candidates_without_selection_index_shows_a_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("order-1"), _order("order-2")],
                }
            }
        )
        result = run(_resolve_order_for_delivery_payment(runtime, "+22670000001", {}))
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert result["pending_menu"] is not None

    def test_multiple_candidates_with_selection_index_resolves(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("order-1"), _order("order-2")],
                }
            }
        )
        result = run(
            _resolve_order_for_delivery_payment(
                runtime, "+22670000001", {"selection_index": 2}
            )
        )
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "order-2"

    def test_order_id_already_resolved_by_memory_update_is_used_directly(self):
        """Incident réel (2026-09-15) — voir le miroir
        test_resolve_order_for_cancellation.py : `nodes/memory.py` résout
        la sélection numérique en `order_id` puis efface `selection_index`
        dans le même mouvement ; ce résolveur ne lisait QUE
        `selection_index`."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("order-1"), _order("order-2")],
                }
            }
        )
        result = run(
            _resolve_order_for_delivery_payment(
                runtime, "+22670000001", {"order_id": "order-2"}
            )
        )
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "order-2"

    def test_a_mix_of_escrow_and_pay_at_delivery_orders_only_offers_the_latter(self):
        """Une commande escrow ET une commande cash toutes deux CONFIRMED :
        seule la seconde est proposée — jamais un mélange qui laisserait le
        producteur accidentellement clôturer une commande escrow ici."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_order_for_delivery_payment,
        )

        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [
                        _order("escrow-1", payment_status="ESCROWED"),
                        _order("cash-1", payment_status="PENDING"),
                    ],
                }
            }
        )
        result = run(_resolve_order_for_delivery_payment(runtime, "+22670000001", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "cash-1"
