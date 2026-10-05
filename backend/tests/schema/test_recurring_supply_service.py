"""`RecurringSupplyMixin` contre un vrai PostgreSQL (mêmes fixtures que `test_query_efficiency.py`) :
transaction besoin+occurrences, idempotence de la matérialisation, sémantique exacte des mises à jour
(permanent / exception / skip / pause / reprise / annulation), ownership, et absence de N+1 sur
`list_my_recurring_needs`. Dates toujours FIXES (jamais `datetime.now()` non contrôlé) : le module
`_today()` de `recurring_supply.py` est monkeypatché pour ancrer chaque test à une date connue.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph, insert, uniq
from observed import observed_update
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.database.recurring_supply as recurring_supply_module
from ladini.domain.models import RecurringNeed, RecurringNeedOccurrence
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin

FIXED_TODAY = date(2026, 9, 15)  # un mardi — jamais un dimanche, pour ne pas biaiser les tests DAILY-except-Sunday


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    """Mixins branchés sur une session réelle — voir `test_query_efficiency.py::_Svc` pour le même
    idiome (la propriété `session` est normalement fournie par le service central `d.py`).
    `ModerationMixin`/`ProducerMgmtMixin` : `_get_or_create_sub_category_for_rfq`
    (auto-provisioning catalogue) vérifie les termes interdits et devine une catégorie — même
    composition que le service central `d.py`."""

    def __init__(self, session, user):
        self._s = session
        self._user = user

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._user_profile


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: FIXED_TODAY)


@pytest.fixture
def market(pg_dsn):
    """Un acheteur, un producteur, une sous-catégorie "tomate"."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        cur.execute("update governance.sub_categories set name = %s where id = %s", ("tomate", g.sub_category))
        buyer_user = SimpleNamespace(id=g.buyer_user)
        buyer_profile = SimpleNamespace(id=g.buyer)
    conn.close()
    return pg_dsn, buyer_user, buyer_profile


def _run(dsn, fn):
    """Chaque appel ouvre sa PROPRE session (comme un tour agent distinct) : sans `commit()` explicite
    ici, une session qui se ferme annule son travail — `@transactional(write=True)` (`d.py`) commit
    normalement à la fin de chaque appel mixin réel, reproduit ici pour que les écritures d'un `_run`
    soient visibles par le `_run` suivant."""

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _run_counted(dsn, fn):
    statements: list[str] = []

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        event.listen(engine.sync_engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go()), statements


def _svc(session, market_tuple) -> _Svc:
    _dsn, user, profile = market_tuple
    svc = _Svc(session, user)
    svc._user_profile = profile
    return svc


async def _create(session, market_tuple, **over) -> dict:
    cols = dict(
        phone="+226", product_query="tomate", quantity=40, unit="KG", recurrence_type="DAILY"
    )
    cols.update(over)
    return await _svc(session, market_tuple).create_recurring_need(**cols)


# ── création : besoin + occurrences dans la même transaction ────────────

def test_create_recurring_need_materializes_the_j_to_j_plus_7_window(market):
    async def fn(session):
        result = await _create(session, market)
        occs = (
            await session.execute(
                select(RecurringNeedOccurrence).where(
                    RecurringNeedOccurrence.recurring_need_id == uuid.UUID(result["recurring_need_id"])
                )
            )
        ).scalars().all()
        return result, occs

    result, occs = _run(market[0], fn)
    assert result["status"] == "success"
    # starts_at par defaut = aujourd'hui (15) + delai minimal admin par defaut (4) = 19 ; fenetre bornee a J+7
    # depuis aujourd'hui (22) -> 19..22 inclus = 4 jours
    assert len(occs) == 4
    assert all(o.requested_quantity == 40 and o.unit == "KG" and o.status == "OPEN" for o in occs)


def test_create_recurring_need_defaults_starts_at_to_today_plus_the_default_lead_time(market):
    async def fn(session):
        result = await _create(session, market)
        need = await session.get(RecurringNeed, uuid.UUID(result["recurring_need_id"]))
        return need

    need = _run(market[0], fn)
    assert need.starts_at.date() == date(2026, 9, 19)  # 15 + 4 jours (réglage par défaut), plus « demain »


def test_create_recurring_need_daily_except_sunday_excludes_sunday_occurrences(market):
    async def fn(session):
        result = await _create(session, market, excluded_weekdays=[7])
        occs = (
            await session.execute(
                select(RecurringNeedOccurrence).where(
                    RecurringNeedOccurrence.recurring_need_id == uuid.UUID(result["recurring_need_id"])
                )
            )
        ).scalars().all()
        return occs

    occs = _run(market[0], fn)
    assert all(o.occurrence_date.isoweekday() != 7 for o in occs)


