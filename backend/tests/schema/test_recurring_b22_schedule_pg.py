"""B22 — prochaine livraison d'un besoin ACTIF, sur un VRAI PostgreSQL.

Smoke prod : « Oignon, 75 KG chaque mois, 🟢 Actif, Prochaine livraison : à planifier ». Cause : la fenêtre de
matérialisation est J..J+7 (`OCCURRENCE_WINDOW_DAYS`) ; une règle MENSUELLE n'a donc AUCUNE occurrence matérialisée entre
deux échéances (ni, pour le cron, avant J-7 de la prochaine). `_next_actionable_occurrence` ne lit que les occurrences
matérialisées -> « à planifier » pour un besoin pourtant valide. Invariant B22 : ACTIVE + règle valide => une prochaine
échéance est TOUJOURS calculable (fonction pure `next_due_date`) et matérialisable à la demande.
"""
from __future__ import annotations

from datetime import date, datetime

import psycopg2
import pytest
from factories import Graph
from test_recurring_e2e_pg import _identities, _run, _sql, _Svc

import ladini.services.database.recurring_supply as recurring_supply_module
from ladini.domain.recurring_supply.recurrence import RecurrenceRule, next_due_date

TODAY = date(2027, 3, 10)  # un mercredi


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: TODAY)


def _need(dsn, *, recurrence_type="MONTHLY", starts=date(2027, 3, 8), status="ACTIVE", occurrences=(), g=None, **over):
    """Un besoin (et ses occurrences `(date, statut)`) pour un acheteur. `g` : mini-marché réutilisé (2e besoin du même acheteur)."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = g or Graph(cur)
        g.cur = cur
        need = g.recurring_need(quantity=75, unit="KG", recurrence_type=recurrence_type, starts_at=_dt(starts), status=status, **over)
        for d, st in occurrences:
            g.occurrence(need, occurrence_date=_dt(d), requested_quantity=75, unit="KG", status=st)
    conn.close()
    return g, need


def _call(dsn, g, name, *args, **kw):
    phone, buyer, producer = _identities(dsn, g)

    async def go(session):
        return await getattr(_Svc(session, buyer, producer), name)(phone, *args, **kw)

    return _run(dsn, go)


def _occ_dates(dsn, need):
    return [(r[0].date(), r[1]) for r in _sql(dsn, "select occurrence_date, status from marketplace.recurring_need_occurrences "
                                                   "where recurring_need_id=%s order by occurrence_date", (str(need),))]


# ── fonction pure ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "starts, today, expected",
    [
        (date(2027, 1, 31), date(2027, 2, 1), date(2027, 2, 28)),   # 31 -> dernier jour de février
        (date(2028, 1, 31), date(2028, 2, 1), date(2028, 2, 29)),   # année bissextile
        (date(2027, 1, 30), date(2027, 1, 31), date(2027, 2, 28)),  # 30 -> 28
        (date(2027, 3, 31), date(2027, 4, 1), date(2027, 4, 30)),   # mois de 30 jours
        (date(2027, 1, 31), date(2027, 3, 1), date(2027, 3, 31)),   # l'ancre revient (jamais dérivée du 28)
        (date(2027, 3, 8), date(2027, 3, 8), date(2027, 3, 8)),     # le jour même reste dû
        (date(2027, 3, 8), date(2027, 3, 9), date(2027, 4, 8)),     # déjà passé -> mois suivant
        (date(2026, 12, 15), date(2027, 3, 10), date(2027, 3, 15)), # début lointain dans le passé
        (date(2027, 12, 31), date(2027, 3, 10), date(2027, 12, 31)),  # début futur
    ],
)
def test_monthly_next_due_date_is_exact(starts, today, expected):
    assert next_due_date(RecurrenceRule("MONTHLY", starts_at=starts), from_date=today) == expected


def test_next_due_date_skips_played_dates_and_respects_the_end():
    rule = RecurrenceRule("WEEKLY", starts_at=date(2027, 3, 3))
    assert next_due_date(rule, from_date=TODAY) == date(2027, 3, 10)
    assert next_due_date(rule, from_date=TODAY, skip={date(2027, 3, 10)}) == date(2027, 3, 17)
    assert next_due_date(RecurrenceRule("MONTHLY", starts_at=date(2027, 1, 5), ends_at=date(2027, 3, 1)), from_date=TODAY) is None
    assert next_due_date(RecurrenceRule("ONE_OFF", starts_at=date(2027, 3, 1)), from_date=TODAY) is None


# ── ROUGE avant correctif : l'Oignon mensuel ────────────────────────────────────────────────────────────────────────
def test_active_monthly_need_whose_occurrence_is_past_lists_next_month(pg_dsn):
    """Créé il y a 2 jours (début le 8), l'occurrence du 8 est jouée ; la suivante (8 avril) est hors fenêtre J+7."""
    g, need = _need(pg_dsn, occurrences=[(date(2027, 3, 8), "EXPIRED")])
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["status"] == "ACTIVE" and item["schedule_state"] == "OK"
    assert item["next_occurrence_date"] == "2027-04-08"
    assert item["next_occurrence_materialized"] is False
    assert _occ_dates(pg_dsn, need) == [(date(2027, 3, 8), "EXPIRED")], "la liste est une LECTURE : rien n'est matérialisé"


