"""HOTFIX 2026-10-03 — PRODUCER PACKAGING PUBLICATION INTEGRITY (vraies conversations WhatsApp).

Incidents prod rejoués sur le VRAI graphe compilé (seuls le LLM et le MCP sont doublés) :

  « J aimerais mettre en vente 100 sachet de lait frais pasteurisé de 500ml »
      -> « Quel est votre prix par millilitre ? » puis « 100 litres à 500 FCFA par millilitre »  (FAUX)
  « 50 bidons de gapal de 500ml et 100 bidons de 330 mL disponible » -> « Vous avez 250 litre de gapal »  (FAUX)

Invariant : QUANTITÉ DE STOCK != NOMBRE DE CONDITIONNEMENTS != TAILLE D'UN CONDITIONNEMENT != BASE DU PRIX.
`100 sachets de 500 ml` = 100 packages x 0,5 L = 50 L de stock, prix PAR SACHET — jamais « 100 litres », jamais
« 500 FCFA/ml ». La structure est lue DÉTERMINISTEMENT : le LLM de ces tests renvoie volontairement des quantités
fausses (100 « sachet », 250 litres) pour prouver qu'il n'est pas l'autorité.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.conftest import StubRuntime, run
from tests.integration.test_buyer_deterministic_product_switch_e2e import _Comp

PHONE = "+22670000001"


class ScriptedLLM:
    """NEW_TASK : `ents(text)` (jamais fiable — quantités fausses à dessein) ; ACTIVE_SLOT : REJECT sur « annul »."""

    def __init__(self, ents: Callable[[str], Optional[Dict[str, Any]]], mode: str = "ok") -> None:
        self.ents, self.mode = ents, mode
        self.chat = self
        self.completions = self

    def create(self, **kw: Any):
        if self.mode == "exception":
            raise TimeoutError("llm timeout")
        if self.mode == "invalid_json":
            return _Comp("{not json")
        if self.mode == "unknown":
            return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.1}))
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{1,3}(.*?)"{1,3}', user, re.S)
        text = (found[-1].strip() if found else "").lower()
        if "CATALOGUE OFFICIEL" in sysm:
            ents = self.ents(text)
            if ents is None:
                return _Comp(json.dumps({"disposition": "REJECT", "confidence": 0.9}))
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT",
                                     "confidence": 0.9, "entities": ents}))
        if "annul" in text:
            return _Comp(json.dumps({"disposition": "REJECT", "confidence": 0.95}))
        return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.1}))


class Rt(StubRuntime):
    def __init__(self, llm: Any) -> None:
        super().__init__(llm=llm)
        self.log: List[tuple] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _n(*a: Any, **k: Any):
            return None

        return _n

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.log.append((tool, dict(kw)))
        if tool == "get_user_by_phone":
            return {"status": "success", "data": {"id": "u1", "phone": kw.get("phone"), "role": "PRODUCER"}}
        if tool in ("get_or_create_farm", "get_producer_farm", "get_farms", "create_farm", "get_farm"):
            farm = {"id": "F1", "farm_id": "F1", "name": "Ferme"}
            return {"status": "success", "data": farm, "farm_id": "F1", "farms": [farm], "results": [farm]}
        return await super().call_db(tool, **kw)


class Conv:
    def __init__(self, ents: Callable[[str], Optional[Dict[str, Any]]], mode: str = "ok") -> None:
        self.llm = ScriptedLLM(ents, mode)
        self.rt = Rt(self.llm)
        self.graph = build_graph("PRODUCER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.sid, self.n = uuid.uuid4().hex, 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": PHONE,
                                "user_role": "PRODUCER", "message_sid": f"{self.sid}-{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values

    def created(self) -> Dict[str, Any]:
        calls = [kw for tool, kw in self.rt.log if tool == "create_product"]
        assert len(calls) == 1, f"create_product attendu une fois, vu {len(calls)}"
        return calls[0]


def _r(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


def _payload(st: Dict[str, Any]) -> Dict[str, Any]:
    return st.get("transaction_payload") or {}


def junk_milk(_text: str) -> Dict[str, Any]:
    """Ce qu'un LLM de prod renvoie de faux pour « 100 sachet de lait … de 500ml » : le compte pris pour la quantité."""
    return {"product": "lait frais pasteurisé", "quantity": 100, "unit": "sachet"}


