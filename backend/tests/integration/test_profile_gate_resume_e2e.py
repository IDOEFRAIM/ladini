"""Onboarding progressif V2.1 — le profile gate est une BARRIÈRE PAUSE/REPRISE, jamais RESET/RECONSTRUCTION.

Transcript WhatsApp réel rejoué sur le VRAI graphe compilé (seuls le LLM et le MCP sont doublés) :

    U: Je veux commander du lait            A: Dans quelle région souhaites-tu être livré ?
    U: A ouagadougou                        A: (catalogue lait — jamais « C'est noté, que voulez-vous faire ? »)
    U: le quatrième m'intéresse bien        A: (producteur n°4 sélectionné)
    U: celui de 0.5 l  /  je propose 5      A: panier 5 × sachet 0,5 L × 100 FCFA = 500 FCFA
    U: ok                                   A: Avant de confirmer… quel nom dois-je utiliser ?
    U: Restau chez zouba                    A: récapitulatif de la précommande — mêmes produit/producteur/quantité/total

Invariant : BUSINESS_STATE_BEFORE_PROFILE_GATE == BUSINESS_STATE_AFTER_PROFILE_GATE (hors nom/région/capacités).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.conftest import run
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _PHONE,
    _Comp,
    _offer,
    _Rt,
)
from tests.integration.test_buyer_preorder_confirmation_e2e import (  # noqa: F401
    _pin_cod_checkout,
    _role_hints,
)

# ── Catalogue : 8 producteurs de lait, le n°4 (« Gilbert-prod ») vend en sachets de 0,5 L à 100 FCFA ──────────────────


def _catalog() -> List[Dict[str, Any]]:
    offers = [_offer("lait", "LITRE", f"Ferme{i}", f"L{i}") for i in range(8)]
    offers[3]["vendor_name"] = "Gilbert-prod"
    offers[3]["vendor"] = {"name": "Gilbert-prod"}
    offers[3]["pricing_tiers"] = [
        {"tier_id": "t05", "quantity": 0.5, "unit": "L", "price": 100.0, "packaging": "sachet",
         "base_unit_quantity": 0.5, "min_order_quantity": 1},
        {"tier_id": "t1", "quantity": 1.0, "unit": "L", "price": 190.0, "packaging": "sachet",
         "base_unit_quantity": 1.0, "min_order_quantity": 1},
    ]
    return offers


class ProfileRt(_Rt):
    """MCP simulé : un CONTACT (nom de repli `User_2876`, aucune région) qui s'enrichit via `complete_user_profile`."""

    def __init__(self, llm: Any, offers: List[Dict[str, Any]], *, profile: Dict[str, Any] | None = None) -> None:
        super().__init__(llm, offers)
        self.profile: Dict[str, Any] = profile if profile is not None else {"name": None, "declared_location": None, "buyer": False, "producer": False}
        self.completions: List[Dict[str, Any]] = []
        self.all_calls: List[str] = []

    def _profile_payload(self) -> Dict[str, Any]:
        p = self.profile
        return {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": p.get("name") or "User_2876",  # exactement ce que sérialise le backend pour un contact sans nom
            "phone": _PHONE,
            "role": "USER",
            "zone": {"id": None, "name": "Zone inconnue"},
            "declared_location": p.get("declared_location"),
            "permissions": {"can_buy": bool(p.get("buyer")), "can_sell": bool(p.get("producer")), "is_admin": False},
            "status": {"producer": "PENDING" if p.get("producer") else None, "identity_verified": False},
        }

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.all_calls.append(tool)
        if tool == "get_user_by_phone":
            self.tool_log.append((tool, dict(kw)))
            return {"status": "success", "data": self._profile_payload()}
        if tool == "complete_user_profile":
            self.tool_log.append((tool, dict(kw)))
            self.completions.append(dict(kw))
            for src, dst in (("name", "name"), ("declared_location", "declared_location")):
                if kw.get(src):
                    self.profile[dst] = kw[src]
            cap = str(kw.get("capability") or "").upper()
            if cap == "BUY":
                self.profile["buyer"] = True
            if cap == "SELL":
                self.profile["producer"] = True
            return {"status": "success", "data": {"id": "22222222-2222-2222-2222-222222222222"}}
        if tool == "create_preorder_draft":
            self.tool_log.append((tool, dict(kw)))
            items = kw.get("cart_items") or []
            total = sum(float(i.get("price") or 0) * float(i.get("quantity") or 0) for i in items)
            return {"status": "success", "preorder_id": "ORD-1", "total_amount": total, "currency": "XOF", "unresolved_items": [],
                    "items": [{"product_id": i.get("product_id"), "name": i.get("name") or "x", "quantity": i.get("quantity"),
                               "unit": i.get("unit") or "UNITE", "price": i.get("price"),
                               "line_total": float(i.get("price") or 0) * float(i.get("quantity") or 0),
                               "producer_id": i.get("producer_id"), "tier_id": i.get("tier_id")} for i in items]}
        return await super().call_db(tool, **kw)


