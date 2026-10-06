"""Autonomie conversationnelle HORIZONTALE — menus génériques (appels d'offres, offres reçues, stocks, occurrences…).

Le micro-prompt SELECTION (LLM scripté : il COMPREND la phrase et renvoie une `reference` structurée) + `selection_reference` (Python)
+ les faits VISIBLES figés à l'affichage (`working_memory["menu_facts"]`). Le modèle ne renvoie jamais d'index ici : un id/index halluciné est
ignoré, une ambiguïté est clarifiée (aucune mutation), un menu périmé n'est jamais résolu.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import pytest

from ladini.graphs.agents.market_coach.interpreter.selection_micro import (
    SelectionOutcome,
    run_selection_microprompt,
)
from tests.conftest import StubRuntime, make_state, run


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": _Msg(content)})()]
        self.model = None


class _Understands:
    """Le modèle : renvoie la `reference` qu'il a comprise de la phrase (jamais un identifiant)."""

    def __init__(self, reference: Dict[str, Any] | None, **extra: Any) -> None:
        self.body = {"event": "SELECTION", "confidence": 0.95, **extra}
        if reference is not None:
            self.body["reference"] = reference

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs: Any):
        return _Completion(json.dumps(self.body))


def _facts(kind: str, options: List[Dict[str, Any]], *, age: float = 5.0) -> Dict[str, Any]:
    return {
        "kind": kind, "menu_id": "m1", "created_at": time.time() - age,
        "options": [{"index": str(i), "entity_id": f"id-{i}", "label": o.pop("label", f"option {i}"), "facts": o}
                    for i, o in enumerate(options, start=1)],
    }


def _go(facts: Dict[str, Any] | None, understood: Dict[str, Any] | None, text: str, **extra: Any):
    candidates = [o["label"] for o in (facts or {}).get("options", [])]
    state = make_state(normalized_text=text, expected_input="SELECTION", expected_candidates=candidates, current_goal="BUYER_CHECK_AUCTION_STATUS",
                       user_role="BUYER", message_sid=None, working_memory={"menu_facts": facts} if facts else {})
    return run(run_selection_microprompt(state, StubRuntime(llm=_Understands(understood, **extra)), text, "BUYER_CHECK_AUCTION_STATUS"))


def _index(result) -> int | None:
    outcome, res = result
    assert outcome == SelectionOutcome.RESULT
    return (res.get("extracted_entities") or {}).get("selection_index")


def _clar(result) -> str:
    _, res = result
    assert not (res.get("extracted_entities") or {}).get("selection_index"), "aucune sélection sur ambiguïté"
    return str((res.get("raw_analysis") or {}).get("selection_clarification") or "")


TODAY = date.today()
AUCTIONS = _facts("buyer_auction_list", [
    {"name": "tomate", "quantity": 100, "unit": "KG", "price": 400, "day": (TODAY - timedelta(days=1)).isoformat()},
    {"name": "pommes de terre", "quantity": 200, "unit": "KG", "price": 350, "day": TODAY.isoformat()},
    {"name": "tomate", "quantity": 50, "unit": "KG", "price": 420, "day": (TODAY - timedelta(days=3)).isoformat()},
])
BIDS = _facts("bid", [
    {"name": "Moussa", "price": 450, "quantity": 200, "unit": "KG"},
    {"name": "Awa", "price": 500, "quantity": 150, "unit": "KG"},
    {"name": "Ibrahim", "price": 430, "quantity": 200, "unit": "KG"},
])
STOCKS = _facts("stock", [
    {"name": "tomates", "quantity": 300, "unit": "KG", "day": (TODAY - timedelta(days=1)).isoformat()},
    {"name": "oignons", "quantity": 120, "unit": "KG", "day": TODAY.isoformat()},
])
OCCURRENCES = _facts("occurrence", [
    {"name": "Livraison", "day": (TODAY + timedelta(days=1)).isoformat()},
    {"name": "Livraison", "day": (TODAY + timedelta(days=3)).isoformat()},
])


def attr(**kw: Any) -> Dict[str, Any]:
    return {"reference_type": "ATTRIBUTE", **kw}


