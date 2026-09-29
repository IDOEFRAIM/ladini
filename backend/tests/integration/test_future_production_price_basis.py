"""PRODUCTION_DECLARE_FUTURE — la base du prix n'est plus jamais devinée (Phase B2c.3).

`market_offers.price_per_unit` était écrit tel quel depuis `payload["price"]`, sans jamais demander
si « 4 000 000 » pour 10 tonnes voulait dire par tonne ou pour l'ensemble du lot. Ces tests tournent
sur le VRAI graphe compilé (harnais conversationnel, même famille que
`test_price_basis_conflict.py`/`test_commercial_pricing_vertical_slice.py`) : ils prouvent le CÂBLAGE
(le moteur `CommercialOffer` lui-même est déjà verrouillé par
`tests/unit/test_future_production_commercial_offer.py`).

État de départ SEEDÉ (`conv.seed`, même technique que
`test_conversation_characterization.py::_stale_catalog_state`) plutôt qu'un aller-retour NEW_TASK/
ACTIVE_SLOT complet : `farm_id`/`production_type`/`estimated_available_at` sont des slots hors
périmètre de cette phase (résolution déjà exercée ailleurs — `test_declare_future_production_
ownership.py`, `tests/nodes/test_confirmation_gate_certified_command.py`) ; ce fichier isole
STRICTEMENT le comportement NOUVEAU de B2c.3 : la base du prix."""
from __future__ import annotations

import pytest

from tests.harness import ConversationHarness

pytestmark = pytest.mark.integration

ETA = "2030-06-01"


def _answer(**entities):
    return {"disposition": "ANSWER", "extracted_entities": entities, "confidence": 0.9}


def _ready_state(**over):
    payload = {
        "farm_id": "farm-1", "production_type": "CROP", "product": "tomates",
        "quantity": 10.0, "unit": "TONNE", "estimated_available_at": ETA,
    }
    payload.update(over)
    return {
        "current_goal": "PRODUCTION_DECLARE_FUTURE",
        "status": "WAITING_INPUT",
        "user_role": "PRODUCER",
        "transaction_payload": payload,
        "working_memory": {"active_goal": "PRODUCTION_DECLARE_FUTURE"},
        "pending_interaction": {
            "kind": "ENTER_FIELD", "goal": "PRODUCTION_DECLARE_FUTURE", "field": "price",
            "status": "ACTIVE", "created_at": 0, "candidates": [], "context_ref": None, "target": None,
        },
    }


@pytest.fixture
def conv():
    with ConversationHarness(role="PRODUCER") as c:
        c.runtime.responses["get_farms"] = {
            "status": "success", "data": [{"id": "farm-1", "name": "Ferme Awa"}],
        }
        c.runtime.responses["get_or_create_farm"] = {
            "status": "success", "data": {"farm_id": "farm-1", "id": "farm-1", "name": "Ferme Awa"},
        }
        c.runtime.responses["declare_future_production"] = {
            "status": "success", "message": "ok",
            "data": {"market_offer_id": "mo-1", "display_label": "tomates", "farm_name": "Ferme Awa"},
        }
        yield c


def _confirm_and_get_call(conv):
    t = conv.send("oui")
    calls = dict(t.mcp_calls)
    assert "declare_future_production" in calls, (t.response, t.pending_after, t.mcp_calls)
    return calls["declare_future_production"]


class TestGoldenAPerBaseUnitOnTheRealGraph:
    def test_450000_la_tonne_is_certified_per_base_unit_and_persisted_normalized(self, conv):
        conv.seed(_ready_state())
        conv.send("400000 la tonne", llm=_answer(price=400000.0, price_unit="TONNE"))
        call = _confirm_and_get_call(conv)
        offer = call["payload"]["commercial_offer"]
        assert offer["pricing"]["basis"] == "PER_BASE_UNIT"
        assert offer["pricing"]["amount"] == 400000.0
        # dérivé en unité de base (KG) — jamais 400000 tel quel appliqué au kg.
        assert call["payload"]["unit"] == "KG"
        assert call["payload"]["price_per_unit"] == pytest.approx(400.0)


class TestGoldenBTotalLotOnTheRealGraph:
    def test_4_millions_pour_tout_never_multiplies_by_the_quantity(self, conv):
        conv.seed(_ready_state())
        conv.send("4 millions pour tout", llm=_answer(price=4_000_000.0))
        call = _confirm_and_get_call(conv)
        offer = call["payload"]["commercial_offer"]
        assert offer["pricing"]["basis"] == "TOTAL_LOT"
        assert offer["pricing"]["amount"] == 4_000_000.0


class TestGoldenCAmbiguousPriceNeverWritesAnything:
    def test_a_bare_amount_asks_and_never_calls_declare_future_production(self, conv):
        conv.seed(_ready_state())
        t = conv.send("4000000", llm=_answer(price=4_000_000.0))
        assert "declare_future_production" not in t.mcp_tools()
        assert (t.pending_after.kind.value, t.pending_after.field) == ("ENTER_FIELD", "price_basis")
        assert "par tonne" in t.response and "ensemble" in t.response

        # Le "oui" ne doit RIEN certifier tant que la base n'est pas tranchée.
        t2 = conv.send("oui")
        assert "declare_future_production" not in t2.mcp_tools()



# Goldens D (question-context) et E (correction de base) : le CÂBLAGE générique (le gate tourne pour
# ce goal, ENTER_FIELD price_basis se pose — golden C ci-dessus) est prouvé ici ; la résolution
# question-context/correction elle-même est un comportement du domaine PARTAGÉ avec
# SALES_PUBLISH_PRODUCT, déjà verrouillé exhaustivement (sans la fragilité du routage LLM d'un
# harnais multi-tours) par `tests/unit/test_future_production_commercial_offer.py::
# TestGoldenDQuestionContextNeverDependsOnTheLLM`/`TestGoldenECorrectionChangesTheBasis`.
