"""B28 — politique de récupération d'une occurrence (pure) + réouverture par la primitive de fulfillment.

`can_recover_occurrence` est LA décision (aucun flux conversationnel ne la prend). Les mêmes règles sont prouvées avec locks et
concurrence réels dans `tests/schema/test_recurring_occurrence_recovery_pg.py`.
"""
from __future__ import annotations

import types
import uuid
from datetime import date, datetime, timedelta

import pytest

from ladini.services.database import recurring_supply as rs
from ladini.services.database.recurring_supply import (
    RECOVERY_REASONS,
    RecurringSupplyMixin,
    can_recover_occurrence,
    recovery_window_open,
)
from tests.conftest import run
from tests.unit.test_recurring_fulfillment_closure import _line, _Rows, _Session

TODAY = date(2026, 10, 5)


def _occurrence(status, day=TODAY):
    return types.SimpleNamespace(status=status, occurrence_date=datetime.combine(day, datetime.min.time()))


def _decide(status, *, day=TODAY, need="ACTIVE", live=0, failed=False, user=True):
    return can_recover_occurrence(_occurrence(status, day), need_status=need, today=TODAY, live_orders=live,
                                  sourcing_failed=failed, user_initiated=user)


# ── matrice de récupérabilité (docs/RECURRING_RECOVERY_CONTRACT.md §4) ──────────────────────────────────────────────────
@pytest.mark.parametrize("status,kwargs,outcome,recoverable", [
    ("OPEN", {}, "RECOVERABLE", True),                                              # déjà cherchable : on re-matche
    ("MATCHED", {}, "RECOVERABLE", True),                                           # proposition pendante / allocation expirée
    ("ACCEPTED", {"live": 0}, "RECOVERABLE", True),                                 # toutes les tentatives ont échoué
    ("PARTIALLY_ACCEPTED", {"live": 0}, "RECOVERABLE", True),
    ("ACCEPTED", {"live": 1}, "OCCURRENCE_COMMITTED", False),                       # engagement irréversible
    ("PARTIALLY_ACCEPTED", {"live": 2}, "OCCURRENCE_COMMITTED", False),
    ("UNFULFILLED", {"failed": True}, "RECOVERABLE", True),                         # clôturé avant B28 par un échec producteur
    ("UNFULFILLED", {"failed": False}, "NOT_RECOVERABLE", False),                   # annulation acheteur / réception litigieuse
    ("REJECTED", {"user": True}, "RECOVERABLE", True),                              # « pas celui-là, cherche un autre »
    ("REJECTED", {"user": False}, "NOT_RECOVERABLE", False),                        # jamais automatique
    ("EXPIRED", {}, "RECOVERY_WINDOW_CLOSED", False),
    ("SKIPPED", {}, "NOT_RECOVERABLE", False),
    ("CANCELLED", {}, "NOT_RECOVERABLE", False),
    ("FULFILLED", {}, "NOT_RECOVERABLE", False),
    ("PARTIALLY_FULFILLED", {}, "NOT_RECOVERABLE", False),
    ("OPEN", {"need": "PAUSED"}, "NEED_NOT_ACTIVE", False),
    ("MATCHED", {"need": "CANCELLED"}, "NEED_NOT_ACTIVE", False),
    ("OPEN", {"day": TODAY - timedelta(days=1)}, "RECOVERY_WINDOW_CLOSED", False),   # date dépassée : jamais « la suivante »
    ("ACCEPTED", {"day": TODAY - timedelta(days=1)}, "RECOVERY_WINDOW_CLOSED", False),
    ("OPEN", {"day": TODAY}, "RECOVERABLE", True),                                  # le jour de livraison est encore dans la fenêtre
    ("OPEN", {"day": TODAY + timedelta(days=30)}, "RECOVERABLE", True),
    ("SOMETHING_NEW", {}, "NOT_RECOVERABLE", False),                                # statut inconnu : fail-closed
])
def test_recoverability_matrix(status, kwargs, outcome, recoverable):
    decision = _decide(status, **kwargs)
    assert (decision.outcome, decision.recoverable) == (outcome, recoverable)


def test_the_window_is_the_delivery_day_inclusive_and_uses_no_hardcoded_hours():
    assert recovery_window_open(datetime(2026, 10, 5), date(2026, 10, 5)) is True
    assert recovery_window_open(datetime(2026, 10, 5, 23, 59), date(2026, 10, 5)) is True
    assert recovery_window_open(datetime(2026, 10, 4), date(2026, 10, 5)) is False


def test_in_place_means_already_searchable():
    assert _decide("OPEN").in_place and _decide("MATCHED").in_place and not _decide("ACCEPTED").in_place


