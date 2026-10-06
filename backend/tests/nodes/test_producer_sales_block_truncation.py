"""`flows/buyer/order_tracking.py::_producer_sales_block` — priorité
d'affichage des ventes en attente (2026-09-13, incident WhatsApp #5).

## Le bug fermé

La troncature ("… et N autres", limite de 5) s'appliquait AVEUGLÉMENT,
sans distinction entre une vente déjà `CONFIRMED` (informative) et une vente
`PENDING_PRODUCER_CONFIRMATION` (🟡 — la seule sur laquelle le producteur
peut réellement agir via *confirmer*/*annuler*). Un producteur avec plus de
5 ventes au total, mais SEULEMENT quelques-unes en attente, ne les voyait
même plus toutes lister — repéré pendant les tests du correctif
double-rôle/confirmation ("POURQUOI COUPE ON LE NOMBRE DE COMMANDE
AFFICHER"). Fix : toutes les ventes 🟡 sont désormais TOUJOURS affichées
en premier, jamais tronquées ; seules les ventes déjà traitées peuvent
l'être, pour compléter jusqu'à la limite d'affichage."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
    _producer_sales_block,
)
from tests.conftest import StubRuntime, run


def _sale(order_id: str, status: str, reference: str) -> dict:
    return {
        "order_id": order_id,
        "reference": reference,
        "status": status,
        "items": [{"product_name": "riz", "quantity": 10, "unit": "KG"}],
        "buyer_name": "Acheteur",
        "total_amount": 1000.0,
        "currency": "XOF",
    }


class TestPendingSalesAreNeverTruncated:
    def test_all_pending_sales_appear_even_beyond_the_display_cap(self):
        pending = [
            _sale(f"pending-{i}", "PENDING_PRODUCER_CONFIRMATION", f"PEND{i}")
            for i in range(7)
        ]
        confirmed = [
            _sale(f"confirmed-{i}", "CONFIRMED", f"CONF{i}") for i in range(3)
        ]
        sales = confirmed + pending  # ordre arbitraire, comme une vraie réponse DB

        runtime = StubRuntime(
            responses={
                "get_producer_orders": {"status": "success", "data": sales}
            }
        )
        text, single_pending_order_id = run(
            _producer_sales_block("+22670000001", runtime)
        )

        for s in pending:
            assert s["reference"] in text
        # toutes les 🟡 (7) + toutes les confirmées (3, ≤ 5) — rien à couper
        for s in confirmed:
            assert s["reference"] in text
        assert "autre" not in text
        # 7 ventes en attente : ambigu, jamais de verrouillage/choix implicite.
        assert single_pending_order_id is None

    def test_only_the_confirmed_overflow_is_truncated(self):
        pending = [_sale("pending-1", "PENDING_PRODUCER_CONFIRMATION", "PEND1")]
        confirmed = [
            _sale(f"confirmed-{i}", "CONFIRMED", f"CONF{i}") for i in range(10)
        ]
        sales = pending + confirmed

        runtime = StubRuntime(
            responses={
                "get_producer_orders": {"status": "success", "data": sales}
            }
        )
        text, single_pending_order_id = run(
            _producer_sales_block("+22670000001", runtime)
        )

        assert "PEND1" in text
        # 1 pending (toujours) + 5 confirmées (plafond) affichées, 5 en trop
        assert "… et 5 autres." in text
        assert single_pending_order_id == "pending-1"


class TestPendingOrderActionHint:
    """`single_pending_order_id` (2026-09-14/15) : `order_id` de l'unique
    vente 🟡 en attente, ou `None` si aucune ou plusieurs (ambigu). Consommé
    par `flows/buyer/order_tracking.py::list_orders` pour verrouiller
    DIRECTEMENT un `PendingInteraction(CONFIRM_ACTION)` sur cette commande —
    remplace un premier correctif (signal d'état donné en CONTEXTE au LLM)
    qui s'est avéré insuffisant : "confirmer"/"annuler" tapé nu retombait
    quand même parfois en UNKNOWN, malgré l'invite explicite de ce même
    bloc."""

    def test_none_when_no_sale_awaits_producer_confirmation(self):
        sales = [_sale("confirmed-1", "CONFIRMED", "CONF1")]
        runtime = StubRuntime(
            responses={"get_producer_orders": {"status": "success", "data": sales}}
        )
        _, single_pending_order_id = run(
            _producer_sales_block("+22670000001", runtime)
        )
        assert single_pending_order_id is None

    def test_none_when_there_are_no_sales_at_all(self):
        runtime = StubRuntime(
            responses={"get_producer_orders": {"status": "success", "data": []}}
        )
        text, single_pending_order_id = run(
            _producer_sales_block("+22670000001", runtime)
        )
        assert text == ""
        assert single_pending_order_id is None

    def test_the_order_id_when_exactly_one_sale_is_pending(self):
        sales = [_sale("pending-1", "PENDING_PRODUCER_CONFIRMATION", "PEND1")]
        runtime = StubRuntime(
            responses={"get_producer_orders": {"status": "success", "data": sales}}
        )
        _, single_pending_order_id = run(
            _producer_sales_block("+22670000001", runtime)
        )
        assert single_pending_order_id == "pending-1"
