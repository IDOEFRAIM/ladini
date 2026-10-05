"""B25 — cohérence des mutations d'un besoin récurrent, contre un VRAI PostgreSQL.

Contrat : docs/RECURRING_MUTATION_CONSISTENCY_CONTRACT.md. Chaque test pose un état réel (besoin, occurrences,
allocations, commande), exécute le service `update_recurring_need` / le cron `replenish_occurrence_windows` et relit
la base. La concurrence est RÉELLE (deux sessions/connexions, verrous de ligne PostgreSQL), jamais simulée par des
doublures. `_today()` est ancré (mardi 2026-09-15) ; les dates ne dépendent jamais de l'horloge.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph, insert
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.database.recurring_supply as rs
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin

TODAY = date(2026, 9, 15)  # mardi (ISO 2)
D = lambda n: datetime(2026, 9, 15) + timedelta(days=n)  # noqa: E731 — date d'occurrence à J+n


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    def __init__(self, session, user, profile):
        self._s, self._user, self._user_profile = session, user, profile

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._user_profile


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(rs, "_today", lambda: TODAY)


@pytest.fixture
def w(pg_dsn):
    """Un monde : acheteur, producteur, produit, besoin DAILY de 2 TETE (`starts_at` ancré avant TODAY)."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        user = SimpleNamespace(id=g.buyer_user, phone="+226", zone_id=None, name="Resto")
        profile = SimpleNamespace(id=g.buyer, establishment_name="Resto")
        need = g.recurring_need(quantity=2, unit="TETE", recurrence_type="DAILY", starts_at=datetime(2026, 9, 1))
        product = g.product_for(quantity_for_sale=500)
    conn.close()
    return SimpleNamespace(dsn=pg_dsn, g=g, user=user, profile=profile, need=str(need), product=product)


# ── utilitaires ───────────────────────────────────────────────────────────────────────────────────────────────────
def _async_dsn(dsn):
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _run(w, fn):
    async def go():
        engine = create_async_engine(_async_dsn(w.dsn))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                out = await fn(_Svc(s, w.user, w.profile), s)
                await s.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _sql(w, query, params=()):
    conn = psycopg2.connect(w.dsn)
    with conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if cur.description else []
    conn.close()
    return rows


def _occurrence_version(w, need, day):
    rows_ = _sql(w, "select version from marketplace.recurring_need_occurrences where recurring_need_id = %s and "
                    "occurrence_date = %s", (need, datetime.combine(day, datetime.min.time())))
    return int(rows_[0][0]) if rows_ else 0  # aucune occurrence : le service refuse avant toute comparaison


def upd(w, action, need=None, **kw):
    """Appelant qui vient de LIRE l'état (B26 : la version de l'état observé est obligatoire — jamais d'omission implicite).
    `expected_version` / `expected_occurrence_version` explicites priment ; `unversioned=True` simule un appelant fautif."""
    nid = need or w.need
    if not kw.pop("unversioned", False):
        if action in rs.RECURRING_OCCURRENCE_ACTIONS:
            kw.setdefault("expected_occurrence_version", _occurrence_version(w, nid, kw["occurrence_date"]))
        else:
            kw.setdefault("expected_version", version(w, nid))
    return _run(w, lambda svc, s: svc.update_recurring_need(phone="+226", recurring_need_id=nid, action=action, **kw))


def cron(w):
    return _run(w, lambda svc, s: svc.replenish_occurrence_windows())


def occ(w, day, status="OPEN", *, requested=2, matched=0, need=None, unit="TETE", **extra):
    return insert(
        _cur(w), "marketplace.recurring_need_occurrences",
        recurring_need_id=need or w.need, occurrence_date=D(day), requested_quantity=requested, unit=unit,
        status=status, quantity_matched=matched, **extra,
    )


def _cur(w):
    conn = psycopg2.connect(w.dsn)
    conn.autocommit = True
    w._conns = getattr(w, "_conns", []) + [conn]
    return conn.cursor()


def alloc(w, occurrence, quantity, status="PROPOSED", **extra):
    return insert(
        _cur(w), "marketplace.need_allocations", occurrence_id=occurrence, producer_id=w.g.producer,
        product_id=w.product, quantity=quantity, unit_price=100, unit="TETE", status=status, **extra,
    )


def rows(w, need=None):
    """`{jour (offset): (statut, requested, matched)}` des occurrences du besoin."""
    got = _sql(
        w, "select occurrence_date, status, requested_quantity, quantity_matched from "
        "marketplace.recurring_need_occurrences where recurring_need_id = %s order by occurrence_date", (need or w.need,))
    return {(r[0] - datetime(2026, 9, 15)).days: (r[1], float(r[2]), float(r[3])) for r in got}


def need_row(w, need=None):
    return _sql(w, "select status, quantity, recurrence_type, weekly_days, paused_until from marketplace.recurring_needs "
                   "where id = %s", (need or w.need,))[0]


