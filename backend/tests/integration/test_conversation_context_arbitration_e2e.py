"""B20 — ARBITRAGE DU CONTEXTE CONVERSATIONNEL, rejoué sur le VRAI graphe compilé (seuls le LLM et le MCP sont doublés).

Principe : un message est attribué au contexte interactif VIVANT le plus récent, sauf commande explicite d'interruption ;
un menu n'est jamais une prison. Le LLM de ces tests est volontairement HOSTILE (il ne sait classer aucune navigation) :
si un test passe, c'est que la décision est déterministe et contextuelle, pas une chance de classification.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca
from tests.conftest import run
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _PHONE,
    HostileLLM,
    _Comp,
    _offer,
)
from tests.integration.test_buyer_preorder_confirmation_e2e import (  # noqa: F401
    _pin_cod_checkout,
    _PreorderRt,  # noqa: F401  (autouse fixtures)
    _role_hints,
)

ORDERS = {"1": "11111111-aaaa-0000-0000-000000000001", "2": "22222222-bbbb-0000-0000-000000000002",
          "3": "33333333-cccc-0000-0000-000000000003"}
NEED = {"recurring_need_id": "N1", "product": "tomate", "unit": "KG", "status": "ACTIVE", "next_occurrence_id": "occ-1",
        "next_occurrence_date": "2026-10-05", "requested_quantity": 350.0, "matched_quantity": 0.0,
        "next_occurrence_version": 1, "next_occurrence_notified": True}

DIGEST_TEXT = (
    "🌾 Approvisionnement de demain\n\n❌ Tomate : 0/350 KG disponibles\n\nDisponibilité globale : 0/350\n"
    "0 de vos 1 besoins ont une disponibilité complète.\n\n1. Voir les détails\n2. Mes besoins"
)


def digest_outbound(sent_at: Optional[float] = None, *, ids=("N1",)) -> Dict[str, Any]:
    return {"template_key": "RECURRING_SUPPLY_DIGEST_BUYER", "owner_type": "RECURRING_DIGEST",
            "sent_at": time.time() if sent_at is None else sent_at, "menu_id": "sig-1",
            "actions": {"1": "VIEW_DETAILS", "2": "MY_NEEDS"}, "recurring_need_ids": list(ids), "occurrence_ids": ["occ-1"]}


def reception_outbound(sent_at: Optional[float] = None) -> Dict[str, Any]:
    return {"template_key": "RECURRING_SUPPLY_ORDER_DELIVERED_BUYER", "owner_type": "ORDER_RECEPTION",
            "sent_at": time.time() if sent_at is None else sent_at, "menu_id": None, "actions": None,
            "recurring_need_ids": [], "occurrence_ids": []}


class NavBlindLLM(HostileLLM):
    """NEW_TASK : sait seulement « acheter du <produit> » ; ne sait RIEN classer d'autre (navigation => UNKNOWN)."""

    def create(self, **kw: Any):
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}', user, re.S)
        text = (found[-1].strip() if found else "").lower()
        if "CATALOGUE OFFICIEL" in sysm:
            m = re.search(r"acheter\s+(?:du|des|de la|de l')?\s*(\w+)", text)
            if m:
                return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.95,
                                         "entities": {"product": m.group(1)}}))
            return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.1}))
        if "liste de choix" in sysm:  # micro-prompt SELECTION : un chiffre désigne l'option, « annuler » interrompt, le reste UNKNOWN
            if text.isdigit():
                return _Comp(json.dumps({"event": "SELECTION", "selection_index": int(text), "selected_value": None}))
            if text in {"annuler", "annule"}:
                return _Comp(json.dumps({"event": "INTERRUPTION", "selection_index": None, "selected_value": None}))
            return _Comp(json.dumps({"event": "UNKNOWN", "selection_index": None, "selected_value": None}))
        if "À L'INTÉRIEUR d'une transaction" in sysm:
            return _Comp(json.dumps({"disposition": "UNKNOWN", "extracted_entities": {}, "confidence": 0.1}))
        return _Comp("{not json")


