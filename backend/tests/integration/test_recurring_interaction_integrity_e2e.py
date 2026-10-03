"""B11-recurring (2026-10-03) — une réponse courte au digest vise UNE interaction métier exacte.

Le digest est un message PROACTIF (outbox) : aucun `PendingInteraction` ne peut l'ancrer. Le lien
message -> objet est reconstruit de façon DÉTERMINISTE depuis la base (occurrences NOTIFIÉES, besoin
ACTIF, digest le plus récent) et l'occurrence EXACTE est transmise à `accept_match_proposal`. Replays sur
le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés. La revalidation métier (expirée, modifiée,
stock, déjà traitée) est prouvée sur le vrai service dans `tests/unit/test_accept_match_proposal_exact_mode.py`.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _PHONE,
    HostileLLM,
    _offer,
    run,
)
from tests.integration.test_buyer_preorder_confirmation_e2e import (  # noqa: F401
    CartLLM,
    _pin_cod_checkout,
    _PreorderRt,
)

_OK_CHARS = ("confirmé",)


def _item(need: str, product: str, *, date: str = "2026-10-04", notified: bool = True, status: str = "ACTIVE",
          matched: float = 50.0) -> Dict[str, Any]:
    return {"recurring_need_id": need, "product": product, "unit": "KG", "status": status,
            "next_occurrence_id": f"occ-{need}-{date}", "next_occurrence_date": date,
            "requested_quantity": 50.0, "matched_quantity": matched, "next_occurrence_version": 1,
            "next_occurrence_notified": notified}


class _RecRt(_PreorderRt):
    """Service factice STATEFUL : applique la sémantique du mode exact (déjà traité / expiré / ...)."""

    def __init__(self, llm, items, outcomes=None):
        super().__init__(llm, [_offer("poulets", "UNITE", "P1", "A1")], items, "ok")
        self.state: Dict[str, str] = {}
        self.outcomes = outcomes or {}
        self.accept_calls: List[Dict[str, Any]] = []

    async def call_db(self, tool: str, **kw: Any) -> Any:
        if tool == "list_my_recurring_needs":
            self.all_calls.append(tool)
            live = [i for i in self.recurring if self.state.get(i["next_occurrence_id"]) is None]
            return {"status": "success", "items": live, "data": {"items": live}}
        if tool == "accept_match_proposal":
            self.all_calls.append(tool)
            self.accept_calls.append(dict(kw))
            occ = kw.get("occurrence_id")
            forced = self.outcomes.get(occ)
            if forced:
                return {"status": "success", "outcome": forced, "action": kw["action"], "occurrence_id": occ}
            if self.state.get(occ):
                return {"status": "success", "outcome": "ALREADY_PROCESSED", "occurrence_id": occ}
            self.state[occ] = kw["action"]
            return {"status": "success", "occurrence_id": occ, "action": kw["action"], "order_ids": [f"O-{occ}"]}
        return await super().call_db(tool, **kw)


class Conv:
    def __init__(self, items, *, outcomes=None, llm=True):
        self.llm = CartLLM()
        self.rt = _RecRt(self.llm if llm else None, items, outcomes)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        run(self.graph.ainvoke(
            {"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
             "message_sid": f"m{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values

    def accepted(self) -> List[str]:
        return [c["occurrence_id"] for c in self.rt.accept_calls if c["action"] == "ACCEPT"
                and self.rt.state.get(c["occurrence_id"]) == "ACCEPT"]


def _resp(st) -> str:
    return str(st.get("final_response") or "")


def test_one_need_one_proposal_yes_targets_that_exact_occurrence():
    c = Conv([_item("A", "tomate")])
    st = c.say("oui")
    assert [x["occurrence_id"] for x in c.rt.accept_calls] == ["occ-A-2026-10-04"]
    assert "confirmé" in _resp(st).lower()


def test_two_needs_same_digest_each_gets_its_own_occurrence_never_more():
    c = Conv([_item("A", "tomate"), _item("B", "lait")])
    c.say("oui")
    assert sorted(x["occurrence_id"] for x in c.rt.accept_calls) == ["occ-A-2026-10-04", "occ-B-2026-10-04"]


def test_older_digest_occurrence_is_not_absorbed_by_a_reply_to_the_latest_digest():
    c = Conv([_item("A", "tomate", date="2026-10-02"), _item("B", "lait", date="2026-10-04")])
    c.say("oui")
    assert [x["occurrence_id"] for x in c.rt.accept_calls] == ["occ-B-2026-10-04"]


def test_never_notified_occurrence_is_never_confirmed_by_a_short_reply():
    c = Conv([_item("A", "tomate", notified=False)])
    st = c.say("oui")
    assert c.rt.accept_calls == []
    assert "rien à confirmer" in _resp(st).lower() and "confirmé" not in _resp(st).lower().replace("rien à confirmer", "")


def test_paused_or_cancelled_need_is_never_confirmed():
    c = Conv([_item("A", "tomate", status="PAUSED"), _item("B", "lait", status="CANCELLED")])
    c.say("okay")
    assert c.rt.accept_calls == []


def test_duplicate_yes_one_acceptance_only():
    c = Conv([_item("A", "tomate")])
    c.say("oui")
    st = c.say("oui")
    assert c.accepted() == ["occ-A-2026-10-04"]
    assert "commandes sont en cours" not in _resp(st)


@pytest.mark.parametrize("outcome, label", [
    ("EXPIRED", "expirée"), ("PROPOSAL_CHANGED", "modifiée"), ("NEED_INACTIVE", "désactivé"),
    ("STOCK_CHANGED", "stock"), ("ALREADY_PROCESSED", "déjà traité"),
])
def test_refused_proposal_never_claims_success(outcome: str, label: str):
    c = Conv([_item("A", "tomate")], outcomes={"occ-A-2026-10-04": outcome})
    st = c.say("oui")
    r = _resp(st).lower()
    assert label in r and "en cours de préparation" not in r and "c'est confirmé" not in r


def test_partial_refusal_reports_each_need_honestly():
    c = Conv([_item("A", "tomate"), _item("B", "lait")], outcomes={"occ-B-2026-10-04": "STOCK_CHANGED"})
    r = _resp(c.say("oui")).lower()
    assert "tomate" in r and "lait" in r and "stock" in r and "en cours de préparation" not in r


@pytest.mark.parametrize("word", ["non", "pas demain"])
def test_reject_targets_the_digest_occurrence_and_creates_no_order(word: str):
    c = Conv([_item("A", "tomate")])
    c.say(word)
    assert [x["action"] for x in c.rt.accept_calls] == ["REJECT"]
    assert c.rt.accept_calls[0]["occurrence_id"] == "occ-A-2026-10-04"


def test_next_occurrence_after_accept_is_not_contaminated_by_the_previous_one():
    items = [_item("A", "tomate", date="2026-10-04")]
    c = Conv(items)
    c.say("oui")
    # O2 générée et notifiée : le besoin A a maintenant une occurrence suivante
    c.rt.recurring = [_item("A", "tomate", date="2026-10-05")]
    c.say("oui")
    assert [x["occurrence_id"] for x in c.rt.accept_calls] == ["occ-A-2026-10-04", "occ-A-2026-10-05"]


@pytest.mark.parametrize("mode", ["unknown", "invalid_json", "exception", "none"])
def test_short_reply_does_not_depend_on_the_llm(mode: str):
    if mode == "none":
        c = Conv([_item("A", "tomate")], llm=False)
    else:
        c = Conv([_item("A", "tomate")])
        c.llm = HostileLLM({"disposition": "UNKNOWN", "confidence": 0.1} if mode == "unknown"
                           else ("{not json" if mode == "invalid_json" else TimeoutError("x")), new_task_ok=False)
        c.rt.llm = c.llm
    c.say("oui")
    assert [x["occurrence_id"] for x in c.rt.accept_calls] == ["occ-A-2026-10-04"]


def test_manual_cart_ready_wins_over_a_pending_recurring_digest():
    c = Conv([_item("A", "tomate")])
    c.rt.offers = [_offer("poulets", "UNITE", "P1", "A1"), _offer("lait", "LITRE", "P2", "B1")]
    for t in ("je veux acheter des poulets", "2"):
        st = c.say(t)
    assert "précommander" in _resp(st) or (st.get("preorder_workflow") or {}).get("phase") == "CART"
    c.say("okay")
    assert c.rt.accept_calls == []  # B9 : le panier prêt gagne, jamais le digest


def test_active_manual_tunnel_is_not_hijacked_by_the_digest():
    c = Conv([_item("A", "tomate")])
    c.say("je veux acheter des poulets")  # tunnel d'achat actif (quantité demandée)
    c.say("oui")
    assert c.rt.accept_calls == []
