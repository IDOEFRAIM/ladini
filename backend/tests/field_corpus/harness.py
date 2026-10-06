"""Harnais du FIELD CORPUS — le même cas se rejoue avec un LLM SCRIPTÉ (CI, déterministe) ou le VRAI modèle (évaluation manuelle).

Chaque cas : `context` (état métier construit en rejouant de vrais tours), `utterance`, `expect` (effets métier attendus : jamais « le texte exact de la
réponse », mais des invariants — cible choisie, quantité au panier, état préservé, clarification ciblée, pas de menu rejoué…).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import MemorySaver

from ladini.core.get_llm import get_llm
from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from ladini.graphs.agents.market_coach.llm_gateway.gateway import get_llm_gateway
from tests.integration.test_buyer_deterministic_product_switch_e2e import _offer, run
from tests.integration.test_profile_gate_resume_e2e import _PHONE, ProfileRt, _Comp

PROFILE = {"name": "Zouba", "declared_location": "Kadiogo", "buyer": True, "producer": False}


def _tier(tid: str, qty: float, price: float, packaging: str = "sachet") -> Dict[str, Any]:
    return {"tier_id": tid, "quantity": qty, "unit": "L", "price": price, "packaging": packaging, "base_unit_quantity": qty, "min_order_quantity": 1}


def catalog() -> List[Dict[str, Any]]:
    rows = [
        ("Ferme Sawadogo", "Bobo-Dioulasso", 700.0, 300, None),
        ("Moussa", "Ouagadougou", 450.0, 300, None),
        ("GARIKO Leila", "Kadiogo", 1200.0, 900, None),
        ("Gilbert-prod", "Ouagadougou", 500.0, 1200, [_tier("g05", 0.5, 100.0), _tier("g1", 1.0, 190.0)]),
    ]
    offers = []
    for i, (name, zone, price, qty, tiers) in enumerate(rows):
        o = _offer("lait", "LITRE", name, f"L{i}")
        o.update({"price": price, "zone": zone, "available_quantity": qty, "pricing_tiers": tiers})
        offers.append(o)
    return offers


class ScriptedLLM:
    """Joue la COMPRÉHENSION : renvoie ce que le modèle devrait produire pour la phrase (`scripted`) ; le reste (résolveur, domaine, graphe) est réel."""

    def __init__(self, scripted: Dict[str, Dict[str, Any]]) -> None:
        self.scripted = scripted
        self.calls: List[str] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw: Any):
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}\s*$', user, re.S | re.M)
        text = (found[-1].strip() if found else user.strip()).lower()
        body = self.scripted.get(text)
        if "tunnel d'achat" in sysm:
            self.calls.append("structured")
            if body is None:
                return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.2}))
            return _Comp(json.dumps({"confidence": 0.95, **body}))
        if "liste de choix déjà affichée" in sysm:
            self.calls.append("selection")
            return _Comp(json.dumps({"event": "UNKNOWN"} if body is None else {"confidence": 0.95, **body}))
        if "CATALOGUE OFFICIEL" in sysm:
            self.calls.append("new_task")
            nt = (body or {}).get("new_task") or {"disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.9, "entities": {"product": "lait"}}
            return _Comp(json.dumps(nt))
        self.calls.append("other")
        return _Comp(json.dumps({"text": "ok"}))


class _RealRt(ProfileRt):
    @property
    def llm_gateway(self):
        return get_llm_gateway()


@dataclass
class FieldConv:
    mode: str = "scripted"  # "scripted" | "real"
    scripted: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    n: int = 0

    def __post_init__(self) -> None:
        if self.mode == "real":
            self.rt = _RealRt(get_llm(), catalog(), profile=dict(PROFILE))
        else:
            self.llm = ScriptedLLM(self.scripted)
            self.rt = ProfileRt(self.llm, catalog(), profile=dict(PROFILE))
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
                                "message_sid": uuid.uuid4().hex}, self.cfg))
        return self.graph.get_state(self.cfg).values


# ── contextes : états métier réels, atteints en rejouant de vrais tours ───────────────────────────────────────────────
CONTEXTS: Dict[str, List[str]] = {
    "producer_menu": ["Je veux du lait"],
    "tier_menu": ["Je veux du lait", "4"],             # Gilbert-prod (2 conditionnements)
    "quantity_slot": ["Je veux du lait", "2"],         # Moussa (prix plat) -> « Quelle quantité ? »
    "cart_has_item": ["Je veux du lait", "2", "10 litres"],
}


def chosen(st: Dict[str, Any]) -> Optional[str]:
    return ((st.get("vendor_selection_context") or {}).get("chosen_vendor") or {}).get("vendor_name")


def cart(st: Dict[str, Any]) -> List[tuple]:
    return [(i.get("vendor_name"), i.get("quantity")) for i in st.get("active_cart") or []]


def business_snapshot(st: Dict[str, Any]) -> Dict[str, Any]:
    """Ce qui est de l'ÉTAT MÉTIER (pas du texte) : choix, panier, tier résolu, quantité/slots du payload."""
    vc = st.get("vendor_selection_context") or {}
    payload = st.get("transaction_payload") or {}
    return {
        "chosen": chosen(st),
        "cart": cart(st),
        "tier": vc.get("resolved_tier_id"),
        "n_vendors": len(vc.get("vendors") or []),
        "slots": {k: payload.get(k) for k in ("product", "quantity", "unit") if payload.get(k) not in (None, "")},
    }