def alloc_statuses(w, occurrence):
    return sorted(r[0] for r in _sql(w, "select status from marketplace.need_allocations where occurrence_id = %s", (str(occurrence),)))


def active_alloc_sum(w, occurrence):
    return float(_sql(w, "select coalesce(sum(quantity),0) from marketplace.need_allocations where occurrence_id = %s "
                         "and status in ('PROPOSED','ACCEPTED','CONVERTED')", (str(occurrence),))[0][0])


def version(w, need=None):
    """Jeton de version courant du besoin, lu en SQL puis calculé par la fonction du service (même arithmétique)."""
    ts = _sql(w, "select updated_at from marketplace.recurring_needs where id = %s", (need or w.need,))[0][0]
    return rs.recurring_need_version_of(ts)


def occ_id(w, day):
    return _sql(w, "select id from marketplace.recurring_need_occurrences where recurring_need_id = %s and occurrence_date = %s",
                (w.need, D(day)))[0][0]


@pytest.fixture(autouse=True)
def _cleanup(request):
    """La base de test est partagée (session) et ces tests COMMITENT : on retire ce qu'ils ont créé pour ne pas polluer
    les tests qui balaient tous les besoins (digest, métriques)."""
    w = request.node.funcargs.get("w")
    started = None
    if w is not None:
        conn = psycopg2.connect(w.dsn)
        with conn, conn.cursor() as cur:
            cur.execute("select id from marketplace.recurring_need_occurrences")
            started = [r[0] for r in cur.fetchall()]
        conn.close()
    yield
    if w is None:
        return
    for c in getattr(w, "_conns", []):
        c.close()
    conn = psycopg2.connect(w.dsn)
    with conn, conn.cursor() as cur:
        cur.execute("delete from marketplace.recurring_needs where buyer_id = %s", (str(w.g.buyer),))
        # Le cron (`replenish_occurrence_windows`) est GLOBAL : il a pu matérialiser des occurrences pour les besoins
        # d'autres tests de la base partagée — on les retire (elles n'existaient pas avant ce test).
        cur.execute("delete from marketplace.recurring_need_occurrences where id <> all(%s::uuid[])", (started,))
    conn.close()


# ═════════════ A. QUANTITÉ PERMANENTE × état des occurrences existantes (tableau « futur vs engagé ») ═════════════
def test_quantity_with_no_occurrence_updates_only_the_need(w):
    res = upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert res["status"] == "success" and res["outcome"] == "APPLIED"
    assert need_row(w)[1] == 3 and rows(w) == {}


def test_quantity_open_occurrence_follows_the_new_contract(w):
    occ(w, 3)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[3] == ("OPEN", 3.0, 0.0)  # un snapshot non-exceptionnel suit le contrat


def test_quantity_never_touches_a_past_occurrence(w):
    occ(w, -1)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[-1] == ("OPEN", 2.0, 0.0)


def test_quantity_matched_occurrence_is_released_and_the_proposal_invalidated(w):
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    before = _sql(w, "select version from marketplace.recurring_need_occurrences where id = %s", (str(o),))[0][0]
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    # jamais « requested=3, matched=2, MATCHED » : la proposition est expirée, l'occurrence repart en recherche.
    assert rows(w)[3] == ("OPEN", 3.0, 0.0)
    assert alloc_statuses(w, o) == ["EXPIRED"]
    assert _sql(w, "select version from marketplace.recurring_need_occurrences where id = %s", (str(o),))[0][0] == before + 1


def test_quantity_decrease_never_leaves_allocations_above_the_new_request(w):
    o = occ(w, 3, "OPEN", requested=10, matched=8, unit="TETE")
    _sql(w, "update marketplace.recurring_needs set quantity = 10 where id = %s", (w.need,))
    alloc(w, o, 8)
    upd(w, "PERMANENT_QUANTITY", quantity=5)
    assert rows(w)[3] == ("OPEN", 5.0, 0.0)
    assert active_alloc_sum(w, o) <= 5.0


def test_quantity_accepted_occurrence_and_its_order_are_immutable(w):
    o = occ(w, 3, "ACCEPTED", matched=2, quantity_confirmed=2)
    order = insert(_cur(w), "marketplace.orders", total_amount=1234, buyer_id=w.g.buyer)
    alloc(w, o, 2, status="CONVERTED")
    before = _sql(w, "select total_amount, status from marketplace.orders where id = %s", (order,))
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[3] == ("ACCEPTED", 2.0, 2.0)
    assert alloc_statuses(w, o) == ["CONVERTED"]
    assert _sql(w, "select total_amount, status from marketplace.orders where id = %s", (order,)) == before


@pytest.mark.parametrize("status", ["PARTIALLY_ACCEPTED", "FULFILLED", "PARTIALLY_FULFILLED", "UNFULFILLED", "REJECTED",
                                    "EXPIRED", "SKIPPED", "CANCELLED"])
def test_quantity_never_rewrites_committed_or_terminal_history(w, status):
    occ(w, 3, status)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[3][0] == status and rows(w)[3][1] == 2.0


