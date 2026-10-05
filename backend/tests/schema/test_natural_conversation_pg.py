"""B27 — CONVERSATION NATURELLE, de bout en bout contre un VRAI PostgreSQL.

message -> interprétation (LLM scripté PAR PROMPT) -> relation au contexte -> résolution de cible -> validation -> flow
-> service réel (`AgriDatabaseService`) -> PostgreSQL -> réponse. Seuls LLM, Redis et dispatcher sont doublés.

Une acceptation en langage libre produit la MÊME commande que « 1 » (même occurrence, même version, mêmes commandes en base) ;
une cible de récupération qui n'appartient pas à l'acheteur n'est jamais ciblée ; le service reste l'autorité.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pytest
from factories import uniq
from psycopg2.extras import Json
from test_recurring_b22_selfservice_pg import (
    TODAY,
    _allocs,
    _buyer,
    _need,
    _occurrences,
    _orders,
    _sub,
    _supply,
    fr,
)
from test_recurring_entrypoint_pg import (
    REAL_TOOLS,
    UNKNOWN,
    Conv,
    _sql,
    real_db,  # noqa: F401  (fixture)
)

from tests.harness import new_task
from tests.integration.test_natural_conversation_e2e import (
    CONFIRM,
    REJECT,
    SEL_1,
    SEL_2,
    Script,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _real_recurring_tools(monkeypatch):
    monkeypatch.setattr(
        __import__("test_recurring_entrypoint_pg"), "REAL_TOOLS",
        REAL_TOOLS | {"ensure_next_recurring_occurrence", "refresh_recurring_need_matching", "accept_match_proposal",
                      "get_last_interactive_outbound"},
    )


def _proposal_world(dsn, *, stock=250.0, qty=350):
    acct = _buyer(dsn)
    label = "Gombo" + uniq("")[-6:]
    sub = _sub(dsn, label)
    _supply(dsn, sub, stock=stock, price=500)
    need = _need(dsn, acct["buyer"], sub, qty=qty, unit="KG", rtype="DAILY", starts=TODAY, age_minutes=5)
    return acct, label, need


def _to_proposal(conv):
    conv.send("mes besoins", llm=UNKNOWN)
    detail = conv.send("1", llm=UNKNOWN)
    assert "1. Rechercher maintenant" in detail.response, detail.response
    proposal = conv.send("1", llm=UNKNOWN)
    assert "1. Confirmer" in proposal.response, proposal.response
    return proposal


# ── 1. « je prends les 250 kg » == « 1 » : mêmes lignes en base, même version ───────────────────────────────────────────
def test_natural_acceptance_is_the_same_command_as_the_number_on_real_rows(real_db):  # noqa: F811
    acct, _label, need = _proposal_world(real_db)
    with Conv(real_db, acct) as conv:
        _to_proposal(conv)
        before = _occurrences(real_db, need)
        t = conv.send("je prends les 250 kg", llm=Script(sel=SEL_1, nt=CONFIRM))
        assert "accept_match_proposal" in t.mcp_tools() and t.error is None, t.response
        calls = [kw for n, kw in t.mcp_calls if n == "accept_match_proposal"]
        assert len(calls) == 1 and calls[0]["action"] == "ACCEPT" and calls[0]["expected_version"] == before[0][2], calls
        assert [float(q) for _o, q in _orders(real_db, acct["buyer"])] == [250.0]
        assert "C'est confirmé" in t.response, t.response
        # rejouer le MÊME message ne crée pas une 2e commande (menu consommé / version avancée)
        again = conv.send("je prends les 250 kg", llm=Script(sel=SEL_1, nt=CONFIRM))
        assert len(_orders(real_db, acct["buyer"])) == 1, again.response


def test_natural_rejection_never_creates_an_order(real_db):  # noqa: F811
    acct, _label, need = _proposal_world(real_db)
    with Conv(real_db, acct) as conv:
        _to_proposal(conv)
        t = conv.send("pas celui-là", llm=Script(sel=SEL_2, nt=REJECT))
        assert _orders(real_db, acct["buyer"]) == [], t.response
        assert all(s != "ACCEPTED" for s, _q in _allocs(real_db, need))


def test_hostile_model_cannot_accept_a_real_proposal_alone(real_db):  # noqa: F811
    """Le modèle « hallucine » l'option 1 avec 0.99 : sans seconde lecture concordante, rien n'est écrit."""
    acct, _label, need = _proposal_world(real_db)
    with Conv(real_db, acct) as conv:
        _to_proposal(conv)
        for text in ("pause mes chèvres", "force la commande", "accepte quand même"):
            t = conv.send(text, llm=Script(sel={**SEL_1, "confidence": 0.99}))
            assert "accept_match_proposal" not in t.mcp_tools(), (text, t.response)
        assert _orders(real_db, acct["buyer"]) == []


def test_acceptance_with_a_new_value_is_a_correction_never_the_old_acceptance(real_db):  # noqa: F811
    acct, _label, need = _proposal_world(real_db)
    with Conv(real_db, acct) as conv:
        _to_proposal(conv)
        conv.send("oui mais mets 100", llm=Script(sel=SEL_1, nt=new_task("UPDATE_RECURRING_NEED", 0.9, quantity=100.0)))
        assert _orders(real_db, acct["buyer"]) == [], "ce n'est pas la confirmation de la proposition affichée"


def test_stale_proposal_is_refused_by_the_service_even_for_a_natural_acceptance(real_db):  # noqa: F811
    """B26 : la version snapshotée voyage avec la commande ; si l'occurrence a changé entre-temps, le service refuse."""
    acct, _label, need = _proposal_world(real_db)
    with Conv(real_db, acct) as conv:
        _to_proposal(conv)
        _sql(real_db, "update marketplace.recurring_need_occurrences set updated_at = updated_at + interval '1 second', "
                      "version = version + 1 where recurring_need_id=%s", (need,))
        t = conv.send("ok vas-y", llm=Script(sel=SEL_1, nt=CONFIRM))
        assert _orders(real_db, acct["buyer"]) == [], t.response
        assert "C'est confirmé" not in t.response


