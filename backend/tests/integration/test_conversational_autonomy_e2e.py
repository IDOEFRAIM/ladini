"""CONVERSATIONAL AUTONOMY — un menu est un CONTEXTE, pas un protocole.

Vrai graphe compilé ; seuls le LLM (qui COMPREND le langage et ne renvoie qu'une RÉFÉRENCE structurée) et le MCP sont doublés. Le
résolveur déterministe (`domain/selection_reference.py`) tranche contre les options RÉELLEMENT affichées : le modèle ne choisit
jamais un identifiant ni « le moins cher » lui-même.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.integration.test_buyer_deterministic_product_switch_e2e import _offer, run
from tests.integration.test_profile_gate_resume_e2e import _PHONE, ProfileRt, _Comp

@pytest.fixture(autouse=True)
def _shortlist_on(monkeypatch):
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "BUYER_SHORTLIST_SIZE", 5, raising=False)


_PROFILE = {"name": "Zouba", "declared_location": "Kadiogo", "buyer": True, "producer": False}


def _tiered(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    offers[3]["pricing_tiers"] = [
        {"tier_id": "t05", "quantity": 0.5, "unit": "L", "price": 100.0, "packaging": "sachet", "base_unit_quantity": 0.5, "min_order_quantity": 1},
        {"tier_id": "t1", "quantity": 1.0, "unit": "L", "price": 190.0, "packaging": "sachet", "base_unit_quantity": 1.0, "min_order_quantity": 1},
        {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 800.0, "packaging": "bidon", "base_unit_quantity": 5.0, "min_order_quantity": 1},
    ]
    return offers


def _catalog(extra: int = 0) -> List[Dict[str, Any]]:
    rows = [
        ("Ferme Sawadogo", "Bobo-Dioulasso", 700.0, 300),
        ("TEST Producteur", "Ouagadougou", 500.0, 50),
        ("GARIKO Leila", "Kadiogo", 1200.0, 900),
        ("Gilbert-prod", "Ouagadougou", 500.0, 1200),
        ("Gilbert Ouedraogo", "Koudougou", 650.0, 20),
    ]
    rows += [(f"Ferme Extra{k}", "Dédougou", 900.0 + k, 10 + k) for k in range(extra)]
    offers = []
    for i, (name, zone, price, qty) in enumerate(rows):
        o = _offer("lait", "LITRE", name, f"L{i}")
        o.update({"price": price, "zone": zone, "available_quantity": qty})
        offers.append(o)
    return offers


#: ce que le MODÈLE comprend de chaque phrase (une référence structurée — jamais un identifiant, jamais « le moins cher » calculé).
UNDERSTANDING: Dict[str, Dict[str, Any]] = {
    "le quatrième": {"reference": {"reference_type": "ORDINAL", "ordinal": 4}},
    "je prends le quatrième": {"reference": {"reference_type": "ORDINAL", "ordinal": 4}},
    "le dernier": {"reference": {"reference_type": "ORDINAL", "position": "LAST"}},
    "l'avant-dernier": {"reference": {"reference_type": "ORDINAL", "position": "PENULTIMATE"}},
    "gilbert-prod": {"reference": {"reference_type": "ATTRIBUTE", "producer_name": "Gilbert-prod"}},
    "je prends gilbert-prod": {"reference": {"reference_type": "ATTRIBUTE", "producer_name": "Gilbert-prod"}},
    "gilbert": {"reference": {"reference_type": "ATTRIBUTE", "producer_name": "Gilbert"}},
    "celui à 1200": {"reference": {"reference_type": "ATTRIBUTE", "price": 1200}},
    "celui à 500": {"reference": {"reference_type": "ATTRIBUTE", "price": 500}},
    "celui avec 1200 litres": {"reference": {"reference_type": "ATTRIBUTE", "availability": 1200}},
    "celui de bobo": {"reference": {"reference_type": "ATTRIBUTE", "region": "Bobo"}},
    "le moins cher": {"reference": {"reference_type": "PREFERENCE", "criterion": "CHEAPEST"}},
    "celui qui a le plus de stock": {"reference": {"reference_type": "PREFERENCE", "criterion": "HIGHEST_AVAILABILITY"}},
    "le plus intéressant": {"reference": {"reference_type": "PREFERENCE", "criterion": "SUBJECTIVE"}},
    "je prends gilbert-prod, 10 litres": {"reference": {"reference_type": "ATTRIBUTE", "producer_name": "Gilbert-prod"},
                                          "quantity": 10, "unit": "L"},
    "le sachet de 500 ml": {"reference": {"reference_type": "ATTRIBUTE", "packaging": "sachet", "volume": 0.5}},
    "celui de 500 ml": {"reference": {"reference_type": "ATTRIBUTE", "volume": 0.5}},
    "le bidon": {"reference": {"reference_type": "ATTRIBUTE", "packaging": "bidon"}},
    "le sachet": {"reference": {"reference_type": "ATTRIBUTE", "packaging": "sachet"}},
    "le moins cher des sachets": {"reference": {"reference_type": "PREFERENCE", "criterion": "CHEAPEST"}},
    "je prends le sachet de 500 ml, j'en veux 5": {"reference": {"reference_type": "ATTRIBUTE", "packaging": "sachet", "volume": 0.5},
                                                   "package_count": 5},
    "je préfère quelqu'un à ouaga": {"reference": {"reference_type": "REFINEMENT", "region": "Ouaga"}},
    "pas plus de 600": {"reference": {"reference_type": "REFINEMENT", "max_price": 600}},
    "je veux moins cher": {"reference": {"reference_type": "REFINEMENT", "criterion": "CHEAPEST"}},
    "pas plus de 100": {"reference": {"reference_type": "REFINEMENT", "max_price": 100}},
    "aucun ne me convient": {"reference": {"reference_type": "NONE_OF_THESE"}},
    "montre les autres": {"reference": {"reference_type": "PAGINATION"}},
    "le septième": {"reference": {"reference_type": "ORDINAL", "ordinal": 7}},
    "ferme extra1": {"reference": {"reference_type": "ATTRIBUTE", "producer_name": "Ferme Extra1"}},
    "annule ce choix": {"disposition": "REJECT"},
    "le dixième": {"reference": {"reference_type": "ORDINAL", "ordinal": 10}},
    # le modèle hostile : prend « 10 litres » pour l'option 10 (bruit) — le domaine ne doit RIEN muter.
    "je veux 10 litres": {"selection_index": 10},
}


class AutonomyLLM:
    def __init__(self) -> None:
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
        if "tunnel d'achat" in sysm:
            self.calls.append("structured")
            body = UNDERSTANDING.get(text)
            if body is not None and "disposition" in body:
                return _Comp(json.dumps({"confidence": 0.95, **body}))
            if body is None:
                return _Comp(json.dumps({"disposition": "UNKNOWN", "confidence": 0.2}))
            action = "SELECT_PRICING_TIER" if "conditionnement parmi" in user else "SELECT_PRODUCER"
            return _Comp(json.dumps({"disposition": "ACTION", "action": action, "confidence": 0.95, **body}))
        if "CATALOGUE OFFICIEL" in sysm:
            self.calls.append("new_task")
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.9,
                                     "entities": {"product": "lait"}}))
        self.calls.append("other")
        return _Comp(json.dumps({"text": "ok"}))


class Conv:
    def __init__(self, *, tiered: bool = False, extra: int = 0) -> None:
        self.llm = AutonomyLLM()
        self.rt = ProfileRt(self.llm, _tiered(_catalog(extra)) if tiered else _catalog(extra), profile=dict(_PROFILE))
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "auto"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
                                "message_sid": f"a{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values

    def at_menu(self) -> Dict[str, Any]:
        st = self.say("Je veux du lait")
        assert len((st.get("vendor_selection_context") or {}).get("vendors") or []) == 5, st.get("final_response")
        return st


def _chosen(st: Dict[str, Any]) -> str | None:
    return ((st.get("vendor_selection_context") or {}).get("chosen_vendor") or {}).get("vendor_name")


def _reply(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


@pytest.mark.parametrize("phrase,vendor", [
    ("4", "Gilbert-prod"),
    ("04", "Gilbert-prod"),
    ("4.", "Gilbert-prod"),
    ("option 4", "Gilbert-prod"),
    ("choix 4", "Gilbert-prod"),
    ("le quatrième", "Gilbert-prod"),
    ("je prends le quatrième", "Gilbert-prod"),
    ("le dernier", "Gilbert Ouedraogo"),
    ("l'avant-dernier", "Gilbert-prod"),
    ("gilbert-prod", "Gilbert-prod"),
    ("je prends gilbert-prod", "Gilbert-prod"),
    ("celui à 1200", "GARIKO Leila"),
    ("celui avec 1200 litres", "Gilbert-prod"),
    ("celui de bobo", "Ferme Sawadogo"),
    ("celui qui a le plus de stock", "Gilbert-prod"),
])
def test_natural_and_numeric_replies_converge_on_the_same_domain_selection(phrase, vendor):
    c = Conv()
    c.at_menu()
    st = c.say(phrase)
    assert _chosen(st) == vendor, _reply(st)


@pytest.mark.parametrize("phrase,must_mention", [
    ("gilbert", ("Gilbert-prod", "Gilbert Ouedraogo")),   # deux Gilbert : jamais un choix arbitraire
    ("celui à 500", ("TEST Producteur", "Gilbert-prod")),
    ("le moins cher", ("TEST Producteur", "Gilbert-prod")),  # égalité 500/500 : clarification, pas de choix arbitraire
    ("le plus intéressant", ("moins cher", "stock")),         # subjectif : jamais tranché
])
def test_ambiguity_gets_a_targeted_clarification_and_selects_nothing(phrase, must_mention):
    c = Conv()
    c.at_menu()
    st = c.say(phrase)
    assert _chosen(st) is None
    text = _reply(st)
    assert all(m in text for m in must_mention), text
    assert "Producteurs disponibles" not in text, "le menu complet n'est pas réaffiché"
    assert "Ferme Sawadogo" not in text or phrase == "le plus intéressant"


def test_a_clarification_is_not_a_failure_the_tunnel_survives_repeated_ambiguity():
    c = Conv()
    c.at_menu()
    for _ in range(4):
        st = c.say("gilbert")
        assert _chosen(st) is None and "Gilbert-prod" in _reply(st)
    st = c.say("gilbert-prod")
    assert _chosen(st) == "Gilbert-prod", "le contexte du menu est resté actif après plusieurs clarifications"


def test_an_option_that_does_not_exist_mutates_nothing():
    c = Conv()
    c.at_menu()
    st = c.say("le dixième")
    assert _chosen(st) is None and not st.get("active_cart")


def test_a_business_number_is_never_an_option_index():
    c = Conv()
    c.at_menu()
    st = c.say("je veux 10 litres")  # le modèle (hostile) propose l'option 10 : hors bornes -> aucune mutation
    assert _chosen(st) is None and not st.get("active_cart")


def test_producer_and_quantity_in_one_sentence_carry_both_facts():
    c = Conv()
    c.at_menu()
    st = c.say("je prends gilbert-prod, 10 litres")
    assert [(x["quantity"], x["vendor_name"], x["line_total"]) for x in st["active_cart"]] == [(10.0, "Gilbert-prod", 5000.0)], _reply(st)


def _at_tier_menu() -> "Conv":
    c = Conv(tiered=True)
    c.at_menu()
    st = c.say("gilbert-prod")
    assert "conditionnement" in _reply(st).lower(), _reply(st)
    return c


def _tier(st: Dict[str, Any]) -> str | None:
    return (st.get("vendor_selection_context") or {}).get("resolved_tier_id") or (st.get("tier_selection_context") or {}).get("resolved_tier_id")


@pytest.mark.parametrize("phrase,tier_id", [("le sachet de 500 ml", "t05"), ("celui de 500 ml", "t05"), ("le bidon", "t5"), ("2", "t1")])
def test_tiers_are_chosen_by_packaging_or_volume_or_number(phrase, tier_id):
    c = _at_tier_menu()
    st = c.say(phrase)
    assert _tier(st) == tier_id, _reply(st)


def test_two_sachets_match_so_the_system_asks_which_one_not_replays_the_menu():
    c = _at_tier_menu()
    st = c.say("le sachet")
    assert _tier(st) is None
    text = _reply(st)
    assert "ou de" in text and "Quel conditionnement souhaitez-vous" not in text, text


def test_tier_and_package_count_in_one_sentence_fill_the_cart_directly():
    c = _at_tier_menu()
    st = c.say("je prends le sachet de 500 ml, j'en veux 5")
    assert [(x["quantity"], x["vendor_name"]) for x in st["active_cart"]] == [(5.0, "Gilbert-prod")], _reply(st)
    assert st["active_cart"][0]["line_total"] == 500.0


class TestShortlist:
    def _menu(self):
        c = Conv(extra=3)  # 8 offres : 5 montrées, 3 gardées à part
        st = c.say("Je veux du lait")
        return c, st

    def test_only_the_shortlist_is_shown_with_a_short_pointer_to_the_rest(self):
        c, st = self._menu()
        text = _reply(st)
        assert len((st["vendor_selection_context"] or {})["vendors"]) == 5
        assert "Gilbert Ouedraogo" in text and "Ferme Extra0" not in text
        assert "J'ai aussi 3 autres offres" in text and "montre les autres" in text
        assert text.count("*") < 60, "message court"

    def test_a_hidden_option_is_never_selected_by_number_or_by_words(self):
        c, _ = self._menu()
        for phrase in ("7", "le septième", "ferme extra1"):
            st = c.say(phrase)
            assert _chosen(st) is None and not st.get("active_cart"), phrase
        assert "pas encore affiché" in _reply(c.say("le septième")) or "montre les autres" in _reply(c.say("le septième"))

    def test_show_more_reveals_only_the_new_offers_which_then_become_selectable(self):
        c, _ = self._menu()
        st = c.say("montre les autres")
        text = _reply(st)
        assert "Ferme Extra0" in text and "Ferme Extra2" in text and "Gilbert-prod" not in text, "seules les nouvelles offres"
        assert len(st["vendor_selection_context"]["vendors"]) == 8 and not st["vendor_selection_context"]["vendors_more"]
        st = c.say("ferme extra1")
        assert _chosen(st) == "Ferme Extra1"

    def test_numeric_and_natural_references_to_already_shown_offers_still_work_after_show_more(self):
        c, _ = self._menu()
        c.say("montre les autres")
        assert _chosen(c.say("le quatrième")) == "Gilbert-prod"


class TestRefinementAndRejection:
    def test_a_region_refinement_narrows_the_list_instead_of_failing_as_an_invalid_selection(self):
        c = Conv()
        c.at_menu()
        st = c.say("je préfère quelqu'un à ouaga")
        vendors = [v["vendor_name"] for v in st["vendor_selection_context"]["vendors"]]
        assert vendors == ["TEST Producteur", "GARIKO Leila", "Gilbert-prod"], vendors  # Ouagadougou + Kadiogo (même région)
        assert "Ferme Sawadogo" not in _reply(st) and not st.get("active_cart")
        assert _chosen(c.say("le dernier")) == "Gilbert-prod", "la numérotation vise la NOUVELLE liste"

    def test_a_max_price_refinement_and_a_cheapest_sort(self):
        c = Conv()
        c.at_menu()
        st = c.say("pas plus de 600")
        assert [v["vendor_name"] for v in st["vendor_selection_context"]["vendors"]] == ["TEST Producteur", "Gilbert-prod"]
        c = Conv()
        c.at_menu()
        st = c.say("je veux moins cher")
        prices = [v["price"] for v in st["vendor_selection_context"]["vendors"]]
        assert prices == sorted(prices)

    def test_a_refinement_with_no_match_keeps_the_current_menu_and_says_so(self):
        c = Conv()
        c.at_menu()
        st = c.say("pas plus de 100")
        assert len(st["vendor_selection_context"]["vendors"]) == 5
        assert "Aucune offre ne correspond" in _reply(st) and "Producteurs disponibles" not in _reply(st)

    def test_none_of_these_proposes_next_steps_without_mutating_or_abandoning(self):
        c = Conv()
        c.at_menu()
        st = c.say("aucun ne me convient")
        assert "appel d'offres" in _reply(st) and not st.get("active_cart")
        assert len(st["vendor_selection_context"]["vendors"]) == 5, "le menu reste actif"


class TestStaleMenu:
    def test_a_natural_reference_never_resolves_against_an_expired_menu(self):
        c = Conv()
        c.at_menu()
        ctx = dict(c.graph.get_state(c.cfg).values["vendor_selection_context"])
        ctx["created_at"] = 1.0  # menu affiché il y a très longtemps
        c.graph.update_state(c.cfg, {"vendor_selection_context": ctx})
        st = c.say("le quatrième")
        assert _chosen(st) is None and not st.get("active_cart")
        assert "date un peu" in _reply(st) and "Producteurs disponibles" not in _reply(st)

    def test_the_same_phrase_on_a_fresh_menu_still_works(self):
        c = Conv()
        c.at_menu()
        assert _chosen(c.say("le quatrième")) == "Gilbert-prod"


class TestCancelScope:
    def test_cancelling_a_choice_cancels_only_that_choice_and_keeps_the_cart(self):
        c = Conv()
        c.at_menu()
        c.say("je prends gilbert-prod, 10 litres")  # panier : 10 L
        c.say("Je veux du lait")                      # nouveau menu producteur
        st = c.say("annule ce choix")
        assert len(st["active_cart"]) == 1, "le panier n'est jamais détruit par un « annule » de sélection"
        text = _reply(st)
        assert "annule ce choix" in text and "panier (1 article) est conservé" in text, text
        assert "pas bien saisi" not in text
