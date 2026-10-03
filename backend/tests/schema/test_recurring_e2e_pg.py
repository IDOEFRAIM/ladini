"""B13 — parcours recurring de bout en bout sur les VRAIS services et un VRAI PostgreSQL.

Aucun de ces tests n'exécute de logique de test : chaque étape appelle le code de production (matching,
digest, liste/détail Buyer, accept, confirmation producteur, livraison/réception, balayage d'expiration,
réapprovisionnement). Ils FIGENT ce qui existe aujourd'hui, y compris les manques (nommés `GAP`).

Exécutés en CI uniquement (aucun PostgreSQL local sur la machine d'audit) : `pg_dsn` saute sinon.
Chaque test utilise sa propre date d'occurrence (`_fresh_date`, 2029+) : la base est partagée par le fichier.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg2
from factories import Graph, insert, uniq
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.models import NotificationOutbox
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin
from ladini.workers.automation.need_matching_service import NeedMatchingService
from ladini.workers.automation.recurring_supply_digest_service import (
    RecurringSupplyDigestService,
)

_DAYS = iter(range(1, 5000))


def _fresh_date() -> date:
    return date(2029, 1, 1) + timedelta(days=next(_DAYS))


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    """Acheteur ET producteur sur la même instance : chaque test choisit l'identité par l'appel."""

    def __init__(self, session, buyer, producer):
        self._s, self._buyer, self._producer = session, buyer, producer

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._buyer

    async def get_producer_profile(self, phone):
        return self._producer


def _run(dsn, fn):
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


def _sql(dsn, query, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if cur.description else []
    conn.close()
    return rows


def _identities(dsn, g):
    phone = _sql(dsn, "select phone from auth.users where id = %s", (str(g.buyer_user),))[0][0]
    buyer = (SimpleNamespace(id=g.buyer_user, phone=phone, zone_id=None, name="Resto"),
             SimpleNamespace(id=g.buyer, establishment_name="Resto"))
    producer = (SimpleNamespace(id=g.producer_user), SimpleNamespace(id=g.producer))
    return phone, buyer, producer


def _seed_one_cycle(dsn, d: date, *, requested=40, stock=100, starts: date | None = None):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=requested, unit="KG", recurrence_type="DAILY", starts_at=_dt(starts or d))
        occ = g.occurrence(need, occurrence_date=_dt(d), requested_quantity=requested, unit="KG", status="OPEN")
        product = g.product_for(quantity_for_sale=stock, price=500)
    conn.close()
    return g, need, occ, product


def _svc_run(dsn, g, fn):
    phone, buyer, producer = _identities(dsn, g)

    async def go(session):
        return await fn(_Svc(session, buyer, producer), session, phone)

    return _run(dsn, go)


def _occ_row(dsn, occ):
    r = _sql(dsn, "select status, version, quantity_matched, quantity_confirmed, quantity_delivered, notified_at "
                  "from marketplace.recurring_need_occurrences where id = %s", (str(occ),))[0]
    return {"status": r[0], "version": r[1], "matched": float(r[2]), "confirmed": float(r[3]),
            "delivered": float(r[4]), "notified": r[5] is not None}


def _digest(dsn, d: date, g):
    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        rows = (await session.execute(
            select(NotificationOutbox).where(NotificationOutbox.template_key == "RECURRING_SUPPLY_DIGEST_BUYER")
        )).scalars().all()
        # Base partagée : on ne garde que le digest de CET acheteur pour CETTE date.
        return [r for r in rows if f":{g.buyer}:{d.isoformat()}:" in r.dedupe_key]

    return _run(dsn, fn)


# ── PARCOURS COMPLET : matching -> digest -> vue Buyer -> accept -> producteur -> livraison -> occurrence ──

def test_full_cycle_matching_digest_buyer_view_accept_producer_delivery_reception(pg_dsn):
    d = date.today() + timedelta(days=1)
    g, need, occ, product = _seed_one_cycle(pg_dsn, d, starts=date.today())

    # (1) Visibilité Buyer AVANT matching/digest : le besoin est listé, l'occurrence OPEN visible, aucune proposition.
    async def view(svc, session, phone):
        return await svc.list_my_recurring_needs(phone)

    items = [i for i in _svc_run(pg_dsn, g, view)["items"] if i["recurring_need_id"] == str(need)]
    assert items and items[0]["status"] == "ACTIVE"
    assert items[0]["next_occurrence_id"] == str(occ) and items[0]["matched_quantity"] == 0.0
    assert items[0]["next_occurrence_notified"] is False and items[0]["in_latest_digest"] is False

    # (2) Matching RÉEL : OPEN -> MATCHED, allocation PROPOSED, version bumpée.
    v_before = _occ_row(pg_dsn, occ)["version"]
    _run(pg_dsn, lambda s: NeedMatchingService(s).rematch_occurrence(occ))
    row = _occ_row(pg_dsn, occ)
    assert row["status"] == "MATCHED" and row["matched"] == 40.0 and row["version"] > v_before
    allocs = _sql(pg_dsn, "select status, quantity from marketplace.need_allocations where occurrence_id = %s", (str(occ),))
    assert [(a[0], float(a[1])) for a in allocs] == [("PROPOSED", 40.0)]

    # (3) AVANT le digest : la proposition est visible dans le détail ET acceptable depuis le détail
    #     (identité exacte occurrence + version affichées). Visibilité != dépendance au cron.
    async def detail(svc, session, phone):
        return await svc.get_recurring_need_detail(phone, str(need))

    det = _svc_run(pg_dsn, g, detail)
    assert det["occurrence_id"] == str(occ) and det["occurrence_version"] == row["version"]
    assert len(det["allocations"]) == 1 and det["allocations"][0]["unit_price"] == 500.0

    # (4) Digest RÉEL : outbox, payload versionné, notified_at posé.
    outbox = _digest(pg_dsn, d, g)
    assert len(outbox) == 1
    payload = outbox[0].payload
    assert payload["occurrences"] == [{"recurring_need_id": str(need), "occurrence_id": str(occ),
                                       "version": row["version"]}]
    assert payload["digest_signature"] and _occ_row(pg_dsn, occ)["notified"] is True

    # (5) Vue Buyer APRÈS digest : l'item appartient au dernier digest, à la version du message.
    after = [i for i in _svc_run(pg_dsn, g, view)["items"] if i["recurring_need_id"] == str(need)][0]
    assert after["in_latest_digest"] is True and after["digest_occurrence_version"] == row["version"]

    # (6) « oui » au digest = accept exact (occurrence + version du digest).
    async def accept(svc, session, phone):
        return await svc.accept_match_proposal(
            phone, str(need), "ACCEPT", occurrence_id=str(occ), expected_version=after["digest_occurrence_version"]
        )

    res = _svc_run(pg_dsn, g, accept)
    assert res.get("outcome") is None and len(res["order_ids"]) == 1
    order_id = res["order_ids"][0]

    # (7) Lignée occurrence -> allocation -> order EXACTE ; stock débité UNE fois ; statut initial.
    assert _occ_row(pg_dsn, occ)["status"] == "ACCEPTED"
    lineage = _sql(pg_dsn, "select na.status, oi.order_id::text, o.order_type, o.status, o.checkout_group_id::text, "
                           "oc.order_group_id::text from marketplace.need_allocations na "
                           "join marketplace.order_items oi on oi.id = na.order_item_id "
                           "join marketplace.orders o on o.id = oi.order_id "
                           "join marketplace.recurring_need_occurrences oc on oc.id = na.occurrence_id "
                           "where na.occurrence_id = %s", (str(occ),))
    assert lineage == [("CONVERTED", order_id, "RECURRING_SUPPLY", "PENDING_PRODUCER_CONFIRMATION",
                        lineage[0][4], lineage[0][4])]
    stock = float(_sql(pg_dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(product),))[0][0])
    assert stock == 60.0

    # (8) Le producteur VOIT l'order (même service que « mes commandes » producteur) puis le confirme.
    # (le listing `get_producer_orders` résout le téléphone via `normalize_phone` — couvert par ses propres tests ;
    #  ici on prouve exactement ce qu'il sélectionne : une ligne de CE producteur, RECURRING_SUPPLY, en attente)
    seen = _sql(pg_dsn, "select o.order_type, o.status from marketplace.orders o "
                        "join marketplace.order_items oi on oi.order_id = o.id "
                        "join marketplace.products p on p.id = oi.product_id "
                        "where o.id = %s and p.producer_id = %s", (order_id, str(g.producer)))
    assert seen == [("RECURRING_SUPPLY", "PENDING_PRODUCER_CONFIRMATION")]

    async def confirm(svc, session, phone):
        return await svc.confirm_order_by_producer("+226", order_id)

    assert _svc_run(pg_dsn, g, confirm)["status"] == "success"
    assert _sql(pg_dsn, "select status from marketplace.orders where id = %s", (order_id,))[0][0] == "CONFIRMED"

    # (9) Chemin de livraison RECURRING_SUPPLY : en route -> livré -> réception acheteur « tout est bon ».
    async def transit(svc, session, phone):
        await svc.mark_order_delivery_status("+226", order_id, "MARK_IN_TRANSIT")
        return await svc.mark_order_delivery_status("+226", order_id, "MARK_DELIVERED")

    assert _svc_run(pg_dsn, g, transit)["delivery_status"] == "DELIVERED"

    async def receive(svc, session, phone):
        return await svc.record_order_reception(phone, order_id, "RECEIVED")

    assert _svc_run(pg_dsn, g, receive)["outcome"] == "RECORDED"
    final = _sql(pg_dsn, "select status, payment_status, delivery_status from marketplace.orders where id = %s", (order_id,))[0]
    assert final == ("COMPLETED", "PENDING", "RECEIVED")  # GAP : jamais PAID (paiement hors plateforme, non suivi)

    # (10) Occurrence APRÈS orders COMPLETED : quantité livrée suivie, mais le statut reste ACCEPTED.
    #      (B14) toutes les commandes sont terminales et la quantité demandée est livrée : FULFILLED.
    closed = _occ_row(pg_dsn, occ)
    assert closed["status"] == "FULFILLED" and closed["delivered"] == 40.0 and closed["confirmed"] == 40.0
    need_status = _sql(pg_dsn, "select status from marketplace.recurring_needs where id = %s", (str(need),))[0][0]
    assert need_status == "ACTIVE"

    # (11) Occurrence SUIVANTE : le réapprovisionnement réel crée les jours à venir, version propre, sans reliquat.
    async def replenish(svc, session, phone):
        return await svc.replenish_occurrence_windows()

    _svc_run(pg_dsn, g, replenish)
    nxt = _sql(pg_dsn, "select id::text, status, version, notified_at is not null, quantity_matched "
                       "from marketplace.recurring_need_occurrences where recurring_need_id = %s and occurrence_date > %s "
                       "order by occurrence_date asc limit 1", (str(need), _dt(d)))
    assert nxt and nxt[0][1] == "OPEN" and nxt[0][2] == 1 and nxt[0][3] is False and float(nxt[0][4]) == 0.0
    leaked = _sql(pg_dsn, "select count(*) from marketplace.need_allocations where occurrence_id = %s", (nxt[0][0],))
    assert int(leaked[0][0]) == 0


# ── B11 x recurring : la clôture « livrée et payée » du producteur sur une commande RECURRING_SUPPLY ──

def test_b11_cash_closure_on_a_recurring_order_counts_as_received_and_fulfills_the_occurrence(pg_dsn):
    """(B14) Avant : `confirm_delivery_and_payment` laissait `quantity_delivered = 0` alors que la commande était
    COMPLETED. Désormais la déclaration « livrée + payée » d'une commande RECURRING_SUPPLY EST la réception
    (`RECEIVED`) : l'occurrence est recalculée dans la même transaction."""
    d = _fresh_date()
    g, need, occ, _product = _seed_one_cycle(pg_dsn, d)
    _run(pg_dsn, lambda s: NeedMatchingService(s).rematch_occurrence(occ))

    async def accept(svc, session, phone):
        return await svc.accept_match_proposal(phone, str(need), "ACCEPT", occurrence_id=str(occ),
                                               expected_version=_occ_row(pg_dsn, occ)["version"])

    order_id = _svc_run(pg_dsn, g, accept)["order_ids"][0]

    async def close(svc, session, phone):
        await svc.confirm_order_by_producer("+226", order_id)
        return await svc.confirm_delivery_and_payment("+226", order_id)

    assert _svc_run(pg_dsn, g, close)["outcome"] == "COMPLETED"
    assert _sql(pg_dsn, "select status, payment_status, delivery_status from marketplace.orders where id = %s", (order_id,))[0] == (
        "COMPLETED", "PAID", "RECEIVED")
    final = _occ_row(pg_dsn, occ)
    assert final["delivered"] == 40.0 and final["status"] == "FULFILLED"


