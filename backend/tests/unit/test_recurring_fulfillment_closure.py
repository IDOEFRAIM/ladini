"""B14 — fermeture du cycle recurring : l'occurrence atteint UN état terminal déterministe.

Code réel de la primitive `_recompute_occurrence_fulfillment` (session factice : les lignes terminales sont fournies
comme le ferait la jointure allocations -> lignes -> commandes). Les mêmes scénarios, avec verrous et concurrence réels,
sont dans `tests/schema/test_recurring_fulfillment_pg.py` (CI).
"""
from __future__ import annotations

import itertools
import types
import uuid
from datetime import datetime, timedelta

import pytest

from ladini.services.database import recurring_supply as rs
from ladini.services.database.recurring_supply import RecurringSupplyMixin
from tests.conftest import run

_NOW = datetime(2026, 10, 3, 8, 0, 0)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows=(), occurrence=None):
        self.rows, self.occurrence = list(rows), occurrence
        self.flushes = 0

    async def scalar(self, _stmt):
        return self.occurrence

    async def execute(self, _stmt):
        return _Rows(self.rows)

    async def flush(self):
        self.flushes += 1


class _Svc(RecurringSupplyMixin):
    def __init__(self, session):
        self._fake = session

    @property
    def session(self):
        return self._fake


def _occ(status="ACCEPTED", requested=100, delivered=0, version=3, unit="KG"):
    return types.SimpleNamespace(id=uuid.uuid4(), recurring_need_id=uuid.uuid4(), status=status,
                                 requested_quantity=requested, quantity_delivered=delivered, version=version, unit=unit)


def _line(state, qty, unit="KG"):
    """state : DELIVERED | ACTIVE | CANCELLED | ISSUE — traduit en (order.status, order.delivery_status)."""
    status, delivery = {
        "DELIVERED": ("COMPLETED", "RECEIVED"),
        "ACTIVE": ("CONFIRMED", "PENDING"),
        "PENDING_PRODUCER": ("PENDING_PRODUCER_CONFIRMATION", "PENDING"),
        "IN_TRANSIT": ("CONFIRMED", "IN_TRANSIT"),
        "DELIVERED_NOT_RECEIVED": ("CONFIRMED", "DELIVERED"),
        "CANCELLED": ("CANCELLED", "PENDING"),
        "ISSUE": ("CONFIRMED", "RECEIVED_WITH_ISSUE"),
    }[state]
    return (uuid.uuid4(), status, delivery, qty, unit)


def _recompute(occ, lines):
    sess = _Session(lines, occ)
    res = run(_Svc(sess)._recompute_occurrence_fulfillment(occ))
    return res, sess


@pytest.mark.parametrize(
    "lines, status, delivered",
    [
        ([("DELIVERED", 100)], "FULFILLED", 100),                                                   # 1 une commande livrée
        ([("DELIVERED", 60), ("DELIVERED", 40)], "FULFILLED", 100),                                 # 2 toutes livrées
        ([("DELIVERED", 60), ("ACTIVE", 40)], "ACCEPTED", 60),                                      # 3 une en cours : pas terminal
        ([("DELIVERED", 60), ("PENDING_PRODUCER", 40)], "ACCEPTED", 60),                            # 3bis attente producteur
        ([("DELIVERED", 60), ("IN_TRANSIT", 40)], "ACCEPTED", 60),
        ([("DELIVERED", 60), ("DELIVERED_NOT_RECEIVED", 40)], "ACCEPTED", 60),                      # livrée, réception attendue
        ([("DELIVERED", 60), ("CANCELLED", 40)], "PARTIALLY_FULFILLED", 60),                        # 4 une livrée + une rejetée
        ([("CANCELLED", 60), ("CANCELLED", 40)], "UNFULFILLED", 0),                                 # 5 toutes rejetées/expirées
        ([("DELIVERED", 40), ("DELIVERED", 35), ("CANCELLED", 25)], "PARTIALLY_FULFILLED", 75),     # 33 A=40 B=35 C rejetée
        ([("DELIVERED", 60), ("ISSUE", 40)], "PARTIALLY_FULFILLED", 60),                            # réception avec problème : terminale, non comptée
        ([("ISSUE", 100)], "UNFULFILLED", 0),
        ([("ACTIVE", 100)], "ACCEPTED", 0),
    ],
)
def test_fulfillment_rule(lines, status, delivered):
    occ = _occ()
    res, _ = _recompute(occ, [_line(s, q) for s, q in lines])
    assert occ.status == status and occ.quantity_delivered == delivered
    assert res["status"] == status


def test_delivery_order_never_changes_the_result():
    results = set()
    for perm in itertools.permutations([("DELIVERED", 40), ("DELIVERED", 35), ("DELIVERED", 25)]):
        occ = _occ()
        _recompute(occ, [_line(s, q) for s, q in perm])
        results.add((occ.status, occ.quantity_delivered))
    assert results == {("FULFILLED", 100)}


def test_progression_a_delivered_b_preparing_then_b_rejected_then_partial():
    occ = _occ()
    _recompute(occ, [_line("DELIVERED", 60), _line("ACTIVE", 40)])
    assert occ.status == "ACCEPTED" and occ.quantity_delivered == 60
    _recompute(occ, [_line("DELIVERED", 60), _line("CANCELLED", 40)])
    assert occ.status == "PARTIALLY_FULFILLED"