class Rt(_PreorderRt):
    def __init__(self, llm: Any, offers: List[Dict[str, Any]]) -> None:
        super().__init__(llm, offers, [NEED], "ok")
        self.outbound: Optional[Dict[str, Any]] = None
        self.order_detail_calls: List[Dict[str, Any]] = []

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.all_calls.append(tool)
        if tool == "get_last_interactive_outbound":
            return {"status": "success", "interactive": self.outbound}
        if tool == "get_buyer_orders_dashboard":
            txt = ("📦 *SUIVI DE VOS COMMANDES :*\n\n*1. Commande #11111111*\n*2. Commande #22222222*\n"
                   "*3. Commande #33333333*")
            return {"status": "success", "formatted_menu": txt, "mapping": dict(ORDERS)}
        if tool == "get_recurring_need_detail":
            return {"status": "success", "product": "tomate", "requested_quantity": 350.0, "unit": "KG",
                    "allocations": [], "occurrence_id": "occ-1", "occurrence_version": 1}
        if tool == "get_transaction_summary":
            self.order_detail_calls.append(dict(kw))
            return {"status": "success", "order_id": str(kw.get("order_id") or ""), "status_label": "CONFIRMED",
                    "data": {"order_id": str(kw.get("order_id") or ""), "status": "CONFIRMED", "items": []}}
        return await super().call_db(tool, **kw)


class Conv:
    def __init__(self, offers: Optional[List[Dict[str, Any]]] = None, outbound: Optional[Dict[str, Any]] = None) -> None:
        self.llm = NavBlindLLM(None, True)
        self.rt = Rt(self.llm, offers if offers is not None else [_offer("lait", "LITRE", f"Ferme{i}", f"L{i}") for i in range(3)])
        self.rt.outbound = outbound
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0
        self.tools: List[str] = []

    def say(self, text: str, **extra: Any) -> Dict[str, Any]:
        self.n += 1
        self.rt.all_calls.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
                                "message_sid": f"m{self.n}", **extra}, self.cfg))
        self.tools = list(self.rt.all_calls)
        return self.graph.get_state(self.cfg).values

    def state(self) -> Dict[str, Any]:
        return self.graph.get_state(self.cfg).values


