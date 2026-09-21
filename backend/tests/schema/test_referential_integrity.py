"""Intégrité référentielle réelle : orphelins rejetés, CASCADE / RESTRICT / SET NULL, unicités critiques."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from factories import Graph, insert, uniq
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


# ── Données valides ──────────────────────────────────────────────────────

def test_a_valid_sale_chain_is_accepted(g):
    auction = g.auction()
    bid = g.bid(auction)
    order = g.order(auction_id=auction, winning_bid_id=bid)
    insert(g.cur, "marketplace.order_items", order_id=order, product_id=g.product, quantity=10, price_at_sale=450)
    insert(g.cur, "marketplace.payments", order_id=order, amount=4500, provider="PAYDUNYA", provider_ref=uniq("ref"))
    insert(g.cur, "marketplace.deliveries", order_id=order)
    g.cur.execute("select status, payment_status, currency from marketplace.orders where id=%s", (order,))
    assert g.cur.fetchone() == ("PENDING", "PENDING", "XOF")


# ── Orphelins rejetés (FK réellement imposées par PostgreSQL) ────────────

ORPHANS = [
    ("orders.buyer_id", lambda g: g.order(buyer_id=GHOST)),
    ("orders.auction_id", lambda g: g.order(auction_id=GHOST)),
    ("order_items.order_id", lambda g: insert(g.cur, "marketplace.order_items", order_id=GHOST, product_id=g.product, quantity=1, price_at_sale=1)),
    ("order_items.product_id", lambda g: insert(g.cur, "marketplace.order_items", order_id=g.order(), product_id=GHOST, quantity=1, price_at_sale=1)),
    ("payments.order_id", lambda g: insert(g.cur, "marketplace.payments", order_id=GHOST, amount=1)),
    ("deliveries.order_id", lambda g: insert(g.cur, "marketplace.deliveries", order_id=GHOST)),
    ("bids.auction_id", lambda g: g.bid(GHOST)),
    ("bids.producer_id", lambda g: g.bid(g.auction(), producer=GHOST)),
    ("auctions.buyer_id", lambda g: g.auction(buyer_id=GHOST)),
    ("auctions.sub_category_id", lambda g: g.auction(sub_category_id=GHOST)),
    ("auctions.winner_bid_id", lambda g: g.auction(winner_bid_id=GHOST)),
    ("products.producer_id", lambda g: insert(g.cur, "marketplace.products", category_label="x", price=1, producer_id=GHOST)),
    ("products.sub_category_id", lambda g: insert(g.cur, "marketplace.products", category_label="x", price=1, producer_id=g.producer, sub_category_id=GHOST)),
    ("producers.user_id", lambda g: insert(g.cur, "marketplace.producers", user_id=GHOST)),
    ("buyer_profiles.user_id", lambda g: insert(g.cur, "marketplace.buyer_profiles", user_id=GHOST)),
    ("sessions.user_id", lambda g: insert(g.cur, "auth.sessions", session_token=uniq("t"), user_id=GHOST, expires=datetime.utcnow())),
    ("accounts.user_id", lambda g: insert(g.cur, "auth.accounts", user_id=GHOST, type="oauth", provider="g", provider_account_id=uniq("a"))),
    ("users.zone_id", lambda g: insert(g.cur, "auth.users", phone=uniq("+226"), zone_id=GHOST)),
    ("stocks.farm_id", lambda g: insert(g.cur, "marketplace.stocks", item_name="x", farm_id=GHOST)),
    ("stock_movements.stock_id", lambda g: insert(g.cur, "marketplace.stock_movements", stock_id=GHOST, type="IN", quantity=1)),
    ("conversations.user_id", lambda g: insert(g.cur, "intelligence.conversations", user_id=GHOST, query="q")),
    ("solicitations.auction_id", lambda g: insert(g.cur, "intelligence.solicitations", kind="AUCTION", auction_id=GHOST)),
    ("solicitations.target_producer_id", lambda g: insert(g.cur, "intelligence.solicitations", kind="AUCTION", target_producer_id=GHOST)),
    ("demand_signals.user_id", lambda g: insert(g.cur, "intelligence.demand_signals", normalized_term=uniq(), raw_query="q", user_id=GHOST)),
    ("moderation_events.user_id", lambda g: insert(g.cur, "intelligence.moderation_events", phone="+226", kind="X", user_id=GHOST)),
    ("order_disputes.raised_by_id", lambda g: insert(g.cur, "marketplace.order_disputes", order_id=g.order(), raised_by_id=GHOST, reason_category="r", description="d", requested_solution="s")),
    ("audit_logs.actor_id", lambda g: insert(g.cur, "intelligence.audit_logs", actor_id=GHOST, action="x", entity_type="x", entity_id=uniq())),
]


@pytest.mark.parametrize("name,make", ORPHANS, ids=[o[0] for o in ORPHANS])
def test_orphan_rows_are_rejected(db, g, name, make):
    _expect(db, errors.ForeignKeyViolation, lambda: make(g))


# ── CASCADE / RESTRICT / SET NULL ────────────────────────────────────────

def test_deleting_a_user_cascades_to_sessions_and_accounts(db, g):
    u = insert(g.cur, "auth.users", phone=uniq("+226"))
    insert(g.cur, "auth.sessions", session_token=uniq("t"), user_id=u, expires=datetime.utcnow() + timedelta(days=1))
    insert(g.cur, "auth.accounts", user_id=u, type="oauth", provider="google", provider_account_id=uniq("a"))
    g.cur.execute("delete from auth.users where id=%s", (u,))
    g.cur.execute("select (select count(*) from auth.sessions where user_id=%s)+(select count(*) from auth.accounts where user_id=%s)", (u, u))
    assert g.cur.fetchone()[0] == 0


def test_a_user_with_a_producer_profile_cannot_be_deleted(db, g):
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from auth.users where id=%s", (g.producer_user,)))


def test_an_order_with_payments_cannot_be_deleted(db, g):
    order = g.order()
    insert(g.cur, "marketplace.payments", order_id=order, amount=10)
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from marketplace.orders where id=%s", (order,)))


def test_an_auction_with_bids_cannot_be_deleted(db, g):
    a = g.auction()
    g.bid(a)
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from marketplace.auctions where id=%s", (a,)))


def test_a_winning_bid_cannot_be_deleted_while_referenced(db, g):
    a = g.auction()
    b = g.bid(a)
    g.cur.execute("update marketplace.auctions set winner_bid_id=%s where id=%s", (b, a))
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from marketplace.bids where id=%s", (b,)))


def test_deleting_a_zone_detaches_users_instead_of_deleting_them(db, g):
    g.cur.execute("update auth.users set zone_id=%s where id=%s", (g.zone, g.buyer_user))
    g.cur.execute("update marketplace.producers set zone_id=NULL where id=%s", (g.producer,))
    g.cur.execute("update auth.users set zone_id=NULL where id=%s", (g.producer_user,))
    g.cur.execute("delete from governance.zones where id=%s", (g.zone,))
    g.cur.execute("select zone_id from auth.users where id=%s", (g.buyer_user,))
    assert g.cur.fetchone() == (None,)


def test_a_sub_category_used_by_a_product_cannot_be_deleted(db, g):
    _expect(db, BLOCKED, lambda: g.cur.execute("delete from governance.sub_categories where id=%s", (g.sub_category,)))


# ── Unicités critiques imposées par PostgreSQL ───────────────────────────

def test_user_phone_is_unique(db, g):
    p = uniq("+226")
    insert(g.cur, "auth.users", phone=p)
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "auth.users", phone=p))


def test_one_producer_profile_and_one_buyer_profile_per_user(db, g):
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "marketplace.producers", user_id=g.producer_user))
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "marketplace.buyer_profiles", user_id=g.buyer_user))


def test_a_producer_can_bid_only_once_per_auction(db, g):
    a = g.auction()
    g.bid(a)
    _expect(db, errors.UniqueViolation, lambda: g.bid(a))


def test_an_auction_has_at_most_one_winning_bid(db, g):
    a = g.auction()
    g.bid(a, is_winner=True)
    _expect(db, errors.UniqueViolation, lambda: g.bid(a, producer=g.extra_producer(), is_winner=True))
    g.bid(a, producer=g.extra_producer(), is_winner=False)  # les perdants restent illimités


def test_an_auction_produces_at_most_one_order(db, g):
    a = g.auction()
    g.order(auction_id=a)
    _expect(db, errors.UniqueViolation, lambda: g.order(auction_id=a))


def test_a_payment_provider_reference_is_recorded_once(db, g):
    ref = uniq("ref")
    o = g.order()
    insert(g.cur, "marketplace.payments", order_id=o, amount=1, provider="PAYDUNYA", provider_ref=ref)
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "marketplace.payments", order_id=o, amount=1, provider="PAYDUNYA", provider_ref=ref))


def test_a_paydunya_invoice_token_identifies_one_order(db, g):
    tok = uniq("tok")
    g.order(paydunya_invoice_token=tok)
    _expect(db, errors.UniqueViolation, lambda: g.order(paydunya_invoice_token=tok))
    g.order()  # NULL multiples autorisés (index partiel)
    g.order()


def test_one_delivery_per_order(db, g):
    o = g.order()
    insert(g.cur, "marketplace.deliveries", order_id=o)
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "marketplace.deliveries", order_id=o))


def test_outbox_dedupe_key_is_unique(db, g):
    k = uniq("dedupe")
    cols = dict(channel="WHATSAPP", template_key="t", payload={"a": 1}, dedupe_key=k)
    insert(g.cur, "intelligence.notification_outbox", **cols)
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "intelligence.notification_outbox", **cols))


def test_solicitation_is_unique_per_auction_and_producer(db, g):
    a = g.auction()
    insert(g.cur, "intelligence.solicitations", kind="AUCTION", auction_id=a, target_producer_id=g.producer)
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "intelligence.solicitations", kind="AUCTION", auction_id=a, target_producer_id=g.producer))


def test_mcp_idempotency_key_is_unique_per_tool(db, g):
    k = uniq("idem")
    insert(g.cur, "marketplace.mcp_idempotency_records", idempotency_key=k, tool_name="create_order", request_hash="h", status="PENDING")
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "marketplace.mcp_idempotency_records", idempotency_key=k, tool_name="create_order", request_hash="h", status="PENDING"))
    insert(g.cur, "marketplace.mcp_idempotency_records", idempotency_key=k, tool_name="other_tool", request_hash="h", status="PENDING")


def test_draft_id_is_the_primary_key(db, g):
    d = uniq("draft")
    cols = dict(draft_id=d, conversation_id="c", version=1, status="OPEN", payload={"x": 1})
    insert(g.cur, "marketplace.preorder_drafts", **cols)
    _expect(db, errors.UniqueViolation, lambda: insert(g.cur, "marketplace.preorder_drafts", **cols))


def test_not_null_business_columns_are_enforced(db, g):
    _expect(db, errors.NotNullViolation, lambda: g.cur.execute("insert into marketplace.orders (buyer_id) values (%s)", (g.buyer,)))
    _expect(db, errors.NotNullViolation, lambda: g.cur.execute("insert into marketplace.payments (order_id) values (%s)", (g.order(),)))


def test_deleting_a_user_keeps_moderation_history_and_detaches_it(db, g):
    u = insert(g.cur, "auth.users", phone=uniq("+226"))
    ev = insert(g.cur, "intelligence.moderation_events", phone="+226", kind="X", user_id=u)
    g.cur.execute("delete from auth.users where id=%s", (u,))
    g.cur.execute("select user_id from intelligence.moderation_events where id=%s", (ev,))
    assert g.cur.fetchone() == (None,)


def test_deleting_a_solicitation_target_producer_removes_the_derived_solicitation(db, g):
    """Sollicitation = donnée dérivée : elle suit son producteur cible (CASCADE)."""
    a = g.auction()
    p = g.extra_producer()
    sol = insert(g.cur, "intelligence.solicitations", kind="AUCTION", auction_id=a, target_producer_id=p)
    g.cur.execute("delete from marketplace.producers where id=%s", (p,))
    g.cur.execute("select count(*) from intelligence.solicitations where id=%s", (sol,))
    assert g.cur.fetchone()[0] == 0