def test_quantity_never_overwrites_an_explicit_override_and_next_occurrences_use_the_new_quantity(w):
    occ(w, 3, requested=5)
    occ(w, 4)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[3][1] == 5.0 and rows(w)[4][1] == 3.0
    cron(w)
    assert rows(w)[6][1] == 3.0  # une occurrence nouvellement générée prend le contrat courant


# ═════════════ B. FRÉQUENCE PERMANENTE : le planning futur est réconcilié ═════════════
def _daily_window(w):
    for d in range(0, 4):
        occ(w, d)


def test_frequency_change_cancels_only_uncommitted_off_schedule_occurrences(w):
    _daily_window(w)                      # J..J+3 (mardi..vendredi)
    occ(w, 4, "ACCEPTED", matched=2)       # samedi, engagé
    m = occ(w, 5, "MATCHED", matched=2)    # dimanche, proposée
    alloc(w, m, 2)
    occ(w, 6, requested=9)                 # lundi, exception explicite
    res = upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3])  # mercredis seulement
    assert res["outcome"] == "APPLIED"
    r = rows(w)
    assert r[0][0] == "CANCELLED" and r[2][0] == "CANCELLED" and r[3][0] == "CANCELLED"
    assert r[1][0] == "OPEN"                       # mercredi : toujours dû
    assert r[4][0] == "ACCEPTED"                   # engagé : jamais touché
    assert r[5][0] == "CANCELLED" and alloc_statuses(w, m) == ["EXPIRED"]
    assert r[6] == ("OPEN", 9.0, 0.0)              # l'exception explicite n'est jamais perdue silencieusement


def test_frequency_flip_back_revives_superseded_dates_without_duplicate(w):
    _daily_window(w)
    upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3])
    upd(w, "PERMANENT_FREQUENCY", recurrence_type="DAILY")
    r = rows(w)
    assert all(r[d][0] == "OPEN" for d in range(0, 8)), r
    assert len(_sql(w, "select 1 from marketplace.recurring_need_occurrences where recurring_need_id = %s", (w.need,))) == len(r)


def test_frequency_change_with_no_occurrence_materializes_the_new_window(w):
    res = upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3])
    assert res["outcome"] == "APPLIED"
    assert sorted(rows(w)) == [1]  # le mercredi de la fenêtre J..J+7


def test_frequency_change_leaves_a_paused_need_without_new_occurrences(w):
    upd(w, "PAUSE")
    upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3])
    assert rows(w) == {} and need_row(w)[0] == "PAUSED"


# ═════════════ C. VERSION / CONCURRENCE OPTIMISTE ═════════════
def test_stale_expected_version_is_a_domain_conflict_and_changes_nothing(w):
    v = version(w)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    res = upd(w, "PERMANENT_QUANTITY", quantity=5, expected_version=v)
    assert res["status"] == "conflict" and res["outcome"] == "VERSION_CONFLICT"
    assert res["current"]["quantity"] == 3.0 and res["current"]["need_version"] == version(w)
    assert need_row(w)[1] == 3


def test_matching_expected_version_applies_and_returns_the_next_version(w):
    res = upd(w, "PERMANENT_QUANTITY", quantity=3, expected_version=version(w))
    assert res["outcome"] == "APPLIED" and res["need_version"] == version(w)


def test_without_expected_version_the_mutation_is_refused_never_applied_on_the_current_state(w):
    """B26 : plus de « `None` = tout passe » — l'omission est refusée (B25 l'appliquait sur l'état verrouillé courant)."""
    before = need_row(w)
    with pytest.raises(BusinessRuleException) as exc:
        upd(w, "PERMANENT_QUANTITY", quantity=3, unversioned=True)
    assert getattr(exc.value, "reason", None) == "version_required" and need_row(w) == before


async def _two_sessions(w, make_a, make_b):
    engines = [create_async_engine(_async_dsn(w.dsn)) for _ in range(2)]
    try:
        async def side(engine, make):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                out = await make(_Svc(s, w.user, w.profile), s)
                await s.commit()
                return out

        return await asyncio.gather(side(engines[0], make_a), side(engines[1], make_b), return_exceptions=True)
    finally:
        for e in engines:
            await e.dispose()


def _call(w, action, **kw):
    return lambda svc, s: svc.update_recurring_need(phone="+226", recurring_need_id=w.need, action=action, **kw)


def test_concurrent_quantity_updates_exactly_one_wins_and_one_conflicts(w):
    v = version(w)
    a, b = asyncio.run(_two_sessions(w, _call(w, "PERMANENT_QUANTITY", quantity=3, expected_version=v),
                                     _call(w, "PERMANENT_QUANTITY", quantity=5, expected_version=v)))
    outcomes = sorted(r["outcome"] for r in (a, b))
    assert outcomes == ["APPLIED", "VERSION_CONFLICT"], (a, b)
    winner = a if a["outcome"] == "APPLIED" else b
    assert need_row(w)[1] == (3 if winner is a else 5)
    assert version(w) == winner["need_version"]