# ── NO-RESPONSE ──────────────────────────────────────────────────────────────

def test_no_response_expiry_then_old_yes_creates_nothing_and_next_occurrence_is_clean(pg_dsn):
    past = date.today() - timedelta(days=3)
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY", starts_at=_dt(past))
        o1 = g.occurrence(need, occurrence_date=_dt(past), requested_quantity=40, unit="KG", status="MATCHED",
                          quantity_matched=40, notified_at=datetime.utcnow() - timedelta(days=4))
        product = g.product_for(quantity_for_sale=100)
        g.allocation(occurrence=o1, producer=g.producer, product=product, quantity=40, unit_price=500, unit="KG")
        o2 = g.occurrence(need, occurrence_date=_dt(date.today() + timedelta(days=1)), requested_quantity=40,
                          unit="KG", status="OPEN")
    conn.close()

    async def sweep(svc, session, phone):
        return await svc.expire_past_occurrences()

    assert _svc_run(pg_dsn, g, sweep)["occurrences_expired"] >= 1
    assert _occ_row(pg_dsn, o1)["status"] == "EXPIRED"
    assert [a[0] for a in _sql(pg_dsn, "select status from marketplace.need_allocations where occurrence_id = %s", (str(o1),))] == ["EXPIRED"]

    async def old_yes(svc, session, phone):
        return await svc.accept_match_proposal(phone, str(need), "ACCEPT", occurrence_id=str(o1), expected_version=1)

    assert _svc_run(pg_dsn, g, old_yes)["outcome"] == "EXPIRED"
    assert float(_sql(pg_dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(product),))[0][0]) == 100.0

    async def view(svc, session, phone):
        return await svc.list_my_recurring_needs(phone)

    item = [i for i in _svc_run(pg_dsn, g, view)["items"] if i["recurring_need_id"] == str(need)][0]
    assert item["next_occurrence_id"] == str(o2)  # O1 expirée ne masque plus O2


