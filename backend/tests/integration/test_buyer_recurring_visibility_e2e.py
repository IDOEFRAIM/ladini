"""B13 (2026-10-03) — AUDIT : ce que le Buyer voit et peut piloter, sur le vrai graphe.

Ces tests ne changent AUCUNE logique : ils figent le comportement RÉEL observé (y compris les manques, nommés
« GAP » dans les noms) pour que toute évolution soit volontaire. Le service est doublé par des données de la forme
exacte de `list_my_recurring_needs` / `get_recurring_need_detail` (la vraie base n'est disponible qu'en CI :
voir `tests/schema/test_recurring_e2e_pg.py`).
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _PHONE,
    HostileLLM,
    _Comp,
    _offer,
    run,
)
from tests.integration.test_buyer_preorder_confirmation_e2e import (  # noqa: F401
    _pin_cod_checkout,
    _PreorderRt,
)

_PHRASES = [
    "mes besoins récurrents",
    "mes recurring",
    "mes approvisionnements récurrents",
    "mes approvisionnements",
    "mes besoins",
    "voir mes besoins récurrents",
]


def _need(need_id: str, product: str, **over: Any) -> Dict[str, Any]:
    base = {
        "recurring_need_id": need_id, "product": product, "quantity": 20.0, "unit": "L",
        "recurrence_type": "WEEKLY", "weekly_days": None, "status": "ACTIVE",
        "next_occurrence_date": None, "next_occurrence_id": None, "requested_quantity": None,
        "matched_quantity": None, "next_occurrence_version": None, "next_occurrence_notified": False,
        "in_latest_digest": False, "digest_occurrence_version": None,
    }
    base.update(over)
    return base


class _ListLLM(HostileLLM):
    """Classifie les 6 formulations en GET_MY_NEEDS — c'est ce que le LLM de prod DOIT faire : aucune route
    déterministe n'existe pour ces phrases (voir test `GAP_no_deterministic_route`)."""

    def __init__(self, classify: bool = True) -> None:
        super().__init__({"disposition": "UNKNOWN", "confidence": 0.1})
        self.classify = classify
        self.seen: List[str] = []

    def create(self, **kw: Any):
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}', user, re.S)
        text = (found[-1].strip() if found else "").lower()
        self.seen.append(text)
        if "CATALOGUE OFFICIEL" in sysm and self.classify and text in {p.lower() for p in _PHRASES}:
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "GET_MY_NEEDS", "confidence": 0.9,
                                     "entities": {}}))
        return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.1}))


class _RecurringRt(_PreorderRt):
    def __init__(self, llm, items, detail=None):
        super().__init__(llm, [_offer("poulets", "UNITE", "P1", "A1")], items, "ok")
        self.items, self.detail = items, detail or {}

    async def call_db(self, tool: str, **kw: Any) -> Any:
        if tool == "list_my_recurring_needs":
            self.all_calls.append(tool)
            return {"status": "success", "items": self.items}
        if tool == "get_recurring_need_detail":
            self.all_calls.append(tool)
            return self.detail.get(kw.get("recurring_need_id")) or {"status": "success", "product": "x",
                                                                    "requested_quantity": 0, "unit": "KG",
                                                                    "allocations": []}
        return await super().call_db(tool, **kw)


class Conv:
    def __init__(self, items, detail=None, llm=None):
        self.llm = llm or _ListLLM()
        self.rt = _RecurringRt(self.llm, items, detail)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0
        self.sid = uuid.uuid4().hex  # sids UNIQUES : le cache d'idempotence du NEW_TASK (Redis) contaminerait sinon les cas

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE,
                                "user_role": "BUYER", "message_sid": f"{self.sid}-{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values


def _r(st) -> str:
    return str(st.get("final_response") or "")


_MILK = _need("N-MILK", "lait")
_DETAIL_MATCHED = {
    "status": "success", "occurrence_id": "O-1", "occurrence_version": 4, "product": "lait",
    "requested_quantity": 20, "unit": "L", "occurrence_date": "2026-10-09",
    "allocations": [{"producer_label": "Ferme A", "quantity": 20, "unit_price": 700, "unit": "L"}],
}


@pytest.mark.parametrize("phrase", _PHRASES)
def test_all_six_phrasings_reach_the_list_when_the_llm_classifies_them(phrase: str):
    c = Conv([_MILK])
    st = c.say(phrase)
    assert "list_my_recurring_needs" in c.rt.all_calls
    assert "Lait" in _r(st) and "20.0 L/semaine" in _r(st)  # UX GAP : « 20.0 » (décimale brute)
    assert st.get("pending_interaction", {}).get("kind") == "SELECTION_MENU"
    assert st.get("current_goal") == "GET_MY_NEEDS"  # B13 : le goal reste vivant pour « 1 »


