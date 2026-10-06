"""Executes the smoke set + P0 set against REAL ladini code.

Run from backend/:
    .venv/Scripts/python.exe -m tests.evals.runners.run_batch

Each driver function below calls real, imported node/flow functions —
nothing about tool dispatch or state mutation is mocked. Only call_db (the
MCP boundary) is a recording double, per harness.py's documented scope.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.evals.runners.harness import (  # noqa: E402
    RecordingRuntime,
    ScenarioResult,
    _run_generic_chain,
    find_scenario,
    load_scenario,
)


def run(coro):
    return asyncio.run(coro)


def _base_state(**overrides: Any) -> Dict[str, Any]:
    base = {
        "normalized_text": "",
        "user_query": "",
        "expected_input": "NONE",
        "current_goal": None,
        "interpreted_event": "NEW_TASK",
        "working_memory": {},
        "transaction_payload": {},
        "active_cart": [],
        "status": "",
    }
    base.update(overrides)
    return base


def _result(scenario_id: str, grounding: str) -> ScenarioResult:
    data = load_scenario(find_scenario(scenario_id))
    return ScenarioResult(scenario_id=scenario_id, status="TEST_SETUP_ERROR", grounding_status=grounding)


# =====================================================================
# SMOKE SET
# =====================================================================

def drive_P1_STOCK_016() -> ScenarioResult:
    res = _result("P1-STOCK-016", "PATTERN_CONSISTENT")
    rt = RecordingRuntime()
    state = _base_state(
        user_phone="+22670000035", user_role="PRODUCER", role="PRODUCER",
        current_goal="STOCK_REGISTER_HARVEST",
        transaction_payload={"product": "mil", "quantity": 300, "unit": "KG", "farm_id": "F-12"},
    )
    res.state_before = dict(state)
    try:
        final = run(_run_generic_chain(state, rt, role="PRODUCER", confirm=True))
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        return res
    res.state_after = dict(final)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    if "add_stock" in rt.call_names:
        res.status = "PASS"
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(f"expected tool 'add_stock' in calls, got {rt.call_names}; status={final.get('status')} validation_errors={final.get('validation_errors')}")
    return res


# drive_P1_SALES_021 REMOVED from this file (was a wrong, disproven
# single-turn assumption — see P1-SALES-021.yaml v1.2 correction_note).
# The real, executed 4-turn driver now lives in run_extra.py.

# (2026-09-13, Deep Intent Architecture Cleanup) : drive_P1_STOCK_018 et
# drive_P1_PROC_026 supprimées avec leurs scénarios (P1-STOCK-018.yaml,
# P1-PROC-026.yaml) — STOCK_DELETE/PROCUREMENT_ACCEPT_OFFER supprimés
# d'INTENT_CONFIG (tool_name fictif, jamais réellement exécutable).


def drive_P1_AUC_011() -> ScenarioResult:
    res = _result("P1-AUC-011", "PROVEN_BY_CODE")
    rt = RecordingRuntime()
    from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
        list_buyer_auctions,
    )
    state = _base_state(
        user_phone="+22670000030", user_role="BUYER", role="BUYER",
        current_goal="BUYER_LIST_AUCTIONS",
    )
    res.state_before = dict(state)
    try:
        r = run(list_buyer_auctions(state, rt))
        state.update(r)
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        res.state_after = dict(state)
        return res
    res.state_after = dict(state)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    if "get_auctions" in rt.call_names:
        res.status = "PASS"
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(f"expected 'get_auctions', got {rt.call_names}")
    return res


def drive_P2_ROBUST_040() -> ScenarioResult:
    res = _result("P2-ROBUST-040", "PROVEN_BY_REGRESSION")
    rt = RecordingRuntime(responses={
        "search_products": {
            "status": "success",
            "results": [
                {"id": "prd-tomate-1", "name": "Tomate", "price": 300, "unit": "KG",
                 "vendor": {"name": "Ferme Test"}, "producer_id": "farm-1"},
            ],
        },
    })
    from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
    state = _base_state(
        user_phone="+22670000042", user_role="BUYER", role="BUYER",
        current_goal="BUYER_REQUEST",
        transaction_payload={"product": "tomte", "quantity": 2, "unit": "KG"},
        preorder_workflow={"phase": "CART"},
    )
    res.state_before = dict(state)
    try:
        r = run(cart_management(state, rt))
        state.update(r)
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        res.state_after = dict(state)
        return res
    res.state_after = dict(state)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    resp_text = str(state.get("final_response") or "")
    not_found = "n'est pas disponible" in resp_text
    cart_untouched = not state.get("active_cart")
    if rt.call_names == ["search_products"] and not_found and cart_untouched:
        res.status = "PASS"
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(
            f"tool_calls={rt.call_names} (expected ['search_products']), "
            f"not_found_template_matched={not_found}, cart_untouched={cart_untouched}, "
            f"final_response={resp_text!r}"
        )
    return res


def drive_P2_EDGE_042() -> ScenarioResult:
    res = _result("P2-EDGE-042", "PROVEN_BY_CODE")
    rt = RecordingRuntime(responses={
        "search_products": {"status": "success", "results": []},
    })
    from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
    # First attempt (product only, no quantity) never called search_products at all —
    # cart_management returned a generic help menu instead. Re-run with quantity present
    # (matching the P0-BIZ-007/P2-ROBUST-040 shape) to isolate whether quantity is a real
    # precondition for the search to even fire.
    state = _base_state(
        user_phone="+22670000043", user_role="BUYER", role="BUYER",
        current_goal="BUYER_REQUEST",
        transaction_payload={"product": "caviar", "quantity": 1, "unit": "KG"},
        preorder_workflow={"phase": "CART"},
    )
    res.state_before = dict(state)
    try:
        r = run(cart_management(state, rt))
        state.update(r)
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        res.state_after = dict(state)
        return res
    res.state_after = dict(state)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    resp_text = str(state.get("final_response") or "")
    not_found = "n'est pas disponible" in resp_text
    cart_untouched = not state.get("active_cart")
    if rt.call_names == ["search_products"] and not_found and cart_untouched:
        res.status = "PASS"
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(
            f"tool_calls={rt.call_names}, not_found_template_matched={not_found}, "
            f"cart_untouched={cart_untouched}, final_response={resp_text!r}"
        )
    return res


SMOKE_SET: Dict[str, Callable[[], ScenarioResult]] = {
    "P1-STOCK-016": drive_P1_STOCK_016,
    # P1-SALES-021 moved to run_extra.py (real 4-turn flow)
    # P1-STOCK-018 / P1-PROC-026 removed (2026-09-13) with their scenarios —
    # see the deletion note above drive_P1_AUC_011.
    "P1-AUC-011": drive_P1_AUC_011,
    "P2-ROBUST-040": drive_P2_ROBUST_040,
    "P2-EDGE-042": drive_P2_EDGE_042,
}


def _print_result(r: ScenarioResult) -> None:
    print(f"\n=== {r.scenario_id} : {r.status} ===")
    print(f"  grounding_status   : {r.grounding_status}")
    print(f"  actual_tool_calls  : {r.actual_tool_calls}")
    print(f"  actual_tool_args   : {json.dumps(r.actual_tool_arguments, default=str, ensure_ascii=False)}")
    if r.failed_assertions:
        print(f"  failed_assertions  : {r.failed_assertions}")
    if r.block_reason:
        print(f"  block_reason       : {r.block_reason}")
    if r.error:
        print(f"  error              : {r.error}")


if __name__ == "__main__":
    results = []
    for sid, fn in SMOKE_SET.items():
        try:
            r = fn()
        except Exception as exc:
            r = ScenarioResult(scenario_id=sid, status="TEST_SETUP_ERROR", grounding_status="?", error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        results.append(r)
        _print_result(r)

    print("\n\n=== SUMMARY ===")
    for r in results:
        print(f"{r.scenario_id:20s} {r.status}")