class TestAuctionSelection:
    def test_by_product_when_unique(self):
        assert _index(_go(AUCTIONS, attr(producer_name="pommes de terre"), "celui de pommes de terre")) == 2

    def test_by_quantity(self):
        assert _index(_go(AUCTIONS, attr(availability=100), "celui de 100 kg")) == 1

    def test_by_launch_day_computed_by_the_domain_not_the_model(self):
        assert _index(_go(AUCTIONS, attr(date_offset_days=0), "celui lancé aujourd'hui")) == 2
        assert _index(_go(AUCTIONS, attr(producer_name="tomate", date_offset_days=-1), "l'appel d'offres tomate lancé hier")) == 1

    def test_last_one(self):
        assert _index(_go(AUCTIONS, {"reference_type": "ORDINAL", "position": "LAST"}, "le dernier")) == 3

    def test_two_tomato_auctions_are_clarified_never_picked(self):
        text = _clar(_go(AUCTIONS, attr(producer_name="tomate"), "l'appel d'offres tomate"))
        assert "Tu parles de" in text and "[n°1]" in text and "[n°3]" in text and "pommes" not in text


class TestBidSelection:
    def test_by_price_and_by_name(self):
        assert _index(_go(BIDS, attr(price=450), "l'offre à 450")) == 1
        assert _index(_go(BIDS, attr(producer_name="Moussa", price=450), "je prends l'offre de Moussa à 450")) == 1
        assert _index(_go(BIDS, attr(producer_name="Awa"), "celle de Awa")) == 2

    def test_cheapest_is_computed_in_python(self):
        assert _index(_go(BIDS, {"reference_type": "PREFERENCE", "criterion": "CHEAPEST"}, "la moins chère")) == 3

    def test_two_bids_with_the_same_quantity_are_clarified(self):
        text = _clar(_go(BIDS, attr(availability=200), "celle avec 200 kg"))
        assert "Moussa" in text and "Ibrahim" in text and "Awa" not in text

    def test_subjective_best_is_never_decided(self):
        assert "moins cher" in _clar(_go(BIDS, {"reference_type": "PREFERENCE", "criterion": "SUBJECTIVE"}, "la meilleure"))

    def test_unknown_price_is_not_found_and_mutates_nothing(self):
        assert "Je ne vois pas" in _clar(_go(BIDS, attr(price=999), "l'offre à 999"))


class TestStockAndOccurrenceSelection:
    def test_stock_by_product_and_quantity(self):
        assert _index(_go(STOCKS, attr(producer_name="tomates"), "le stock de tomates")) == 1
        assert _index(_go(STOCKS, attr(availability=120), "celui de 120 kg")) == 2
        assert _index(_go(STOCKS, attr(date_offset_days=-1), "celui ajouté hier")) == 1

    def test_occurrence_by_day(self):
        assert _index(_go(OCCURRENCES, attr(date_offset_days=3), "celle d'après-demain+1")) == 2
        assert _index(_go(OCCURRENCES, {"reference_type": "ORDINAL", "ordinal": 1}, "la prochaine")) == 1


class TestSafety:
    def test_a_stale_menu_is_never_resolved_silently(self):
        old = _facts("bid", [{"name": "Moussa", "price": 450}, {"name": "Awa", "price": 500}], age=3600)
        text = _clar(_go(old, {"reference_type": "ORDINAL", "ordinal": 4}, "le quatrième"))
        assert "date un peu" in text and "réaffiche" in text

    def test_no_menu_facts_means_no_natural_resolution(self):
        assert "redemande" in _clar(_go(None, attr(price=450), "l'offre à 450"))

    def test_a_hostile_model_returning_an_id_or_index_with_a_reference_is_rejected_by_the_contract(self):
        # `reference` + `selection_index` ensemble = deux désignations : contrat invalide -> jamais exécuté
        outcome, res = _go(BIDS, attr(price=450), "l'offre à 450", selection_index=3)
        assert (res or {}).get("extracted_entities", {}).get("selection_index") != 3

    @pytest.mark.parametrize("ref", [{"reference_type": "ORDINAL", "ordinal": 9}, attr(producer_name="Zoe")])
    def test_a_reference_to_nothing_mutates_nothing(self, ref):
        assert _clar(_go(BIDS, ref, "x")) != ""