def test_monthly_need_with_no_occurrence_at_all_lists_its_next_due_date(pg_dsn):
    g, need = _need(pg_dsn, starts=date(2027, 1, 31))  # donnée legacy : aucune occurrence
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["next_occurrence_date"] == "2027-03-31"


def test_ensure_materializes_the_next_monthly_due_date_on_demand(pg_dsn):
    g, need = _need(pg_dsn, occurrences=[(date(2027, 3, 8), "EXPIRED")])
    res = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    assert res["occurrence_id"] and res["occurrence_status"] == "OPEN" and res["created"] == 1
    assert _occ_dates(pg_dsn, need) == [(date(2027, 3, 8), "EXPIRED"), (date(2027, 4, 8), "OPEN")]
    again = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    assert again["occurrence_id"] == res["occurrence_id"] and again["created"] == 0
    cron = _run(pg_dsn, lambda s: _Svc(s, *_identities(pg_dsn, g)[1:]).replenish_occurrence_windows())
    assert cron["occurrences_created"] >= 0
    assert len(_occ_dates(pg_dsn, need)) == 2, "le cron ne duplique pas l'occurrence déjà matérialisée"
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["next_occurrence_date"] == "2027-04-08" and item["next_occurrence_materialized"] is True
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    assert detail["occurrence_date"] == "2027-04-08" and detail["occurrence_status"] == "OPEN"


def test_a_played_occurrence_today_is_not_the_next_delivery(pg_dsn):
    """Aujourd'hui (10 mars) la livraison est EXPIRÉE : la prochaine est dans 7 jours, jamais « aujourd'hui »."""
    g, need = _need(pg_dsn, recurrence_type="WEEKLY", starts=date(2027, 3, 3),
                    occurrences=[(date(2027, 3, 10), "EXPIRED")])
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["next_occurrence_date"] == "2027-03-17" and item["next_occurrence_is_today"] is False
    res = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    assert [d for d, _ in _occ_dates(pg_dsn, need)] == [date(2027, 3, 10), date(2027, 3, 17)] and res["created"] == 1


def test_an_actionable_occurrence_today_is_reported_as_today(pg_dsn):
    for status in ("OPEN", "MATCHED", "ACCEPTED", "PARTIALLY_ACCEPTED"):
        g, need = _need(pg_dsn, recurrence_type="WEEKLY", starts=date(2027, 3, 3), occurrences=[(date(2027, 3, 10), status)])
        item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
        assert item["next_occurrence_date"] == "2027-03-10" and item["next_occurrence_is_today"] is True, status
        assert item["next_occurrence_materialized"] is True


@pytest.mark.parametrize("terminal", ["REJECTED", "EXPIRED", "FULFILLED", "PARTIALLY_FULFILLED", "UNFULFILLED", "SKIPPED", "CANCELLED"])
def test_terminal_statuses_never_hide_a_future_occurrence(pg_dsn, terminal):
    g, need = _need(pg_dsn, recurrence_type="DAILY", starts=date(2027, 3, 1),
                    occurrences=[(date(2027, 3, 10), terminal), (date(2027, 3, 11), "OPEN")])
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["next_occurrence_date"] == "2027-03-11" and item["next_occurrence_status"] == "OPEN"


# ── règles invalides / terminées / suspendues ───────────────────────────────────────────────────────────────────────
def test_an_ended_schedule_is_reported_explicitly(pg_dsn):
    g, need = _need(pg_dsn, starts=date(2027, 1, 5), ends_at=_dt(date(2027, 3, 1)))
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["status"] == "ACTIVE" and item["schedule_state"] == "ENDED" and item["next_occurrence_date"] is None
    res = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    assert res["occurrence_id"] is None and _occ_dates(pg_dsn, need) == []


