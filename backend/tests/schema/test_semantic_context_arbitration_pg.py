"""B24 — arbitrage sémantique sur le VRAI graphe + un VRAI PostgreSQL.

Seuls LLM (scripté), Redis et dispatcher sont doublés ; les outils récurrents (liste, détail, mise à jour, relance) passent par
le vrai `AgriDatabaseService`. Un besoin Bœuf réel est créé puis consulté ; on vérifie ce qui est RÉELLEMENT écrit en base.
"""
from __future__ import annotations

import pytest

from tests.harness import new_task
from tests.integration.test_semantic_context_arbitration_e2e import (
    AMBIGUOUS_CHANGE,
    UPDATE_MONTHLY,
    UPDATE_QTY_3,
    ScriptLLM,
)
from tests.schema import test_recurring_entrypoint_pg as entry
from tests.schema.test_recurring_entrypoint_pg import Conv, _needs_of, _user, real_db  # noqa: F401

pytestmark = pytest.mark.integration

UNKNOWN = entry.UNKNOWN
BOEUF_LLM = new_task("CREATE_RECURRING_NEED", product="Boeuf", quantity=2.0, unit="TETE", recurrence_type="WEEKLY")
CHEVRE_LLM = new_task("CREATE_RECURRING_NEED", 0.95, product="chevre", quantity=3.0, unit="TETE", recurrence_type="WEEKLY")


@pytest.fixture(autouse=True)
def _real_recurring_tools(monkeypatch):
    monkeypatch.setattr(
        entry, "REAL_TOOLS",
        entry.REAL_TOOLS | {"ensure_next_recurring_occurrence", "refresh_recurring_need_matching", "accept_match_proposal",
                            "update_recurring_need"},
    )


def _account(real_db, role):  # noqa: F811
    if role == "ADMIN":
        return _user(real_db, role="ADMIN", name="Admin Ido")
    return _user(real_db, role="BUYER", name="Awa", buyer=True)


def _boeuf_detail(conv):
    conv.send("boeuf 2 tete chaque semaine", llm=BOEUF_LLM)
    assert "C'est noté" in conv.send("Confirmer").response
    conv.send("mes besoins", llm=UNKNOWN)
    detail = conv.send("1", llm=UNKNOWN)
    assert "Boeuf" in detail.response and "1. Rechercher maintenant" in detail.response, detail.response
    return detail


def _rows(real_db, account):  # noqa: F811
    return [(r[0].lower().rstrip("s"), float(r[1]), r[2], r[4], r[5]) for r in _needs_of(real_db, account["phone"])]


def _need_ids(real_db, account):  # noqa: F811
    return entry._sql(
        real_db,
        "select n.id::text from marketplace.recurring_needs n join marketplace.buyer_profiles b on b.id = n.buyer_id "
        "join auth.users u on u.id = b.user_id where u.phone = %s order by n.created_at", (account["phone"],))


@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_scenario_a_mets_en_3_updates_the_displayed_need_in_the_database(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        _boeuf_detail(conv)
        (need_id,) = [r[0] for r in _need_ids(real_db, account)]
        t = conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert t.intent == "UPDATE_RECURRING_NEED" and "refresh_recurring_need_matching" not in t.mcp_tools(), t.response
        assert "quantité 2 → 3" in t.response, t.response
        assert _rows(real_db, account) == [("boeuf", 3.0, "TETE", "ACTIVE", "WEEKLY")]
        assert [r[0] for r in _need_ids(real_db, account)] == [need_id], "exactement le même recurring_need"
        # la même phrase répétée est idempotente : valeur identique, aucune nouvelle ligne
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("1", llm=UNKNOWN)
        conv.send("mets-en 3", llm=ScriptLLM(UPDATE_QTY_3))
        assert _rows(real_db, account) == [("boeuf", 3.0, "TETE", "ACTIVE", "WEEKLY")] and len(_need_ids(real_db, account)) == 1


def test_scenario_a_bis_frequency_correction_in_the_database(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        t = conv.send("plutôt chaque mois", llm=ScriptLLM(UPDATE_MONTHLY))
        assert "chaque semaine → chaque mois" in t.response, t.response
        assert _rows(real_db, account) == [("boeuf", 2.0, "TETE", "ACTIVE", "MONTHLY")]


@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_scenario_b_new_task_from_the_detail_creates_exactly_one_chevre_and_leaves_the_boeuf(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        _boeuf_detail(conv)
        t = conv.send("j'ai besoin de 2 chevres chaque semaine", llm=ScriptLLM(
            new_task("CREATE_RECURRING_NEED", 0.95, product="chevre", quantity=2.0, unit="TETE", recurrence_type="WEEKLY")))
        assert t.intent == "CREATE_RECURRING_NEED" and "update_recurring_need" not in t.mcp_tools()
        assert len(_rows(real_db, account)) == 1, "rien n'est écrit avant la confirmation"
        assert "C'est noté" in conv.send("Confirmer").response
        assert _rows(real_db, account) == [("boeuf", 2.0, "TETE", "ACTIVE", "WEEKLY"), ("chevre", 2.0, "TETE", "ACTIVE", "WEEKLY")]


@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_scenario_c_finalement_5_corrects_the_same_draft_then_one_need_with_quantity_5(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        t1 = conv.send("j'ai besoin de 3 chevres chaque semaine", llm=CHEVRE_LLM)
        draft_id = t1.draft()["draft_id"]
        t2 = conv.send("finalement 5", llm=ScriptLLM(new_task("CREATE_RECURRING_NEED", 0.9, quantity=5.0)))
        t3 = conv.send("plutôt chaque mois", llm=ScriptLLM(new_task("CREATE_RECURRING_NEED", 0.9, recurrence_type="MONTHLY")))
        assert t2.draft()["draft_id"] == t3.draft()["draft_id"] == draft_id
        assert _rows(real_db, account) == [], "aucune écriture avant la confirmation"
        assert "C'est noté" in conv.send("Confirmer").response
        assert _rows(real_db, account) == [("chevre", 5.0, "TETE", "ACTIVE", "MONTHLY")]
        conv.send("Confirmer")  # confirmation répétée : toujours un seul besoin
        assert len(_rows(real_db, account)) == 1


def test_a_named_other_product_and_a_destructive_ambiguity_never_change_the_database(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        before = _rows(real_db, account)
        t = conv.send("mets plutôt 5 chèvres", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="chevres", quantity=5.0)))
        assert "update_recurring_need" not in t.mcp_tools() and "ne trouve pas" in t.response
        assert _rows(real_db, account) == before
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("1", llm=UNKNOWN)
        t = conv.send("annule mes boeufs", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="boeufs", update_action="CANCEL")))
        assert "Ignorer seulement la prochaine livraison" in t.response and "update_recurring_need" not in t.mcp_tools()
        assert _rows(real_db, account) == before and before[0][3] == "ACTIVE"
        conv.send("mes besoins", llm=ScriptLLM(new_task("GET_MY_NEEDS", 0.95)))
        assert "Boeuf" in conv.send("1", llm=UNKNOWN).response
        t = conv.send("change ça", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9)))
        assert "la quantité, la fréquence ou la prochaine livraison" in t.response
        assert _rows(real_db, account) == before