# ── idempotence de la matérialisation (mandat §8) ────────────────────────

def test_materializing_the_same_window_twice_creates_no_duplicate(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await svc.create_recurring_need(phone="+226", product_query="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
        need = await session.get(RecurringNeed, uuid.UUID(result["recurring_need_id"]))
        from ladini.domain.recurring_supply.recurrence import RecurrenceRule

        rule = RecurrenceRule(recurrence_type="DAILY", starts_at=need.starts_at.date())
        second_call_created = await svc._materialize_occurrences(
            need, rule, from_date=FIXED_TODAY, to_date=date(2026, 9, 22)
        )
        total = (
            await session.execute(
                select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == need.id)
            )
        ).scalars().all()
        return second_call_created, len(total)

    second_call_created, total = _run(market[0], fn)
    assert second_call_created == 0
    assert total == 7


# ── réapprovisionnement générique des occurrences (Phase 3, mandat MONTHLY §14) ──

def test_monthly_replenishment_materializes_successive_month_end_occurrences_without_duplicates(market, monkeypatch):
    """`replenish_occurrence_windows` — le mécanisme manquant identifié par le mandat MONTHLY :
    sans lui, un besoin MONTHLY (ancre 31 janvier) ne recevrait jamais plus d'UNE occurrence.
    Ancre fixe (jamais dérivée de l'occurrence précédente) : 31 janvier -> 28 février -> 31 mars.
    Un second passage à la MÊME date ne doit créer aucun doublon (contrainte unique
    (recurring_need_id, occurrence_date))."""
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: date(2026, 1, 31))
    result = _run(market[0], lambda session: _create(session, market, recurrence_type="MONTHLY", starts_at="2026-01-31"))
    need_id = uuid.UUID(result["recurring_need_id"])

    async def occurrence_dates(session):
        occs = (
            await session.execute(
                select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == need_id)
            )
        ).scalars().all()
        return sorted(o.occurrence_date.date() for o in occs)

    async def replenish(session):
        return await _svc(session, market).replenish_occurrence_windows()

    # Création : fenêtre J->J+7 depuis le 31 janvier -> seule l'occurrence du 31 janvier lui-même.
    assert _run(market[0], occurrence_dates) == [date(2026, 1, 31)]

    # 22 février (2026 n'est pas bissextile) : le 28 février entre dans la fenêtre J->J+7.
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: date(2026, 2, 22))
    _run(market[0], replenish)
    assert _run(market[0], occurrence_dates) == [date(2026, 1, 31), date(2026, 2, 28)]

    # 25 mars : le 31 mars entre dans la fenêtre J->J+7 — l'ancre reste 31, jamais dérivée du 28.
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: date(2026, 3, 25))
    _run(market[0], replenish)
    assert _run(market[0], occurrence_dates) == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]

    # Second passage à la MÊME date : idempotent, aucun doublon.
    _run(market[0], replenish)
    assert _run(market[0], occurrence_dates) == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


# ── création multi-produits, atomique (chantier 2026-09-23) ─────────────

def test_create_recurring_needs_creates_one_need_per_item(market):
    async def fn(session):
        result = await _svc(session, market).create_recurring_needs(
            phone="+226",
            items=[
                {"product_query": "tomate", "quantity": 10, "unit": "KG"},
                {"product_query": "oignon", "quantity": 20, "unit": "KG"},
            ],
            recurrence_type="DAILY",
        )
        needs = (
            await session.execute(select(RecurringNeed).where(RecurringNeed.buyer_id == market[2].id))
        ).scalars().all()
        return result, needs

    result, needs = _run(market[0], fn)
    assert result["status"] == "success"
    assert len(result["items"]) == 2
    assert len(needs) == 2
    assert {n.quantity for n in needs} == {10, 20}


def test_create_recurring_needs_materializes_occurrences_for_every_item(market):
    async def fn(session):
        result = await _svc(session, market).create_recurring_needs(
            phone="+226",
            items=[
                {"product_query": "tomate", "quantity": 10, "unit": "KG"},
                {"product_query": "oignon", "quantity": 20, "unit": "KG"},
            ],
            recurrence_type="DAILY",
        )
        occurrence_counts = []
        for item in result["items"]:
            rows = (
                await session.execute(
                    select(RecurringNeedOccurrence).where(
                        RecurringNeedOccurrence.recurring_need_id == uuid.UUID(item["recurring_need_id"])
                    )
                )
            ).scalars().all()
            occurrence_counts.append(len(rows))
        return occurrence_counts

    occurrence_counts = _run(market[0], fn)
    assert occurrence_counts == [7, 7]