def test_GAP_no_deterministic_route_without_the_llm_the_list_is_unreachable():
    """FEATURE GAP : « mes besoins récurrents » n'a AUCUNE route déterministe — sans LLM (ou LLM en échec) le
    Buyer n'atteint jamais sa liste. Contrairement à « mes commandes » / « mon panier »."""
    c = Conv([_MILK], llm=_ListLLM(classify=False))
    st = c.say("mes besoins récurrents")
    assert "Lait" not in _r(st) and "Vos approvisionnements" not in _r(st)


def test_visibility_does_not_depend_on_cron_state_A_active_need_without_occurrence():
    st = Conv([_MILK]).say("mes besoins récurrents")
    r = _r(st)
    assert "Lait — 20.0 L/semaine — actif" in r
    assert "disponibles demain" not in r  # aucune occurrence : pas de disponibilité affichée


def test_visibility_state_B_open_occurrence_shows_availability_label_hardcoded_demain():
    open_need = _need("N-MILK", "lait", next_occurrence_date="2026-10-09", next_occurrence_id="O-1",
                      requested_quantity=20.0, matched_quantity=0.0, next_occurrence_version=1)
    r = _r(Conv([open_need]).say("mes besoins récurrents"))
    # OBSERVABILITY GAP : « demain » est codé en dur, la date réelle de l'occurrence (2026-10-09) n'est jamais affichée.
    assert "❌ 0/20 L disponibles demain" in r and "2026-10-09" not in r


def test_visibility_state_C_matched_before_digest_proposal_is_visible_and_confirmable_from_detail():
    matched = _need("N-MILK", "lait", next_occurrence_date="2026-10-09", next_occurrence_id="O-1",
                    requested_quantity=20.0, matched_quantity=20.0, next_occurrence_version=4,
                    next_occurrence_notified=False)  # AUCUN digest envoyé
    c = Conv([matched], detail={"N-MILK": _DETAIL_MATCHED})
    assert "✅ 20/20 L disponibles demain" in _r(c.say("mes besoins récurrents"))
    detail = c.say("1")
    assert "Ferme A" in _r(detail) and "700 FCFA/L" in _r(detail) and "1. Confirmer" in _r(detail)
    # La date/version/statut d'occurrence NE sont PAS affichés (GAP) mais la confirmation vise l'identité exacte :
    mapping = detail["working_memory"]["recurring_need_menu"]["mapping"]
    assert mapping["1"] == "CONFIRM:N-MILK|O-1|4"
    assert "2026-10-09" not in _r(detail) and "version" not in _r(detail).lower()


def test_accept_from_detail_works_before_any_digest_with_exact_identity():
    seen: List[Dict[str, Any]] = []
    matched = _need("N-MILK", "lait", next_occurrence_date="2026-10-09", next_occurrence_id="O-1",
                    requested_quantity=20.0, matched_quantity=20.0, next_occurrence_version=4)
    c = Conv([matched], detail={"N-MILK": _DETAIL_MATCHED})
    orig = c.rt.call_db

    async def spy(tool, **kw):
        if tool == "accept_match_proposal":
            seen.append(dict(kw))
            return {"status": "success", "occurrence_id": "O-1", "order_ids": ["ORD-1"], "action": "ACCEPT"}
        return await orig(tool, **kw)

    c.rt.call_db = spy
    c.say("mes besoins récurrents")
    c.say("1")
    st = c.say("1")
    assert seen == [{"phone": _PHONE, "recurring_need_id": "N-MILK", "action": "ACCEPT",
                     "occurrence_id": "O-1", "expected_version": 4}]
    assert "confirmé" in _r(st).lower()


def test_listed_actions_pause_resume_cancel_exist_only_through_update_recurring_need():
    """Le menu de la liste ne propose AUCUNE action pause/reprise/annulation : elles passent par l'intent
    UPDATE_RECURRING_NEED (LLM). UX GAP."""
    r = _r(Conv([_MILK]).say("mes besoins récurrents"))
    assert "pause" not in r.lower() and "annul" not in r.lower() and "reprend" not in r.lower()


def test_cancelled_needs_are_never_listed_status_label_annule_is_dead_code():
    """BUG EXISTANT (mineur) : le service exclut CANCELLED (`status != 'CANCELLED'`) ; la branche « annulé » du
    rendu est donc inatteignable avec le vrai service. Ici on fournit un item CANCELLED pour figer le rendu."""
    r = _r(Conv([_need("N-X", "tomate", status="CANCELLED")]).say("mes besoins récurrents"))
    assert "annulé" in r
