"""`_resolve_order_for_cancellation` — résolution de QUELLE commande le
producteur annule (Phase 5, décision produit #1).

Même gabarit que `test_resolve_order_for_delivery_payment.py` : ce flux en
est le pendant « je ne peux pas honorer », et partage volontairement le
même jeu de candidates (`CONFIRMED` + `payment_status=PENDING`) — une
commande escrow ne relève d'aucun des deux chemins.

(2026-09-13, confirmation explicite producteur) : le résolveur interroge
désormais `get_producer_orders` DEUX fois (`status="CONFIRMED"` puis
`status="PENDING_PRODUCER_CONFIRMATION"`) — refuser une commande pas encore
confirmée est un cas d'usage légitime désormais accepté. Les tests qui ne se
soucient QUE du chemin `CONFIRMED` historique répondent la même liste aux
deux appels via une fonction (le `StubRuntime` invoque toute réponse
callable avec les kwargs de CET appel) filtrant sur `status` pour éviter de
compter les mêmes commandes en double."""
from __future__ import annotations

from tests.conftest import StubRuntime, run
from tests.harness.state import clears


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
    from ladini.graphs.agents.market_coach.flows.producer.flow import (
        _resolve_order_for_cancellation,
    )

    return _resolve_order_for_cancellation


def _by_status(data):
    """Réponse `get_producer_orders` filtrée par `status` — évite de
    compter les mêmes commandes deux fois (le résolveur interroge
    maintenant `CONFIRMED` puis `PENDING_PRODUCER_CONFIRMATION`
    séparément)."""

    def _respond(*, status="", **_kwargs):
        filtered = [o for o in data if o.get("status") == status]
        return {"status": "success", "data": filtered}

    return _respond


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
                "get_producer_orders": _by_status(
                    [_order("escrow-1", payment_status="ESCROWED")]
                )
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert "no_cancellable_order" in result["validation_errors"]

    def test_single_candidate_is_auto_selected(self):
        runtime = rt({"get_producer_orders": _by_status([_order("ord-1")])})
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "ord-1"

    def test_several_candidates_never_pick_implicitly(self):
        runtime = rt(
            {
                "get_producer_orders": _by_status(
                    [_order("ord-1"), _order("ord-2")]
                )
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
                "get_producer_orders": _by_status(
                    [_order("ord-1"), _order("ord-2")]
                )
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {"selection_index": 2}))
        assert result["transaction_payload"]["order_id"] == "ord-2"
        assert clears(result["transaction_payload"], "selection_index")

    def test_order_id_already_resolved_by_memory_update_is_used_directly(self):
        """Incident réel (2026-09-15) : `nodes/memory.py` résout la
        sélection numérique en `order_id` PUIS efface `selection_index`
        dans le même mouvement (comportement voulu une fois traduit) — ce
        résolveur ne lisait QUE `selection_index`, jamais `order_id` déjà
        résolu, donc le menu se réaffichait indéfiniment en production quel
        que soit le numéro tapé. Reproduit ici l'état EXACT que memory.py
        produit réellement : `order_id` présent, `selection_index` absent."""
        runtime = rt(
            {
                "get_producer_orders": _by_status(
                    [_order("ord-1"), _order("ord-2")]
                )
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {"order_id": "ord-2"}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "ord-2"

    def test_out_of_range_selection_re_displays_the_menu(self):
        runtime = rt(
            {
                "get_producer_orders": _by_status(
                    [_order("ord-1"), _order("ord-2")]
                )
            }
        )
        result = run(_resolver()(runtime, "+22670000001", {"selection_index": 42}))
        assert result["status"] == "WAITING_INPUT"

    def test_a_not_yet_confirmed_order_is_also_cancellable(self):
        """(2026-09-13, confirmation explicite producteur) : refuser une
        commande AVANT de la confirmer est désormais un cas d'usage
        légitime, au même titre qu'annuler après confirmation."""
        pending = _order("ord-pending", status="PENDING_PRODUCER_CONFIRMATION")
        runtime = rt({"get_producer_orders": _by_status([pending])})
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["order_id"] == "ord-pending"

    def test_confirmed_and_pending_orders_are_merged_without_duplication(self):
        confirmed = _order("ord-confirmed")
        pending = _order("ord-pending", status="PENDING_PRODUCER_CONFIRMATION")
        runtime = rt({"get_producer_orders": _by_status([confirmed, pending])})
        result = run(_resolver()(runtime, "+22670000001", {}))
        assert result["status"] == "WAITING_INPUT"
        assert result["available_mapping"] == {
            "1": "ord-confirmed",
            "2": "ord-pending",
        }
