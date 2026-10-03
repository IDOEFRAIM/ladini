"""B11.1 (2026-10-03) — la clôture producteur est ATTEIGNABLE depuis une vraie conversation.

Bug prod : « mes commandes » -> liste avec #11DE2D1B (CONFIRMED) ; « commande #11DE2D1B livrée »
-> « Je n'ai pas bien compris » (le LLM renvoyait UNKNOWN : NewTaskEntities interdit tout identifiant
technique). Le routage est désormais déterministe (aucun LLM) pour une déclaration explicite.

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés. Le LLM est HOSTILE (UNKNOWN)
sur tout sauf « mes commandes », pour prouver que le routage ne dépend pas de lui.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from tests.integration.test_buyer_deterministic_product_switch_e2e import _Comp
from tests.integration.test_producer_order_completion_e2e import (
    LLM,
    ORD1,
    ORD2,
    ORD3,
    ORD4,
    Conv,
    _order,
    _resp,
)

_GOAL = "PRODUCER_CONFIRM_DELIVERY_PAYMENT"
_GENERIC = "pas bien compris"


class HostileExceptList(LLM):
    """UNKNOWN partout, sauf « mes commandes » -> BUYER_LIST_ORDERS (comme en prod)."""

    def __init__(self, mode: str = "unknown") -> None:
        super().__init__(mode)

    def create(self, **kw: Any):
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}', user, re.S)
        text = (found[-1].strip() if found else "").lower()
        if "CATALOGUE OFFICIEL" in sysm and text == "mes commandes":
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "BUYER_LIST_ORDERS",
                                     "confidence": 0.9, "entities": {}}))
        return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.2}))


def _conv(orders, mode: str = "unknown") -> Conv:
    return Conv(orders, llm=HostileExceptList(mode))


@pytest.mark.parametrize("msg", [
    "commande #11DE2D1B livrée",
    "j'ai livré la commande #11DE2D1B",
    "commande 11DE2D1B livrée",
    "#11DE2D1B livrée",
    "la commande #11DE2D1B est livrée",
    "commande #11de2d1b livrée",
])
def test_explicit_reference_routes_without_llm(msg: str):
    c = _conv([_order(ORD1, "11DE2D1B")])
    st = c.say(msg)
    assert st.get("current_goal") == _GOAL
    assert _GENERIC not in _resp(st)
    assert "11DE2D1B" in _resp(st) and "oignons" in _resp(st)
    assert c.rt.closures == []  # aucune mutation avant « oui »


def test_exact_prod_replay_mes_commandes_then_explicit_delivery_then_yes():
    c = _conv([_order(ORD1, "11DE2D1B")])
    st = c.say("mes commandes")
    assert "11DE2D1B" in _resp(st) and "commande #11DE2D1B livrée" in _resp(st)  # découvrable
    st = c.say("commande #11DE2D1B livrée")
    assert st.get("current_goal") == _GOAL and _GENERIC not in _resp(st) and "Confirmez" in _resp(st)
    assert c.rt.closures == []
    c.say("oui")
    o = c.rt.orders[0]
    assert (o["status"], o["payment_status"], o["delivery_status"]) == ("COMPLETED", "PAID", "DELIVERED")
    st = c.say("mes commandes")
    assert "restent à livrer" not in _resp(st)  # plus présentée comme simple « à livrer »
    assert "Aucune commande en attente" in _resp(st) or "✅" in _resp(st)


def test_mes_commandes_shows_closing_instruction():
    c = _conv([_order(ORD1, "11DE2D1B")])
    r = _resp(c.say("mes commandes"))
    assert "livrée" in r and "paiement" in r.lower() and "annuler" in r.lower()


def test_no_reference_single_eligible_order_goes_to_recap():
    c = _conv([_order(ORD1, "11DE2D1B")])
    for phrase in ("commande livrée", "j'ai livré", "le client a reçu la commande"):
        c = _conv([_order(ORD1, "11DE2D1B")])
        st = c.say(phrase)
        assert st.get("current_goal") == _GOAL, phrase
        assert "11DE2D1B" in _resp(st) and c.rt.closures == []


def test_no_reference_multiple_orders_menu_then_numeric_selection_then_yes():
    c = _conv([_order(ORD1, "11DE2D1B"), _order(ORD2, "22AA2D1B", "Moussa")])
    st = c.say("commande livrée")
    assert "1." in _resp(st) and "2." in _resp(st) and c.rt.closures == []
    st = c.say("2")
    assert "22AA2D1B" in _resp(st) and c.rt.closures == []
    c.say("oui")
    assert c.rt.closures == [ORD2]


def test_no_reference_zero_eligible_gives_clear_message():
    c = _conv([_order(ORD4, "44CC2D1B", status="COMPLETED", payment_status="PAID", delivery_status="DELIVERED")])
    st = c.say("commande livrée")
    assert _GENERIC not in _resp(st) and "Aucune commande" in _resp(st)


def test_invalid_reference():
    c = _conv([_order(ORD1, "11DE2D1B")])
    st = c.say("commande #ABC99999 livrée")
    assert "Je ne trouve pas cette commande parmi vos commandes actives." in _resp(st)
    assert c.rt.closures == []


def test_foreign_reference_gives_same_message():
    c = _conv([_order(ORD1, "11DE2D1B")])  # la commande d'un autre producteur n'est pas listée
    st = c.say("commande #77FFAA11 livrée")
    assert "Je ne trouve pas cette commande parmi vos commandes actives." in _resp(st)
    assert "77FFAA11" not in _resp(st)


def test_completed_and_cancelled_references():
    c = _conv([_order(ORD4, "44CC2D1B", status="COMPLETED", payment_status="PAID", delivery_status="DELIVERED"),
               _order(ORD3, "33BB2D1B", status="CANCELLED", payment_status="CANCELLED")])
    assert "déjà clôturée" in _resp(c.say("commande #44CC2D1B livrée"))
    assert "annulée" in _resp(c.say("commande #33BB2D1B livrée"))
    assert c.rt.closures == []


@pytest.mark.parametrize("answer", ["non", "annuler"])
def test_no_or_cancel_during_recap_never_mutates(answer: str):
    c = _conv([_order(ORD1, "11DE2D1B")])
    c.say("commande #11DE2D1B livrée")
    c.say(answer)
    assert c.rt.closures == [] and c.rt.orders[0]["status"] == "CONFIRMED"


@pytest.mark.parametrize("mode", ["unknown", "invalid_json", "exception"])
def test_hostile_llm_modes(mode: str):
    c = _conv([_order(ORD1, "11DE2D1B")], mode)
    c.llm = LLM(mode)  # LLM hostile pour TOUS les tours, y compris la liste
    c.rt.llm = c.llm
    st = c.say("commande #11DE2D1B livrée")
    assert st.get("current_goal") == _GOAL
    c.say("oui")
    assert c.rt.closures == [ORD1]


def test_no_llm_at_all():
    c = _conv([_order(ORD1, "11DE2D1B")])
    c.rt.llm = None
    st = c.say("commande #11DE2D1B livrée")
    assert st.get("current_goal") == _GOAL and _GENERIC not in _resp(st)
    c.say("oui")
    assert c.rt.closures == [ORD1]


@pytest.mark.parametrize("msg", [
    "je cherche une livraison de tomates",
    "quand sera livrée ma commande ?",
    "ma commande a-t-elle été livrée ?",
    "ma commande #11DE2D1B n'est pas livrée",
])
def test_questions_and_other_intents_are_not_hijacked(msg: str):
    c = _conv([_order(ORD1, "11DE2D1B")])
    st = c.say(msg)
    assert st.get("current_goal") != _GOAL
    assert c.rt.closures == []
