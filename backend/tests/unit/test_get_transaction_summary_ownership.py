"""`BuyerMixin.get_transaction_summary` — ownership check on the `order_id`
lookup path (P0-SEC-004, backend/tests/evals/datasets/security_redteam/P0-SEC-004.yaml).

CONFIRMED bug (real code trace + eval-harness execution, prior session): when
`order_id` is provided, the query filters ONLY on `Order.id == order_id` and
never touches `buyer_phone`, even though `buyer_phone` is always passed
alongside it by the real caller (flows/buyer/order_tracking.py::check_order_status).
A buyer who merely knows another buyer's order_id gets that order's full
details. Same style as tests/unit/test_select_winning_bid_geofencing.py — a
mixin instantiated directly with a stubbed `session`, no real DB required.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from tests.conftest import run

ORDER_A_ID = uuid.uuid4()
BUYER_A_PROFILE_ID = uuid.uuid4()
BUYER_B_PROFILE_ID = uuid.uuid4()


def _fake_order(buyer_id) -> SimpleNamespace:
    """Minimal stand-in for the real `Order` ORM row — only the attributes
    `get_transaction_summary` actually reads."""
    return SimpleNamespace(
        id=ORDER_A_ID,
        buyer_id=buyer_id,
        items=[],
        auction_id=None,
        order_type="DIRECT",
        status="CONFIRMED",
        payment_status="PAID",
        delivery_status="PENDING",
        subtotal=45000.0,
        delivery_fee=0.0,
        total_amount=45000.0,
        currency="XOF",
        created_at=None,
    )


class _StubSession:
    """Stands in for the real AsyncSession's `.scalar()` — returns ORDER-A
    unconditionally for ANY statement, exactly like a real Postgres would for
    the CURRENT query (`select(Order)....where(Order.id == order_id)`, no
    buyer predicate). This is a faithful simulation, not a mock of desired
    behavior: the real WHERE clause genuinely has no buyer_id filter."""

    def __init__(self, order):
        self._order = order

    async def scalar(self, stmt):
        return self._order


def _service(order, *, resolved_buyer_profile_id):
    from ladini.services.database.buyer import BuyerMixin

    class _Svc(BuyerMixin):
        @property
        def session(self):
            return _StubSession(order)

        async def get_buyer_profile(self, phone: str):
            # Bypasses BaseMixin's real (session-querying) implementation —
            # only the resolved profile id matters for this test.
            user = SimpleNamespace(id=uuid.uuid4(), phone=phone)
            profile = SimpleNamespace(id=resolved_buyer_profile_id)
            return user, profile

    return _Svc()


class TestGetTransactionSummaryOwnership:
    def test_owner_requesting_their_own_order_by_id_gets_the_data(self):
        """Legitimate case: Buyer A, requesting ORDER-A (which they own), by
        order_id — must keep working after any fix."""
        order = _fake_order(buyer_id=BUYER_A_PROFILE_ID)
        svc = _service(order, resolved_buyer_profile_id=BUYER_A_PROFILE_ID)
        result = run(svc.get_transaction_summary(
            order_id=str(ORDER_A_ID), buyer_phone="+22670000004",
        ))
        assert result["status"] == "success"
        assert result["data"] is not None
        assert result["data"]["order_id"] == str(ORDER_A_ID)

    def test_a_different_buyer_who_knows_the_order_id_cannot_read_it(self):
        """The bug: Buyer B, who owns a DIFFERENT order, requests ORDER-A
        (owned by Buyer A) by order_id, using their OWN buyer_phone.
        Must NOT receive ORDER-A's data."""
        order = _fake_order(buyer_id=BUYER_A_PROFILE_ID)
        svc = _service(order, resolved_buyer_profile_id=BUYER_B_PROFILE_ID)
        result = run(svc.get_transaction_summary(
            order_id=str(ORDER_A_ID), buyer_phone="+22670000099",
        ))
        assert result["data"] is None, (
            f"Buyer B received another buyer's order data: {result!r}"
        )
