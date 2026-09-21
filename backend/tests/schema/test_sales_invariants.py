"""Invariants critiques vente / paiement / commande garantis par PostgreSQL SOUS CONCURRENCE.

Aucune de ces garanties ne dépend de Redis ni du code applicatif : chaque test lance plusieurs
connexions réelles en parallèle (transactions committées) et exige qu'UN SEUL gagne.
"""
from __future__ import annotations

import threading

import psycopg2
import pytest
from factories import Graph, insert, uniq
from psycopg2 import errors


@pytest.fixture
def committed(pg_dsn):
    """Graphe métier COMMITTÉ (visible des autres connexions) ; la base entière est jetée en fin de session."""
    conn = psycopg2.connect(pg_dsn)
    with conn:
        with conn.cursor() as cur:
            g = Graph(cur)
            auction = g.auction()
            bids = [g.bid(auction)] + [g.bid(auction, producer=g.extra_producer()) for _ in range(3)]
    conn.close()
    return pg_dsn, g, auction, bids


def _race(dsn: str, n: int, work):
    """Lance `work(conn, i)` sur n connexions simultanées (barrière) ; retourne la liste des résultats/exceptions."""
    barrier = threading.Barrier(n)
    out: list = [None] * n

    def run(i: int):
        conn = psycopg2.connect(dsn)
        try:
            barrier.wait()
            try:
                out[i] = work(conn, i)
                conn.commit()
            except Exception as exc:  # noqa: BLE001 — on veut le type exact
                conn.rollback()
                out[i] = exc
        finally:
            conn.close()

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    return out


def test_concurrent_acceptance_of_different_bids_elects_exactly_one_winner(committed):
    dsn, _g, auction, bids = committed

    def accept(conn, i):
        cur = conn.cursor()
        cur.execute("update marketplace.bids set is_winner = true where id = %s", (bids[i],))
        return "won"

    out = _race(dsn, len(bids), accept)
    winners = [o for o in out if o == "won"]
    rejected = [o for o in out if isinstance(o, errors.UniqueViolation)]
    assert len(winners) == 1 and len(rejected) == len(bids) - 1, out
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("select count(*) from marketplace.bids where auction_id=%s and is_winner", (auction,))
    assert cur.fetchone()[0] == 1
    conn.close()


def test_concurrent_order_creation_for_one_auction_creates_exactly_one_order(committed):
    dsn, g, auction, _bids = committed

    def create(conn, i):
        cur = conn.cursor()
        insert(cur, "marketplace.orders", total_amount=1000, buyer_id=g.buyer, auction_id=auction)
        return "created"

    out = _race(dsn, 6, create)
    assert out.count("created") == 1
    assert sum(isinstance(o, errors.UniqueViolation) for o in out) == 5


def test_concurrent_payment_notifications_with_same_provider_ref_record_one_payment(committed):
    """IPN Paydunya rejoué N fois en parallèle : un seul paiement enregistré."""
    dsn, g, _a, _b = committed
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        order = g.__class__(cur).order()  # commande indépendante
    conn.close()
    ref = uniq("ipn")

    def notify(conn, i):
        cur = conn.cursor()
        cur.execute(
            "insert into marketplace.payments (order_id, amount, provider, provider_ref) values (%s, 100, 'PAYDUNYA', %s) "
            "on conflict (provider_ref) do nothing returning id",
            (order, ref),
        )
        return "inserted" if cur.fetchone() else "duplicate"

    out = _race(dsn, 8, notify)
    assert out.count("inserted") == 1 and out.count("duplicate") == 7, out


def test_concurrent_status_transition_is_applied_once(committed):
    """Compare-and-swap : UPDATE ... WHERE status = 'PENDING' — un seul des N appelants transitionne."""
    dsn, g, _a, _b = committed
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        order = g.__class__(cur).order()
    conn.close()

    def confirm(conn, i):
        cur = conn.cursor()
        cur.execute("update marketplace.orders set status='CONFIRMED' where id=%s and status='PENDING'", (order,))
        return cur.rowcount

    out = _race(dsn, 8, confirm)
    assert sorted(out) == [0] * 7 + [1], out


def test_mcp_idempotency_claim_is_exactly_once_under_concurrency(committed):
    dsn, *_ = committed
    key = uniq("idem")

    def claim(conn, i):
        cur = conn.cursor()
        cur.execute(
            "insert into marketplace.mcp_idempotency_records (idempotency_key, tool_name, request_hash, status) "
            "values (%s, 'create_order', 'h', 'PENDING') on conflict do nothing returning 1",
            (key,),
        )
        return "owner" if cur.fetchone() else "replay"

    out = _race(dsn, 10, claim)
    assert out.count("owner") == 1 and out.count("replay") == 9, out


def test_outbox_dedupe_makes_notification_enqueue_exactly_once(committed):
    dsn, *_ = committed
    key = uniq("dedupe")

    def enqueue(conn, i):
        cur = conn.cursor()
        cur.execute(
            "insert into intelligence.notification_outbox (channel, template_key, payload, dedupe_key) "
            "values ('WHATSAPP', 't', '{}'::jsonb, %s) on conflict (dedupe_key) do nothing returning 1",
            (key,),
        )
        return "queued" if cur.fetchone() else "skipped"

    out = _race(dsn, 8, enqueue)
    assert out.count("queued") == 1, out