def test_reasons_are_structured_not_free_text():
    assert set(RECOVERY_REASONS) == {"PRODUCER_TIMEOUT", "PRODUCER_REJECTED", "PRODUCER_CANCELLED", "PROPOSAL_REJECTED"}


# ── la primitive de fulfillment rouvre ou clôture selon la même décision ─────────────────────────────────────────────────
class _FailSession(_Session):
    """Session factice : lignes de commandes, statut du besoin et notes d'historique."""

    def __init__(self, rows, occurrence, *, need_status="ACTIVE", notes=("producer_confirmation_expired",)):
        super().__init__(rows, occurrence)
        self.need_status, self.notes = need_status, list(notes)

    async def scalar(self, stmt):
        return self.need_status if "recurring_needs" in str(stmt) else self.occurrence

    async def execute(self, stmt):
        return _Rows(self.notes if "order_status_history" in str(stmt) else self.rows)


class _Svc(RecurringSupplyMixin):
    def __init__(self, session):
        self._fake = session

    @property
    def session(self):
        return self._fake


def _failed_occurrence(day):
    return types.SimpleNamespace(
        id=uuid.uuid4(), recurring_need_id=uuid.uuid4(), status="ACCEPTED", requested_quantity=40, quantity_delivered=0,
        version=3, unit="KG", order_group_id=None, occurrence_date=datetime.combine(day, datetime.min.time()),
        quantity_confirmed=40, quantity_matched=40,
    )


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    monkeypatch.setattr(rs, "_today", lambda: TODAY)


@pytest.mark.parametrize("role,notes,reason", [
    ("SYSTEM", ("producer_confirmation_expired",), "PRODUCER_TIMEOUT"),
    ("PRODUCER", ("declined_by_producer",), "PRODUCER_REJECTED"),
    ("PRODUCER", ("cancelled_by_producer",), "PRODUCER_CANCELLED"),
])
def test_a_failed_attempt_with_an_open_window_reopens_the_occurrence_with_a_structured_reason(role, notes, reason):
    occ = _failed_occurrence(TODAY + timedelta(days=1))
    sess = _FailSession([_line("CANCELLED", 40, role=role)], occ, notes=notes)
    res = run(_Svc(sess)._recompute_occurrence_fulfillment(occ, reason="x"))
    assert occ.status == "OPEN" and occ.quantity_matched == 0 and occ.quantity_confirmed == 0 and occ.version == 4
    assert res["recovery"]["outcome"] == "RECOVERABLE" and res["recovery"]["reason"] == reason
    assert res["recovery"]["occurrence_version"] == 4 and res["recovery"]["occurrence_id"] == str(occ.id)


def test_a_failed_attempt_after_the_delivery_date_is_closed_honestly():
    occ = _failed_occurrence(TODAY - timedelta(days=1))
    res = run(_Svc(_FailSession([_line("CANCELLED", 40, role="SYSTEM")], occ))._recompute_occurrence_fulfillment(occ))
    assert occ.status == "UNFULFILLED" and res["recovery"]["outcome"] == "RECOVERY_WINDOW_CLOSED"


def test_a_buyer_cancellation_is_not_a_sourcing_failure():
    occ = _failed_occurrence(TODAY + timedelta(days=1))
    res = run(_Svc(_FailSession([_line("CANCELLED", 40, role="BUYER")], occ))._recompute_occurrence_fulfillment(occ))
    assert occ.status == "UNFULFILLED" and "recovery" not in res


def test_a_paused_need_does_not_reopen_a_failed_delivery():
    occ = _failed_occurrence(TODAY + timedelta(days=1))
    res = run(_Svc(_FailSession([_line("CANCELLED", 40, role="SYSTEM")], occ, need_status="PAUSED"))._recompute_occurrence_fulfillment(occ))
    assert occ.status == "UNFULFILLED" and res["recovery"]["outcome"] == "NEED_NOT_ACTIVE"


def test_a_partly_delivered_occurrence_is_never_reopened():
    occ = _failed_occurrence(TODAY + timedelta(days=1))
    rows = [_line("DELIVERED", 25), _line("CANCELLED", 15, role="PRODUCER")]
    run(_Svc(_FailSession(rows, occ))._recompute_occurrence_fulfillment(occ))
    assert occ.status == "PARTIALLY_FULFILLED"


def test_one_live_order_keeps_the_occurrence_committed():
    occ = _failed_occurrence(TODAY + timedelta(days=1))
    rows = [_line("ACTIVE", 25), _line("CANCELLED", 15, role="PRODUCER")]
    run(_Svc(_FailSession(rows, occ))._recompute_occurrence_fulfillment(occ))
    assert occ.status == "ACCEPTED"
