import sys
import os
import json

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools import formation as form_mod


class DummyLLM:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                class Choice:
                    def __init__(self, content):
                        self.message = type("M", (), {"content": content})

                # Return a simple JSON for planner and analyzer
                payload = json.dumps({"optimized_query": "test", "modules": ["module1"], "prerequisites": [], "reasoning": "ok"})
                return type("R", (), {"choices": [Choice(payload)]})


def test_extract_json_block_with_plain_json():
    tool = form_mod.FormationTool(None)
    txt = '{"a":1, "b": 2}'
    assert tool._extract_json_block(txt)["a"] == 1


def test_plan_retrieval_fallback_when_no_llm():
    tool = form_mod.FormationTool(None)
    res = tool._plan_retrieval("Comment fertiliser?", {})
    assert "optimized_query" in res and res["warnings"] != []


def test_plan_retrieval_with_dummy_llm():
    tool = form_mod.FormationTool(DummyLLM())
    res = tool._plan_retrieval("Comment semer?", {"niveau": "débutant"})
    assert res["optimized_query"] == "test"
    assert "modules" in res
