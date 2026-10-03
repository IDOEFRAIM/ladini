"""B12 — la VERSION vue par l'acheteur est la seule que sa réponse peut valider.

Digest : chaque occurrence est confirmée/refusée avec `expected_version` = version du digest (payload
outbox), jamais « la plus proche ouverte ». Écran détail : même service, version AFFICHÉE. Succès partiel
honnête. La revalidation SQL (version, verrous, expiration, concurrence) est prouvée sur le vrai
PostgreSQL dans `tests/schema/test_recurring_lifecycle_pg.py` et sur le vrai service dans
`tests/unit/test_accept_match_proposal_exact_mode.py`.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
    recurring_need_flow,
)
from tests.conftest import StubRuntime, make_state, run
from tests.integration.test_recurring_interaction_integrity_e2e import (
    Conv,
    _item,
    _RecRt,
    _resp,
)


def _digest_item(need: str, product: str, *, digest_v: int = 7, current_v: int = 7, date: str = "2026-10-04",
                 in_digest: bool = True, matched: float = 50.0) -> Dict[str, Any]:
    it = _item(need, product, date=date, matched=matched)
    it.update({"in_latest_digest": in_digest, "digest_occurrence_version": digest_v if in_digest else None,
               "next_occurrence_version": current_v})
    return it


class _VersionedRt(_RecRt):
    """Service factice qui applique le CONTRAT de version : `expected_version` != version courante ->
    PROPOSAL_CHANGED, sans mutation (ACCEPT comme REJECT)."""

    def __init__(self, llm, items, outcomes=None):
        super().__init__(llm, items, outcomes)
        self.current = {i["next_occurrence_id"]: i["next_occurrence_version"] for i in items if "next_occurrence_version" in i}

    async def call_db(self, tool: str, **kw: Any) -> Any:
        if tool == "accept_match_proposal" and kw.get("expected_version") is not None:
            occ = kw["occurrence_id"]
            if self.current.get(occ) != kw["expected_version"]:
                self.all_calls.append(tool)
                self.accept_calls.append(dict(kw))
                return {"status": "success", "outcome": "PROPOSAL_CHANGED", "action": kw["action"],
                        "occurrence_id": occ, "expected_version": kw["expected_version"],
                        "current_version": self.current.get(occ)}
        return await super().call_db(tool, **kw)


class VConv(Conv):
    def __init__(self, items, outcomes=None):
        super().__init__(items, outcomes=outcomes)
        self.rt = _VersionedRt(self.llm, items, outcomes)
        from langgraph.checkpoint.memory import MemorySaver

        from ladini.graphs.agents.market_coach.core.graph_builder import build_graph

        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())


def test_digest_accept_sends_the_digest_version_as_expected_version():
    c = VConv([_digest_item("A", "tomate", digest_v=7, current_v=7)])
    st = c.say("oui")
    assert c.rt.accept_calls[0]["expected_version"] == 7
    assert c.rt.accept_calls[0]["occurrence_id"] == "occ-A-2026-10-04"
    assert "confirmé" in _resp(st).lower()


def test_digest_accept_with_changed_proposal_is_refused_with_a_clear_message_and_no_acceptance():
    c = VConv([_digest_item("A", "tomate", digest_v=7, current_v=8)])
    st = c.say("oui")
    r = _resp(st).lower()
    assert "modifiée" in r and "nouvelle validation" in r
    assert "en cours de préparation" not in r and c.accepted() == []


@pytest.mark.parametrize("word, expect_action", [("non", "REJECT"), ("pas demain", "REJECT")])
def test_digest_reject_uses_the_same_version_contract(word: str, expect_action: str):
    c = VConv([_digest_item("A", "tomate", digest_v=7, current_v=7)])
    c.say(word)
    assert c.rt.accept_calls[0]["action"] == expect_action and c.rt.accept_calls[0]["expected_version"] == 7


def test_digest_reject_on_a_changed_proposal_is_not_silently_applied():
    c = VConv([_digest_item("A", "tomate", digest_v=7, current_v=9)])
    st = c.say("non")
    assert c.rt.state.get("occ-A-2026-10-04") is None  # la NOUVELLE version n'a pas été rejetée
    assert "rien confirmé" in _resp(st).lower() or "modifiée" in _resp(st).lower()


def test_partial_success_one_ok_one_changed_one_expired_is_reported_honestly():
    items = [_digest_item("A", "tomate", digest_v=3, current_v=3),
             _digest_item("B", "lait", digest_v=5, current_v=6),
             _digest_item("C", "oignon", digest_v=2, current_v=2)]
    c = VConv(items, outcomes={"occ-C-2026-10-04": "EXPIRED"})
    r = _resp(c.say("oui")).lower()
    assert "tomate" in r and "lait" in r and "oignon" in r
    assert "modifiée" in r and "expirée" in r
    assert "en cours de préparation" not in r  # jamais « tout est confirmé »
    assert c.accepted() == ["occ-A-2026-10-04"]


def test_multiple_needs_all_unchanged_are_all_accepted_each_with_its_own_version():
    c = VConv([_digest_item("A", "tomate", digest_v=3, current_v=3), _digest_item("B", "lait", digest_v=9, current_v=9)])
    c.say("oui")
    got = {x["occurrence_id"]: x["expected_version"] for x in c.rt.accept_calls}
    assert got == {"occ-A-2026-10-04": 3, "occ-B-2026-10-04": 9}


def test_stale_digest_occurrence_is_not_targeted_when_the_latest_digest_lists_another():
    items = [_digest_item("A", "tomate", in_digest=False, date="2026-10-02"),
             _digest_item("B", "lait", in_digest=True, date="2026-10-04")]
    c = VConv(items)
    c.say("oui")
    assert [x["occurrence_id"] for x in c.rt.accept_calls] == ["occ-B-2026-10-04"]


def test_nothing_in_the_latest_digest_means_nothing_is_confirmed():
    c = VConv([_digest_item("A", "tomate", in_digest=False, date="2026-10-04")])
    # Aucun besoin n'est dans le dernier digest ET la proposition n'est pas notifiée (repli transitoire exclu).
    c.rt.recurring = [{**c.rt.recurring[0], "next_occurrence_notified": False}]
    st = c.say("oui")
    assert c.rt.accept_calls == [] and "rien à confirmer" in _resp(st).lower()


def test_next_occurrence_after_no_response_targets_the_new_occurrence_only():
    """O1 (passée, jamais répondue) est expirée par le balayage ; le service ne l'expose plus comme
    « prochaine » : l'item du besoin est O2, dans le NOUVEAU digest."""
    c = VConv([_digest_item("A", "tomate", digest_v=1, current_v=1, date="2026-10-05")])
    c.say("oui")
    assert [x["occurrence_id"] for x in c.rt.accept_calls] == ["occ-A-2026-10-05"]


