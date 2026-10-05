"""B28 — récupération de bout en bout : message naturel -> interprétation B27 -> contexte de récupération (lu en base) ->
version B26 -> `recover_occurrence_sourcing` -> matching réel -> PostgreSQL -> réponse.

PARCOURS RÉEL, aucune charge utile d'outbox écrite à la main : l'acheteur accepte la proposition par la conversation ; l'échec vient
du VRAI service de timeout producteur (ou d'un VRAI refus producteur) ; la notification d'échec est celle que ce service a mise en
file ; seul son passage à `SENT` (le travail de l'envoi WhatsApp) est simulé. Seuls LLM, Redis et dispatcher sont doublés.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Tuple

import psycopg2
import pytest
from factories import insert
from test_recurring_b22_selfservice_pg import TODAY, _buyer, _need, _sub, fr
from test_recurring_entrypoint_pg import (
    REAL_TOOLS,
    UNKNOWN,
    Conv,
    _sql,
    _user,
    real_db,  # noqa: F401  (fixture)
)

from tests.harness import new_task
from tests.integration.test_natural_conversation_e2e import SEL_1, Script

pytestmark = pytest.mark.integration

FIND_OTHER = Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93))


@pytest.fixture(autouse=True)
def _real_recurring_tools(monkeypatch):
    monkeypatch.setattr(
        __import__("test_recurring_entrypoint_pg"), "REAL_TOOLS",
        REAL_TOOLS | {"ensure_next_recurring_occurrence", "refresh_recurring_need_matching", "accept_match_proposal",
                      "get_last_interactive_outbound"},
    )


# ── monde ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _producer(dsn, sub, *, stock: float, price: float) -> Dict[str, Any]:
    acct = _user(dsn, role="PRODUCER", name="Producteur", producer=True)
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        acct["product"] = str(insert(cur, "marketplace.products", category_label="Légumes", price=price, unit="KG",
                                     producer_id=acct["producer"], sub_category_id=sub, quantity_for_sale=stock, is_available=True))
    conn.close()
    return acct


def _world(dsn, *, label="Oignon", second: bool = True, qty: int = 40) -> Dict[str, Any]:
    acct = _buyer(dsn)
    sub = _sub(dsn, label)
    a, b = _producer(dsn, sub, stock=200, price=500), _producer(dsn, sub, stock=200, price=700)
    days = [(TODAY, "OPEN")] + ([(TODAY + timedelta(days=7), "OPEN")] if second else [])
    need = _need(dsn, acct["buyer"], sub, qty=qty, unit="KG", rtype="DAILY", starts=TODAY, age_minutes=5, occurrences=days)
    return {"acct": acct, "sub": sub, "A": a, "B": b, "need": need, "label": label}


def _occ_rows(dsn, need) -> List[tuple]:
    return _sql(dsn, "select id::text, occurrence_date::date, status, version from marketplace.recurring_need_occurrences "
                     "where recurring_need_id=%s order by occurrence_date", (need,))


def _alloc_rows(dsn, occ) -> List[tuple]:
    return _sql(dsn, "select p.user_id::text, na.status from marketplace.need_allocations na "
                     "join marketplace.producers p on p.id = na.producer_id where na.occurrence_id=%s order by na.created_at", (occ,))


def _orders_of(dsn, buyer) -> List[tuple]:
    return _sql(dsn, "select o.id::text, o.status, o.cancellation_role from marketplace.orders o "
                     "where o.buyer_id=%s and o.order_type='RECURRING_SUPPLY' order by o.created_at", (buyer,))


def _accept_through_conversation(conv: Conv, w) -> None:
    """Le vrai parcours acheteur : liste -> détail -> « rechercher maintenant » -> proposition -> confirmer."""
    conv.send("mes besoins", llm=UNKNOWN)
    detail = conv.send("1", llm=UNKNOWN)
    assert "1. Rechercher maintenant" in detail.response, detail.response
    proposal = conv.send("1", llm=UNKNOWN)
    assert "1. Confirmer" in proposal.response, proposal.response
    done = conv.send("1", llm=UNKNOWN)
    assert "accept_match_proposal" in done.mcp_tools() and done.error is None, done.response
    assert [o[1] for o in _orders_of(conv.dsn, w["acct"]["buyer"])] == ["PENDING_PRODUCER_CONFIRMATION"]


def _expire(conv: Conv, monkeypatch, *, lead_days: int = 1) -> Dict[str, Any]:
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS", lead_days)
    return conv.harness._run(conv._svc.expire_unconfirmed_recurring_orders())


def _deliver_notifications(dsn, phone) -> List[Dict[str, Any]]:
    """Le travail de l'envoi (hors périmètre) : les notifications déjà MISES EN FILE par les services d'échec passent à SENT."""
    rows = _sql(dsn, "update intelligence.notification_outbox set status='SENT', sent_at = now() at time zone 'UTC' "
                     "where recipient_phone=%s and template_key='ORDER_CANCELLED_BY_PRODUCER_BUYER' returning payload", (phone,))
    return [r[0] for r in rows]


def _refresh_calls(turn) -> List[Dict[str, Any]]:
    return [kw for n, kw in turn.mcp_calls if n == "refresh_recurring_need_matching"]


def _failed_by_timeout(conv: Conv, w, monkeypatch) -> Tuple[Dict[str, Any], str]:
    _accept_through_conversation(conv, w)
    occ_id = _occ_rows(conv.dsn, w["need"])[0][0]
    assert _expire(conv, monkeypatch)["recurring_orders_expired"] >= 1
    payloads = _deliver_notifications(conv.dsn, w["acct"]["phone"])
    assert len(payloads) == 1
    return payloads[0], occ_id


# ═══ 1. LE CAS RÉEL : timeout producteur -> « cherche quelqu'un d'autre » -> LA MÊME occurrence re-matchée ═══════════════════
def test_full_producer_timeout_then_natural_find_someone_else_rematches_the_exact_occurrence(real_db, monkeypatch):  # noqa: F811
    w = _world(real_db)
    with Conv(real_db, w["acct"]) as conv:
        payload, occ_id = _failed_by_timeout(conv, w, monkeypatch)
        before = _occ_rows(real_db, w["need"])
        failed, nxt = before[0], before[1]
        assert failed[2] == "OPEN"  # l'échec de la tentative n'a pas tué l'occurrence
        assert payload["recovery_candidate"] is True and payload["recovery_outcome"] == "RECOVERABLE"
        assert payload["occurrences"][0] == {"recurring_need_id": w["need"], "occurrence_id": occ_id, "date": TODAY.isoformat(),
                                             "occurrence_version": failed[3]}

        t = conv.send("cherche quelqu'un d'autre", llm=FIND_OTHER)
        calls = _refresh_calls(t)
        assert len(calls) == 1 and t.error is None, t.response
        assert calls[0]["recurring_need_id"] == w["need"] and calls[0]["occurrence_id"] == occ_id  # EXACTEMENT la livraison en échec
        assert calls[0]["expected_occurrence_version"] == failed[3]  # la version de la notification (B26), jamais relue
        assert "relancée" in t.response, t.response

        after = _occ_rows(real_db, w["need"])
        assert after[0][0] == occ_id and after[0][2] == "MATCHED" and after[0][3] > failed[3]
        assert after[1] == nxt  # la livraison SUIVANTE n'est pas touchée
        assert _alloc_rows(real_db, occ_id) == [(w["A"]["user"], "CONVERTED"), (w["B"]["user"], "PROPOSED")]  # A écarté, B proposé
        assert _alloc_rows(real_db, nxt[0]) == []
        assert [(o[1], o[2]) for o in _orders_of(real_db, w["acct"]["buyer"])] == [("CANCELLED", "SYSTEM")]  # historique intact
        assert _sql(real_db, "select status from marketplace.recurring_needs where id=%s", (w["need"],))[0][0] == "ACTIVE"

        # la nouvelle proposition est confirmée par l'acheteur (jamais automatiquement) : une NOUVELLE commande, même occurrence
        confirm = conv.send("1", llm=UNKNOWN)
        assert "accept_match_proposal" in confirm.mcp_tools() and confirm.error is None, confirm.response
        assert sorted(o[1] for o in _orders_of(real_db, w["acct"]["buyer"])) == ["CANCELLED", "PENDING_PRODUCER_CONFIRMATION"]
        assert _occ_rows(real_db, w["need"])[0][2] == "ACCEPTED"


@pytest.mark.parametrize("phrase", ["réessaie", "trouve un autre", "je veux quelqu'un d'autre", "relance", "pas celui-là, cherche un autre producteur"])
def test_every_natural_phrasing_converges_to_the_same_domain_command(real_db, monkeypatch, phrase):  # noqa: F811
    """Le modèle comprend (scripté par prompt) ; le code ne connaît aucune phrase : même commande métier, même cible."""
    w = _world(real_db)
    with Conv(real_db, w["acct"]) as conv:
        _payload, occ_id = _failed_by_timeout(conv, w, monkeypatch)
        version = _occ_rows(real_db, w["need"])[0][3]
        t = conv.send(phrase, llm=FIND_OTHER)
        calls = _refresh_calls(t)
        assert [(c["recurring_need_id"], c["occurrence_id"], c["expected_occurrence_version"]) for c in calls] == [(w["need"], occ_id, version)]
        assert [a[1] for a in _alloc_rows(real_db, occ_id)] == ["CONVERTED", "PROPOSED"]


# ═══ 2. REFUS EXPLICITE DU PRODUCTEUR (pas un timeout) ═══════════════════════════════════════════════════════════════════
def test_full_explicit_producer_rejection_then_find_another(real_db):  # noqa: F811
    w = _world(real_db)
    with Conv(real_db, w["acct"]) as conv:
        _accept_through_conversation(conv, w)
        occ_id = _occ_rows(real_db, w["need"])[0][0]
        order_id = _orders_of(real_db, w["acct"]["buyer"])[0][0]
        res = conv.harness._run(conv._svc.cancel_confirmed_order(w["A"]["phone"], order_id, "je ne peux pas livrer"))
        assert res["outcome"] == "CANCELLED"
        payloads = _deliver_notifications(real_db, w["acct"]["phone"])
        assert payloads[0]["failure_reason"] == "PRODUCER_REJECTED" and payloads[0]["recovery_outcome"] == "RECOVERABLE"
        version = _occ_rows(real_db, w["need"])[0][3]
        t = conv.send("trouve un autre", llm=FIND_OTHER)
        calls = _refresh_calls(t)
        assert [(c["occurrence_id"], c["expected_occurrence_version"]) for c in calls] == [(occ_id, version)], t.response
        assert [a[1] for a in _alloc_rows(real_db, occ_id)] == ["CONVERTED", "PROPOSED"]
        assert _occ_rows(real_db, w["need"])[1][2] == "OPEN" and _alloc_rows(real_db, _occ_rows(real_db, w["need"])[1][0]) == []


# ═══ 3. FENÊTRE FERMÉE : réponse honnête, le besoin continue ═════════════════════════════════════════════════════════════
def test_a_timeout_after_the_delivery_date_is_answered_honestly_and_the_need_continues(real_db, monkeypatch):  # noqa: F811
    w = _world(real_db)
    with Conv(real_db, w["acct"]) as conv:
        _accept_through_conversation(conv, w)
        occ_id = _occ_rows(real_db, w["need"])[0][0]
        yesterday = TODAY - timedelta(days=1)
        _sql(real_db, "update marketplace.orders set expected_fulfillment_date = %s where buyer_id = %s", (yesterday, w["acct"]["buyer"]))
        _sql(real_db, "update marketplace.recurring_need_occurrences set occurrence_date = %s where id = %s", (yesterday, occ_id))
        _expire(conv, monkeypatch, lead_days=0)
        payload = _deliver_notifications(real_db, w["acct"]["phone"])[0]
        assert payload["recovery_outcome"] == "RECOVERY_WINDOW_CLOSED"
        t = conv.send("cherche quelqu'un d'autre", llm=FIND_OTHER)
        assert _refresh_calls(t)[0]["occurrence_id"] == occ_id
        assert "ne peut plus être relancée à temps" in t.response and "reste actif" in t.response, t.response
        assert _occ_rows(real_db, w["need"])[0][2] == "UNFULFILLED"
        assert [a[1] for a in _alloc_rows(real_db, occ_id)] == ["CONVERTED"]  # aucune nouvelle recherche
        assert _sql(real_db, "select status from marketplace.recurring_needs where id=%s", (w["need"],))[0][0] == "ACTIVE"
        assert _occ_rows(real_db, w["need"])[1][2] == "OPEN"  # les livraisons futures continuent


# ═══ 4. PAUSE PENDANT LA RÉCUPÉRATION ════════════════════════════════════════════════════════════════════════════════════
def test_a_paused_need_is_never_rematched(real_db, monkeypatch):  # noqa: F811
    w = _world(real_db)
    with Conv(real_db, w["acct"]) as conv:
        _failed_by_timeout(conv, w, monkeypatch)
        listing = conv.harness._run(conv._svc.list_my_recurring_needs(w["acct"]["phone"]))
        version = listing["items"][0]["need_version"]
        conv.harness._run(conv._svc.update_recurring_need(
            w["acct"]["phone"], w["need"], "PAUSE", paused_until=(TODAY + timedelta(days=30)).isoformat(), expected_version=version))
        t = conv.send("cherche quelqu'un d'autre", llm=FIND_OTHER)
        assert [a[1] for a in _alloc_rows(real_db, _occ_rows(real_db, w["need"])[0][0])] == ["CONVERTED"], t.response
        assert not any(a[1] == "PROPOSED" for rows in (_alloc_rows(real_db, o[0]) for o in _occ_rows(real_db, w["need"])) for a in rows)


# ═══ 5. COMMANDE PÉRIMÉE : le cron est passé avant l'utilisateur ═════════════════════════════════════════════════════════
def test_a_recovery_command_after_the_cron_already_rematched_changes_nothing(real_db, monkeypatch):  # noqa: F811
    import asyncio

    from test_recurring_fulfillment_pg import _engine

    from ladini.workers.automation.need_matching_service import NeedMatchingService

    w = _world(real_db)
    with Conv(real_db, w["acct"]) as conv:
        _failed_by_timeout(conv, w, monkeypatch)
        occ_id = _occ_rows(real_db, w["need"])[0][0]

        async def cron():
            from sqlalchemy.ext.asyncio import AsyncSession

            engine = _engine(real_db)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    await NeedMatchingService(session).match_upcoming_occurrences(within_hours=72, trigger="scheduled")
            finally:
                await engine.dispose()

        asyncio.run(cron())  # le cron a déjà re-matché la livraison rouverte (même moteur, même exclusion)
        rows_after_cron = _alloc_rows(real_db, occ_id)
        assert [a[1] for a in rows_after_cron] == ["CONVERTED", "PROPOSED"]
        t = conv.send("cherche quelqu'un d'autre", llm=FIND_OTHER)  # le contexte porte l'ancienne version
        assert "déjà été relancée" in t.response, t.response
        assert _alloc_rows(real_db, occ_id) == rows_after_cron  # un seul effet : aucune allocation dupliquée
        assert len(_orders_of(real_db, w["acct"]["buyer"])) == 1


# ═══ 6. DEUX ÉCHECS SIMULTANÉS : clarification ciblée, puis relance exacte ═══════════════════════════════════════════════
def test_two_simultaneous_failures_are_clarified_then_recovered_exactly(real_db, monkeypatch):  # noqa: F811
    w1 = _world(real_db, label="Oignon")
    acct = w1["acct"]
    sub2 = _sub(real_db, "Tomate")
    _producer(real_db, sub2, stock=200, price=500)
    _producer(real_db, sub2, stock=200, price=700)
    need2 = _need(real_db, acct["buyer"], sub2, qty=20, unit="KG", rtype="DAILY", starts=TODAY, age_minutes=3, occurrences=[(TODAY, "OPEN")])
    with Conv(real_db, acct) as conv:
        # les deux livraisons sont acceptées puis échouent ensemble (acceptation par le service : le parcours conversationnel est prouvé ailleurs)
        for need in (w1["need"], need2):
            conv.harness._run(conv._svc.refresh_recurring_need_matching(acct["phone"], need))
            occ = [o for o in _occ_rows(real_db, need) if o[1] == TODAY][0]
            res = conv.harness._run(conv._svc.accept_match_proposal(acct["phone"], need, "ACCEPT", occurrence_id=occ[0], expected_version=occ[3]))
            assert len(res["order_ids"]) == 1
        assert _expire(conv, monkeypatch)["recurring_orders_expired"] >= 2
        payloads = _deliver_notifications(real_db, acct["phone"])
        assert len(payloads) == 2 and len({p["occurrences"][0]["occurrence_id"] for p in payloads}) == 2  # deux contextes distincts
        orders = _orders_of(real_db, acct["buyer"])
        assert len(orders) == 2 and {o[1] for o in orders} == {"CANCELLED"}

        t = conv.send("cherche quelqu'un d'autre", llm=FIND_OTHER)
        assert not _refresh_calls(t), "ambigu : jamais de relance sur une devinette"
        assert "Plusieurs livraisons viennent d'échouer" in t.response, t.response
        assert "Oignon" in t.response and "Tomate" in t.response

        pick = conv.send("la première", llm=Script(sel=SEL_1))
        assert "Oignon" in pick.response or "Tomate" in pick.response, pick.response
        opened_label = "Oignon" if "Oignon" in pick.response else "Tomate"
        need_opened, occ_ids = (w1["need"], _occ_rows(real_db, w1["need"])) if opened_label == "Oignon" else (need2, _occ_rows(real_db, need2))
        assert "1. Rechercher maintenant" in pick.response, pick.response
        go = conv.send("1", llm=UNKNOWN)  # le bouton de L'ÉCRAN affiché : cible EXACTE (occurrence + version affichées)
        calls = _refresh_calls(go)
        assert len(calls) == 1 and calls[0]["recurring_need_id"] == need_opened and calls[0]["occurrence_id"] == occ_ids[0][0], go.response
        assert _alloc_rows(real_db, occ_ids[0][0])[-1][1] == "PROPOSED"
        other_need = need2 if need_opened == w1["need"] else w1["need"]
        other_occ = _occ_rows(real_db, other_need)[0][0]
        assert all(a[1] == "CONVERTED" for a in _alloc_rows(real_db, other_occ))  # l'autre échec n'a pas été touché ni écrasé


# ═══ 7. LISTE / DÉTAIL : la même livraison, occurrence-scoped (cas réel « Oignon ») ════════════════════════════════════════
def test_the_real_onion_case_list_and_detail_name_the_same_delivery(real_db):  # noqa: F811
    acct = _buyer(real_db)
    sub = _sub(real_db, "Oignon")
    _producer(real_db, sub, stock=100, price=500)
    far = TODAY + timedelta(days=22)
    need = _need(real_db, acct["buyer"], sub, qty=75, unit="KG", rtype="MONTHLY", starts=far, age_minutes=5, occurrences=[(far, "OPEN")])
    conv_occ = _sql(real_db, "select id::text from marketplace.recurring_need_occurrences where recurring_need_id=%s", (need,))[0][0]
    with Conv(real_db, acct) as conv:
        conv.harness._run(conv._svc.refresh_recurring_need_matching(acct["phone"], need))
        listing = conv.send("mes besoins", llm=UNKNOWN).response
        assert fr(far) in listing and "75" in listing, listing
        detail = conv.send("1", llm=UNKNOWN)
        assert fr(far) in detail.response, detail.response  # le détail parle de la MÊME livraison que la liste
        assert "mismatch" not in detail.response.lower() and "La liste annonçait" not in detail.response
        got = [kw for n, kw in detail.mcp_calls if n == "get_recurring_need_detail"]
        assert got and conv.harness._run(conv._svc.get_recurring_need_detail(acct["phone"], need))["occurrence_id"] == conv_occ