def test_reject_exact_marks_only_that_occurrence_and_creates_no_order(pg_dsn):
    d = _fresh_date()
    g, need, occ, product = _seed_one_cycle(pg_dsn, d)
    _run(pg_dsn, lambda s: NeedMatchingService(s).rematch_occurrence(occ))
    version = _occ_row(pg_dsn, occ)["version"]

    async def reject(svc, session, phone):
        return await svc.accept_match_proposal(phone, str(need), "REJECT", occurrence_id=str(occ), expected_version=version)

    assert _svc_run(pg_dsn, g, reject)["action"] == "REJECT"
    assert _occ_row(pg_dsn, occ)["status"] == "REJECTED"
    assert int(_sql(pg_dsn, "select count(*) from marketplace.orders where buyer_id = %s and order_type = 'RECURRING_SUPPLY'", (str(g.buyer),))[0][0]) == 0
    assert float(_sql(pg_dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(product),))[0][0]) == 100.0


# ── PARTIEL + MULTI-PRODUCTEUR ───────────────────────────────────────────────

def test_partial_match_accepts_only_the_matched_quantity(pg_dsn):
    d = _fresh_date()
    g, need, occ, product = _seed_one_cycle(pg_dsn, d, requested=100, stock=60)
    _run(pg_dsn, lambda s: NeedMatchingService(s).rematch_occurrence(occ))
    row = _occ_row(pg_dsn, occ)
    assert row["matched"] == 60.0 and row["status"] == "OPEN"

    async def accept(svc, session, phone):
        return await svc.accept_match_proposal(phone, str(need), "ACCEPT", occurrence_id=str(occ), expected_version=row["version"])

    # `notified_at` n'est pas requis quand une version est fournie (même contrat que l'écran détail).
    res = _svc_run(pg_dsn, g, accept)
    assert res["quantity_confirmed"] == 60.0 and _occ_row(pg_dsn, occ)["status"] == "PARTIALLY_ACCEPTED"


