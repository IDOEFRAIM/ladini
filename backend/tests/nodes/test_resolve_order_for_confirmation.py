"""`_resolve_order_for_confirmation` — résolution de QUELLE commande le
producteur confirme explicitement (2026-09-13, confirmation explicite
producteur — signalé par un producteur réel : aucune logique n'existait
pour accepter/refuser une commande reçue, seulement une notification
informative "préparez la commande").

Même gabarit que `test_resolve_order_for_cancellation.py` : auto-sélection
si une seule commande, menu numéroté strict sinon, jamais de choix
implicite. Candidates = uniquement `status=="PENDING_PRODUCER_CONFIRMATION"`
— contrairement à l'annulation (qui couvre aussi `CONFIRMED`), une commande
déjà confirmée n'a plus de raison d'être reconfirmée."""
from __future__ import annotations

from tests.conftest import StubRuntime, run
from tests.harness.state import clears


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _order(order_id, *, status="PENDING_PRODUCER_CONFIRMATION", **overrides):
    base = {
        "order_id": order_id,
        "reference": order_id[:8].upper(),
        "status": status,
        "payment_status": "PENDING",
        "total_amount": 15000.0,
        "currency": "XOF",
        "buyer_name": "Acheteur Test",
    }
    base.update(overrides)
    return base


def _resolver():
    from ladini.graphs.agents.market_coach.flows.producer.flow import (
        _resolve_order_for_confirmation,
    )

    return _resolve_order_for_confirmation


class TestResolveOrderForConfirmation:
    def test_no_phone_returns_error(self):
        assert run(_resolver()(rt(), "", {}))["status"] == "ERROR"

    def test_nothing_to_confirm_is_a_clean_error(self):
        runtime = rt({"get_producer_orders": {"status": "success", "data": []}})
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert "no_confirmable_order" in result["validation_errors"]

    def test_an_already_confirmed_order_is_not_a_candidate(self):
        """Le résolveur interroge exclusivement
        `status="PENDING_PRODUCER_CONFIRMATION"` — une commande déjà
        `CONFIRMED` renvoyée par erreur ne doit jamais être proposée."""
        runtime = rt({"get_producer_orders": {"status": "success", "data": []}})
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert "no_confirmable_order" in result["validation_errors"]

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
        assert clears(result["transaction_payload"], "selection_index")

    def test_order_id_already_resolved_by_memory_update_is_used_directly(self):
        """Incident réel (2026-09-15) — voir le miroir
        test_resolve_order_for_cancellation.py : `nodes/memory.py` résout
        la sélection numérique en `order_id` puis efface `selection_index`
        dans le même mouvement ; ce résolveur ne lisait QUE
        `selection_index`."""
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [_order("ord-1"), _order("ord-2")],
                }
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {"order_id": "ord-2"}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "ord-2"

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
