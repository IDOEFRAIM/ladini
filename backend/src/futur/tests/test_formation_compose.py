import sys
from pathlib import Path

import pytest

# Ensure backend/src is on sys.path when tests are executed from repo root
sys.path.append(str(Path(__file__).resolve().parents[2]))

from futur.formation.nodes import FormationConfig, compose_node


def _base_state(**overrides):
    state = {
        "user_query": "comment bien semer ?",
        "learner_profile": {},
        "warnings": [],
        "identified_crop": "MAIS",
        "crop_specs": {},
        "canvas_markdown": "",
        "retrieved_context": "",
        "sources": [],
        "technical_canvas": {},
        "advisor_error": True,
        "retrieval_mode": "DEGRADED",
        "degraded_mode": True,
        "localized_response": "",
        "localized_status": "UNAVAILABLE",
        "fertilizer_instructions": [],
        "fertilizer_breakdown": [],
        "ag_ui_payloads": [],
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_compose_node_fallback_without_localized_response():
    cfg = FormationConfig(enable_llm=False)
    state = _base_state()

    result = await compose_node(state, cfg)

    assert result["status"] == "ANSWER_GENERATED"
    assert "LLM n'est pas configuré" in result["final_response"]
    assert result["ag_ui_payloads"] == []


@pytest.mark.asyncio
async def test_compose_node_preserves_localized_payloads():
    cfg = FormationConfig(enable_llm=False)
    fertilizer_component = {
        "type": "DataTable",
        "props": {"title": "Plan", "columns": [], "rows": []},
    }
    state = _base_state(
        localized_response="message hyper localisé",
        localized_status="OK",
        fertilizer_instructions=["Apporter 2 tasses"],
        ag_ui_payloads=[fertilizer_component],
    )

    result = await compose_node(state, cfg)

    assert result["final_response"] == "message hyper localisé"
    assert result["ag_ui_payloads"] == [fertilizer_component]
    assert "Apporter 2 tasses" in result["agri_response"]["text"]