def test_concurrent_quantity_and_frequency_on_the_same_version_is_a_strict_conflict(w):
    v = version(w)
    a, b = asyncio.run(_two_sessions(
        w, _call(w, "PERMANENT_QUANTITY", quantity=3, expected_version=v),
        _call(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3], expected_version=v)))
    assert sorted(r["outcome"] for r in (a, b)) == ["APPLIED", "VERSION_CONFLICT"]
    q, rec = need_row(w)[1], need_row(w)[2]
    assert (q, rec) in ((3, "DAILY"), (2, "WEEKLY_DAYS"))  # jamais un mélange partiel


def test_concurrent_updates_on_the_same_observed_version_are_serialized_never_interleaved(w):
    v = version(w)
    a, b = asyncio.run(_two_sessions(w, _call(w, "PERMANENT_QUANTITY", quantity=3, expected_version=v),
                                     _call(w, "PERMANENT_QUANTITY", quantity=5, expected_version=v)))
    assert sorted(r["outcome"] for r in (a, b)) == ["APPLIED", "VERSION_CONFLICT"]
    assert need_row(w)[1] in (3, 5)


def test_a_conflict_emits_no_audit_record(w, caplog):
    v = version(w)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    with caplog.at_level(logging.INFO, logger="ladini.services.database.recurring_supply"):
        upd(w, "PERMANENT_QUANTITY", quantity=5, expected_version=v)
    assert not [r for r in caplog.records if "recurring_need.mutation" in r.getMessage()]


# ═════════════ D. IDEMPOTENCE ═════════════
def test_same_quantity_twice_is_already_applied_without_version_bump(w):
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    v = version(w)
    res = upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert res["outcome"] == "ALREADY_APPLIED" and version(w) == v


def test_same_frequency_twice_is_already_applied(w):
    upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3])
    v = version(w)
    assert upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[3])["outcome"] == "ALREADY_APPLIED"
    assert version(w) == v


def test_pause_twice_resume_twice_cancel_twice(w):
    assert upd(w, "PAUSE")["outcome"] == "APPLIED"
    assert upd(w, "PAUSE")["outcome"] == "ALREADY_APPLIED"
    assert upd(w, "RESUME")["outcome"] == "APPLIED"
    assert upd(w, "RESUME")["outcome"] == "ALREADY_APPLIED"
    assert upd(w, "CANCEL")["outcome"] == "APPLIED"
    v = version(w)
    assert upd(w, "CANCEL")["outcome"] == "ALREADY_APPLIED" and version(w) == v


def test_skip_replay_is_already_applied_with_a_single_event(w):
    occ(w, 3)
    assert upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date())["outcome"] == "APPLIED"
    assert upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date())["outcome"] == "ALREADY_APPLIED"
    n = _sql(w, "select count(*) from analytics.event_outbox where event_name = 'RECURRING_OCCURRENCE_SKIPPED' "
                "and dedupe_key = %s", (f"RECURRING_OCCURRENCE_SKIPPED:{occ_id(w, 3)}",))[0][0]
    assert n == 1


def test_override_same_quantity_twice_is_already_applied(w):
    occ(w, 3)
    assert upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=5)["outcome"] == "APPLIED"
    assert upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=5)["outcome"] == "ALREADY_APPLIED"


def test_a_cancelled_need_accepts_no_mutation_and_is_never_resurrected(w):
    upd(w, "CANCEL")
    for action, kw in (("RESUME", {}), ("PAUSE", {}), ("PERMANENT_QUANTITY", {"quantity": 3}),
                       ("PERMANENT_FREQUENCY", {"recurrence_type": "DAILY"})):
        with pytest.raises(BusinessRuleException) as exc:
            upd(w, action, **kw)
        assert getattr(exc.value, "reason", None) == "need_not_active", action
    assert need_row(w)[0] == "CANCELLED"


def test_invalid_quantity_is_a_domain_error_not_a_database_error(w):
    for bad in (0, -1):
        with pytest.raises(BusinessRuleException):
            upd(w, "PERMANENT_QUANTITY", quantity=bad)


# ═════════════ E. PAUSE / REPRISE ═════════════
def test_pause_blocks_materialization_and_matching_eligibility(w):
    upd(w, "PAUSE")
    cron(w)  # le cron examine TOUS les besoins de la base partagée : seul CE besoin est asserté
    assert rows(w) == {}


def test_pause_releases_open_and_matched_but_preserves_accepted(w):
    occ(w, 3)
    m = occ(w, 4, "MATCHED", matched=2)
    alloc(w, m, 2)
    occ(w, 5, "ACCEPTED", matched=2)
    upd(w, "PAUSE")
    r = rows(w)
    assert r[3][0] == "SKIPPED" and r[4] == ("SKIPPED", 2.0, 0.0) and alloc_statuses(w, m) == ["EXPIRED"]
    assert r[5][0] == "ACCEPTED"  # l'engagement accepté survit à la pause