def test_progression_a_delivered_b_delivered_fulfilled():
    occ = _occ()
    _recompute(occ, [_line("DELIVERED", 60), _line("ACTIVE", 40)])
    _recompute(occ, [_line("DELIVERED", 60), _line("DELIVERED", 40)])
    assert occ.status == "FULFILLED" and occ.quantity_delivered == 100


def test_recompute_is_idempotent_no_version_bump_no_flush_on_replay():
    occ = _occ()
    lines = [_line("DELIVERED", 100)]
    _recompute(occ, lines)
    version, status = occ.version, occ.status
    res, sess = _recompute(occ, lines)  # rejeu
    assert occ.version == version and occ.status == status and res["changed"] is False and sess.flushes == 0


def test_partially_accepted_occurrence_closes_partially_when_only_the_matched_part_is_delivered():
    occ = _occ(status="PARTIALLY_ACCEPTED", requested=100)
    _recompute(occ, [_line("DELIVERED", 60)])
    assert occ.status == "PARTIALLY_FULFILLED" and occ.quantity_delivered == 60  # le reste n'est jamais inventé


@pytest.mark.parametrize("status", ["EXPIRED", "REJECTED", "FULFILLED", "SKIPPED", "OPEN", "MATCHED", "UNFULFILLED"])
def test_non_accepted_statuses_are_never_reopened_or_closed_by_a_recompute(status):
    occ = _occ(status=status)
    _recompute(occ, [_line("CANCELLED", 100)])
    assert occ.status == status


def test_cross_unit_lines_are_never_summed():
    occ = _occ(requested=100, unit="L")
    _recompute(occ, [_line("DELIVERED", 60, unit="L"), _line("DELIVERED", 40, unit="KG")])
    assert occ.quantity_delivered == 60 and occ.status == "PARTIALLY_FULFILLED"  # kg + L jamais additionnés


def test_order_without_lineage_is_ignored():
    occ = _occ()
    res = run(_Svc(_Session([], occ))._recompute_occurrence_fulfillment_for_order(
        types.SimpleNamespace(order_type="PREORDER", checkout_group_id=uuid.uuid4())))
    assert res is None
    res = run(_Svc(_Session([], occ))._recompute_occurrence_fulfillment_for_order(
        types.SimpleNamespace(order_type="RECURRING_SUPPLY", checkout_group_id=None)))
    assert res is None


def test_no_orders_at_all_never_closes_the_occurrence():
    occ = _occ()
    _recompute(occ, [])
    assert occ.status == "ACCEPTED"


# ── TIMEOUT PRODUCTEUR ─────────────────────────────────────────────────────────────────────────────

class _TimeoutSession:
    def __init__(self, orders, items, products, phone="+226"):
        self.orders, self.items, self.products, self.phone = orders, items, products, phone
        self.added: list = []
        self._exec = 0

    async def execute(self, _stmt):
        self._exec += 1
        if self._exec == 1:
            return _Rows(self.orders)
        # alternance par commande : items puis téléphone acheteur
        return _Rows(self.items if self._exec % 2 == 0 else [self.phone])

    async def scalars(self, _stmt):
        return _Rows(self.products)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def test_producer_confirmation_timeout_cancels_restores_stock_once_and_recomputes(monkeypatch):
    import ladini.workers.repositories.outbox_repo as outbox_repo

    sent: list = []

    async def _enqueue(session, entries):
        sent.extend(entries)

    monkeypatch.setattr(outbox_repo, "enqueue", _enqueue)

    class _Emitter:
        def __init__(self, _s):
            pass

        async def emit_product_quantity_changed(self, *a, **k):
            return None

    monkeypatch.setattr(rs, "BusinessEventEmitter", _Emitter)
    monkeypatch.setattr(rs, "_today", lambda: _NOW.date())
    group = uuid.uuid4()
    order = types.SimpleNamespace(id=uuid.uuid4(), status="PENDING_PRODUCER_CONFIRMATION", cancellation_role=None,
                                  buyer_id=uuid.uuid4(), checkout_group_id=group, order_type="RECURRING_SUPPLY",
                                  expected_fulfillment_date=_NOW - timedelta(days=1))
    pid = uuid.uuid4()
    item = types.SimpleNamespace(product_id=pid, quantity=40.0, base_unit_quantity=None, order_id=order.id)
    product = types.SimpleNamespace(id=pid, quantity_for_sale=60.0)
    sess = _TimeoutSession([order], [item], [product])
    svc = _Svc(sess)
    recomputed: list = []

    async def _rc(o, *, reason="x", require_recurring=True):
        recomputed.append((o.id, reason))

    svc._recompute_occurrence_fulfillment_for_order = _rc  # type: ignore[assignment]
    res = run(svc.expire_unconfirmed_recurring_orders())
    assert res == {"recurring_orders_expired": 1}
    assert order.status == "CANCELLED" and order.cancellation_role == "SYSTEM"
    assert product.quantity_for_sale == 100.0  # 60 + 40 restitués UNE fois
    assert recomputed == [(order.id, "producer_confirmation_expired")]
    assert sent and sent[0]["dedupe_key"] == f"ORDER_CANCELLED_BUYER:{order.id}"
    assert any(getattr(h, "note", None) == "producer_confirmation_expired" for h in sess.added)
