"""P0 — confirmation d'un besoin récurrent : au plus UN jeu de besoins, quelles que soient les
pannes. Rejoué sur le graphe COMPILÉ réel (harnais `tests/harness`) ; la garde PostgreSQL
elle-même est prouvée dans `tests/schema/test_recurring_need_confirmation_ledger.py`.

Fenêtres de panne couvertes :
  A. avant la mutation         : EXECUTING non durable -> aucun appel MCP
  B. pendant la mutation       : erreur métier (rollback) -> FAILED, jamais « C'est noté »
  C. après COMMIT, avant l'état : réponse MCP perdue / état du tour perdu -> pas de doublon
  D. après l'état, avant l'envoi : couvert par la reprise de message (Celery), voir C9
"""
from __future__ import annotations

import pytest

from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

_COQ = new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY")
_TWO = new_task(
    "CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY",
    additional_items=[{"product": "chèvre", "quantity": 20.0}],
)


@pytest.fixture()
def conv():
    with ConversationHarness() as harness:
        yield harness


def _draft_id(turn) -> str:
    return turn.draft()["draft_id"]


class TestDurableLifecycle:
    def test_every_draft_mutation_is_persisted_in_the_draft_table(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        row = conv.drafts.rows[_draft_id(t1)]
        assert (row["status"], row["version"]) == ("DRAFT", t1.draft()["version"])
        assert row["conversation_id"] == conv.phone

    def test_executing_is_durable_before_the_mcp_call(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        draft_id = _draft_id(t1)
        seen = []
        original = conv.server.create_recurring_need

        def spy(**kw):
            seen.append(conv.drafts.status_of(draft_id))
            return original(**kw)

        conv.runtime.responses["create_recurring_need"] = spy
        conv.send("oui")
        assert seen == ["EXECUTING"]
        assert conv.drafts.status_of(draft_id) == "EXECUTED"

    def test_the_confirmation_carries_its_durable_execution_key(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        t2 = conv.send("oui")
        (_tool, kwargs), = [c for c in t2.mcp_calls if c[0] == "create_recurring_need"]
        assert kwargs["draft_id"] == _draft_id(t1)
        assert kwargs["draft_version"] == t1.draft()["version"] + 1
        assert kwargs["idempotency_key"] == f"recurring_need:{_draft_id(t1)}:{kwargs['draft_version']}"


class TestWindowA_BeforeMutation:
    def test_no_mcp_call_when_executing_cannot_be_made_durable(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.drafts.fail_writes = True
        t = conv.send("oui")
        assert conv.server.executions == 0
        assert "create_recurring_need" not in t.mcp_tools()
        assert conv.drafts.rows and all(r["status"] == "DRAFT" for r in conv.drafts.rows.values())
        conv.drafts.fail_writes = False
        conv.send("oui")
        assert conv.server.executions == 1


class TestWindowB_DuringMutation:
    def test_a_business_error_is_a_failure_never_a_success(self, conv):
        conv.server.fail_with = {"status": "error", "message": "Produit inconnu."}
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        t2 = conv.send("oui")
        assert conv.server.created == []
        assert "noté" not in t2.response.lower()
        assert conv.drafts.status_of(_draft_id(t1)) == "FAILED"
        assert t2.draft() is None, "un draft FAILED est terminal"


class TestWindowC_AfterCommit:
    def test_a_lost_mcp_response_after_commit_is_resolved_without_duplicate(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.server.lose_response_after_commit = 1
        t2 = conv.send("oui")
        assert conv.server.executions == 1
        assert conv.drafts.status_of(_draft_id(t1)) == "EXECUTED"
        assert "noté" in t2.response.lower(), "la base dit EXECUTED : c'est un succès"

    def test_a_timeout_before_commit_leaves_the_confirmation_resumable(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.runtime.responses["create_recurring_need"] = TimeoutError("MCP timeout")
        t2 = conv.send("oui")
        assert conv.drafts.status_of(_draft_id(t1)) == "EXECUTION_UNKNOWN"
        assert t2.draft()["status"] == "EXECUTION_UNKNOWN", "un draft en doute reste actif"
        assert t2.pending_after.kind.value == "CONFIRM_ACTION"
        conv.runtime.responses["create_recurring_need"] = conv.server.create_recurring_need
        t3 = conv.send("oui")
        assert conv.server.executions == 1
        assert conv.drafts.status_of(_draft_id(t1)) == "EXECUTED"
        assert "noté" in t3.response.lower()

    def test_repeated_oui_after_an_ambiguous_timeout_never_duplicates(self, conv):
        conv.send("je veux 14 coqs et 20 chèvres chaque semaine", llm=_TWO)
        conv.server.lose_response_after_commit = 3
        for _ in range(4):
            conv.send("oui")
        assert conv.server.executions == 1
        assert len(conv.server.created) == 2

    def test_a_lost_turn_state_after_commit_never_executes_twice(self, conv):
        """L'état LangGraph du tour de confirmation est perdu (flush en échec après le
        COMMIT métier) : au tour suivant, la projection montre encore un DRAFT en attente."""
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.store.fail_saves = True
        conv.send("oui")
        conv.store.fail_saves = False
        assert conv.state()["recurring_need_draft"]["status"] == "DRAFT", "projection périmée simulée"
        t3 = conv.send("oui")
        assert conv.server.executions == 1
        assert conv.drafts.status_of(_draft_id(t1)) == "EXECUTED"
        assert "déjà" in t3.response.lower()

    @pytest.mark.xfail(strict=True, reason="H1: le rendu CONFIRMATION générique remplace le message du flow")
    def test_the_user_is_told_the_confirmation_is_being_verified(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.runtime.responses["create_recurring_need"] = TimeoutError("MCP timeout")
        t = conv.send("oui")
        assert "aucun doublon" in t.response.lower()

    def test_idempotency_does_not_depend_on_redis(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.redis.set = lambda *a, **k: True  # claims Redis toujours accordés (Redis « absent »)
        conv.send("oui")
        conv.send("oui")
        assert conv.server.executions == 1
