"""Approvisionnement récurrent (Phase 1 — fondation de données) : les 3 tables existent après migration,
FK réelles (allocations orphelines rejetées), uniques (occurrence dupliquée, allocation dupliquée), CHECK
(quantités, version), et la propriété fondamentale du modèle — modifier `recurring_needs` ne réécrit
JAMAIS une occurrence déjà créée. Voir aussi `test_referential_integrity.py` / `test_schema_postgres.py`
pour la couverture générique (orphelins, cascades, comparaison Drizzle/SQLAlchemy/PostgreSQL).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from factories import Graph, insert
from psycopg2 import errors

GHOST = str(uuid.uuid4())
# ON DELETE RESTRICT lève RestrictViolation (23001), NO ACTION lève ForeignKeyViolation (23503) : les deux protègent.
BLOCKED = (errors.ForeignKeyViolation, errors.RestrictViolation)


@pytest.fixture
def g(db):
    cur = db.cursor()
    return Graph(cur)


def _expect(db, exc, fn):
    """Exécute fn dans un SAVEPOINT : l'exception attendue doit survenir, la transaction reste utilisable."""
    cur = db.cursor()
    cur.execute("SAVEPOINT chk")
    with pytest.raises(exc):
        fn()
    cur.execute("ROLLBACK TO SAVEPOINT chk")


# ── Les 3 tables existent après migration (base vide → migrations) ──────────

def test_the_three_tables_exist_after_migration(pg_dsn):
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "select table_name from information_schema.tables where table_schema = 'marketplace' "
            "and table_name in ('recurring_needs', 'recurring_need_occurrences', 'need_allocations')"
        )
        assert {r[0] for r in cur.fetchall()} == {
            "recurring_needs",
            "recurring_need_occurrences",
            "need_allocations",
        }
    finally:
        conn.close()


# ── Un graphe valide est accepté ─────────────────────────────────────────────

def test_a_valid_recurring_need_with_occurrence_and_allocation_is_accepted(g):
    need = g.recurring_need(quantity=40)
    occ = g.occurrence(need, requested_quantity=40)
    g.allocation(occ, quantity=25)
    g.cur.execute(
        "select status, version, quantity_matched from marketplace.recurring_need_occurrences where id=%s", (occ,)
    )
    assert g.cur.fetchone() == ("OPEN", 1, 0)


# ── FK réelles : une allocation ne peut référencer aucun de ces 3 identifiants fantômes ──

ALLOCATION_ORPHANS = [
    ("need_allocations.occurrence_id", lambda g: g.allocation(occurrence=GHOST)),
    ("need_allocations.producer_id", lambda g: g.allocation(producer=GHOST)),
    ("need_allocations.product_id", lambda g: g.allocation(product=GHOST)),
]


@pytest.mark.parametrize("name,make", ALLOCATION_ORPHANS, ids=[o[0] for o in ALLOCATION_ORPHANS])
def test_an_allocation_cannot_reference_a_ghost_row(db, g, name, make):
    _expect(db, BLOCKED, lambda: make(g))


def test_an_occurrence_cannot_reference_a_ghost_recurring_need(db, g):
    _expect(db, BLOCKED, lambda: g.occurrence(need=GHOST))


def test_deleting_a_recurring_need_cascades_to_its_occurrences(db, g):
    need = g.recurring_need()
    occ = g.occurrence(need)
    g.cur.execute("delete from marketplace.recurring_needs where id=%s", (need,))
    g.cur.execute("select count(*) from marketplace.recurring_need_occurrences where id=%s", (occ,))
    assert g.cur.fetchone() == (0,)


def test_deleting_an_occurrence_cascades_to_its_allocations(db, g):
    occ = g.occurrence()
    alloc = g.allocation(occ)
    g.cur.execute("delete from marketplace.recurring_need_occurrences where id=%s", (occ,))
    g.cur.execute("select count(*) from marketplace.need_allocations where id=%s", (alloc,))
    assert g.cur.fetchone() == (0,)


def test_a_producer_with_an_allocation_cannot_be_deleted(db, g):
    g.allocation()
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from marketplace.producers where id=%s", (g.producer,)))


def test_a_product_with_an_allocation_cannot_be_deleted(db, g):
    g.allocation()
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from marketplace.products where id=%s", (g.product,)))


def test_deleting_an_order_item_detaches_the_allocation_instead_of_blocking_it(db, g):
    order = g.order()
    item = insert(g.cur, "marketplace.order_items", order_id=order, product_id=g.product, quantity=10, price_at_sale=100)
    alloc = g.allocation(order_item_id=item, status="CONVERTED")
    g.cur.execute("delete from marketplace.order_items where id=%s", (item,))
    g.cur.execute("select order_item_id from marketplace.need_allocations where id=%s", (alloc,))
    assert g.cur.fetchone() == (None,)


# ── Uniques ───────────────────────────────────────────────────────────────