def test_multi_producer_acceptance_creates_one_order_per_producer_with_no_cross_lines(pg_dsn):
    d = _fresh_date()
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        pB = g.extra_producer()
        need = g.recurring_need(quantity=70, unit="KG", recurrence_type="DAILY", starts_at=_dt(d))
        occ = g.occurrence(need, occurrence_date=_dt(d), requested_quantity=70, unit="KG", status="MATCHED",
                           quantity_matched=70, notified_at=datetime.utcnow())
        prodA = g.product_for(producer=g.producer, quantity_for_sale=50)
        prodB = g.product_for(producer=pB, quantity_for_sale=50)
        g.allocation(occurrence=occ, producer=g.producer, product=prodA, quantity=40, unit_price=500, unit="KG")
        g.allocation(occurrence=occ, producer=pB, product=prodB, quantity=30, unit_price=600, unit="KG")
    conn.close()
    version = _occ_row(pg_dsn, occ)["version"]

    async def accept(svc, session, phone):
        return await svc.accept_match_proposal(phone, str(need), "ACCEPT", occurrence_id=str(occ), expected_version=version)

    res = _svc_run(pg_dsn, g, accept)
    assert len(res["order_ids"]) == 2 and res["quantity_confirmed"] == 70.0
    rows = _sql(pg_dsn, "select o.id::text, p.producer_id::text, sum(oi.quantity), o.total_amount "
                        "from marketplace.orders o join marketplace.order_items oi on oi.order_id = o.id "
                        "join marketplace.products p on p.id = oi.product_id where o.id = any(%s::uuid[]) "
                        "group by o.id, p.producer_id, o.total_amount", (res["order_ids"],))
    assert len(rows) == 2  # une seule ligne producteur par order : aucune ligne croisée
    by_producer = {r[1]: (float(r[2]), float(r[3])) for r in rows}
    assert by_producer[str(g.producer)] == (40.0, 20000.0) and by_producer[str(pB)] == (30.0, 18000.0)