def reply(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


def is_blind_redisplay(text: str) -> bool:
    return text.lstrip().startswith(("Veuillez choisir une option", "{\"text\"")) or "Répondez uniquement par le numéro" in text


REAL = os.environ.get("FIELD_CORPUS_REAL") == "1"


# ── vendeur : même harnais, graphe PRODUCER, fermes simulées ──────────────────────────────────────────────────────────
SELLER_PROFILE = {"name": "Moussa", "declared_location": "Guiriko", "buyer": False, "producer": True}
_FARM = {"farm_id": "33333333-3333-3333-3333-333333333333", "id": "33333333-3333-3333-3333-333333333333", "name": "Ferme Moussa"}


class _SellerStubMixin:
    async def call_db(self, tool: str, **kw: Any) -> Any:
        if tool in ("get_producer_farm", "get_or_create_farm"):
            self.tool_log.append((tool, dict(kw)))
            return {"status": "success", "data": _FARM, **_FARM}
        return await super().call_db(tool, **kw)  # type: ignore[misc]


class _SellerRt(_SellerStubMixin, ProfileRt):
    pass


class _SellerRealRt(_SellerStubMixin, _RealRt):
    pass


@dataclass
class SellerConv(FieldConv):
    def __post_init__(self) -> None:
        if self.mode == "real":
            self.rt = _SellerRealRt(get_llm(), [], profile=dict(SELLER_PROFILE))
        else:
            self.llm = ScriptedLLM(self.scripted)
            self.rt = _SellerRt(self.llm, [], profile=dict(SELLER_PROFILE))
        self.graph = build_graph("PRODUCER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "PRODUCER",
                                "message_sid": uuid.uuid4().hex}, self.cfg))
        return self.graph.get_state(self.cfg).values


SELLER_CONTEXTS: Dict[str, List[str]] = {
    "seller_recap": ["J'ai 300 kg de tomates à vendre à 250 FCFA/kg"],
    "seller_missing_quantity": ["J'ai des oignons à vendre à 250 FCFA/kg"],
}


def seller_snapshot(st: Dict[str, Any]) -> Dict[str, Any]:
    payload = st.get("transaction_payload") or {}
    return {
        "slots": {k: payload.get(k) for k in ("product", "quantity", "unit", "price") if payload.get(k) not in (None, "")},
        "pending": (st.get("pending_interaction") or {}).get("kind"),
        "missing": list(st.get("missing_fields") or []),
    }


# ── le CONTEXTE doit être atteint avant de juger la phrase (un vrai modèle échoue parfois le tour de mise en place) ─────────
def context_reached(ctx: str, st: Dict[str, Any]) -> bool:
    pending = (st.get("pending_interaction") or {}).get("kind")
    vc = st.get("vendor_selection_context") or {}
    if ctx == "producer_menu":
        return len(vc.get("vendors") or []) > 1 and not vc.get("chosen_vendor")
    if ctx == "tier_menu":
        return bool(vc.get("chosen_vendor")) and bool((st.get("tier_selection_context") or {}).get("tiers"))
    if ctx == "quantity_slot":
        return chosen(st) == "Moussa" and not st.get("active_cart")
    if ctx == "cart_has_item":
        return bool(st.get("active_cart"))
    if ctx == "seller_recap":
        return pending == "CONFIRM_ACTION"
    if ctx == "seller_missing_quantity":
        return pending == "ENTER_FIELD" and "quantity" in (st.get("missing_fields") or [])
    return True