def junk_gapal(_text: str) -> Dict[str, Any]:
    return {"product": "gapal", "quantity": 250, "unit": "litre"}


_CAS1 = "J aimerais mettre en vente 100 sachet de lait frais pasteurisé de 500ml"
_CAS3 = "50 bidons de gapal de 500ml et 100 bidons de 330 mL disponible"
_FORBIDDEN_CAS1 = ("100 litres", "par millilitre", "par ml", "250")


# ── CAS 1 : 100 sachets de 500 ml ──────────────────────────────────────────────────────────────────

def test_cas1_turn1_is_100_packages_of_half_a_litre_50_litres_and_asks_the_PACKAGE_price():
    c = Conv(junk_milk)
    st = c.say(_CAS1)
    p = _payload(st)
    assert (p.get("package_count"), p.get("package_size"), p.get("package_unit"), p.get("package_label")) == (
        100, 0.5, "LITRE", "SACHET")
    assert p.get("quantity") == 50.0 and p.get("unit") == "LITRE"
    r = _r(st)
    assert "prix d'un *sachet de 500 ml*" in r
    assert "millilitre" not in r.lower() and "par litre" not in r.lower()


def test_cas1_turn2_500f_pour_500mililitre_is_500_FCFA_PER_PACKAGE_never_per_ml():
    c = Conv(junk_milk)
    c.say(_CAS1)
    st = c.say("500f pour 500mililitre")
    r = _r(st)
    assert "100 sachets de lait frais pasteurisé de 500 ml à 500 FCFA le sachet" in r
    assert "Quantité totale : 50 litres" in r
    assert not any(bad in r.lower() for bad in _FORBIDDEN_CAS1)
    offer = _payload(st)["commercial_offer"]
    assert offer["pricing"]["basis"] == "PER_PACKAGE" and offer["pricing"]["amount"] == 500.0
    assert offer["package"]["count"] == 100 and offer["package"]["content_amount"] == 0.5
    assert offer["inventory_quantity"] == {"amount": 50.0, "unit": "LITRE", "source": "DOMAIN_DERIVED"}