def test_pause_until_is_exclusive_and_keeps_later_occurrences(w):
    for d in (1, 2, 3, 4):
        occ(w, d)
    res = upd(w, "PAUSE", paused_until=D(3).date())
    assert res["outcome"] == "APPLIED"
    r = rows(w)
    assert r[1][0] == r[2][0] == "SKIPPED" and r[3][0] == r[4][0] == "OPEN"  # J+3 = jour de reprise : non sauté
    assert need_row(w)[0] == "PAUSED" and need_row(w)[4] == D(3)


@pytest.mark.parametrize("offset", [0, -1, -30])
def test_pause_until_a_non_future_date_is_rejected(w, offset):
    with pytest.raises(BusinessRuleException) as exc:
        upd(w, "PAUSE", paused_until=D(offset).date())
    assert exc.value.reason == "invalid_pause_date"
    assert need_row(w)[0] == "ACTIVE"


def test_pause_until_tomorrow_skips_only_today(w):
    occ(w, 0)
    occ(w, 1)
    upd(w, "PAUSE", paused_until=D(1).date())
    assert rows(w)[0][0] == "SKIPPED" and rows(w)[1][0] == "OPEN"


def test_temporal_pause_resumes_automatically_through_the_existing_cron_without_backfill(w, monkeypatch):
    upd(w, "PAUSE", paused_until=D(21).date())  # 3 semaines
    cron(w)
    assert rows(w) == {} and need_row(w)[0] == "PAUSED"
    monkeypatch.setattr(rs, "_today", lambda: date(2026, 10, 6))  # J+21
    out = cron(w)
    assert need_row(w)[0] == "ACTIVE" and need_row(w)[4] is None and out["needs_resumed"] >= 1
    days = sorted(rows(w))
    assert days and min(days) >= 21, "aucune occurrence rétroactive pour la période de pause"
    snapshot = rows(w)
    assert cron(w)["needs_resumed"] == 0 and rows(w) == snapshot  # idempotent


def test_a_pause_without_end_never_auto_resumes(w, monkeypatch):
    upd(w, "PAUSE")
    monkeypatch.setattr(rs, "_today", lambda: date(2026, 12, 1))
    cron(w)
    assert need_row(w)[0] == "PAUSED"


def test_self_service_next_occurrence_resumes_an_elapsed_pause(w, monkeypatch):
    upd(w, "PAUSE", paused_until=D(2).date())
    monkeypatch.setattr(rs, "_today", lambda: D(2).date())
    res = _run(w, lambda svc, s: svc.ensure_next_recurring_occurrence("+226", w.need))
    assert need_row(w)[0] == "ACTIVE" and res["occurrence_id"] is not None


def test_manual_resume_does_not_reopen_skipped_dates_nor_backfill(w):
    occ(w, 1)
    upd(w, "PAUSE", paused_until=D(5).date())
    upd(w, "RESUME")
    r = rows(w)
    assert r[1][0] == "SKIPPED"             # un skip reste un fait historique
    assert min(r) >= 0 and need_row(w)[0] == "ACTIVE"
    assert all(d >= 0 for d in r)           # jamais d'occurrence antérieure à aujourd'hui


def test_pause_changes_its_end_date_when_replayed_with_another_date(w):
    upd(w, "PAUSE", paused_until=D(3).date())
    assert upd(w, "PAUSE", paused_until=D(6).date())["outcome"] == "APPLIED"
    assert need_row(w)[4] == D(6)


# ═════════════ F. ANNULATION ═════════════
def test_cancel_stops_open_and_matched_future_work_but_preserves_accepted_and_its_order(w):
    occ(w, 3)
    m = occ(w, 4, "MATCHED", matched=2)
    alloc(w, m, 2)
    a = occ(w, 5, "ACCEPTED", matched=2)
    alloc(w, a, 2, status="CONVERTED")
    order = insert(_cur(w), "marketplace.orders", total_amount=999, buyer_id=w.g.buyer)
    res = upd(w, "CANCEL")
    assert res["outcome"] == "APPLIED"
    r = rows(w)
    assert r[3][0] == "CANCELLED" and r[4][0] == "CANCELLED" and r[5][0] == "ACCEPTED"
    assert alloc_statuses(w, m) == ["EXPIRED"] and alloc_statuses(w, a) == ["CONVERTED"]
    assert _sql(w, "select total_amount from marketplace.orders where id = %s", (order,))[0][0] == 999
    cron(w)
    assert rows(w) == r  # aucune résurrection, aucune nouvelle occurrence


# ═════════════ G. SKIP / OVERRIDE ═════════════
def test_skip_is_scoped_to_one_occurrence_and_the_schedule_continues(w):
    occ(w, 3)
    occ(w, 4)
    q = need_row(w)
    upd(w, "OCCURRENCE_SKIP", occurrence_date=D(3).date())
    assert rows(w)[3][0] == "SKIPPED" and rows(w)[4][0] == "OPEN" and need_row(w) == q
    cron(w)
    assert rows(w)[3][0] == "SKIPPED"          # pas de résurrection
    assert rows(w)[5][0] == "OPEN"            # la suivante est générée normalement