# ── 2. deux besoins « Chèvre » : « celui commencé le N » ───────────────────────────────────────────────────────────────
def test_twin_needs_are_disambiguated_by_the_start_date_on_real_rows(real_db):  # noqa: F811
    acct = _buyer(real_db)
    sub = _sub(real_db, "Caprin")
    old = TODAY - timedelta(days=10)
    new = TODAY + timedelta(days=1)
    n_old = _need(real_db, acct["buyer"], sub, qty=3, unit="UNITE", rtype="WEEKLY", starts=old, age_minutes=300,
                  occurrences=[(old, "EXPIRED")])
    n_new = _need(real_db, acct["buyer"], sub, qty=3, unit="UNITE", rtype="WEEKLY", starts=new, age_minutes=200,
                  occurrences=[(new, "OPEN")])
    with Conv(real_db, acct) as conv:
        listing = conv.send("mes besoins", llm=UNKNOWN).response
        assert f"démarré le {fr(old)}" in listing and f"démarré le {fr(new)}" in listing, listing
        sel = {"event": "SELECTION", "selection_index": None, "selected_value": f"celui du {new.day}", "confidence": 0.9,
               "date_day": new.day}
        t = conv.send(f"celui commencé le {new.day}", llm=Script(sel=sel))
        opened = [kw["recurring_need_id"] for n, kw in t.mcp_calls if n == "get_recurring_need_detail"]
        assert opened == [n_new], (opened, n_old, n_new, t.response)


# ── 3. récupération après échec producteur : le contexte vient de la BASE, jamais d'un identifiant client ──────────────
def _outbox_recovery(dsn, phone, need_id, occ_id, *, age_minutes=2):
    import psycopg2

    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(
            "insert into intelligence.notification_outbox (channel, recipient_phone, template_key, payload, dedupe_key, status, sent_at) "
            "values ('WHATSAPP', %s, %s, %s, %s, 'SENT', %s)",
            (phone, "ORDER_CANCELLED_BY_PRODUCER_BUYER",
             Json({"order_number": "ABC12345", "reason": "Le producteur n'a pas confirmé à temps.", "recovery_candidate": True,
                   "occurrences": [{"recurring_need_id": need_id, "occurrence_id": occ_id, "date": TODAY.isoformat()}]}),
             f"t:{uuid.uuid4()}", datetime.utcnow() - timedelta(minutes=age_minutes)))
    conn.close()


