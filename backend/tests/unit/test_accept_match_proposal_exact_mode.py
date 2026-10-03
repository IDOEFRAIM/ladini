"""B11-recurring (2026-10-03) — `accept_match_proposal` en MODE EXACT (`occurrence_id`).

La réponse « oui » à un digest doit viser UNE occurrence précise et la REVALIDER au moment de
l'action. Code réel du service, session factice (aucune base) : on prouve (a) chaque refus métier
renvoie un `outcome` structuré SANS aucune mutation, (b) l'acceptation crée les commandes UNE fois et
un second « oui » devient ALREADY_PROCESSED (aucune seconde commande/allocation/évènement).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta

import pytest

from ladini.services.database import recurring_supply as rs
from ladini.services.database.recurring_supply import RecurringSupplyMixin
from tests.conftest import run

_NOW = datetime(2026, 10, 3, 8, 0, 0)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, need, occurrence, allocations, products):
        self.need, self.occurrence, self.allocations, self.products = need, occurrence, allocations, products
        self.added: list = []
        self._scalars = [need, occurrence]

    async def scalar(self, _stmt):
        return self._scalars.pop(0) if self._scalars else None

    async def execute(self, _stmt):
        return _Result(self.allocations)

    async def get(self, _model, pk):
        return self.products.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


class _Service(RecurringSupplyMixin):
    def __init__(self, session):
        self._fake_session = session

    @property
    def session(self):
        return self._fake_session

    async def get_buyer_profile(self, phone):
        return types.SimpleNamespace(id=uuid.uuid4(), zone_id=None, name="Resto"), types.SimpleNamespace(
            id=uuid.uuid4(), establishment_name="Resto"
        )


class _Emitter:
    events: list = []

    def __init__(self, _session):
        pass

    async def emit(self, **kw):
        _Emitter.events.append(kw)

    async def emit_product_quantity_changed(self, *a, **kw):
        _Emitter.events.append({"qty_changed": True})


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    _Emitter.events = []
    monkeypatch.setattr(rs, "BusinessEventEmitter", _Emitter)
    monkeypatch.setattr(rs, "order_item_snapshot_columns", lambda *a, **k: {})
    monkeypatch.setattr(rs, "_today", lambda: _NOW.date())
    monkeypatch.setattr(rs, "normalize_phone", lambda p, required=False: p)


def _case(*, need_status="ACTIVE", occ_status="MATCHED", occ_date=None, notified=_NOW - timedelta(hours=2),
          updated=_NOW - timedelta(hours=3), stock=100.0, qty=40.0, requested=40.0, allocations=True):
    need = types.SimpleNamespace(id=uuid.uuid4(), status=need_status, sub_category_id=None)
    occ = types.SimpleNamespace(
        id=uuid.uuid4(), status=occ_status, occurrence_date=occ_date or (_NOW + timedelta(days=1)),
        quantity_matched=qty, requested_quantity=requested, unit="KG", version=3, notified_at=notified,
        updated_at=updated, quantity_confirmed=0, order_group_id=None, accepted_at=None,
    )
    pid = uuid.uuid4()
    product = types.SimpleNamespace(id=pid, name="tomate", quantity_for_sale=stock, unit="KG")
    allocs = [
        types.SimpleNamespace(id=uuid.uuid4(), producer_id=uuid.uuid4(), product_id=pid, quantity=qty,
                              unit_price=500.0, unit="KG", status="PROPOSED", order_item_id=None)
    ] if allocations else []
    session = _Session(need, occ, allocs, {pid: product})
    return _Service(session), need, occ, allocs, product, session


def _accept(svc, need, occ, action="ACCEPT", **kw):
    return run(svc.accept_match_proposal("+22670000001", str(need.id), action, occurrence_id=str(occ.id), **kw))


def _no_mutation(session, allocs, product, stock):
    assert session.added == []
    assert all(a.status == "PROPOSED" for a in allocs)
    assert product.quantity_for_sale == stock
    assert _Emitter.events == []


@pytest.mark.parametrize(
    "kwargs, outcome",
    [
        ({"need_status": "PAUSED"}, "NEED_INACTIVE"),
        ({"need_status": "CANCELLED"}, "NEED_INACTIVE"),
        ({"occ_status": "ACCEPTED"}, "ALREADY_PROCESSED"),
        ({"occ_status": "REJECTED"}, "ALREADY_PROCESSED"),
        ({"occ_date": _NOW - timedelta(days=1)}, "EXPIRED"),
        ({"notified": None}, "NO_PROPOSAL"),
        ({"notified": _NOW - timedelta(hours=5), "updated": _NOW - timedelta(hours=1)}, "PROPOSAL_CHANGED"),
        ({"qty": 0.0}, "NO_PROPOSAL"),
        ({"allocations": False}, "NO_PROPOSAL"),
        ({"stock": 10.0}, "STOCK_CHANGED"),
    ],
)
def test_invalid_proposal_is_refused_with_structured_outcome_and_zero_mutation(kwargs, outcome):
    svc, need, occ, allocs, product, session = _case(**kwargs)
    stock_before = product.quantity_for_sale
    res = _accept(svc, need, occ)
    assert res["status"] == "success" and res["outcome"] == outcome
    _no_mutation(session, allocs, product, stock_before)
    assert occ.status == kwargs.get("occ_status", "MATCHED")


def test_unknown_occurrence_is_not_found_without_leak():
    svc, need, occ, allocs, product, session = _case()
    session._scalars = [need, None]
    res = _accept(svc, need, occ)
    assert res["outcome"] == "NOT_FOUND"
    _no_mutation(session, allocs, product, 100.0)


def test_reject_exact_marks_that_occurrence_only_and_creates_no_order():
    svc, need, occ, allocs, product, session = _case()
    res = _accept(svc, need, occ, action="REJECT")
    assert res["action"] == "REJECT" and occ.status == "REJECTED"
    assert all(a.status == "REJECTED" for a in allocs)
    assert session.added == [] and product.quantity_for_sale == 100.0


def test_accept_exact_creates_orders_once_then_replay_is_already_processed():
    svc, need, occ, allocs, product, session = _case()
    res = _accept(svc, need, occ)
    assert res.get("outcome") is None and res["action"] == "ACCEPT"
    assert occ.status == "ACCEPTED" and len(res["order_ids"]) == 1
    assert product.quantity_for_sale == 60.0  # débité UNE fois
    assert all(a.status == "CONVERTED" for a in allocs)
    orders = [o for o in session.added if o.__class__.__name__ == "Order"]
    assert len(orders) == 1
    events = len(_Emitter.events)

    # « oui » rejoué : la même occurrence n'est plus OPEN/MATCHED
    session._scalars = [need, occ]
    res2 = _accept(svc, need, occ)
    assert res2["outcome"] == "ALREADY_PROCESSED"
    assert len([o for o in session.added if o.__class__.__name__ == "Order"]) == 1
    assert product.quantity_for_sale == 60.0 and len(_Emitter.events) == events


def test_partial_proposal_leaves_occurrence_partially_accepted_for_the_matched_quantity_only():
    svc, need, occ, allocs, product, session = _case(qty=60.0, requested=100.0)
    res = _accept(svc, need, occ)
    assert occ.status == "PARTIALLY_ACCEPTED" and res["quantity_confirmed"] == 60.0
    assert product.quantity_for_sale == 40.0


def test_legacy_mode_now_refuses_inactive_need():
    from ladini.services.database.errors import BusinessRuleException

    svc, need, occ, allocs, product, session = _case(need_status="CANCELLED")
    with pytest.raises(BusinessRuleException) as exc:
        run(svc.accept_match_proposal("+22670000001", str(need.id), "ACCEPT"))
    assert exc.value.reason == "need_inactive"
    assert session.added == []


# ── B12 : contrat de VERSION (garde principale) ─────────────────────────────────────────────────

def test_expected_version_match_accepts_even_without_notification_detail_screen_path():
    # Écran détail : aucune notification, la version AFFICHÉE fait foi (même service durci).
    svc, need, occ, allocs, product, session = _case(notified=None)
    res = _accept(svc, need, occ, expected_version=3)
    assert res.get("outcome") is None and occ.status == "ACCEPTED"


@pytest.mark.parametrize("action", ["ACCEPT", "REJECT"])
def test_expected_version_mismatch_is_refused_for_accept_and_reject_with_zero_mutation(action):
    svc, need, occ, allocs, product, session = _case()
    res = _accept(svc, need, occ, action=action, expected_version=2)
    assert res["outcome"] == "PROPOSAL_CHANGED"
    assert res["expected_version"] == 2 and res["current_version"] == 3
    assert occ.status == "MATCHED" and all(a.status == "PROPOSED" for a in allocs)
    _no_mutation(session, allocs, product, 100.0)


def test_updated_at_is_no_longer_the_main_guard_when_a_version_is_provided():
    # updated_at > notified_at (ancienne heuristique) NE refuse PLUS quand la version correspond.
    svc, need, occ, allocs, product, session = _case(
        notified=_NOW - timedelta(hours=5), updated=_NOW - timedelta(hours=1)
    )
    res = _accept(svc, need, occ, expected_version=3)
    assert res.get("outcome") is None and occ.status == "ACCEPTED"


def test_expired_status_gives_expired_outcome():
    svc, need, occ, allocs, product, session = _case(occ_status="EXPIRED")
    assert _accept(svc, need, occ)["outcome"] == "EXPIRED"


def test_unavailable_product_is_refused_before_any_mutation():
    svc, need, occ, allocs, product, session = _case()
    product.is_available = False
    res = _accept(svc, need, occ, expected_version=3)
    assert res["outcome"] == "PRODUCER_UNAVAILABLE"
    _no_mutation(session, allocs, product, 100.0)


def test_blocked_producer_account_is_refused_before_any_mutation(monkeypatch):
    async def _blocked(_session, _producer_id):
        return True

    monkeypatch.setattr(_Service, "_producer_account_blocked", staticmethod(_blocked))
    svc, need, occ, allocs, product, session = _case()
    res = _accept(svc, need, occ, expected_version=3)
    assert res["outcome"] == "PRODUCER_UNAVAILABLE"
    _no_mutation(session, allocs, product, 100.0)


def test_sweep_marks_past_occurrences_expired_with_version_bump_and_expires_their_allocations():
    class _SweepSession:
        def __init__(self, rows):
            self.rows, self.executed, self.flushed = rows, [], False

        async def execute(self, stmt):
            self.executed.append(stmt)
            return _Result(self.rows)

        async def flush(self):
            self.flushed = True

    rows = [
        types.SimpleNamespace(id=uuid.uuid4(), recurring_need_id=uuid.uuid4(), status="MATCHED", version=4,
                              notified_at=_NOW),
        types.SimpleNamespace(id=uuid.uuid4(), recurring_need_id=uuid.uuid4(), status="OPEN", version=1,
                              notified_at=None),
    ]
    sess = _SweepSession(rows)
    res = run(_Service(sess).expire_past_occurrences())
    assert res == {"occurrences_expired": 2}
    assert [r.status for r in rows] == ["EXPIRED", "EXPIRED"] and [r.version for r in rows] == [5, 2]
    assert len(sess.executed) == 2 and sess.flushed  # le SELECT puis l'UPDATE des allocations PROPOSED


def test_sweep_with_nothing_to_expire_is_a_no_op():
    class _Empty:
        async def execute(self, stmt):
            return _Result([])

    assert run(_Service(_Empty()).expire_past_occurrences()) == {"occurrences_expired": 0}


def test_latest_digest_snapshot_reads_occurrence_versions_from_the_outbox_payload():
    class _S:
        def __init__(self, payload):
            self.payload = payload

        async def scalar(self, _stmt):
            return self.payload

    ok = {"occurrences": [{"occurrence_id": "o1", "version": 7}, {"occurrence_id": "o2", "version": 2}]}
    assert run(_Service(_S(ok))._latest_digest_snapshot("+226")) == {"o1": 7, "o2": 2}
    assert run(_Service(_S({"body": "legacy digest"}))._latest_digest_snapshot("+226")) is None
    assert run(_Service(_S(None))._latest_digest_snapshot("+226")) is None