def test_override_is_local_survives_permanent_quantity_and_frequency_changes(w):
    occ(w, 3)
    occ(w, 4)
    upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=5)
    assert rows(w)[3][1] == 5.0 and rows(w)[4][1] == 2.0 and need_row(w)[1] == 2
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[3][1] == 5.0 and rows(w)[4][1] == 3.0
    upd(w, "PERMANENT_FREQUENCY", recurrence_type="WEEKLY_DAYS", weekly_days=[1])  # lundis : J+3/J+4 hors planning
    assert rows(w)[3] == ("OPEN", 5.0, 0.0)  # l'exception n'est jamais perdue
    assert rows(w)[4][0] == "CANCELLED"


def test_override_on_a_matched_occurrence_invalidates_its_stale_proposal(w):
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    upd(w, "OCCURRENCE_OVERRIDE", occurrence_date=D(3).date(), quantity=5)
    assert rows(w)[3] == ("OPEN", 5.0, 0.0) and alloc_statuses(w, o) == ["EXPIRED"]


# ═════════════ H. CRON × SELF-SERVICE : convergence et courses réelles ═════════════
def test_cron_after_every_mutation_never_duplicates_resurrects_or_loses_an_override(w):
    occ(w, 3, requested=5)
    cron(w)
    n = len(rows(w))
    for action, kw in (("PERMANENT_QUANTITY", {"quantity": 3}), ("PERMANENT_FREQUENCY", {"recurrence_type": "DAILY"}),
                       ("PAUSE", {}), ("RESUME", {})):
        upd(w, action, **kw)
        cron(w)
        cron(w)
    assert len(rows(w)) >= n and len(_sql(w, "select 1 from marketplace.recurring_need_occurrences where recurring_need_id=%s", (w.need,))) == len(rows(w))
    assert rows(w)[3][1] == 5.0


async def _stale_materialization(w, mutate, *, concurrent: bool):
    """Le cron lit le besoin ACTIF, la mutation commite, PUIS le cron matérialise avec son objet périmé."""
    engine_a, engine_b = (create_async_engine(_async_dsn(w.dsn)) for _ in range(2))
    try:
        async with AsyncSession(engine_a, expire_on_commit=False) as a:
            svc_a = _Svc(a, w.user, w.profile)
            need = await a.get(rs.RecurringNeed, uuid.UUID(w.need))
            assert need.status == "ACTIVE"
            async with AsyncSession(engine_b, expire_on_commit=False) as b:
                await mutate(_Svc(b, w.user, w.profile))
                await b.commit()
            created = await svc_a._ensure_window_for_need(need)
            await a.commit()
            return created
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


@pytest.mark.parametrize("action,kw", [("CANCEL", {}), ("PAUSE", {})])
def test_cron_holding_a_stale_active_need_creates_nothing_after_cancel_or_pause(w, action, kw):
    created = asyncio.run(_stale_materialization(
        w, lambda svc: svc.update_recurring_need(phone="+226", recurring_need_id=w.need, action=action, expected_version=version(w), **kw), concurrent=False))
    assert created == 0 and rows(w) == {}


def test_cron_holding_a_stale_need_materializes_the_NEW_quantity_never_a_mix(w):
    created = asyncio.run(_stale_materialization(
        w, lambda svc: svc.update_recurring_need(phone="+226", recurring_need_id=w.need, action="PERMANENT_QUANTITY", quantity=7,
                                          expected_version=version(w)),
        concurrent=False))
    assert created > 0 and {v[1] for v in rows(w).values()} == {7.0}


async def _lock_race(w, mutate, materialize):
    """`mutate` tient le verrou du besoin (non commité) pendant que `materialize` démarre : il doit ATTENDRE puis voir
    l'état commité."""
    engine_a, engine_b = (create_async_engine(_async_dsn(w.dsn)) for _ in range(2))
    try:
        async with AsyncSession(engine_a, expire_on_commit=False) as a, AsyncSession(engine_b, expire_on_commit=False) as b:
            await mutate(_Svc(a, w.user, w.profile))            # verrou tenu, transaction ouverte
            task = asyncio.create_task(materialize(_Svc(b, w.user, w.profile)))
            await asyncio.sleep(0.5)
            assert not task.done(), "la matérialisation doit attendre le verrou du besoin"
            await a.commit()
            created = await asyncio.wait_for(task, 10)
            await b.commit()
            return created
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


def _materialize_all(svc):
    return svc.replenish_occurrence_windows()


@pytest.mark.parametrize("action,kw", [("CANCEL", {}), ("PAUSE", {})])
def test_materialization_blocks_on_the_need_lock_then_creates_nothing(w, action, kw):
    out = asyncio.run(_lock_race(
        w, lambda svc: svc.update_recurring_need(phone="+226", recurring_need_id=w.need, action=action, expected_version=version(w), **kw), _materialize_all))
    assert out["occurrences_created"] == 0 and rows(w) == {}