def test_create_recurring_needs_rolls_back_everything_if_one_item_fails(market):
    """Mandat multi-produits (2026-09-23) : un produit invalide dans le 2e item ne doit JAMAIS
    laisser le 1er item déjà créé — rollback complet, jamais de création partielle silencieuse."""

    async def fn(session):
        svc = _svc(session, market)
        raised = False
        try:
            await svc.create_recurring_needs(
                phone="+226",
                items=[
                    {"product_query": "tomate", "quantity": 10, "unit": "KG"},
                    {"product_query": "", "quantity": 20, "unit": "KG"},  # produit manquant
                ],
                recurrence_type="DAILY",
            )
        except BusinessRuleException:
            raised = True
            # Même geste que `@transactional(write=True)` sur exception (`base_service.py`) —
            # ce test reproduit le rollback réel, jamais seulement l'absence d'exception.
            await session.rollback()
        needs = (
            await session.execute(select(RecurringNeed).where(RecurringNeed.buyer_id == market[2].id))
        ).scalars().all()
        return raised, needs

    raised, needs = _run(market[0], fn)
    assert raised, "un produit vide doit lever BusinessRuleException, jamais réussir silencieusement"
    assert needs == [], "aucun besoin ne doit rester créé si un item échoue — rollback complet attendu"


# ── mise à jour permanente : occurrences futures OPEN uniquement ────────

def test_permanent_quantity_update_changes_future_open_occurrences(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        upd = await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="PERMANENT_QUANTITY", quantity=25)
        occs = (
            await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id)))
        ).scalars().all()
        return upd, occs

    upd, occs = _run(market[0], fn)
    assert upd["occurrences_updated"] == 7
    assert all(o.requested_quantity == 25 for o in occs)


def test_permanent_update_never_touches_an_occurrence_that_left_open(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = uuid.UUID(result["recurring_need_id"])
        occs = (await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == need_id))).scalars().all()
        frozen = sorted(occs, key=lambda o: o.occurrence_date)[0]
        frozen.status = "ACCEPTED"  # simule un travail déjà engagé (matching/acceptation, hors scope Phase 2)
        await session.flush()

        await observed_update(svc, session, phone="+226", recurring_need_id=str(need_id), action="PERMANENT_QUANTITY", quantity=25)
        await session.refresh(frozen)
        return frozen

    frozen = _run(market[0], fn)
    assert frozen.requested_quantity == 40  # inchangé : n'était plus OPEN
    assert frozen.status == "ACCEPTED"


def test_a_permanent_update_never_silently_overwrites_an_explicit_exception(market):
    """Le point du mandat §10 : lundi, exception "demain 10 kg" ; puis modification permanente à
    25 kg/jour. L'exception explicite doit survivre — sinon elle serait écrasée silencieusement."""
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        occs = (
            await session.execute(
                select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id))
            )
        ).scalars().all()
        tomorrow_occ = min(occs, key=lambda o: o.occurrence_date)
        tomorrow = tomorrow_occ.occurrence_date.date()

        await observed_update(svc, session, 
            phone="+226", recurring_need_id=need_id, action="OCCURRENCE_OVERRIDE", occurrence_date=tomorrow, quantity=10
        )
        await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="PERMANENT_QUANTITY", quantity=25)

        await session.refresh(tomorrow_occ)
        others = [o for o in occs if o.id != tomorrow_occ.id]
        for o in others:
            await session.refresh(o)
        return tomorrow_occ, others

    tomorrow_occ, others = _run(market[0], fn)
    assert tomorrow_occ.requested_quantity == 10  # l'exception explicite survit
    assert tomorrow_occ.version == 2
    assert all(o.requested_quantity == 25 for o in others)  # les autres suivent bien la nouvelle règle


# ── skip ponctuel ─────────────────────────────────────────────────────────

def test_occurrence_skip_sets_status_and_bumps_version_without_touching_the_need(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        occs = (await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id)))).scalars().all()
        tomorrow = min(occs, key=lambda o: o.occurrence_date)

        await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="OCCURRENCE_SKIP", occurrence_date=tomorrow.occurrence_date.date())

        await session.refresh(tomorrow)
        need = await session.get(RecurringNeed, uuid.UUID(need_id))
        return tomorrow, need

    tomorrow, need = _run(market[0], fn)
    assert tomorrow.status == "SKIPPED" and tomorrow.version == 2
    assert need.status == "ACTIVE"  # le besoin permanent n'est jamais désactivé par un skip ponctuel


def test_skipping_a_non_open_occurrence_is_rejected(market):
    from ladini.services.database.errors import BusinessRuleException

    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        occs = (await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id)))).scalars().all()
        occ = min(occs, key=lambda o: o.occurrence_date)
        occ.status = "FULFILLED"
        await session.flush()
        try:
            await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="OCCURRENCE_SKIP", occurrence_date=occ.occurrence_date.date())
            return "no_exception"
        except BusinessRuleException:
            return "rejected"

    assert _run(market[0], fn) == "rejected"