def test_two_boeuf_needs_ask_which_one_and_the_stale_choice_is_never_a_target(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        conv.send("j'ai besoin de 4 boeufs chaque semaine", llm=ScriptLLM(
            new_task("CREATE_RECURRING_NEED", 0.95, product="Boeuf", quantity=4.0, unit="TETE", recurrence_type="WEEKLY")))
        conv.send("Confirmer")
        assert len(_rows(real_db, account)) == 2
        conv.send("mes besoins", llm=UNKNOWN)
        conv.send("2", llm=UNKNOWN)  # écran du second besoin
        t = conv.send("cherche pour mes boeufs", llm=ScriptLLM(new_task("REFRESH_RECURRING_MATCHING", 0.93, product="boeufs")))
        assert "refresh_recurring_need_matching" not in t.mcp_tools() and "2 besoins récurrents de Boeuf" in t.response, t.response
        t = conv.send("mets-en 9", llm=ScriptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, quantity=9.0)))
        # l'écran affiché est maintenant la LISTE (aucune cible) : deux besoins -> choix explicite, jamais une devinette
        assert "update_recurring_need" not in t.mcp_tools(), t.response
        assert sorted(r[1] for r in _rows(real_db, account)) == [2.0, 4.0]


def test_ownership_the_service_rejects_another_buyers_need_even_for_an_admin_can_buy(real_db):  # noqa: F811
    owner = _account(real_db, "BUYER")
    intruder = _account(real_db, "ADMIN")
    with Conv(real_db, owner, profile_role="BUYER") as conv:
        conv.send("boeuf 2 tete chaque semaine", llm=BOEUF_LLM)
        conv.send("Confirmer")
    (need_id,) = [r[0] for r in _need_ids(real_db, owner)]
    before = _rows(real_db, owner)
    with Conv(real_db, intruder, profile_role="ADMIN") as conv:
        svc = conv._svc

        async def attempt(action, **kw):
            try:
                return await svc.update_recurring_need(phone=intruder["phone"], recurring_need_id=need_id, action=action, **kw)
            except Exception as exc:  # contrat MCP : l'erreur métier est la réponse
                return {"status": "error", "message": str(exc)}

        for action, kw in (("PERMANENT_QUANTITY", {"quantity": 99.0}), ("PERMANENT_FREQUENCY", {"recurrence_type": "MONTHLY"}),
                           ("CANCEL", {})):
            res = conv.harness._run(attempt(action, **kw))
            assert res.get("status") != "success", (action, res)
    assert _rows(real_db, owner) == before, "aucune fuite cross-buyer"


def test_ambiguous_vague_message_on_live_detail_asks_what_to_change(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        before = _rows(real_db, account)
        t = conv.send("change ça", llm=ScriptLLM(AMBIGUOUS_CHANGE))
        assert "la quantité, la fréquence ou la prochaine livraison" in t.response, t.response
        assert _rows(real_db, account) == before
