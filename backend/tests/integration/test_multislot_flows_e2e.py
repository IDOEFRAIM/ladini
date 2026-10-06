"""MULTI-SLOT — vendeur, besoin récurrent, appel d'offres : tout ce qui est dit dans la phrase est conservé, rien n'est redemandé.

Vrai graphe ; LLM (extraction scriptée, comme le vrai modèle qui renvoie TOUTES les entités) et MCP doublés.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.integration.test_buyer_deterministic_product_switch_e2e import run
from tests.integration.test_flow_compression_e2e import CompressionLLM
from tests.integration.test_profile_gate_resume_e2e import _PHONE, ProfileRt, _Comp

FARM = {"farm_id": "33333333-3333-3333-3333-333333333333", "id": "33333333-3333-3333-3333-333333333333", "name": "Ferme Moussa"}
_QUESTION_WORDS = ("quelle quantité", "quel prix", "à quel prix", "quelle région", "dans quelle région", "quelle fréquence", "quel produit")


class FlowRt(ProfileRt):
    async def call_db(self, tool: str, **kw: Any) -> Any:
        if tool in ("get_producer_farm", "get_or_create_farm"):
            self.tool_log.append((tool, dict(kw)))
            return {"status": "success", "data": FARM, **FARM}
        return await super().call_db(tool, **kw)


def _conversation(role: str, intent: str, entities: Dict[str, Any], profile: Dict[str, Any], text: str):
    class LLM(CompressionLLM):
        def create(self, **kw: Any):
            sysm = " ".join(m["content"] for m in (kw.get("messages") or []) if m["role"] == "system")
            if "CATALOGUE OFFICIEL" in sysm:
                return _Comp(json.dumps({"disposition": "NEW_TASK", "intent": intent, "confidence": 0.95, "entities": entities}))
            return super().create(**kw)

    rt = FlowRt(LLM(entities), [], profile=profile)
    graph = build_graph(role, mc_runtime=rt, checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "ms"}}
    run(graph.ainvoke({"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": role, "message_sid": uuid.uuid4().hex}, cfg))
    return graph.get_state(cfg).values


def _reply(st: Dict[str, Any]) -> str:
    return str(st.get("final_response") or "")


def _no_question(st: Dict[str, Any]) -> None:
    text = _reply(st).lower()
    assert not any(q in text for q in _QUESTION_WORDS), text
    assert (st.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION", st.get("pending_interaction")


SELLER = {"name": "Moussa", "declared_location": "Guiriko", "buyer": False, "producer": True}
BUYER = {"name": "Zouba", "declared_location": "Kadiogo", "buyer": True, "producer": False}


def test_seller_full_sentence_goes_straight_to_the_recap_with_every_slot_kept():
    st = _conversation(
        "PRODUCER", "SALES_PUBLISH_PRODUCT",
        {"product": "oignon", "quantity": 300, "unit": "kg", "price": 250, "price_unit": "kg", "zone": "Bobo"},
        SELLER, "J'ai 300 kg d'oignons à vendre à 250 FCFA/kg à Bobo",
    )
    _no_question(st)
    assert "300 kg" in _reply(st) and "250 FCFA par kg" in _reply(st) and "oignon" in _reply(st)


def test_recurring_full_sentence_goes_straight_to_the_recap():
    st = _conversation(
        "BUYER", "CREATE_RECURRING_NEED",
        {"product": "tomates", "quantity": 50, "unit": "kg", "recurrence_type": "WEEKLY_DAYS", "weekly_days": [1], "zone": "Ouaga"},
        BUYER, "J'ai besoin de 50 kg de tomates chaque lundi à Ouaga",
    )
    _no_question(st)
    assert "50 KG" in _reply(st) and "lundi" in _reply(st).lower()


@pytest.mark.parametrize("missing", ["price", "quantity"])
def test_only_what_is_truly_missing_is_asked(missing):
    entities = {"product": "oignon", "quantity": 300, "unit": "kg", "price": 250, "price_unit": "kg", "zone": "Bobo"}
    entities.pop(missing)
    if missing == "price":
        entities.pop("price_unit")
    st = _conversation("PRODUCER", "SALES_PUBLISH_PRODUCT", entities, SELLER, "J'ai des oignons à vendre")
    pending = st.get("pending_interaction") or {}
    assert pending.get("kind") == "ENTER_FIELD" and pending.get("field") == missing, pending
    assert st.get("missing_fields") == [missing], "UNE seule information manque — les autres sont conservées"
    payload = st.get("transaction_payload") or {}
    for kept in ("product", "zone"):
        assert payload.get(kept), payload


def test_a_price_is_never_copied_into_the_quantity():
    st = _conversation(
        "PRODUCER", "SALES_PUBLISH_PRODUCT",
        {"product": "oignon", "unit": "kg", "price": 250, "price_unit": "kg", "zone": "Bobo"},
        SELLER, "J'ai des oignons à vendre à 250 FCFA/kg",
    )
    assert (st.get("transaction_payload") or {}).get("quantity") in (None, 0, ""), st.get("transaction_payload")
    assert "250 kg" not in _reply(st)
    assert st.get("missing_fields") == ["quantity"]


def test_auction_full_sentence_keeps_quantity_ceiling_and_deadline():
    st = _conversation(
        "BUYER", "PROCUREMENT_CREATE_REQUEST",
        {"product": "pommes de terre", "quantity": 200, "unit": "kg", "price": 400, "price_unit": "kg", "deadline": "2026-10-09", "zone": "Ouaga"},
        BUYER, "Je cherche 200 kg de pommes de terre à moins de 400 FCFA/kg pour vendredi",
    )
    _no_question(st)
    text = _reply(st)
    assert "200 KG" in text and "pommes de terre" in text and "Prix plafond : 400 FCFA par kg" in text and "80 000 FCFA" in text
    assert (st.get("transaction_payload") or {}).get("deadline") == "2026-10-09"