def test_cas1_confirmation_persists_50_L_and_a_package_tier_not_a_per_ml_price():
    c = Conv(junk_milk)
    c.say(_CAS1)
    c.say("500f pour 500mililitre")
    c.say("oui")
    sent = c.created()
    assert sent["quantity_for_sale"] == 50.0 and sent["unit"] == "LITRE"
    # B16 : le COMPTE de sachets est désormais persisté (source de vérité de l'inventaire par conditionnement).
    assert sent["pricing_tiers"] == [
        {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet", "available_count": 100}
    ]
    assert sent["price"] != 0.5 and sent["price"] != 500.0 * 1.0 / 1000.0  # jamais « 0,5 FCFA/ml » ni 500 FCFA/ml


@pytest.mark.parametrize("reply", [
    "500f", "500 francs", "500 FCFA", "500", "500f pour 500ml", "500 FCFA le sachet", "500 FCFA par sachet",
    "500 francs pour 0,5 litre", "500f pour 50cl",
])
def test_cas1_price_phrasings_all_resolve_to_per_package(reply: str):
    c = Conv(junk_milk)
    c.say(_CAS1)
    st = c.say(reply)
    offer = _payload(st)["commercial_offer"]
    assert offer["pricing"]["basis"] == "PER_PACKAGE" and offer["pricing"]["amount"] == 500.0, reply
    assert "500 FCFA le sachet" in _r(st) and "millilitre" not in _r(st).lower()


def test_an_EXPLICIT_per_litre_price_on_packaged_stock_stays_per_base_unit():
    """La conversion physique sert au stock, jamais à changer la base commerciale que le producteur a DITE."""
    c = Conv(junk_milk)
    c.say(_CAS1)
    st = c.say("1000 francs le litre")
    r = _r(st)
    assert "par litre" in r and "1 000 FCFA" in r and "le sachet" not in r
    assert _payload(st)["commercial_offer"]["pricing"]["basis"] == "PER_BASE_UNIT"


@pytest.mark.parametrize("mode", ["unknown", "invalid_json", "exception"])
def test_the_package_price_reply_does_not_depend_on_the_llm(mode: str):
    c = Conv(junk_milk)
    c.say(_CAS1)
    c.llm.mode = mode
    st = c.say("500f pour 500mililitre")
    assert "500 FCFA le sachet" in _r(st) and "100 sachets" in _r(st)


# ── CAS 2 : le chemin simple par litre ne change PAS ───────────────────────────────────────────────

def test_cas2_simple_per_litre_publication_is_untouched():
    c = Conv(lambda t: {"product": "lait frais pasteurise", "quantity": 50, "unit": "litres"})
    st = c.say("J aimerais vendre 50 litres de lait frais pasteurise")
    assert "Quel est votre prix *par litre*" in _r(st)
    assert not _payload(st).get("package_count") and not _payload(st).get("package_groups")
    st = c.say("1200francs par litre")
    r = _r(st)
    assert "Publication de 50 litres de lait frais pasteurise à 1 200 FCFA par litre." in r
    offer = _payload(st)["commercial_offer"]
    assert offer["pricing"]["basis"] == "PER_BASE_UNIT" and offer["pricing"]["amount"] == 1200.0
    c.say("oui")
    sent = c.created()
    assert sent["quantity_for_sale"] == 50.0 and sent["price"] == 1200.0 and not sent.get("pricing_tiers")


# ── CAS 3 : plusieurs conditionnements ───────────────────────────────────────────────────────────────

def test_cas3_exact_prod_message_never_says_250_litres_and_keeps_both_packagings():
    c = Conv(junk_gapal)
    st = c.say(_CAS3)
    r = _r(st)
    assert "50 bidons de 500 ml et 100 bidons de 330 ml (58 litres au total) de gapal" in r
    assert "250" not in r


def test_cas3_after_choosing_to_sell_it_asks_the_price_of_EACH_packaging_then_publishes_58_L_with_two_tiers():
    c = Conv(junk_gapal)
    c.say(_CAS3)
    st = c.say("je veux les vendre")
    r = _r(st)
    assert "un *bidon de 500 ml*" in r and "un *bidon de 330 ml*" in r and "par litre" not in r.lower()
    assert _payload(st)["quantity"] == 58.0 and len(_payload(st)["package_groups"]) == 2  # jamais 250
    st = c.say("500 et 350")
    r = _r(st)
    assert "58 L disponibles" in r
    assert "50 bidons de 500 ml : 500 FCFA le bidon" in r and "100 bidons de 330 ml : 350 FCFA le bidon" in r
    c.say("oui")
    sent = c.created()
    assert sent["quantity_for_sale"] == 58.0 and sent["unit"] == "LITRE"
    tiers = sent["pricing_tiers"]
    assert [(t["quantity"], t["unit"], t["price"], t["packaging"]) for t in tiers] == [
        (500.0, "ml", 500.0, "bidon"), (330.0, "ml", 350.0, "bidon")]


def test_cas3_with_a_sales_verb_two_prices_in_one_message_publish_directly():
    c = Conv(junk_gapal)
    st = c.say("Je veux vendre 50 bidons de gapal de 500 ml à 500 FCFA et 100 bidons de 330 ml à 350 FCFA")
    r = _r(st)
    assert "58 L disponibles" in r and "500 FCFA le bidon" in r and "350 FCFA le bidon" in r
    c.say("oui")
    sent = c.created()
    assert sent["quantity_for_sale"] == 58.0 and len(sent["pricing_tiers"]) == 2


@pytest.mark.parametrize("reply", ["500 pour 500ml et 350 pour 330ml", "500 et 350"])
def test_cas3_both_prices_in_one_reply(reply: str):
    c = Conv(junk_gapal)
    c.say("Je veux vendre 50 bidons de gapal de 500ml et 100 bidons de 330 mL")
    st = c.say(reply)
    assert "500 FCFA le bidon" in _r(st) and "350 FCFA le bidon" in _r(st)


def test_two_packaging_TYPES_are_never_merged():
    c = Conv(lambda t: {"product": "lait", "quantity": 70, "unit": "litre"})
    st = c.say("Je veux vendre 50 sachets de lait de 500 ml et 20 bidons de 2 L")
    groups = _payload(st)["package_groups"]
    assert [(g["count"], g["label"], g["size"]) for g in groups] == [(50, "SACHET", 0.5), (20, "BIDON", 2.0)]
    assert _payload(st)["quantity"] == 65.0
    r = _r(st)
    assert "un *sachet de 500 ml*" in r and "un *bidon de 2 litres*" in r


# ── masse ─────────────────────────────────────────────────────────────────────────────────────────

def test_mass_packaging_20_sacks_of_25_kg_is_500_kg_and_a_per_sack_price():
    c = Conv(lambda t: {"product": "riz", "quantity": 20, "unit": "sac"})
    st = c.say("Je veux vendre 20 sacs de riz de 25 kg")
    assert _payload(st)["quantity"] == 500.0 and _payload(st)["unit"] == "KG"
    assert "prix d'un *sac de 25 kg*" in _r(st)
    st = c.say("15000 FCFA le sac")
    assert "Publication de 20 sacs de riz de 25 kg à 15 000 FCFA le sac." in _r(st)
    assert "Quantité totale : 500 kg" in _r(st)
    c.say("oui")
    sent = c.created()
    assert sent["quantity_for_sale"] == 500.0 and sent["pricing_tiers"][0]["price"] == 15000.0


# ── hygiène d'état ───────────────────────────────────────────────────────────────────────────────────

def test_cancel_then_new_publication_never_inherits_the_packaging_of_the_cancelled_one():
    def ents(text: str):
        if "annul" in text:
            return None
        if "50 litres" in text:
            return {"product": "lait", "quantity": 50, "unit": "litres"}
        return junk_milk(text)

    c = Conv(ents)
    c.say("Je veux vendre 100 sachets de lait frais pasteurisé de 500 ml")
    c.say("annuler")
    st = c.say("Je veux vendre 50 litres de lait")
    p = _payload(st)
    assert not p.get("package_count") and not p.get("package_size") and not p.get("package_groups")
    st = c.say("1200 francs par litre")
    assert "Publication de 50 litres de lait à 1 200 FCFA par litre." in _r(st)
    assert "sachet" not in _r(st)


# ── Buyer : le produit publié reste achetable par conditionnement ─────────────────────────────────────

def test_buyer_sees_the_published_package_tier_never_a_per_ml_price():
    from tests.integration.test_buyer_deterministic_product_switch_e2e import (
        _Conv,
        _offer,
    )

    offer = _offer("lait frais pasteurisé", "LITRE", "Ferme1", "L0")
    offer["pricing_tiers"] = [{"tier_id": "t1", "quantity": 0.5, "unit": "LITRE", "price": 500.0,
                               "packaging": "sachet", "base_unit_quantity": 0.5, "min_order_quantity": 1}]
    b = _Conv([offer], {"disposition": "UNKNOWN", "confidence": 0.1})
    texts = [_r(b.say("je veux acheter du lait frais pasteurisé"))]
    joined = " ".join(texts).lower()
    assert "millilitre" not in joined and "/ml" not in joined and "par ml" not in joined
    assert "500" in joined and "sachet" in joined