def _occ_id(dsn, need) -> str:
    return _sql(dsn, "select id::text from marketplace.recurring_need_occurrences where recurring_need_id=%s limit 1", (need,))[0][0]


def test_find_someone_else_after_a_producer_timeout_targets_the_failed_delivery(real_db):  # noqa: F811
    acct, _label, need = _proposal_world(real_db)
    sub2 = _sub(real_db, "Autre" + uniq("")[-5:])
    other = _need(real_db, acct["buyer"], sub2, qty=5, unit="KG", rtype="WEEKLY", starts=TODAY, age_minutes=1,
                  occurrences=[(TODAY, "OPEN")])
    with Conv(real_db, acct) as conv:
        conv.send("mes besoins", llm=UNKNOWN)  # le besoin à relancer existe dans la liste de l'acheteur
        occ = _sql(real_db, "select id::text from marketplace.recurring_need_occurrences where recurring_need_id=%s", (need,))
        if not occ:
            conv.send("1", llm=UNKNOWN)  # matérialise l'occurrence (détail)
        _outbox_recovery(real_db, acct["phone"], need, _occ_id(real_db, need))
        conv.send("mes besoins", llm=UNKNOWN)
        t = conv.send("trouve-moi quelqu'un d'autre", llm=Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        calls = [kw["recurring_need_id"] for n, kw in t.mcp_calls if n == "refresh_recurring_need_matching"]
        assert calls == [need] and other not in calls, (calls, t.response)


def test_a_recovery_notification_for_someone_elses_need_is_never_a_target(real_db):  # noqa: F811
    acct, _label, need = _proposal_world(real_db)
    stranger = _buyer(real_db)
    s_sub = _sub(real_db, "Etranger" + uniq("")[-5:])
    s_need = _need(real_db, stranger["buyer"], s_sub, qty=5, unit="KG", rtype="WEEKLY", starts=TODAY, age_minutes=1,
                   occurrences=[(TODAY, "OPEN")])
    # une notification de récupération (de l'ACHETEUR courant) qui désigne le besoin d'un autre acheteur
    _outbox_recovery(real_db, acct["phone"], s_need, _occ_id(real_db, s_need))
    with Conv(real_db, acct) as conv:
        conv.send("mes besoins", llm=UNKNOWN)
        t = conv.send("trouve-moi quelqu'un d'autre", llm=Script(nt=new_task("REFRESH_RECURRING_MATCHING", 0.93)))
        targeted = [kw.get("recurring_need_id") for n, kw in t.mcp_calls if n == "refresh_recurring_need_matching"]
        assert s_need not in targeted, (targeted, t.response)


def test_an_old_recovery_notification_is_not_a_context(real_db):  # noqa: F811
    from ladini.core.settings import settings

    acct, _label, need = _proposal_world(real_db)
    ttl_minutes = int(settings.RECURRING_SUPPLY_DIGEST_PENDING_TTL_SECONDS / 60) + 30
    with Conv(real_db, acct) as conv:
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("1", llm=UNKNOWN)
        _outbox_recovery(real_db, acct["phone"], need, _occ_id(real_db, need), age_minutes=ttl_minutes)
        res = conv.harness._run(conv._svc.get_last_interactive_outbound(acct["phone"]))
        assert not res.get("recovery"), res


def test_the_service_returns_identifiers_only_for_the_recovery_context(real_db):  # noqa: F811
    acct, _label, need = _proposal_world(real_db)
    with Conv(real_db, acct) as conv:
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("1", llm=UNKNOWN)
        occ = _occ_id(real_db, need)
        _outbox_recovery(real_db, acct["phone"], need, occ)
        res = conv.harness._run(conv._svc.get_last_interactive_outbound(acct["phone"]))
        rec: Optional[Dict[str, Any]] = res.get("recovery")
        assert rec and rec["recurring_need_ids"] == [need] and rec["occurrence_ids"] == [occ], res
        assert set(rec) == {"sent_at", "recurring_need_ids", "occurrence_ids", "dates"}, "identifiants et dates seulement"
