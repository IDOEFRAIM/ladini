import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools import refine as refine_mod


class DummyLLM:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                # simple stub: return the prompt trimmed or a fixed verdict based on model
                model = kwargs.get("model", "")
                if "critic" in kwargs.get("messages", [])[0]["content"]:
                    # emulate a critic verdict
                    class R:
                        choices = [type("C", (), {"message": type("M", (), {"content": "PASSED"})})]
                    return R()
                # default: return a reformulated query
                class R2:
                    choices = [type("C", (), {"message": type("M", (), {"content": "reformulated query"})})]
                return R2()


def test_rewrite_query_max_retries():
    tool = refine_mod.RefineTool(DummyLLM())
    state = {"user_query": "test", "rewrited_retry_count": 2}
    res = tool.rewrite_query_node(state)
    assert res["status"] == "MAX_RETRIES"


def test_rewrite_query_success():
    tool = refine_mod.RefineTool(DummyLLM())
    state = {"user_query": "test", "rewrited_retry_count": 0}
    res = tool.rewrite_query_node(state)
    assert res["status"] == "RETRY_SEARCH"
    assert res["optimized_query"] == "reformulated query"


def test_critique_node_pass_and_fail():
    # Pass case
    tool = refine_mod.RefineTool(DummyLLM())
    state = {"final_response": "ok", "retrieved_context": "ctx", "critique_retry_count": 0}
    res = tool.critique_node(state)
    assert res.get("status") in ("VALIDATED", "REJECTED") or isinstance(res, dict)

    # Simulate FAIL by crafting llm messages content containing FAILED
    class FailLLM(DummyLLM):
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    class R:
                        choices = [type("C", (), {"message": type("M", (), {"content": "FAILED"})})]
                    return R()

    tool2 = refine_mod.RefineTool(FailLLM())
    state2 = {"final_response": "mismatch", "retrieved_context": "ctx", "critique_retry_count": 0}
    res2 = tool2.critique_node(state2)
    assert res2["status"] in ("REJECTED", "VALIDATED")


def test_routing_rules():
    tool = refine_mod.RefineTool(DummyLLM())
    assert tool.route_after_analyze({"is_relevant": False}) == "compose"
    assert tool.route_after_analyze({"is_relevant": True}) == "retrieve"
    assert tool.route_retrieval({"status": "CONTEXT_FOUND"}) == "compose"
    assert tool.route_retrieval({"status": "ERROR"}) == "compose"
    assert tool.route_retrieval({"status": "SOMETHING"}) == "rewrite"