# ── LLM scripté : comprend la demande d'achat, répond comme le VRAI extracteur d'inscription (ancien) quand on l'appelle ──

def _structured_answer(text: str) -> Dict[str, Any]:
    """Ce que répond le micro-prompt STRUCTURED_ACTION en production : un index humain, jamais un identifiant."""
    if "quatri" in text or text == "4":
        return {"disposition": "ACTION", "action": "SELECT_PRODUCER", "selection_index": 4, "confidence": 0.95}
    if "0.5" in text:
        return {"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 1, "confidence": 0.95}
    if text.startswith("je propose"):
        return {"disposition": "ACTION", "action": "SET_PACKAGE_COUNT", "package_count": 5, "confidence": 0.95}
    return {"disposition": "UNKNOWN", "confidence": 0.2}


class GateLLM:
    def __init__(self, *, name_answers: Dict[str, Any] | None = None) -> None:
        self.calls: List[str] = []
        #: réponses du micro-prompt « slot de profil » (V2.1) par message : {"value", "kind"}.
        self.name_answers = name_answers or {}

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
        if "SLOT DE PROFIL" in sysm:  # extracteur de slot de profil (V2.1) — réponse structurée, jamais une réplique
            self.calls.append("profile_slot")
            answer = self.name_answers.get(user.strip().lower()) or self.name_answers.get(text) or {"value": None, "kind": "UNCLEAR"}
            return _Comp(json.dumps(answer))
        if "inscription Ladini" in sysm:  # ANCIEN extracteur : il « répond » lui-même avec le discours de création de compte
            self.calls.append("legacy_onboarding_extract")
            return _Comp(json.dumps({"role": None, "name": None, "zone": None, "confirm": None, "is_question": True,
                                     "reply": "Bonjour ! Sur Ladini, on vous aide à créer un compte. Pour l'inscription, je dois d'abord connaître votre nom."}))
        if "liste de choix déjà affichée" in sysm:  # micro-prompt SELECTION : l'index HUMAIN lu dans le message
            self.calls.append("selection")
            if "quatri" in text:
                return _Comp(json.dumps({"event": "SELECTION", "selection_index": 4, "confidence": 0.95}))
            return _Comp(json.dumps({"event": "UNKNOWN"}))
        if "tunnel d'achat" in sysm:
            self.calls.append("structured")
            return _Comp(json.dumps(_structured_answer(text)))
        if "À L'INTÉRIEUR d'une transaction" in sysm:
            self.calls.append("active_slot")
            return _Comp(json.dumps({"disposition": "UNKNOWN", "extracted_entities": {}, "confidence": 0.1}))
        if "CATALOGUE OFFICIEL" in sysm:
            self.calls.append("new_task")
            if text in {"ok", "okay", "oui"}:
                return _Comp(json.dumps({"disposition": "CONFIRM", "confidence": 0.95}))
            ents: Dict[str, Any] = {"product": "lait"}
            if "5 l" in text:
                ents.update({"quantity": 5, "unit": "L"})
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.9, "entities": ents}))
        self.calls.append("other")
        self.others = getattr(self, "others", []) + [(sysm[:200], text)]
        return _Comp(json.dumps({"text": "ok"}))


