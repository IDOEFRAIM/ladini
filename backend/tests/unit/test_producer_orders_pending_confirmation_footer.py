"""`get_producer_orders` — rappel "comment confirmer/annuler" (2026-09-14,
incident WhatsApp #7).

## Le gap fermé

`SALES_LIST_ORDERS` ("mes commandes reçues") affichait des ventes 🟡
(`PENDING_PRODUCER_CONFIRMATION`) sans jamais dire au producteur que
*confirmer*/*annuler* s'y appliquaient — contrairement à
`_producer_sales_block` (flows/buyer/order_tracking.py), qui porte déjà ce
rappel. Signalé par un producteur réel après un test du parcours de
confirmation : la liste montrait des commandes en attente sans lui dire
comment agir dessus."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone

from tests.conftest import run

from ladini.services.database.producer import ProducerMgmtMixin


async def _async_return(value):
    return value


def _order(producer_id, status):
    product = types.SimpleNamespace(id=uuid.uuid4(), name="Riz", unit="KG", producer_id=producer_id)
    item = types.SimpleNamespace(product=product, quantity=10.0, price_at_sale=250.0)
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        whatsapp_id=None,
        status=status,
        items=[item],
        offer=None,
        total_amount=2500.0,
        currency="XOF",
        customer_name="Acheteur Test",
        customer_phone="+22670000099",
        market_offer_id=None,
        order_type="STANDARD",
        created_at=datetime.now(timezone.utc),
        gps_lat=None,
        gps_lng=None,
        city=None,
        delivery_desc=None,
        delivery_status="PENDING",
        payment_status="PENDING",
    )


class _Result:
    def __init__(self, orders):
        self._orders = orders

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self._orders


def _service_with_orders(orders, producer_id):
    class _Session:
        async def execute(self, stmt):
            return _Result(orders)

    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return _Session()

    svc = _Svc()
    svc._resolve_producer_phone = lambda phone=None, producer_id=None: _async_return(
        phone or "+22670000001"
    )
    svc.get_producer_profile = lambda phone: _async_return(
        (
            types.SimpleNamespace(id=uuid.uuid4(), phone=phone, name="Producteur Test"),
            types.SimpleNamespace(id=producer_id),
        )
    )
    return svc


class TestPendingConfirmationFooter:
    def test_a_pending_confirmation_order_gets_the_action_footer(self):
        producer_id = uuid.uuid4()
        order = _order(producer_id, "PENDING_PRODUCER_CONFIRMATION")
        svc = _service_with_orders([order], producer_id)

        result = run(svc.get_producer_orders(phone="+22670000001"))

        assert "confirmer" in result["formatted_menu"].lower()
        assert "annuler" in result["formatted_menu"].lower()

    def test_no_pending_confirmation_order_skips_the_footer(self):
        producer_id = uuid.uuid4()
        order = _order(producer_id, "CONFIRMED")
        svc = _service_with_orders([order], producer_id)

        result = run(svc.get_producer_orders(phone="+22670000001"))

        assert "confirmer" not in result["formatted_menu"].lower()