def test_at_most_one_occurrence_per_need_and_date(db, g):
    need = g.recurring_need()
    date = datetime.utcnow()
    g.occurrence(need, occurrence_date=date)
    _expect(db, errors.UniqueViolation, lambda: g.occurrence(need, occurrence_date=date))


def test_at_most_one_allocation_per_occurrence_producer_and_product(db, g):
    occ = g.occurrence()
    g.allocation(occ)
    _expect(db, errors.UniqueViolation, lambda: g.allocation(occ))


# ── CHECK ────────────────────────────────────────────────────────────────

CHECK_VIOLATIONS = [
    ("recurring_needs.quantity <= 0", lambda g: g.recurring_need(quantity=0)),
    ("recurring_needs.max_price_per_unit < 0", lambda g: g.recurring_need(max_price_per_unit=-1)),
    ("recurring_needs.recurrence_type invalide", lambda g: g.recurring_need(recurrence_type="MONTHLY")),
    ("recurring_needs.status invalide", lambda g: g.recurring_need(status="DONE")),
    ("recurring_needs.WEEKLY_DAYS sans jours", lambda g: g.recurring_need(recurrence_type="WEEKLY_DAYS")),
    ("recurring_need_occurrences.requested_quantity <= 0", lambda g: g.occurrence(requested_quantity=0)),
    ("recurring_need_occurrences.quantity_matched < 0", lambda g: g.occurrence(quantity_matched=-1)),
    ("recurring_need_occurrences.quantity_confirmed < 0", lambda g: g.occurrence(quantity_confirmed=-1)),
    ("recurring_need_occurrences.quantity_delivered < 0", lambda g: g.occurrence(quantity_delivered=-1)),
    ("recurring_need_occurrences.version < 1", lambda g: g.occurrence(version=0)),
    ("recurring_need_occurrences.status invalide", lambda g: g.occurrence(status="DONE")),
    ("need_allocations.quantity <= 0", lambda g: g.allocation(quantity=0)),
    ("need_allocations.unit_price < 0", lambda g: g.allocation(unit_price=-1)),
    ("need_allocations.status invalide", lambda g: g.allocation(status="DONE")),
]


@pytest.mark.parametrize("name,make", CHECK_VIOLATIONS, ids=[c[0] for c in CHECK_VIOLATIONS])
def test_check_constraints_reject_invalid_values(db, g, name, make):
    _expect(db, errors.CheckViolation, lambda: make(g))


def test_weekly_days_recurrence_is_accepted_when_days_are_listed(g):
    need = g.recurring_need(recurrence_type="WEEKLY_DAYS", weekly_days=[1, 3, 5])
    g.cur.execute("select weekly_days from marketplace.recurring_needs where id=%s", (need,))
    assert g.cur.fetchone() == ([1, 3, 5],)


# ── Propriété fondamentale : l'historique d'une occurrence ne bouge pas si le besoin est modifié ──

def test_modifying_a_recurring_need_never_rewrites_an_already_created_occurrence(g):
    need = g.recurring_need(quantity=40)
    occ = g.occurrence(need, requested_quantity=40)

    g.cur.execute("update marketplace.recurring_needs set quantity = 25 where id=%s", (need,))

    g.cur.execute("select requested_quantity from marketplace.recurring_need_occurrences where id=%s", (occ,))
    assert g.cur.fetchone() == (40,)


# ── SKIPPED représente une exception ponctuelle sans table d'override séparée ──

def test_skipped_status_represents_a_one_off_pause_without_a_separate_override_table(g):
    """« Suspends demain » = une occurrence normalement OPEN passe à SKIPPED — aucune autre table impliquée."""
    need = g.recurring_need()
    tomorrow = datetime.utcnow() + timedelta(days=1)
    occ = g.occurrence(need, occurrence_date=tomorrow)

    g.cur.execute("update marketplace.recurring_need_occurrences set status = 'SKIPPED' where id=%s", (occ,))

    g.cur.execute("select status, requested_quantity from marketplace.recurring_need_occurrences where id=%s", (occ,))
    assert g.cur.fetchone() == ("SKIPPED", 40)


def test_a_one_off_quantity_change_is_an_edit_of_the_occurrence_itself(g):
    """« Demain seulement 10 kg » = modifier CETTE occurrence, pas une ligne dans une table séparée."""
    need = g.recurring_need(quantity=40)
    tomorrow = datetime.utcnow() + timedelta(days=1)
    occ = g.occurrence(need, occurrence_date=tomorrow, requested_quantity=40)

    g.cur.execute(
        "update marketplace.recurring_need_occurrences set requested_quantity = 10, version = version + 1 where id=%s",
        (occ,),
    )

    g.cur.execute("select requested_quantity, version from marketplace.recurring_need_occurrences where id=%s", (occ,))
    assert g.cur.fetchone() == (10, 2)
