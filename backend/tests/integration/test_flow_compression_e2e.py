"""FLOW COMPRESSION — l'utilisateur donne plusieurs contraintes d'un coup ; Ladini ne les redemande pas et filtre dès la 1ʳᵉ recherche.

Vrai graphe ; seuls le LLM (extraction structurée scriptée) et le MCP sont doublés. Aucune contrainte n'est relâchée en silence.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.integration.test_buyer_deterministic_product_switch_e2e import _offer, run
from tests.integration.test_conversational_autonomy_e2e import _PROFILE, AutonomyLLM
from tests.integration.test_profile_gate_resume_e2e import _PHONE, ProfileRt, _Comp

FULL_REQUEST = "Je veux 20 litres de lait en sachets de 500 ml à Ouaga, pas plus de 600 FCFA le litre"
FULL_ENTITIES = {
    "product": "lait", "quantity": 20, "unit": "L", "zone": "Ouaga",
    "package_type": "sachet", "package_content_amount": 500, "package_content_unit": "ml", "max_price_per_unit": 600,
}


def _tier(tid, qty, price, packaging="sachet"):
    return {"tier_id": tid, "quantity": qty, "unit": "L", "price": price, "packaging": packaging, "base_unit_quantity": qty, "min_order_quantity": 1}


def _catalog() -> List[Dict[str, Any]]:
    spec = [
        ("Ferme Sawadogo", "Bobo-Dioulasso", 700.0, 300, None),                                  # plat 700/L : trop cher, pas de sachet
        ("TEST Producteur", "Ouagadougou", 250.0, 50, [_tier("a2", 2.0, 500.0)]),                # sachet de 2 L : mauvais volume
        ("GARIKO Leila", "Kadiogo", 1200.0, 900, None),                                          # plat 1200/L
        ("Gilbert-prod", "Ouagadougou", 200.0, 1200, [_tier("g05", 0.5, 100.0), _tier("g1", 1.0, 190.0)]),  # OK : 200/L
        ("Laiterie Nana", "Ouagadougou", 700.0, 80, [_tier("n05", 0.5, 350.0)]),                  # bon sachet mais 700/L : trop cher
    ]
    offers = []
    for i, (name, zone, price, qty, tiers) in enumerate(spec):
        o = _offer("lait", "LITRE", name, f"L{i}")
        o.update({"price": price, "zone": zone, "available_quantity": qty, "pricing_tiers": tiers})
        offers.append(o)
    return offers


class CompressionLLM(AutonomyLLM):
    """Comme `AutonomyLLM`, mais l'extraction NEW_TASK renvoie TOUTES les contraintes de la phrase (ce que fait le vrai modèle)."""

    def __init__(self, entities: Dict[str, Any]) -> None:
        super().__init__()
        self.entities = entities
        self.new_task_calls = 0

    def create(self, **kw: Any):
        sysm = " ".join(m["content"] for m in (kw.get("messages") or []) if m["role"] == "system")
        if "CATALOGUE OFFICIEL" in sysm:
            self.new_task_calls += 1
            self.calls.append("new_task")
            return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": "BUYER_REQUEST", "confidence": 0.95, "entities": self.entities}))
        return super().create(**kw)


class Conv:
    def __init__(self, entities: Dict[str, Any] = FULL_ENTITIES, *, profile: Dict[str, Any] | None = None) -> None:
        self.llm = CompressionLLM(entities)
        self.rt = ProfileRt(self.llm, _catalog(), profile=dict(profile or _PROFILE))
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "cmp"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
                                "message_sid": f"c{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values