# ── pause / reprise / annulation ──────────────────────────────────────────

def test_pause_skips_future_open_occurrences_within_the_pause_window(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="PAUSE", paused_until=date(2026, 9, 18))
        need = await session.get(RecurringNeed, uuid.UUID(need_id))
        occs = (await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id)))).scalars().all()
        return need, occs

    need, occs = _run(market[0], fn)
    assert need.status == "PAUSED"
    skipped = [o for o in occs if o.occurrence_date.date() < date(2026, 9, 18)]  # B25 : paused_until = jour de reprise (exclu)
    still_open = [o for o in occs if o.occurrence_date.date() >= date(2026, 9, 18)]
    assert all(o.status == "SKIPPED" for o in skipped)
    assert all(o.status == "OPEN" for o in still_open)


def test_resume_reactivates_the_need_without_reverting_past_skips(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="PAUSE", paused_until=date(2026, 9, 18))
        await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="RESUME")
        need = await session.get(RecurringNeed, uuid.UUID(need_id))
        occs = (await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id)))).scalars().all()
        return need, occs

    need, occs = _run(market[0], fn)
    assert need.status == "ACTIVE" and need.paused_until is None
    skipped_by_pause = [o for o in occs if o.occurrence_date.date() < date(2026, 9, 18)]
    assert all(o.status == "SKIPPED" for o in skipped_by_pause)  # un skip reste un fait historique


def test_cancel_marks_future_open_occurrences_cancelled(market):
    async def fn(session):
        svc = _svc(session, market)
        result = await _create(session, market)
        need_id = result["recurring_need_id"]
        await observed_update(svc, session, phone="+226", recurring_need_id=need_id, action="CANCEL")
        need = await session.get(RecurringNeed, uuid.UUID(need_id))
        occs = (await session.execute(select(RecurringNeedOccurrence).where(RecurringNeedOccurrence.recurring_need_id == uuid.UUID(need_id)))).scalars().all()
        return need, occs

    need, occs = _run(market[0], fn)
    assert need.status == "CANCELLED"
    assert all(o.status == "CANCELLED" for o in occs)


# ── ownership (mandat §12) ────────────────────────────────────────────────

def test_updating_another_buyers_recurring_need_is_rejected(pg_dsn):
    from ladini.services.database.errors import BusinessRuleException

    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        u2 = insert(cur, "auth.users", phone=uniq("+226"))
        other_buyer = insert(cur, "marketplace.buyer_profiles", user_id=u2)
        owner = SimpleNamespace(id=g.buyer_user)
        owner_profile = SimpleNamespace(id=g.buyer)
        intruder = SimpleNamespace(id=u2)
        intruder_profile = SimpleNamespace(id=other_buyer)
    conn.close()

    async def fn(session):
        owner_svc = _Svc(session, owner)
        owner_svc._user_profile = owner_profile
        result = await owner_svc.create_recurring_need(phone="+226", product_query="tomate", quantity=40, unit="KG", recurrence_type="DAILY")

        intruder_svc = _Svc(session, intruder)
        intruder_svc._user_profile = intruder_profile
        try:
            await observed_update(intruder_svc, session, phone="+226", recurring_need_id=result["recurring_need_id"], action="CANCEL")
            return "no_exception"
        except BusinessRuleException:
            return "rejected"

    assert _run(pg_dsn, fn) == "rejected"


# ── liste sans N+1 (mandat §19) ────────────────────────────────────────────

def test_list_my_recurring_needs_uses_a_constant_number_of_queries(market):
    def listing():
        async def fn(session):
            svc = _svc(session, market)
            return await svc.list_my_recurring_needs(phone="+226")

        return _run_counted(market[0], fn)

    # 1 besoin
    async def create_one(session):
        return await _create(session, market)

    _run(market[0], create_one)
    _result_one, sql_one = listing()

    # 4 besoins de plus (produits distincts pour éviter la même sous-catégorie)
    async def create_many(session):
        svc = _svc(session, market)
        for i in range(4):
            await svc.create_recurring_need(phone="+226", product_query=f"produit-{uniq(str(i))}", quantity=10, unit="KG", recurrence_type="DAILY")

    _run(market[0], create_many)
    result_many, sql_many = listing()

    assert len(result_many["items"]) == 5
    assert len(sql_many) == len(sql_one), f"N+1 : {len(sql_one)} requêtes pour 1 besoin, {len(sql_many)} pour 5"
    assert all(item["next_occurrence_date"] == "2026-09-16" for item in result_many["items"])