def test_an_invalid_legacy_schedule_does_not_crash_and_is_reported_incomplete(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        try:
            cur.execute("SAVEPOINT s")
            need = g.recurring_need(recurrence_type="WEEKLY_DAYS", weekly_days=None, starts_at=_dt(date(2027, 3, 1)))
        except psycopg2.Error:
            cur.execute("ROLLBACK TO SAVEPOINT s")
            need = None
    conn.close()
    if need is None:
        pytest.skip("la base refuse déjà ce planning invalide (CHECK) : aucune donnée legacy de ce type possible")
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["schedule_state"] == "INVALID" and item["next_occurrence_date"] is None
    assert _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))["occurrence_id"] is None


def test_paused_need_is_not_active_and_never_auto_materializes(pg_dsn):
    g, need = _need(pg_dsn, recurrence_type="WEEKLY", starts=date(2027, 3, 3), status="PAUSED")
    item = _call(pg_dsn, g, "list_my_recurring_needs")["items"][0]
    assert item["status"] == "PAUSED" and item["next_occurrence_date"] is None and item["schedule_state"] == "PAUSED"
    res = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    assert res["occurrence_id"] is None and _occ_dates(pg_dsn, need) == []
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    assert detail["need_status"] == "PAUSED" and detail["occurrence_date"] is None


def test_cancelled_need_is_not_listed_and_not_materialized(pg_dsn):
    g, need = _need(pg_dsn, status="CANCELLED")
    assert _call(pg_dsn, g, "list_my_recurring_needs")["items"] == []
    assert _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))["occurrence_id"] is None
    assert _occ_dates(pg_dsn, need) == []


# ── propriété, ordre, doublons légitimes ────────────────────────────────────────────────────────────────────────────
def test_two_equal_needs_started_on_different_days_are_both_kept_and_distinguishable(pg_dsn):
    """Chèvre #3 / #4 du smoke : 3 UNITE chaque semaine démarrées à des dates différentes = deux engagements distincts."""
    g, first = _need(pg_dsn, recurrence_type="WEEKLY", starts=date(2027, 3, 13), occurrences=[(date(2027, 3, 13), "EXPIRED")])
    _g, second = _need(pg_dsn, recurrence_type="WEEKLY", starts=date(2027, 3, 11), g=g)
    items = _call(pg_dsn, g, "list_my_recurring_needs")["items"]
    assert [i["recurring_need_id"] for i in items] == [str(first), str(second)], "ordre = création, puis id"
    assert [i["starts_on"] for i in items] == ["2027-03-13", "2027-03-11"]
    assert items[0]["next_occurrence_date"] == "2027-03-20" and items[1]["next_occurrence_date"] == "2027-03-11"


def test_list_order_is_creation_then_id_and_stable(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        ids = [str(g.recurring_need(quantity=10 + i, unit="KG", recurrence_type="DAILY", starts_at=_dt(TODAY),
                                     created_at=datetime(2027, 3, 1, 12, 0, 0))) for i in range(4)]
    conn.close()
    first = [i["recurring_need_id"] for i in _call(pg_dsn, g, "list_my_recurring_needs")["items"]]
    second = [i["recurring_need_id"] for i in _call(pg_dsn, g, "list_my_recurring_needs")["items"]]
    assert first == second == sorted(ids), "ex aequo sur created_at : départagés par id, jamais par l'ordre physique"


def test_ownership_a_buyer_never_sees_nor_touches_another_buyers_need(pg_dsn):
    g, mine = _need(pg_dsn, occurrences=[(date(2027, 3, 8), "EXPIRED")])
    other_g, theirs = _need(pg_dsn)
    assert [i["recurring_need_id"] for i in _call(pg_dsn, g, "list_my_recurring_needs")["items"]] == [str(mine)]
    from ladini.services.database.errors import BusinessRuleException

    for name in ("get_recurring_need_detail", "ensure_next_recurring_occurrence", "refresh_recurring_need_matching"):
        with pytest.raises(BusinessRuleException):
            _call(pg_dsn, g, name, str(theirs))
    assert _occ_dates(pg_dsn, theirs) == [], "rien n'a été matérialisé pour le besoin d'autrui"