class GateConv:
    def __init__(self, *, llm: GateLLM | None = None, profile: Dict[str, Any] | None = None) -> None:
        self.llm = llm or GateLLM()
        self.rt = ProfileRt(self.llm, _catalog(), profile=profile)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "gate"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
                                "message_sid": f"m{self.n}"}, self.cfg))
        return self.state()

    def state(self) -> Dict[str, Any]:
        return self.graph.get_state(self.cfg).values

    def tools(self) -> List[str]:
        return [t for t, _ in self.rt.tool_log if t not in ("get_account_status", "get_prohibited_terms", "get_last_interactive_outbound")]


def _reply(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


_LEGACY_COPY = ("créer un compte", "pour l'inscription", "on vous aide")
_NO_PLACEHOLDER = re.compile(r"(User|Utilisateur|Contact)_\d+")


def _assert_no_placeholder_and_no_legacy(st: Dict[str, Any]) -> None:
    text = _reply(st)
    assert not _NO_PLACEHOLDER.search(text), f"identifiant technique affiché à l'utilisateur : {text!r}"
    assert not any(c in text.lower() for c in _LEGACY_COPY), f"discours d'inscription historique : {text!r}"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1. RÉGION : la demande d'achat reprend dans le MÊME tour
# ═══════════════════════════════════════════════════════════════════════════════════════════════════


class TestRegionPauseResume:
    def test_the_buy_request_resumes_after_the_region_without_being_repeated(self):
        c = GateConv()
        st = c.say("Je veux commander du lait")
        assert "région" in _reply(st).lower() and "livré" in _reply(st).lower()
        assert (st.get("profile_gate") or {}).get("goal") == "BUYER_REQUEST"
        assert "search_products" not in c.tools(), "rien n'est exécuté tant que la région manque"

        st = c.say("A ouagadougou")
        assert c.rt.profile["declared_location"] == "Kadiogo"  # canonique : Ouagadougou -> Kadiogo
        assert "search_products" in c.tools(), "la RECHERCHE de lait reprend automatiquement dans le même tour"
        assert "*1.*" in _reply(st) and "Ferme0" in _reply(st), "le catalogue des producteurs est affiché"
        assert "Dites-moi ce que vous souhaitez faire" not in _reply(st) and "C'est noté" not in _reply(st)
        assert not st.get("profile_gate")
        _assert_no_placeholder_and_no_legacy(st)

    def test_no_placeholder_is_ever_shown_even_while_collecting(self):
        c = GateConv()
        for text in ("Je veux commander du lait", "A ouagadougou"):
            _assert_no_placeholder_and_no_legacy(c.say(text))


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 2. NOM : la précommande reprend avec EXACTEMENT le même panier
# ═══════════════════════════════════════════════════════════════════════════════════════════════════


def _ready_cart(c: GateConv) -> Dict[str, Any]:
    c.say("Je veux commander du lait")
    c.say("A ouagadougou")
    c.say("le quatrième m'intéresse")
    c.say("celui de 0.5 l")
    st = c.say("je propose 5")
    assert [(x["quantity"], x["vendor_name"]) for x in st["active_cart"]] == [(5, "Gilbert-prod")], st["active_cart"]
    return st


def _business_snapshot(st: Dict[str, Any]) -> Dict[str, Any]:
    cart = st["active_cart"][0]
    return {
        "product": cart["name"], "vendor": cart["vendor_name"], "producer_id": cart.get("producer_id"), "count": cart["quantity"],
        "base_unit_quantity": cart.get("base_unit_quantity"), "line_total": cart.get("line_total"), "tier_id": cart.get("tier_id"),
    }


class TestNamePauseResume:
    NAMES = {
        "restau chez zouba": {"value": "Restau chez zouba", "kind": "ANSWER"},
        "c'est zouba": {"value": "Zouba", "kind": "ANSWER"},
        "mon nom c est zouba": {"value": "Zouba", "kind": "ANSWER"},
    }

    def test_the_preorder_resumes_from_the_exact_cart_after_the_name(self):
        c = GateConv(llm=GateLLM(name_answers=self.NAMES))
        st = _ready_cart(c)
        before = _business_snapshot(st)
        assert before["product"] == "lait" and before["count"] == 5 and before["line_total"] == 500.0

        st = c.say("Ok")
        assert "nom" in _reply(st).lower() and "confirmer" in _reply(st).lower()
        assert (st.get("profile_gate") or {}).get("goal") == "BUYER_PREORDER_INIT"
        assert "create_preorder_draft" not in c.tools(), "rien n'est créé tant que le nom manque"
        assert _business_snapshot(st) == before, "le panier est INTACT pendant la pause"
        _assert_no_placeholder_and_no_legacy(st)

        st = c.say("Restau chez zouba")
        assert c.rt.profile["name"] == "Restau chez zouba"
        assert _business_snapshot(st) == before, "BUSINESS_STATE_BEFORE == AFTER (hors nom/région/capacités)"
        # la précommande reprend DEPUIS LE PANIER (le service est l'autorité), avec les bons articles
        (call,) = c.rt.of("create_preorder_draft")
        items = call["cart_items"]
        assert [(i["name"], i["quantity"], i["price"]) for i in items] == [("lait", 5, 100.0)] or [(i["name"], i["quantity"]) for i in items] == [("lait", 5.0)]
        text = _reply(st)
        assert "lait" in text.lower() and "5" in text and "500" in text
        assert "pour 0" not in text and "votre demande" not in text, f"valeur de repli rendue comme donnée métier : {text!r}"
        _assert_no_placeholder_and_no_legacy(st)

    @pytest.mark.parametrize("answer", ["Restau chez zouba", "C'est Zouba", "Mon nom c est zouba"])
    def test_paraphrases_of_the_name_are_accepted_by_the_pending_slot(self, answer):
        c = GateConv(llm=GateLLM(name_answers=self.NAMES))
        _ready_cart(c)
        c.say("Ok")
        c.say(answer)
        assert c.rt.profile["name"] and "zouba" in c.rt.profile["name"].lower()
        assert len(c.rt.of("create_preorder_draft")) == 1

    def test_a_replayed_answer_never_creates_a_second_preorder(self):
        c = GateConv(llm=GateLLM(name_answers=self.NAMES))
        _ready_cart(c)
        c.say("Ok")
        c.say("Restau chez zouba")
        c.say("Mon nom c est zouba")
        assert len(c.rt.of("create_preorder_draft")) <= 1
        assert c.rt.all_calls.count("confirm_preorder_draft") == 0

    @pytest.mark.parametrize("text", ["montre-moi d'abord le prix", "pourquoi ?", "plus tard", "je ne veux pas donner mon nom"])
    def test_interruptions_and_refusals_are_never_taken_as_a_name(self, text):
        llm = GateLLM(name_answers={text.lower(): {"value": None, "kind": "INTERRUPTION" if "montre" in text else "REFUSAL"}})
        c = GateConv(llm=llm)
        _ready_cart(c)
        c.say("Ok")
        c.say(text)
        assert not c.rt.profile.get("name"), "une interruption/un refus n'est jamais enregistré comme nom"
        assert c.rt.of("create_preorder_draft") == []
        assert c.state()["active_cart"], "le panier reste conservé"