def _r(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


def _is_orders_list(st: Dict[str, Any]) -> bool:
    return "SUIVI DE VOS COMMANDES" in _r(st)


def _is_needs(st: Dict[str, Any]) -> bool:
    r = _r(st)
    return "Tomate" in r or "tomate" in r.lower() and "COMMANDES" not in r


def _chosen_order(c: Conv) -> Optional[str]:
    """Commande désignée par la dernière réponse chiffrée (résolue par le pipeline via `available_mapping`)."""
    return (c.state().get("transaction_payload") or {}).get("order_id")


def _pending_kind(st: Dict[str, Any]) -> str:
    return get_pending_interaction(st).kind.value


# ══════════════════════ le digest (message PROACTIF : aucun état) et sa réponse ══════════════════════
@pytest.mark.parametrize("reply", ["voir les détails", "voir les details", "détails", "details", "1"])
def test_digest_menu_view_details_reply_never_goes_to_orders(reply):
    c = Conv(outbound=digest_outbound())
    st = c.say(reply)
    assert not _is_orders_list(st), _r(st)
    assert "get_buyer_orders_dashboard" not in c.tools
    assert "get_recurring_need_detail" in c.tools  # un seul besoin notifié -> son détail
    assert "Tomate" in _r(st) and "350" in _r(st), _r(st)


@pytest.mark.parametrize("reply", ["mes besoins", "voir mes besoins", "2"])
def test_digest_menu_my_needs_reply_lists_needs_without_llm(reply):
    c = Conv(outbound=digest_outbound())
    st = c.say(reply)
    assert not _is_orders_list(st), _r(st)
    assert "list_my_recurring_needs" in c.tools and "get_buyer_orders_dashboard" not in c.tools
    assert "tomate" in _r(st).lower(), _r(st)


def test_digest_menu_with_several_needs_view_details_shows_the_needs_list():
    c = Conv(outbound=digest_outbound(ids=("N1", "N2")))
    st = c.say("voir les détails")
    assert "list_my_recurring_needs" in c.tools and "get_recurring_need_detail" not in c.tools
    assert not _is_orders_list(st)


def test_same_words_without_a_digest_menu_do_not_hijack():
    """« 1 »/« voir les détails » ne sont attribués au digest QUE s'il existe un digest interactif récent."""
    c = Conv(outbound=None)
    st = c.say("voir les détails")
    assert "get_recurring_need_detail" not in c.tools
    assert not _is_needs(st) or "Tomate" not in _r(st)


# ══════════════════════ menu de commandes : numéro, interruption, annulation ══════════════════════
def _orders_menu(c: Conv) -> Dict[str, Any]:
    st = c.say("mes commandes")
    assert _is_orders_list(st) and _pending_kind(st) == InteractionKind.SELECTION_MENU.value, _r(st)
    return st


def test_orders_menu_numeric_selection_still_opens_that_order():
    c = Conv()
    _orders_menu(c)
    c.say("2")
    assert _chosen_order(c) == ORDERS["2"]  # « 2 » appartient au menu de commandes encore vivant


def test_orders_menu_then_mes_besoins_leaves_the_menu_and_lists_needs():
    c = Conv()
    _orders_menu(c)
    st = c.say("mes besoins")
    assert "list_my_recurring_needs" in c.tools
    assert "Veuillez choisir" not in _r(st) and not _is_orders_list(st), _r(st)
    assert "tomate" in _r(st).lower()


def test_orders_menu_then_new_purchase_starts_the_purchase_flow():
    c = Conv()
    _orders_menu(c)
    st = c.say("je veux acheter du lait")
    assert "search_products" in c.tools, c.tools
    assert "Veuillez choisir une option" not in _r(st) and "SUIVI DE VOS COMMANDES" not in _r(st), _r(st)


def test_orders_menu_then_cancel_leaves_cleanly_and_no_stale_menu_remains():
    c = Conv()
    _orders_menu(c)
    st = c.say("annuler")
    assert _pending_kind(st) != InteractionKind.SELECTION_MENU.value, _r(st)
    assert not (c.state().get("available_mapping") or {}), "le menu annulé ne laisse aucun mapping"
    c.say("2")  # le menu n'existe plus : « 2 » ne désigne plus la commande #2
    assert _chosen_order(c) != ORDERS["2"]


def test_orders_menu_with_unrelated_text_keeps_the_menu_contract_without_hijack():
    c = Conv()
    _orders_menu(c)
    c.say("blablabla")
    assert _chosen_order(c) is None
    assert "list_my_recurring_needs" not in c.tools


# ══════════════════════ message sortant interactif vs informationnel ══════════════════════
def test_newer_interactive_outbound_supersedes_the_old_orders_menu():
    c = Conv()
    _orders_menu(c)
    c.rt.outbound = reception_outbound(sent_at=time.time() + 5)  # « tout est-il bon ? » envoyé APRÈS le menu
    st = c.say("YUP")
    assert "Veuillez choisir" not in _r(st) and "Je vois que vous avez dit" not in _r(st), _r(st)
    assert not c.rt.order_detail_calls
    assert _pending_kind(st) != InteractionKind.SELECTION_MENU.value or "SUIVI DE VOS COMMANDES" not in _r(st)


def test_informational_outbound_does_not_supersede_the_menu():
    """Une notification purement informative n'est jamais retournée par `get_last_interactive_outbound` : le menu
    reste propriétaire d'un chiffre nu."""
    c = Conv()
    _orders_menu(c)
    c.rt.outbound = None  # « Votre commande a été mise à jour. » : template non interactif -> aucun contexte sortant
    c.say("3")
    assert _chosen_order(c) == ORDERS["3"]


def test_older_interactive_outbound_does_not_override_a_newer_menu():
    c = Conv(outbound=digest_outbound(sent_at=time.time() - 3600))
    _orders_menu(c)  # le menu de commandes est plus récent que le digest
    c.say("2")
    assert _chosen_order(c) == ORDERS["2"]
    assert "get_recurring_need_detail" not in c.tools


def test_digest_newer_than_orders_menu_owns_the_numeric_reply():
    c = Conv()
    _orders_menu(c)
    c.rt.outbound = digest_outbound(sent_at=time.time() + 5)
    st = c.say("1")  # « 1 » = Voir les détails du digest, pas la commande #1
    assert _chosen_order(c) != ORDERS["1"]
    assert "get_recurring_need_detail" in c.tools and "Tomate" in _r(st)


# ══════════════════════ TTL, menu fantôme, working_memory ══════════════════════
def test_expired_menu_ttl_cannot_trap_the_user():
    c = Conv()
    _orders_menu(c)
    snap = c.state()
    pend = dict(snap["pending_interaction"])
    pend["created_at"] = time.time() - 10_000  # au-delà de PENDING_INTERACTION_TTL_SECONDS
    c.graph.update_state(c.cfg, {"pending_interaction": pend})
    st = c.say("mes besoins")
    assert "list_my_recurring_needs" in c.tools and "Veuillez choisir" not in _r(st)


def test_ghost_menu_without_pending_never_resurrects():
    c = Conv()
    _orders_menu(c)
    c.graph.update_state(c.cfg, {"pending_interaction": None})  # pending consommé/expiré, mapping resté
    assert c.state().get("available_mapping")
    c.say("2")
    assert _chosen_order(c) != ORDERS["2"], "un menu fantôme ne doit plus désigner la commande #2"


def test_stale_working_memory_menu_cannot_resurrect_after_interruption():
    c = Conv()
    c.say("mes besoins")
    c.say("mes commandes")  # interrompt le menu des besoins (working_memory.recurring_need_menu)
    wm = c.state().get("working_memory") or {}
    assert "recurring_need_menu" not in wm, wm.keys()


# ══════════════════════ menus successifs : le plus récent gagne ══════════════════════
def test_three_successive_menus_newest_wins():
    c = Conv()
    c.say("mes besoins")  # menu A (besoins)
    _orders_menu(c)  # menu B (commandes) remplace A
    st = c.say("je veux acheter du lait")  # tunnel producteurs : menu C
    assert _pending_kind(st) in {InteractionKind.SELECT_PRODUCER.value, InteractionKind.SELECTION_MENU.value} or "Ferme" in _r(st)
    c.say("2")
    assert _chosen_order(c) != ORDERS["2"], "« 2 » vise le menu le plus récent (producteurs), pas les commandes"


# ══════════════════════ parcours complet (cas prod) ══════════════════════
def test_exact_production_replay_digest_details_orders_needs_purchase_orders():
    c = Conv(outbound=digest_outbound())
    st = c.say("voir les details")  # ← avant B20 : liste de commandes
    assert not _is_orders_list(st) and "Tomate" in _r(st), _r(st)

    st = c.say("mes commandes")
    assert _is_orders_list(st), _r(st)

    st = c.say("mes besoins")  # ← avant B20 : « Veuillez choisir une option : 1. Commande ... »
    assert "Veuillez choisir" not in _r(st) and not _is_orders_list(st) and "list_my_recurring_needs" in c.tools, _r(st)

    st = c.say("je veux acheter du lait")
    assert "search_products" in c.tools and "SUIVI DE VOS COMMANDES" not in _r(st), _r(st)
    assert "Ferme0" in _r(st) or "Ferme" in _r(st) or "quantité" in _r(st).lower(), _r(st)

    st = c.say("mes commandes")  # menu producteurs actif -> sortie vers les commandes
    assert _is_orders_list(st), _r(st)
    assert "get_buyer_orders_dashboard" in c.tools


# ══════════════════════ non-régression des vrais tunnels ══════════════════════
def test_quantity_slot_number_is_still_a_quantity():
    c = Conv(offers=[_offer("lait", "LITRE", "Ferme0", "L0")])
    st = c.say("je veux acheter du lait")
    assert "ENTER_QUANTITY" in _pending_kind(st) or "quantité" in _r(st).lower(), _r(st)
    st = c.say("3")
    assert st.get("active_cart") or "3" in _r(st), _r(st)


def test_digit_during_data_slot_is_not_hijacked_by_a_digest_menu():
    c = Conv(offers=[_offer("lait", "LITRE", "Ferme0", "L0")])
    st = c.say("je veux acheter du lait")
    assert "QUANTITY" in _pending_kind(st), _pending_kind(st)
    c.rt.outbound = digest_outbound(sent_at=time.time() + 5)
    c.say("2")  # un CHIFFRE pendant un slot de données reste la quantité
    assert "get_recurring_need_detail" not in c.tools and "list_my_recurring_needs" not in c.tools


# ══════════════════════ unité : la primitive pure ══════════════════════
class _S(dict):
    pass


def _state(kind: Optional[str] = None, created_at: Optional[float] = None, **more: Any) -> Dict[str, Any]:
    st: Dict[str, Any] = dict(more)
    if kind:
        st["pending_interaction"] = {"kind": kind, "created_at": created_at if created_at is not None else time.time(),
                                     "goal": "X", "candidates": [], "status": "ACTIVE"}
    return st


def test_unit_arbitration_decisions():
    now = time.time()
    out = ca.InteractiveOutbound("RECURRING_DIGEST", now, "sig", {"1": "VIEW_DETAILS", "2": "MY_NEEDS"}, ("N1",), ("o1",))
    d = ca.resolve_conversation_context({}, "voir les détails", role="BUYER", outbound=out)
    assert d.kind == ca.ArbitrationKind.ACTIVE_MENU_ACTION and d.raw["detected_intent"] == "GET_MY_NEEDS"
    assert d.raw["extracted_entities"]["digest_action"] == "VIEW_DETAILS"
    assert ca.resolve_conversation_context({}, "2", role="BUYER", outbound=out).raw["extracted_entities"]["digest_action"] == "MY_NEEDS"
    # pas de contexte sortant -> la même phrase n'est attribuée à personne
    assert ca.resolve_conversation_context({}, "voir les détails", role="BUYER").kind == ca.ArbitrationKind.GENERIC_CLASSIFICATION
    # navigation explicite contre un menu
    menu = _state("SELECTION_MENU", now - 5, available_mapping={"1": "a"}, expected_candidates=["Commande"])
    nav = ca.resolve_conversation_context(menu, "Mes besoins", role="BUYER")
    assert nav.kind == ca.ArbitrationKind.INTERRUPT_WITH_NEW_GOAL and nav.raw["detected_intent"] == "GET_MY_NEEDS" and nav.purge
    assert ca.resolve_conversation_context(menu, "mes commandes", role="BUYER").raw["detected_intent"] == "BUYER_LIST_ORDERS"
    # un producteur n'a pas la navigation acheteur
    assert ca.resolve_conversation_context(menu, "mes besoins", role="PRODUCER").kind == ca.ArbitrationKind.ACTIVE_SLOT
    # chiffre nu + menu vivant sans sortant plus récent : le menu garde la main
    assert ca.resolve_conversation_context(menu, "2", role="BUYER").kind == ca.ArbitrationKind.ACTIVE_SLOT
    # nouvelle demande explicite contre un menu générique
    buy = ca.resolve_conversation_context(menu, "je veux acheter du lait", role="BUYER")
    assert buy.kind == ca.ArbitrationKind.INTERRUPT_WITH_NEW_GOAL and buy.reclassify
    # sortant interactif plus récent qu'un menu : supersede (texte non-chiffre)
    newer = ca.InteractiveOutbound("ORDER_RECEPTION", now + 1)
    sup = ca.resolve_conversation_context(menu, "YUP", role="BUYER", outbound=newer)
    assert sup.kind == ca.ArbitrationKind.SUPERSEDE_STALE_CONTEXT and sup.reclassify
    # slot de données : un chiffre n'est jamais détourné par un menu sortant
    slot = _state("ENTER_QUANTITY", now - 5)
    assert ca.resolve_conversation_context(slot, "2", role="BUYER", outbound=ca.InteractiveOutbound(
        "RECURRING_DIGEST", now + 1, "s", {"1": "VIEW_DETAILS", "2": "MY_NEEDS"})).kind == ca.ArbitrationKind.ACTIVE_SLOT
    # menu fantôme
    ghost = ca.resolve_conversation_context({"available_mapping": {"1": "x"}}, "bonjour", role="BUYER")
    assert ghost.kind == ca.ArbitrationKind.SUPERSEDE_STALE_CONTEXT and ghost.reason == "ghost_menu_without_live_pending"