# ── DEUX BESOINS, UN DIGEST ──────────────────────────────────────────────────

def test_two_needs_one_digest_both_listed_in_latest_digest_each_with_own_version(pg_dsn):
    d = _fresh_date()
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        occs = []
        for label, qty in (("tomate", 40), ("lait", 20)):
            cat = insert(cur, "governance.categories", name=uniq("cat"))
            sub = insert(cur, "governance.sub_categories", category_id=cat, name=label)
            need = g.recurring_need(quantity=qty, unit="KG", sub_category_id=sub, starts_at=_dt(d))
            occ = g.occurrence(need, occurrence_date=_dt(d), requested_quantity=qty, unit="KG", status="MATCHED",
                               quantity_matched=qty)
            prod = g.product_for(quantity_for_sale=100, sub_category_id=sub)
            g.allocation(occurrence=occ, producer=g.producer, product=prod, quantity=qty, unit_price=500, unit="KG")
            occs.append((need, occ))
    conn.close()

    outbox = _digest(pg_dsn, d, g)
    assert len(outbox) == 1 and {o["occurrence_id"] for o in outbox[0].payload["occurrences"]} == {str(o) for _n, o in occs}

    async def view(svc, session, phone):
        return await svc.list_my_recurring_needs(phone)

    items = {i["recurring_need_id"]: i for i in _svc_run(pg_dsn, g, view)["items"]}
    for need, occ in occs:
        assert items[str(need)]["in_latest_digest"] is True
        assert items[str(need)]["digest_occurrence_version"] == _occ_row(pg_dsn, occ)["version"]
