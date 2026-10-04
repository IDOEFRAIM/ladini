"""B23 — scénario critique sur le VRAI graphe + un VRAI PostgreSQL : un menu récurrent est une ATTENTE, pas une prison.

Besoin Bœuf existant -> « mes besoins » -> détail (matérialisation réelle de l'occurrence) -> LANGAGE LIBRE « j ai besoin de
2 chevres chaque semaine » -> récapitulatif -> confirmation -> EXACTEMENT un nouveau besoin Chèvre en base, le Bœuf intact.
Compte ADMIN réel (graphe PRODUCER, capacité `can_buy`) puis BUYER. Seuls LLM (scripté PAR PROMPT), Redis et dispatcher sont
doublés ; les outils récurrents passent par le vrai `AgriDatabaseService`.
"""
from __future__ import annotations

import pytest

from tests.harness import new_task
from tests.integration.test_recurring_intent_integration_e2e import (
    CREATE_CHEVRE,
    FREE_TEXT,
    PromptLLM,
)
from tests.schema import test_recurring_entrypoint_pg as entry
from tests.schema.test_recurring_entrypoint_pg import (  # noqa: F401
    Conv,
    _needs_of,
    _user,
    real_db,
)

pytestmark = pytest.mark.integration

UNKNOWN = entry.UNKNOWN
BOEUF_LLM = new_task("CREATE_RECURRING_NEED", product="Boeuf", quantity=2.0, unit="TETE", recurrence_type="WEEKLY")


@pytest.fixture(autouse=True)
def _real_recurring_tools(monkeypatch):
    monkeypatch.setattr(
        entry, "REAL_TOOLS",
        entry.REAL_TOOLS | {"ensure_next_recurring_occurrence", "refresh_recurring_need_matching", "accept_match_proposal"},
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


@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_free_text_from_the_boeuf_detail_creates_exactly_one_new_chevre_need(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        _boeuf_detail(conv)
        assert len(_needs_of(real_db, account["phone"])) == 1

        t = conv.send(FREE_TEXT, llm=PromptLLM(CREATE_CHEVRE))
        assert t.intent == "CREATE_RECURRING_NEED", (t.intent, t.response)
        assert "refresh_recurring_need_matching" not in t.mcp_tools()
        assert len(_needs_of(real_db, account["phone"])) == 1, "rien n'est écrit avant la confirmation"

        t2 = conv.send("Confirmer")
        assert "C'est noté" in t2.response, t2.response
        rows = _needs_of(real_db, account["phone"])
        # (le nom de sous-catégorie peut être le pluriel d'une ligne de référence partagée : on compare la racine)
        assert [(r[0].lower().rstrip("s"), float(r[1]), r[2], r[4], r[5]) for r in rows] == [
            ("boeuf", 2.0, "TETE", "ACTIVE", "WEEKLY"),
            ("chevre", 2.0, "TETE", "ACTIVE", "WEEKLY")  # unité canonique du bétail,
        ]
        assert len({r[3] for r in rows}) == 1, "les deux besoins appartiennent au même acheteur"


@pytest.mark.parametrize("role", ["ADMIN", "BUYER"])
def test_closed_replies_still_run_the_real_matching_without_llm(real_db, role):  # noqa: F811
    account = _account(real_db, role)
    with Conv(real_db, account, profile_role=role) as conv:
        _boeuf_detail(conv)
        for answer in ("1", "Rechercher maintenant"):
            t = conv.send(answer, llm=UNKNOWN)
            assert "refresh_recurring_need_matching" in t.mcp_tools() and t.llm_calls == 0, (answer, t.response)
            assert "Boeuf" in t.response


def test_a_misclassified_update_never_changes_the_boeuf_need(real_db):  # noqa: F811
    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        t = conv.send("mets 5 chevres plutot", llm=PromptLLM(new_task("UPDATE_RECURRING_NEED", 0.9, product="chevres", quantity=5.0), mode="always_interrupt"))
        assert "update_recurring_need" not in t.mcp_tools(), t.response
        rows = _needs_of(real_db, account["phone"])
        assert [(r[0].lower().rstrip("s"), float(r[1])) for r in rows] == [("boeuf", 2.0)]


def test_a_free_text_reply_never_accepts_a_real_proposal(real_db):  # noqa: F811
    """Fail-safe sur la vraie base : même un LLM aveugle qui « choisit » 1 ne crée aucune commande."""
    import psycopg2

    account = _account(real_db, "ADMIN")
    with Conv(real_db, account, profile_role="ADMIN") as conv:
        _boeuf_detail(conv)
        before = len(entry._sql(real_db, "select 1 from marketplace.orders"))
        t = conv.send(FREE_TEXT, llm=PromptLLM(mode="prompt_blind"))
        assert "accept_match_proposal" not in t.mcp_tools()
        assert len(entry._sql(real_db, "select 1 from marketplace.orders")) == before
        assert psycopg2  # (connexion SQL directe utilisée par les helpers)


def test_ownership_a_buyer_cannot_reach_another_buyers_need(real_db):  # noqa: F811
    """25. Tous les targets sont vérifiés côté service : la cible d'un autre acheteur est refusée, même pour un ADMIN `can_buy`."""
    owner = _account(real_db, "BUYER")
    intruder = _account(real_db, "ADMIN")
    with Conv(real_db, owner, profile_role="BUYER") as conv:
        conv.send("boeuf 2 tete chaque semaine", llm=BOEUF_LLM)
        conv.send("Confirmer")
    need_id = entry._sql(
        real_db,
        "select n.id::text from marketplace.recurring_needs n join marketplace.buyer_profiles b on b.id = n.buyer_id "
        "join auth.users u on u.id = b.user_id where u.phone = %s", (owner["phone"],))[0][0]
    with Conv(real_db, intruder, profile_role="ADMIN") as conv:
        svc = conv._svc

        async def attempt(tool):
            try:
                return await getattr(svc, tool)(phone=intruder["phone"], recurring_need_id=need_id)
            except Exception as exc:  # contrat MCP : l'erreur métier est la réponse
                return {"status": "error", "message": str(exc)}

        for tool in ("get_recurring_need_detail", "ensure_next_recurring_occurrence", "refresh_recurring_need_matching"):
            res = conv.harness._run(attempt(tool))
            assert res.get("status") != "success", (tool, res)