def _reply(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


def _shown(st: Dict[str, Any]) -> List[str]:
    return [v["vendor_name"] for v in (st.get("vendor_selection_context") or {}).get("vendors") or []]


def _entities(**over: Any) -> Dict[str, Any]:
    base = {"product": "lait", "quantity": 20, "unit": "L", "zone": "Ouaga"}
    base.update(over)
    return base


class TestBuyerFullRequest:
    def test_no_question_already_answered_is_asked_again(self):
        text = _reply(Conv().say(FULL_REQUEST)).lower()
        for asked in ("quelle région", "dans quelle région", "quelle quantité", "quel conditionnement"):
            assert asked not in text, text

    def test_the_first_search_applies_price_and_package_and_skips_the_tier_step(self):
        c = Conv()
        st = c.say(FULL_REQUEST)
        text = _reply(st)
        for out in ("Ferme Sawadogo", "GARIKO Leila", "TEST Producteur", "Laiterie Nana"):
            assert out not in text, text
        assert "Gilbert-prod" in text and "0.5 L (sachet)" in text
        assert "Quel conditionnement souhaitez-vous" not in text, "l'étape palier est sautée : le conditionnement est déjà dit"
        assert "Combien de" in text and "il en faut 40" in text, "le calcul exact est une SUGGESTION, pas une conversion silencieuse"
        assert not st.get("active_cart")

    def test_the_suggested_count_is_confirmed_by_the_user_then_the_cart_is_exact(self):
        c = Conv()
        c.say(FULL_REQUEST)
        st = c.say("40")
        assert [(x["quantity"], x["vendor_name"], x["line_total"]) for x in st["active_cart"]] == [(40.0, "Gilbert-prod", 4000.0)], _reply(st)


class TestPriceAndPackageFiltersOnTheFirstSearch:
    def test_max_price_alone_is_compared_per_unit_after_normalisation(self):
        c = Conv(_entities(max_price_per_unit=300))
        st = c.say("Je veux 20 litres de lait à moins de 300 FCFA le litre")
        # TEST Producteur : 500 FCFA le sachet de 2 L = 250 FCFA/L (jamais 500) -> gardé ; 700 et 1200 FCFA/L -> écartés
        assert sorted(_shown(st)) == ["Gilbert-prod", "TEST Producteur"], _reply(st)
        constraints = st["vendor_selection_context"]["search_constraints"]
        assert constraints["max_price_per_unit"] == 300, "les contraintes restent dans le snapshot du menu"

    def test_500_per_two_litre_sachet_is_not_read_as_500_per_litre(self):
        c = Conv(_entities(max_price_per_unit=400))
        st = c.say("Je veux 20 litres de lait à moins de 400 FCFA le litre")
        assert "TEST Producteur" in _shown(st) and "Ferme Sawadogo" not in _shown(st)

    def test_package_alone_keeps_only_offers_that_can_supply_that_package(self):
        c = Conv(_entities(package_type="sachet", package_content_amount=500, package_content_unit="ml"))
        st = c.say("Je veux 20 litres de lait en sachets de 500 ml")
        assert sorted(_shown(st)) == ["Gilbert-prod", "Laiterie Nana"], _reply(st)
        for vendor in st["vendor_selection_context"]["vendors"]:
            assert [t["tier_id"] for t in vendor["pricing_tiers"]] in (["g05"], ["n05"]), "seuls les paliers qui correspondent restent proposés"

    def test_without_constraints_nothing_is_filtered(self):
        st = Conv(_entities()).say("Je veux 20 litres de lait")
        assert len(_shown(st)) == 5


class TestNoAutomaticRelaxation:
    def test_no_result_says_so_keeps_every_constraint_and_proposes_to_widen(self):
        c = Conv(_entities(max_price_per_unit=100))
        st = c.say("Je veux 20 litres de lait à moins de 100 FCFA le litre")
        text = _reply(st)
        assert "Je n'ai rien trouvé" in text and "100" in text and "appel d'offres" in text
        assert "Producteurs disponibles" not in text, "aucune offre n'est présentée comme si le critère avait été relâché"
        assert st["working_memory"]["last_search_constraints"]["max_price_per_unit"] == 100
        assert not st.get("active_cart")


# ── ÉDITION des contraintes : poser / remplacer / RETIRER, puis rafraîchir (jamais un faux plafond, jamais une relaxation silencieuse) ──
from tests.integration import test_conversational_autonomy_e2e as _auto  # noqa: E402

_auto.UNDERSTANDING.update({
    "max 800 finalement": {"reference": {"reference_type": "REFINEMENT", "max_price": 800}},
    "le prix n'importe plus": {"reference": {"reference_type": "REFINEMENT", "remove": ["max_price"]}},
    "plutôt max 100": {"reference": {"reference_type": "REFINEMENT", "max_price": 100}},
})
_MAX300 = "Je veux 20 litres de lait à moins de 300 FCFA le litre"


class TestConstraintEdits:
    def _at_300(self) -> Conv:
        c = Conv(_entities(max_price_per_unit=300))
        st = c.say(_MAX300)
        assert sorted(_shown(st)) == ["Gilbert-prod", "TEST Producteur"]
        return c

    def test_a_ceiling_can_be_replaced_and_the_list_refreshed_from_the_original_pool(self):
        c = self._at_300()
        st = c.say("max 800 finalement")
        assert sorted(_shown(st)) == sorted(["Gilbert-prod", "Laiterie Nana", "TEST Producteur", "Ferme Sawadogo"])
        assert st["vendor_selection_context"]["search_constraints"]["max_price_per_unit"] == 800
        assert "GARIKO Leila" not in _shown(st), "1200/L reste au-dessus du nouveau plafond"

    def test_removing_the_price_ceiling_really_removes_it_not_a_fake_huge_number(self):
        c = self._at_300()
        st = c.say("le prix n'importe plus")
        assert len(_shown(st)) == 5 and "GARIKO Leila" in _shown(st)
        constraints = st["vendor_selection_context"]["search_constraints"]
        assert "max_price_per_unit" not in constraints, constraints
        assert "Plafond de prix retiré" in _reply(st)

    def test_an_edit_with_no_result_keeps_the_current_list_and_the_current_constraints(self):
        c = self._at_300()
        st = c.say("plutôt max 100")
        assert sorted(_shown(st)) == ["Gilbert-prod", "TEST Producteur"], "la liste actuelle est conservée"
        assert "Je n'ai rien trouvé" in _reply(st)
        assert st["vendor_selection_context"]["search_constraints"]["max_price_per_unit"] == 300
