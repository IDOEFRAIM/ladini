import json


def test_sentinelle_workflow_completes():
    from agriconnect.graphs.nodes.sentinelle import ClimateSentinel

    agent = ClimateSentinel()
    wf = agent.build()

    state = {
        "user_query": "Y aura-t-il du risque hydrique cette semaine ?",
        "location_profile": {"village": "Testville"},
    }

    result = wf.invoke(state)
    assert isinstance(result, dict)
    # Ensure we don't loop forever and we return a final status
    assert "status" in result
    # Allow either a composed answer or a degraded/no-context status
    assert result["status"] in ("ANSWER_READY", "NO_CONTEXT", "DEGRADED_MODE", "LLM_ERROR", "REJECTED", "CONTEXT_READY", "CONTEXT_FOUND", "DONE")