def test_materialization_blocks_on_a_quantity_update_then_snapshots_the_new_quantity(w):
    out = asyncio.run(_lock_race(
        w, lambda svc: svc.update_recurring_need(phone="+226", recurring_need_id=w.need, action="PERMANENT_QUANTITY", quantity=4,
                                          expected_version=version(w)),
        _materialize_all))
    assert out["occurrences_created"] > 0 and {v[1] for v in rows(w).values()} == {4.0}


def test_matching_in_flight_then_quantity_update_leaves_no_stale_proposal(w):
    """Le matching a verrouillé l'occurrence et posé une proposition (non commitée) ; la mutation attend, puis invalide."""
    o = occ(w, 3)
    v0 = version(w)  # état observé AVANT la course (le matching ne touche pas la version du besoin)

    async def go():
        engine_a, engine_b = (create_async_engine(_async_dsn(w.dsn)) for _ in range(2))
        try:
            async with AsyncSession(engine_a) as a, AsyncSession(engine_b) as b:
                await a.execute(text("select id from marketplace.recurring_need_occurrences where id = :i for update"), {"i": o})
                await a.execute(text(
                    "insert into marketplace.need_allocations (occurrence_id, producer_id, product_id, quantity, unit_price, unit, status) "
                    "values (:o, :p, :pr, 2, 100, 'TETE', 'PROPOSED')"), {"o": o, "p": w.g.producer, "pr": w.product})
                await a.execute(text("update marketplace.recurring_need_occurrences set status='MATCHED', quantity_matched=2, "
                                     "version=version+1 where id = :i"), {"i": o})
                task = asyncio.create_task(_Svc(b, w.user, w.profile).update_recurring_need(
                    phone="+226", recurring_need_id=w.need, action="PERMANENT_QUANTITY", quantity=3, expected_version=v0))
                await asyncio.sleep(0.5)
                assert not task.done()
                await a.commit()
                res = await asyncio.wait_for(task, 10)
                await b.commit()
                return res
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    assert asyncio.run(go())["outcome"] == "APPLIED"
    assert rows(w)[3] == ("OPEN", 3.0, 0.0) and alloc_statuses(w, o) == ["EXPIRED"]


def test_accept_in_flight_then_quantity_update_leaves_the_order_and_the_accepted_occurrence_untouched(w):
    """L'acceptation verrouille l'occurrence d'abord : la mutation permanente attend, voit ACCEPTED, ne touche rien."""
    o = occ(w, 3, "MATCHED", matched=2)
    alloc(w, o, 2)
    v0 = version(w)

    async def go():
        engine_a, engine_b = (create_async_engine(_async_dsn(w.dsn)) for _ in range(2))
        try:
            async with AsyncSession(engine_a) as a, AsyncSession(engine_b) as b:
                await a.execute(text("select id from marketplace.recurring_need_occurrences where id = :i for update"), {"i": o})
                task = asyncio.create_task(_Svc(b, w.user, w.profile).update_recurring_need(
                    phone="+226", recurring_need_id=w.need, action="PERMANENT_QUANTITY", quantity=3, expected_version=v0))
                await asyncio.sleep(0.5)
                await a.execute(text("update marketplace.recurring_need_occurrences set status='ACCEPTED' where id=:i"), {"i": o})
                await a.execute(text("update marketplace.need_allocations set status='CONVERTED' where occurrence_id=:i"), {"i": o})
                await a.commit()
                res = await asyncio.wait_for(task, 10)
                await b.commit()
                return res
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    asyncio.run(go())
    assert rows(w)[3] == ("ACCEPTED", 2.0, 2.0) and alloc_statuses(w, o) == ["CONVERTED"]


# ═════════════ I. OWNERSHIP, AUDIT, LEGACY, DATES ═════════════
def test_another_buyer_cannot_mutate_the_need_and_learns_nothing(w):
    intruder = SimpleNamespace(id=uuid.uuid4())

    async def fn(svc, s):
        svc._user_profile = intruder
        # la bonne version ET une mauvaise : même réponse « introuvable » (la propriété est vérifiée AVANT la version)
        return await svc.update_recurring_need(phone="+226", recurring_need_id=w.need, action="CANCEL", expected_version=ver)

    for ver in (version(w), 12345):
        with pytest.raises(BusinessRuleException) as exc:
            _run(w, fn)
        assert "introuvable" in str(exc.value).lower() and need_row(w)[0] == "ACTIVE"


def test_each_effective_mutation_leaves_one_audit_record_without_pii(w, caplog):
    with caplog.at_level(logging.INFO, logger="ladini.services.database.recurring_supply"):
        upd(w, "PERMANENT_QUANTITY", quantity=3)
        upd(w, "PERMANENT_QUANTITY", quantity=3)  # rejeu : aucun enregistrement de plus
    audit = [r.getMessage() for r in caplog.records if "recurring_need.mutation" in r.getMessage()]
    assert len(audit) == 1
    line = audit[0]
    assert w.need in line and "action=PERMANENT_QUANTITY" in line and "old=2" in line and "new=3" in line
    assert "+226" not in line and "version=" in line


