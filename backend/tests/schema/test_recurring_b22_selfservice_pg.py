"""B22 — tout le sous-système self-service recurring, sur le VRAI graphe et un VRAI PostgreSQL :

création -> « mes besoins » -> sélection -> prochaine livraison -> « rechercher maintenant » -> proposition -> accepter ->
commandes -> « mes besoins » (accepté), SANS cron ni digest ; puis cron/digest compatibles (ni 2e allocation, ni 2e commande).
Rien n'est doublé côté conversation ni côté données (voir `test_recurring_entrypoint_pg.Conv`).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import psycopg2
import pytest
from factories import insert, uniq
from test_recurring_entrypoint_pg import (
    REAL_TOOLS,
    UNKNOWN,
    Conv,
    _needs_of,
    _sql,
    _user,
)
from test_recurring_entrypoint_pg import (
    real_db as entrypoint_real_db,  # noqa: F401  (fixture, importée sous alias)
)

from tests.harness import new_task

pytestmark = pytest.mark.integration


@pytest.fixture()
def real_db(entrypoint_real_db):  # noqa: F811
    """PostgreSQL de test branché sur `core.database` (fixture du test d'entrée B21.1)."""
    return entrypoint_real_db

REAL_TOOLS.update({"ensure_next_recurring_occurrence", "refresh_recurring_need_matching", "accept_match_proposal"})

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def fr(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def one_month_later(d: date) -> date:
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


TODAY = datetime.now().date()


def _buyer(dsn) -> Dict[str, Any]:
    return _user(dsn, role="ADMIN", name="Admin Ido", buyer=True)


def _sub(dsn, name: str) -> str:
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cat = insert(cur, "governance.categories", name=uniq("cat"))
        sub = insert(cur, "governance.sub_categories", category_id=cat, name=name)
    conn.close()
    return str(sub)


def _need(dsn, buyer_id, sub, *, qty, unit, rtype, starts: date, age_minutes: int, occurrences=(), status="ACTIVE") -> str:
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        need = insert(cur, "marketplace.recurring_needs", buyer_id=buyer_id, sub_category_id=sub, quantity=qty, unit=unit,
                      recurrence_type=rtype, starts_at=_dt(starts), status=status,
                      created_at=datetime.utcnow() - timedelta(minutes=age_minutes))
        for d, st in occurrences:
            insert(cur, "marketplace.recurring_need_occurrences", recurring_need_id=need, occurrence_date=_dt(d),
                   requested_quantity=qty, unit=unit, status=st)
    conn.close()
    return str(need)


def _supply(dsn, sub, *, stock: float, price: float = 500, unit: str = "KG") -> str:
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        user = insert(cur, "auth.users", phone=uniq("+226"))
        producer = insert(cur, "marketplace.producers", user_id=user, business_name=uniq("Ferme"))
        product = insert(cur, "marketplace.products", category_label="Légumes", price=price, unit=unit, producer_id=producer,
                         sub_category_id=sub, quantity_for_sale=stock, is_available=True)
    conn.close()
    return str(product)


def _occurrences(dsn, need) -> List[tuple]:
    return _sql(dsn, "select occurrence_date::date, status, version, quantity_matched, notified_at from "
                     "marketplace.recurring_need_occurrences where recurring_need_id=%s order by occurrence_date", (need,))


def _allocs(dsn, need) -> List[tuple]:
    return _sql(dsn, "select na.status, na.quantity from marketplace.need_allocations na join marketplace.recurring_need_occurrences o "
                     "on o.id = na.occurrence_id where o.recurring_need_id=%s order by na.created_at, na.id", (need,))


def _orders(dsn, buyer_id) -> List[tuple]:
    return _sql(dsn, "select o.id, sum(oi.quantity) from marketplace.orders o join marketplace.order_items oi on oi.order_id=o.id "
                     "where o.buyer_id=%s and o.order_type='RECURRING_SUPPLY' group by o.id", (buyer_id,))


# ═══════════════════════════ le smoke prod exact ═════════════════════════════════════════════════════════════════════
def _prod_world(dsn):
    acct = _buyer(dsn)
    b = acct["buyer"]
    subs = {n: _sub(dsn, n) for n in ("Tomate", "Oignon", "Caprin", "Boeufs")}
    ids = {
        "tomate": _need(dsn, b, subs["Tomate"], qty=350, unit="KG", rtype="WEEKLY", starts=TODAY - timedelta(days=7), age_minutes=500,
                        occurrences=[(TODAY, "OPEN")]),
        # créé il y a 2 jours (début la veille de ce jour-là) : l'occurrence est jouée, la suivante est dans un mois
        "oignon": _need(dsn, b, subs["Oignon"], qty=75, unit="KG", rtype="MONTHLY", starts=TODAY - timedelta(days=2), age_minutes=400,
                        occurrences=[(TODAY - timedelta(days=2), "EXPIRED")]),
        "chevre_old": _need(dsn, b, subs["Caprin"], qty=3, unit="UNITE", rtype="WEEKLY", starts=TODAY - timedelta(days=3), age_minutes=300,
                            occurrences=[(TODAY - timedelta(days=3), "EXPIRED")]),
        "chevre_new": _need(dsn, b, subs["Caprin"], qty=3, unit="UNITE", rtype="WEEKLY", starts=TODAY + timedelta(days=1), age_minutes=200,
                            occurrences=[(TODAY + timedelta(days=1), "OPEN")]),
    }
    return acct, subs, ids


def test_exact_prod_scenario_list_is_correct_stable_and_unambiguous(real_db):
    acct, subs, ids = _prod_world(real_db)
    with Conv(real_db, acct) as conv:
        conv.send("boeufs 2 tete chaque semaine", llm=new_task("CREATE_RECURRING_NEED", product="Boeufs", quantity=2.0, unit="TETE",
                                                              recurrence_type="WEEKLY"))
        done = conv.send("Confirmer")
        assert "C'est noté" in done.response
        t = conv.send("mes besoins", llm=UNKNOWN)
        r = t.response
        assert t.llm_calls == 0 and "list_my_recurring_needs" in t.mcp_tools()
        blocks = [b for b in r.split("\n\n") if b[:2] in {"1.", "2.", "3.", "4.", "5."}]
        names = [b.split("\n")[0] for b in blocks]
        assert names == ["1. Tomate", "2. Oignon", "3. Caprin", "4. Caprin", "5. Boeufs"], r  # ordre = création (stable)
        assert "350 KG chaque semaine" in r and "75 KG chaque mois" in r and "3 UNITE chaque semaine" in r and "2 TETE chaque semaine" in r
        assert r.count("🟢 Actif") == 5
        assert "Livraison prévue aujourd'hui" in blocks[0]                                  # Tomate : l'occurrence du jour est encore OPEN
        assert f"Prochaine livraison : {fr(one_month_later(TODAY - timedelta(days=2)))}" in blocks[1]  # Oignon mensuel calculé
        assert "à planifier" not in r and "Planning incomplet" not in r
        assert f"Prochaine livraison : {fr(TODAY + timedelta(days=4))}" in blocks[2]        # Caprin #3 : -3 j + 7
        assert f"démarré le {fr(TODAY - timedelta(days=3))}" in blocks[2] and f"démarré le {fr(TODAY + timedelta(days=1))}" in blocks[3]
        assert f"Prochaine livraison : {fr(TODAY + timedelta(days=1))}" in blocks[3] and "démarré" not in blocks[0] + blocks[1] + blocks[4]
        # aucun besoin dupliqué artificiellement : 4 semés + 1 créé
        assert len(_needs_of(real_db, acct["phone"])) == 5
        assert len(_sql(real_db, "select 1 from marketplace.recurring_needs where buyer_id=%s", (acct["buyer"],))) == 5


def test_the_list_is_identical_in_a_fresh_conversation(real_db):
    acct, _subs, _ids = _prod_world(real_db)
    with Conv(real_db, acct) as first:
        a = first.send("mes besoins", llm=UNKNOWN).response
    with Conv(real_db, acct) as fresh:
        assert fresh.state() == {}
        assert fresh.send("mes besoins", llm=UNKNOWN).response == a


# ═══════════════════════════ menu : snapshot, stabilité, sélection ═══════════════════════════════════════════════════
def test_menu_number_targets_the_displayed_need_even_if_the_database_changes_before_the_reply(real_db):
    acct, subs, ids = _prod_world(real_db)
    boeuf = _need(real_db, acct["buyer"], subs["Boeufs"], qty=2, unit="TETE", rtype="WEEKLY", starts=TODAY + timedelta(days=1), age_minutes=10)
    with Conv(real_db, acct) as conv:
        listing = conv.send("mes besoins", llm=UNKNOWN).response
        assert "5. Boeufs" in listing
        # un 6e besoin, créé AVANT tous les autres, décalerait toute la numérotation si la liste était relue
        _need(real_db, acct["buyer"], subs["Tomate"], qty=1, unit="KG", rtype="DAILY", starts=TODAY, age_minutes=100000)
        calls_before = conv.runtime.calls.count
        t = conv.send("5", llm=UNKNOWN)
        assert t.error is None and t.llm_calls == 0
        assert "Boeufs" in t.response and "Besoin : 2 TETE" in t.response, t.response
        assert "list_my_recurring_needs" not in t.mcp_tools(), "aucune relecture implicite de la liste avant la sélection"
        assert "get_recurring_need_detail" in t.mcp_tools()
        assert calls_before is not None
        assert boeuf in str(t.mcp_calls)


def test_a_stale_menu_is_refused_cleanly_with_a_fresh_list_never_a_wrong_need(real_db, monkeypatch):
    from ladini.graphs.agents.market_coach.flows.buyer import recurring_need as flow

    acct, subs, ids = _prod_world(real_db)
    with Conv(real_db, acct) as conv:
        conv.send("mes besoins", llm=UNKNOWN)
        monkeypatch.setattr(flow, "_MENU_TTL_SECONDS", -1.0)
        t = conv.send("2", llm=UNKNOWN)
        assert t.error is None
        assert "Mes besoins récurrents" in t.response or "Je n'ai pas" in t.response or "précis" in t.response.lower()
        assert "Oignon" not in t.response.split("Mes besoins récurrents")[0]


def test_select_five_opens_the_boeuf_detail_with_the_next_delivery_and_a_search_action(real_db):
    acct, subs, ids = _prod_world(real_db)
    with Conv(real_db, acct) as conv:
        conv.send("boeufs 2 tete chaque semaine", llm=new_task("CREATE_RECURRING_NEED", product="Boeufs", quantity=2.0, unit="TETE",
                                                              recurrence_type="WEEKLY"))
        conv.send("Confirmer")
        conv.send("mes besoins", llm=UNKNOWN)
        t = conv.send("5", llm=UNKNOWN)
        r = t.response
        assert t.llm_calls == 0 and "get_orders" not in str(t.mcp_tools())
        assert r.startswith("📦 Boeufs") or "📦 Boeufs" in r
        assert "Besoin : 2 TETE · chaque semaine" in r and "Statut : 🟢 Actif" in r
        assert f"Prochaine livraison : {fr(TODAY + timedelta(days=4))}" in r  # début = aujourd'hui + délai minimal (4)
        assert "Disponibilité : aucune offre disponible pour le moment" in r
        assert "1. Rechercher maintenant" in r and "2. Retour" in r and "Accepter" not in r


def test_detail_without_any_occurrence_materializes_it_on_demand(real_db):
    acct = _buyer(real_db)
    sub = _sub(real_db, "Oignon")
    need = _need(real_db, acct["buyer"], sub, qty=75, unit="KG", rtype="MONTHLY", starts=TODAY - timedelta(days=2), age_minutes=5,
                 occurrences=[(TODAY - timedelta(days=2), "EXPIRED")])
    with Conv(real_db, acct) as conv:
        conv.send("mes besoins", llm=UNKNOWN)
        t = conv.send("1", llm=UNKNOWN)
        assert "ensure_next_recurring_occurrence" in t.mcp_tools()
        assert f"Prochaine livraison : {fr(one_month_later(TODAY - timedelta(days=2)))}" in t.response
        assert "Aucune prochaine livraison" not in t.response and "1. Rechercher maintenant" in t.response
        assert [(d, s) for d, s, *_ in _occurrences(real_db, need)][-1] == (one_month_later(TODAY - timedelta(days=2)), "OPEN")


def test_paused_and_cancelled_needs_are_not_listed_as_active_and_never_materialize(real_db):
    acct = _buyer(real_db)
    sub = _sub(real_db, "Mil")
    paused = _need(real_db, acct["buyer"], sub, qty=10, unit="KG", rtype="WEEKLY", starts=TODAY - timedelta(days=3), age_minutes=30, status="PAUSED")
    cancelled = _need(real_db, acct["buyer"], sub, qty=11, unit="KG", rtype="WEEKLY", starts=TODAY - timedelta(days=3), age_minutes=20, status="CANCELLED")
    with Conv(real_db, acct) as conv:
        t = conv.send("mes besoins", llm=UNKNOWN)
        assert "⏸ Suspendu" in t.response and "🟢 Actif" not in t.response and "11 KG" not in t.response
        assert "Prochaine livraison" not in t.response and "Livraison prévue" not in t.response
        d = conv.send("1", llm=UNKNOWN)
        assert "suspendu" in d.response.lower() and "Rechercher maintenant" not in d.response
    assert _occurrences(real_db, paused) == [] and _occurrences(real_db, cancelled) == []


def test_ownership_a_buyer_only_sees_his_own_needs(real_db):
    mine, other = _buyer(real_db), _buyer(real_db)
    sub = _sub(real_db, "Mil")
    _need(real_db, mine["buyer"], sub, qty=10, unit="KG", rtype="WEEKLY", starts=TODAY, age_minutes=9)
    theirs = _need(real_db, other["buyer"], sub, qty=999, unit="KG", rtype="WEEKLY", starts=TODAY, age_minutes=9)
    with Conv(real_db, mine) as conv:
        r = conv.send("mes besoins", llm=UNKNOWN).response
        assert "10 KG" in r and "999" not in r
    assert _occurrences(real_db, theirs) == []


# ═══════════════════════════ self-service complet, cron compatible ═══════════════════════════════════════════════════
def test_full_self_service_scenario_then_cron_and_digest_are_compatible(real_db):
    from test_recurring_e2e_pg import _run

    from ladini.workers.automation.need_matching_service import NeedMatchingService
    from ladini.workers.automation.recurring_supply_digest_service import (
        RecurringSupplyDigestService,
    )

    acct = _buyer(real_db)
    label = "Gombo" + uniq("")[-6:]  # la base de test est partagée : un libellé unique résout vers CETTE sous-catégorie
    sub = _sub(real_db, label)
    _supply(real_db, sub, stock=250, price=500)
    with Conv(real_db, acct) as conv:
        conv.send(f"{label} 350 kg chaque jour", llm=new_task("CREATE_RECURRING_NEED", product=label, quantity=350.0, unit="KG",
                                                             recurrence_type="DAILY"))
        assert "C'est noté" in conv.send("Confirmer").response
        need = _sql(real_db, "select id::text from marketplace.recurring_needs where buyer_id=%s", (acct["buyer"],))[0][0]
        assert conv.send("mes besoins", llm=UNKNOWN).response.count(label) == 1
        detail = conv.send("1", llm=UNKNOWN)
        assert "1. Rechercher maintenant" in detail.response and _allocs(real_db, need) == []
        proposal = conv.send("1", llm=UNKNOWN)                                   # « Rechercher maintenant » (moteur partagé)
        assert "refresh_recurring_need_matching" in proposal.mcp_tools()
        assert "250 KG disponibles sur 350 KG demandés" in proposal.response, proposal.response
        assert "1. Confirmer" in proposal.response
        assert [s for s, _q in _allocs(real_db, need)] == ["PROPOSED"]
        occ_before = _occurrences(real_db, need)
        accepted = conv.send("1", llm=UNKNOWN)                                   # accepter : occurrence_id + expected_version
        assert "accept_match_proposal" in accepted.mcp_tools(), accepted.response
        assert accepted.error is None
        orders = _orders(real_db, acct["buyer"])
        assert [float(q) for _o, q in orders] == [250.0]
        assert all(o[4] is None for o in _occurrences(real_db, need)), "notified_at jamais posé : aucun digest n'a existé"
        assert occ_before[0][4] is None
        listing = conv.send("mes besoins", llm=UNKNOWN).response
        assert "partiellement accepté" in listing or "Approvisionnement accepté" in listing, listing
        orders_before, allocs_before = len(orders), _allocs(real_db, need)

        # cron + digest APRÈS l'action manuelle : ni 2e allocation, ni 2e commande, ni nouvelle proposition actionnable
        target = _occurrences(real_db, need)[0][0]
        occ_id = _sql(real_db, "select id::text from marketplace.recurring_need_occurrences where recurring_need_id=%s "
                               "order by occurrence_date limit 1", (need,))[0][0]
        outbox_before = _sql(real_db, "select count(*) from intelligence.notification_outbox where payload::text like %s",
                             (f"%{occ_id}%",))[0][0]

        async def cron(session):
            await NeedMatchingService(session).match_upcoming_occurrences(within_hours=72, trigger="b22")
            await RecurringSupplyDigestService(session).run(target_date=target, trigger="b22")

        _run(real_db, cron)
        assert len(_orders(real_db, acct["buyer"])) == orders_before
        assert _allocs(real_db, need) == allocs_before, "pas de 2e allocation : l'occurrence acceptée n'est plus rematchée"
        assert _sql(real_db, "select count(*) from intelligence.notification_outbox where payload::text like %s",
                    (f"%{occ_id}%",))[0][0] == outbox_before, "aucune nouvelle proposition actionnable pour l'occurrence acceptée"
        accepted_occ = [o for o in _occurrences(real_db, need) if o[1] in ("ACCEPTED", "PARTIALLY_ACCEPTED")]
        assert len(accepted_occ) == 1
        again = conv.send("mes besoins", llm=UNKNOWN).response
        assert again == listing or "accepté" in again


def test_cron_matching_before_the_buyer_opens_the_need_is_visible_immediately(real_db):
    from test_recurring_e2e_pg import _run

    from ladini.workers.automation.need_matching_service import NeedMatchingService

    acct = _buyer(real_db)
    sub = _sub(real_db, "Tomate")
    _supply(real_db, sub, stock=400, price=500)
    need = _need(real_db, acct["buyer"], sub, qty=350, unit="KG", rtype="DAILY", starts=TODAY, age_minutes=5, occurrences=[(TODAY, "OPEN")])
    occ_id = _sql(real_db, "select id::text from marketplace.recurring_need_occurrences where recurring_need_id=%s", (need,))[0][0]

    async def match(session):
        return await NeedMatchingService(session).rematch_occurrence(occ_id, trigger="cron")

    _run(real_db, match)
    v_cron = _occurrences(real_db, need)[0]
    with Conv(real_db, acct) as conv:
        r = conv.send("mes besoins", llm=UNKNOWN).response
        assert "Disponibilité actuelle : 350 / 350 KG" in r, r
        detail = conv.send("1", llm=UNKNOWN).response
        assert "✅ Disponibilité complète : 350 KG" in detail and "1. Confirmer" in detail
        refreshed = conv.send("actualiser", llm=UNKNOWN)
        assert refreshed.error is None
    assert _occurrences(real_db, need)[0][2] == v_cron[2], "contenu identique : la version n'est pas bumpée"