def test_blocked_producer_outcome_is_reported_as_unavailable():
    c = VConv([_digest_item("A", "tomate")], outcomes={"occ-A-2026-10-04": "PRODUCER_UNAVAILABLE"})
    r = _resp(c.say("oui")).lower()
    assert "indisponible" in r and "en cours de préparation" not in r


def test_legacy_digest_without_snapshot_falls_back_without_expected_version():
    legacy = _item("A", "tomate")  # pas de in_latest_digest : digest antérieur à B12
    c = VConv([legacy])
    c.say("oui")
    assert c.rt.accept_calls[0].get("expected_version") is None
    assert c.rt.accept_calls[0]["occurrence_id"] == "occ-A-2026-10-04"


@pytest.mark.parametrize("mode", ["unknown", "invalid_json", "exception", "none"])
def test_versioned_short_reply_is_independent_of_the_llm(mode: str):
    from tests.integration.test_buyer_deterministic_product_switch_e2e import HostileLLM

    c = VConv([_digest_item("A", "tomate")])
    if mode == "none":
        c.rt.llm = None
    else:
        c.rt.llm = HostileLLM({"disposition": "UNKNOWN", "confidence": 0.1} if mode == "unknown"
                              else ("{not json" if mode == "invalid_json" else TimeoutError("x")), new_task_ok=False)
    c.say("oui")
    assert c.rt.accept_calls[0]["expected_version"] == 7


# ── écran détail : même service, version AFFICHÉE ───────────────────────────────────────────────

_DETAIL = {
    "status": "success", "occurrence_id": "occ-tomate", "occurrence_version": 4, "product": "tomate",
    "requested_quantity": 40, "unit": "KG", "occurrence_date": "2026-10-04",
    "allocations": [{"producer_label": "Coop A", "quantity": 40, "unit_price": 500, "unit": "KG"}],
}


def _menu_state(mapping_value: str, index: str = "1"):
    return make_state(
        current_goal="GET_MY_NEEDS",
        transaction_payload={"selection_index": index},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
        working_memory={"recurring_need_menu": {
            "mapping": {"1": f"CONFIRM:{mapping_value}", "2": f"REJECT:{mapping_value}", "3": "LIST"},
            "created_at": time.time()}},
    )


def test_detail_screen_menu_carries_the_exact_occurrence_and_version():
    state = make_state(
        current_goal="GET_MY_NEEDS", transaction_payload={"selection_index": "1"},
        pending_interaction={"kind": "SELECTION_MENU", "goal": "GET_MY_NEEDS", "created_at": time.time()},
        working_memory={"recurring_need_menu": {"mapping": {"1": "need-tomate"}, "created_at": time.time()}},
    )
    result = run(recurring_need_flow(state, StubRuntime(responses={"get_recurring_need_detail": _DETAIL})))
    assert result["working_memory"]["recurring_need_menu"]["mapping"]["1"] == "CONFIRM:need-tomate|occ-tomate|4"


def test_detail_confirm_calls_the_same_hardened_service_with_exact_identity():
    seen: List[Dict[str, Any]] = []

    def _accept(**kw):
        seen.append(kw)
        return {"status": "success", "occurrence_id": "occ-tomate", "order_ids": ["o1"]}

    rt = StubRuntime(responses={"accept_match_proposal": _accept})
    result = run(recurring_need_flow(_menu_state("need-tomate|occ-tomate|4"), rt))
    assert seen[0]["recurring_need_id"] == "need-tomate" and seen[0]["occurrence_id"] == "occ-tomate"
    assert seen[0]["expected_version"] == 4 and seen[0]["action"] == "ACCEPT"
    assert "confirmé" in result["final_response"].lower()


def test_detail_confirm_with_changed_version_is_refused_and_never_claims_success():
    def _accept(**kw):
        return {"status": "success", "outcome": "PROPOSAL_CHANGED", "occurrence_id": kw["occurrence_id"]}

    rt = StubRuntime(responses={"accept_match_proposal": _accept})
    r = run(recurring_need_flow(_menu_state("need-tomate|occ-tomate|4"), rt))["final_response"].lower()
    assert "modifiée" in r and "confirmé" not in r.replace("rien enregistré", "")


def test_detail_reject_uses_the_same_version_contract():
    seen: List[Dict[str, Any]] = []

    def _accept(**kw):
        seen.append(kw)
        return {"status": "success", "occurrence_id": "occ-tomate", "action": "REJECT"}

    run(recurring_need_flow(_menu_state("need-tomate|occ-tomate|4", index="2"), StubRuntime(responses={"accept_match_proposal": _accept})))
    assert seen[0]["action"] == "REJECT" and seen[0]["expected_version"] == 4