def test_legacy_need_without_expected_version_keeps_working_and_lists_its_version(w):
    listing = _run(w, lambda svc, s: svc.list_my_recurring_needs("+226"))
    item = listing["items"][0]
    assert item["need_version"] == version(w) and item["paused_until"] is None
    assert upd(w, "PERMANENT_QUANTITY", quantity=3)["outcome"] == "APPLIED"
    detail = _run(w, lambda svc, s: svc.get_recurring_need_detail("+226", w.need))
    assert detail["need_version"] == version(w)


def test_monthly_end_of_month_mutation_is_idempotent_and_duplicate_free(w, monkeypatch):
    monkeypatch.setattr(rs, "_today", lambda: date(2026, 2, 27))
    _sql(w, "update marketplace.recurring_needs set starts_at = %s where id = %s", (datetime(2026, 1, 31), w.need))
    res = upd(w, "PERMANENT_FREQUENCY", recurrence_type="MONTHLY")
    assert res["outcome"] == "APPLIED"
    before = rows(w)
    cron(w)
    cron(w)
    assert rows(w) == before and len({d for d in before}) == len(before)
    assert upd(w, "PERMANENT_QUANTITY", quantity=3)["outcome"] == "APPLIED"


def test_occurrence_dated_today_is_mutable_and_yesterday_is_not_touched(w):
    occ(w, 0)
    occ(w, -1)
    upd(w, "PERMANENT_QUANTITY", quantity=3)
    assert rows(w)[0][1] == 3.0 and rows(w)[-1][1] == 2.0


# ═════════════ J. RÔLE RÉEL ADMIN (can_buy) via le vrai AgriDatabaseService + message de conflit ═════════════
def test_admin_with_buyer_capability_mutates_with_versioning_through_the_real_service(pg_dsn, monkeypatch):
    import random

    from ladini.core import database
    from ladini.core.settings import settings
    from ladini.services.database.d import AgriDatabaseService

    monkeypatch.setattr(settings, "DATABASE_URL", pg_dsn)
    monkeypatch.setattr(settings, "DB_SSL_MODE", "disable")
    monkeypatch.setattr(database, "_async_engine", None)
    monkeypatch.setattr(database, "_AsyncSessionLocal", None)
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        from factories import uniq

        region = insert(cur, "governance.climatic_regions", name=uniq("region"))
        zone = insert(cur, "governance.zones", name=uniq("zone"), code=uniq("Z"), climatic_region_id=region)
        phone = "+2267" + "".join(random.choice("0123456789") for _ in range(7))
        insert(cur, "auth.users", phone=phone, name="Admin Ido", role="ADMIN", zone_id=zone, onboarding_completed=True)
    conn.close()

    async def go():
        svc = AgriDatabaseService()
        try:
            created = await svc.create_recurring_need(
                phone=phone, product_query="Chevre", quantity=3, unit="UNITE", recurrence_type="WEEKLY")
            need_id = created["recurring_need_id"]
            item = next(i for i in (await svc.list_my_recurring_needs(phone=phone))["items"] if i["recurring_need_id"] == need_id)
            v = item["need_version"]
            ok = await svc.update_recurring_need(
                phone=phone, recurring_need_id=need_id, action="PERMANENT_QUANTITY", quantity=4, expected_version=v)
            stale = await svc.update_recurring_need(
                phone=phone, recurring_need_id=need_id, action="PERMANENT_QUANTITY", quantity=5, expected_version=v)
            cancelled = await svc.update_recurring_need(
                phone=phone, recurring_need_id=need_id, action="CANCEL", expected_version=ok["need_version"])
            again = await svc.update_recurring_need(
                phone=phone, recurring_need_id=need_id, action="CANCEL", expected_version=cancelled["need_version"])
            return ok, stale, cancelled, again
        finally:
            await database.close_db()

    ok, stale, cancelled, again = asyncio.run(go())
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:  # base partagée : ne laisse aucun besoin derrière
        cur.execute("delete from marketplace.recurring_needs where buyer_id in "
                    "(select b.id from marketplace.buyer_profiles b join auth.users u on u.id = b.user_id where u.phone = %s)", (phone,))
    conn.close()
    assert ok["outcome"] == "APPLIED" and ok["need_version"] != 0
    assert stale["status"] == "conflict" and stale["current"]["quantity"] == 4.0
    assert cancelled["outcome"] == "APPLIED" and again["outcome"] == "ALREADY_APPLIED"


def test_the_conflict_reply_is_a_business_message_showing_the_current_state():
    from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
        _version_conflict_reply,
    )

    msg = _version_conflict_reply("Bœuf", {"status": "conflict", "current": {
        "quantity": 5.0, "unit": "TETE", "recurrence_type": "WEEKLY", "weekly_days": None}})
    assert "modifié entre-temps" in msg and "5" in msg and "TETE" in msg and "Rien n'a été changé" in msg
    assert "Traceback" not in msg and "409" not in msg
