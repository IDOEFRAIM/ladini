"""B21 — RECURRING SELF-SERVICE BUYER : « mes besoins » -> besoin -> prochaine livraison -> recherche à la demande ->
proposition -> accepter / refuser, SANS cron et SANS digest, sur le VRAI graphe compilé (LLM aveugle : aucune navigation
n'est classée par le LLM). Le service est simulé par un double STATEFUL qui reproduit le contrat du vrai (`ensure`,
`refresh`, détail, `accept_match_proposal` exact : version + stock) ; le vrai service est prouvé contre PostgreSQL dans
`tests/schema/test_recurring_self_service_pg.py`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.conftest import run
from tests.integration.test_buyer_deterministic_product_switch_e2e import _PHONE, _offer
from tests.integration.test_buyer_preorder_confirmation_e2e import (  # noqa: F401
    _pin_cod_checkout,
    _PreorderRt,
    _role_hints,
)
from tests.integration.test_conversation_context_arbitration_e2e import NavBlindLLM, _r

NEED_ID, OCC_ID = "N-TOM", "OCC-1"


class SimRt(_PreorderRt):
    """Double stateful du service récurrent côté Buyer (aucun digest, aucun cron)."""

    def __init__(self, llm: Any, *, requested: float = 350.0, supply: Optional[List[tuple]] = None) -> None:
        super().__init__(llm, [_offer("lait", "LITRE", "F0", "L0")], [], "ok")
        self.requested, self.supply = requested, list(supply or [])
        self.occ: Optional[Dict[str, Any]] = None
        self.alloc: List[tuple] = []
        self.orders: List[Dict[str, Any]] = []
        self.stock_ok = True
        self.accept_calls: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.last: Optional[Dict[str, Any]] = None
        self.need_status = "ACTIVE"

    def matched(self) -> float:
        return sum(q for _p, q, _pr in self.alloc)

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.calls.append(tool)
        if tool == "get_last_interactive_outbound":
            return {"status": "success", "interactive": None}  # aucun digest, jamais
        if tool == "list_my_recurring_needs":
            o = self.occ
            return {"status": "success", "items": [{
                "recurring_need_id": NEED_ID, "product": "tomate", "quantity": self.requested, "unit": "KG",
                "recurrence_type": "WEEKLY_DAYS", "weekly_days": [5], "status": self.need_status,
                "next_occurrence_date": "2026-10-09" if o else None, "next_occurrence_id": OCC_ID if o else None,
                "next_occurrence_status": o["status"] if o else None, "requested_quantity": self.requested if o else None,
                "matched_quantity": self.matched() if o else None, "next_occurrence_version": o["version"] if o else None,
                "next_occurrence_notified": False}]}
        if tool == "ensure_next_recurring_occurrence":
            if self.occ is None:
                self.occ = {"status": "OPEN", "version": 1}
            return {"status": "success", "occurrence_id": OCC_ID, "created": 1}
        if tool == "refresh_recurring_need_matching":
            before = list(self.alloc)
            self.alloc = [(p, q, pr) for p, q, pr in self.supply]
            if self.alloc != before:
                self.occ["version"] += 1
                self.occ["status"] = "MATCHED" if self.matched() >= self.requested else "OPEN"
            return {"status": "success", "outcome": "MATCHED" if self.matched() > 0 else "NO_AVAILABILITY",
                    "changed": self.alloc != before, "occurrence_version": self.occ["version"]}
        if tool == "get_recurring_need_detail":
            base = {"status": "success", "recurring_need_id": NEED_ID, "recurrence_type": "WEEKLY_DAYS", "weekly_days": [5],
                    "need_status": self.need_status, "product": "tomate", "requested_quantity": self.requested, "unit": "KG"}
            if self.occ is None:
                return {**base, "occurrence_date": None, "allocations": [], "last_occurrence": self.last}
            return {**base, "occurrence_id": OCC_ID, "occurrence_version": self.occ["version"], "occurrence_date": "2026-10-09",
                    "occurrence_status": self.occ["status"], "quantity_matched": self.matched(),
                    "quantity_confirmed": sum(o["quantity"] for o in self.orders), "orders": list(self.orders),
                    "allocations": [] if self.orders else [
                        {"producer_label": p, "quantity": q, "unit_price": pr, "unit": "KG"} for p, q, pr in self.alloc]}
        if tool == "accept_match_proposal":
            self.accept_calls.append(dict(kw))
            if self.occ["status"] not in ("OPEN", "MATCHED"):
                return {"status": "success", "outcome": "ALREADY_PROCESSED", "occurrence_id": OCC_ID}
            if kw.get("expected_version") != self.occ["version"]:
                return {"status": "success", "outcome": "PROPOSAL_CHANGED", "occurrence_id": OCC_ID}
            if kw["action"] == "REJECT":
                self.occ["status"] = "REJECTED"
                return {"status": "success", "occurrence_id": OCC_ID, "action": "REJECT"}
            if not self.stock_ok:
                return {"status": "success", "outcome": "STOCK_CHANGED", "occurrence_id": OCC_ID}
            self.orders = [{"order_id": f"O{i}", "order_status": "PENDING_PRODUCER_CONFIRMATION", "producer_label": p, "quantity": q}
                           for i, (p, q, _pr) in enumerate(self.alloc)]
            self.occ["status"] = "ACCEPTED" if self.matched() >= self.requested else "PARTIALLY_ACCEPTED"
            self.occ["version"] += 1
            return {"status": "success", "occurrence_id": OCC_ID, "action": "ACCEPT",
                    "order_ids": [o["order_id"] for o in self.orders], "quantity_confirmed": self.matched()}
        if tool == "get_buyer_orders_dashboard":
            return {"status": "success", "formatted_menu": "📦 *SUIVI DE VOS COMMANDES :*\n*1. Commande #AAAA*",
                    "mapping": {"1": "11111111-aaaa"}}
        return await super().call_db(tool, **kw)


class Conv:
    def __init__(self, **kw: Any) -> None:
        self.rt = SimRt(NavBlindLLM(None, True), **kw)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.calls.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
                                "message_sid": f"m{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values


def _to_proposal(c: Conv) -> Dict[str, Any]:
    c.say("mes besoins")
    c.say("1")
    return c.say("rechercher maintenant")


# ── parcours principal : aucun digest, aucun cron ───────────────────────────────────────────────────────────
def test_full_cycle_partial_without_cron_or_digest():
    c = Conv(supply=[("Producteur A", 250.0, 500.0)])
    st = c.say("mes besoins")
    assert "Mes besoins récurrents" in _r(st) and "Tomate" in _r(st) and "350 KG chaque vendredi" in _r(st)
    assert "Prochaine livraison : à planifier" in _r(st)  # aucune occurrence matérialisée, et pourtant consultable
    assert "get_buyer_orders_dashboard" not in c.rt.calls  # « mes besoins » n'est JAMAIS les commandes

    st = c.say("1")  # sélection numérique du besoin (LLM aveugle)
    assert "ensure_next_recurring_occurrence" in c.rt.calls  # prochaine occurrence matérialisée à la demande
    assert "Livraison du 9 octobre" in _r(st) and "Aucune disponibilité trouvée" in _r(st) and "1. Rechercher maintenant" in _r(st)
    assert "Accepter" not in _r(st)

    st = c.say("1")  # « Rechercher maintenant » : le moteur de matching à la demande
    assert "refresh_recurring_need_matching" in c.rt.calls
    assert "250 KG disponibles sur 350 KG demandés" in _r(st) and "Producteur A" in _r(st)
    version = c.rt.occ["version"]

    st = c.say("accepter les 250 kg")
    call = c.rt.accept_calls[-1]
    assert call["action"] == "ACCEPT" and call["occurrence_id"] == OCC_ID and call["expected_version"] == version
    assert "confirmé" in _r(st).lower()
    assert c.rt.occ["status"] == "PARTIALLY_ACCEPTED" and sum(o["quantity"] for o in c.rt.orders) == 250.0

    st = c.say("mes besoins")  # reste valide après acceptation
    assert "partiellement accepté" in _r(st)
    st = c.say("1")
    assert "déjà accepté (partiellement : 250 sur 350 KG)" in _r(st) and "Producteur A : 250 KG" in _r(st) and "Accepter" not in _r(st)
    st = c.say("1")  # « Voir les commandes »
    assert "get_buyer_orders_dashboard" in c.rt.calls


def test_complete_availability_wording():
    c = Conv(supply=[("A", 200.0, 500.0), ("B", 150.0, 520.0)])
    st = _to_proposal(c)
    assert "Disponibilité complète : 350 KG" in _r(st)
    c.say("accepter")
    assert c.rt.occ["status"] == "ACCEPTED" and len(c.rt.orders) == 2


def test_zero_availability_offers_no_accept_and_retry_works():
    c = Conv(supply=[])
    st = _to_proposal(c)
    assert "Aucune disponibilité trouvée" in _r(st) and "1. Rechercher maintenant" in _r(st) and "Accepter" not in _r(st)
    c.say("accepter")  # aucune action « accepter » n'existe dans ce menu
    assert c.rt.accept_calls == [] and c.rt.orders == []
    c.rt.supply = [("A", 100.0, 500.0)]
    st = c.say("1")  # rechercher à nouveau
    assert "100 KG disponibles sur 350 KG demandés" in _r(st)


def test_numeric_replies_do_not_depend_on_the_llm_and_text_aliases_work():
    c = Conv(supply=[("A", 250.0, 500.0)])
    c.say("mes besoins")
    c.say("1")
    c.say("actualiser")  # alias textuel de « Rechercher maintenant »
    assert "refresh_recurring_need_matching" in c.rt.calls
    c.say("2")  # « Refuser »
    assert c.rt.accept_calls[-1]["action"] == "REJECT" and c.rt.occ["status"] == "REJECTED"


def test_version_changed_since_display_is_refused_without_any_order():
    c = Conv(supply=[("A", 250.0, 500.0)])
    _to_proposal(c)
    c.rt.occ["version"] += 1  # le matching a changé depuis l'affichage (cron ou autre)
    st = c.say("accepter les 250 kg")
    assert c.rt.orders == [] and "proposition modifiée" in _r(st)
    assert c.rt.accept_calls[-1]["expected_version"] != c.rt.occ["version"]


def test_stock_changed_since_display_is_refused_without_any_order():
    c = Conv(supply=[("A", 250.0, 500.0)])
    _to_proposal(c)
    c.rt.stock_ok = False
    st = c.say("accepter")
    assert c.rt.orders == [] and c.rt.occ["status"] == "OPEN" and "stock" in _r(st).lower()


def test_terminal_and_empty_states_show_requested_vs_delivered():
    c = Conv()
    c.rt.last = {"status": "PARTIALLY_FULFILLED", "date": "2026-10-02", "requested_quantity": 350.0, "quantity_delivered": 200.0,
                 "unit": "KG"}

    async def no_ensure(tool_name: str = "", **kw: Any) -> Any:
        return None

    orig = c.rt.call_db

    async def spy(tool: str, **kw: Any) -> Any:
        if tool == "ensure_next_recurring_occurrence":
            return {"status": "success", "occurrence_id": None, "created": 0}
        return await orig(tool, **kw)

    c.rt.call_db = spy
    c.say("mes besoins")
    st = c.say("1")
    assert "Demandé : 350 KG" in _r(st) and "Reçu : 200 KG" in _r(st) and "partiellement livré" in _r(st)
    assert "Accepter" not in _r(st)


def test_interruption_from_the_occurrence_screen_follows_b20():
    c = Conv(supply=[("A", 250.0, 500.0)])
    _to_proposal(c)
    st = c.say("mes commandes")
    assert "SUIVI DE VOS COMMANDES" in _r(st)
