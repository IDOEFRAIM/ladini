"""Drivers for scenarios corrected/added during the consolidation pass:
P1-CAP-003 (replaces retired P0-SEC-003), P1-SALES-021 (real 4-turn flow),
the GPS pair P0-GEO-008/P1-PROC-008 (finalize_winner argument-threading),
and P1-NEG-006 (real 3-turn negotiation-to-acceptance flow).

Kept separate from run_batch.py/run_p0.py so those two files' existing
SMOKE_SET/P0_SET don't have to absorb scenarios that don't fit their
pre-existing dispatch shape (some need >2 turns, or a non-generic-chain
flow function called directly). No new framework — same RecordingRuntime,
same run()/_base_state()/_result() helpers, imported from run_batch.py.

Run from backend/:
    .venv/Scripts/python.exe -m tests.evals.runners.run_extra
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.evals.runners.harness import (  # noqa: E402
    RecordingRuntime,
    ScenarioResult,
    _run_generic_chain,
    gps_share_runtime_response,
    gps_share_state_patch,
)
from tests.evals.runners.run_batch import _base_state, _result, run  # noqa: E402


def _print_result(r: ScenarioResult) -> None:
    print(f"\n=== {r.scenario_id} : {r.status} ===")
    print(f"  grounding_status   : {r.grounding_status}")
    print(f"  actual_tool_calls  : {r.actual_tool_calls}")
    print(f"  actual_tool_args   : {json.dumps(r.actual_tool_arguments, default=str, ensure_ascii=False)}")
    if r.failed_assertions:
        print(f"  failed_assertions  : {r.failed_assertions}")
    if r.error:
        print(f"  error              : {r.error}")


# =====================================================================
# P1-CAP-003 — dual-role capability (BUYER-workspace STOCK_REGISTER_HARVEST)
# =====================================================================

def drive_P1_CAP_003() -> ScenarioResult:
    res = _result("P1-CAP-003", "PROVEN_BY_CODE")
    rt = RecordingRuntime(responses={
        "add_stock": {"status": "success", "data": {"stock_id": "ST-901"}, "message": "Stock ajouté."},
    })
    state = _base_state(
        user_phone="+22670000003", user_role="BUYER", role="BUYER",
        current_goal="STOCK_REGISTER_HARVEST",
        transaction_payload={"product": "mil", "quantity": 50, "unit": "KG", "farm_id": "F-12"},
    )
    res.state_before = dict(state)
    try:
        final = run(_run_generic_chain(state, rt, role="BUYER", confirm=True))
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
        res.failed_assertions.append(f"expected 'add_stock', got {rt.call_names}; status={final.get('status')}")
    return res


# =====================================================================
# P1-SALES-021 — real 4-turn SALES_UPDATE_PRODUCT mini state machine
# =====================================================================

def drive_P1_SALES_021() -> ScenarioResult:
    res = _result("P1-SALES-021", "PROVEN_BY_EXECUTION")
    rt = RecordingRuntime(responses={
        "get_my_products": {
            "status": "success",
            "data": [{"id": "PRD-100", "product": "Tomates", "quantity": 50, "unit": "KG", "price": 300}],
        },
        "update_product_price_and_qty": {"status": "success", "message": "Produit mis à jour."},
    })
    from ladini.graphs.agents.market_coach.flows.producer.flow import (
        producer_context_resolver,
    )
    state = _base_state(
        user_phone="+22670000037", user_role="PRODUCER", role="PRODUCER",
        current_goal="SALES_UPDATE_PRODUCT",
    )
    res.state_before = dict(state)
    try:
        # Turn 1 — open the flow, no field yet: shows the catalog menu.
        r = run(producer_context_resolver(state, rt))
        state.update(r)
        # Turn 2 — bare selection "1" -> PRD-100 (real code reads
        # payload.get("product_id"), not an index; index->product_id
        # resolution is interpreter-layer, so product_id is seeded directly).
        state["transaction_payload"] = {"product_id": "PRD-100"}
        state["normalized_text"] = "1"
        r = run(producer_context_resolver(state, rt))
        state.update(r)
        # Turn 3 — field + value in one message.
        state["transaction_payload"] = {"price": 400}
        state["normalized_text"] = "prix 400"
        r = run(producer_context_resolver(state, rt))
        state.update(r)
        # Turn 4 — confirm ("Oui"). normalized_text MUST change from turn 3's
        # "prix 400" — the CONFIRM-phase branch re-parses text for a
        # correction FIRST (_parse_update_correction), so leaving "prix 400"
        # in place would re-trigger the correction branch instead of executing.
        state["transaction_payload"] = {}
        state["normalized_text"] = "Oui"
        state["interpreted_event"] = "CONFIRM"
        r = run(producer_context_resolver(state, rt))
        state.update(r)
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        res.state_after = dict(state)
        return res
    res.state_after = dict(state)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    if "update_product_price_and_qty" in rt.call_names:
        res.status = "PASS"
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(
            f"expected 'update_product_price_and_qty' by turn 4, got tool_calls={rt.call_names}, "
            f"status={state.get('status')}, working_memory={state.get('working_memory')}"
        )
    return res


# =====================================================================
# GPS PAIR — P0-GEO-008 (out-of-bounds) / P1-PROC-008 (in-bounds)
# Same code path (order_tracking.py::finalize_winner), only the shared
# get_user_by_phone canned coordinates differ.
# =====================================================================

def _drive_finalize_winner(scenario_id: str, grounding: str, bid_id: str, phone: str, lat: float, lon: float) -> ScenarioResult:
    res = _result(scenario_id, grounding)
    rt = RecordingRuntime(responses={
        "get_user_by_phone": gps_share_runtime_response(lat, lon),
        "select_winning_bid": {"status": "success", "summary_buyer": "Offre retenue, commande créée."},
    })
    from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
        finalize_winner,
    )
    state = _base_state(
        user_phone=phone, user_role="BUYER", role="BUYER",
        current_goal="BUYER_CHECK_AUCTION_STATUS",
        working_memory={"pending_winner_bid": bid_id},
        interpreted_event="CONFIRM",
    )
    res.state_before = dict(state)
    try:
        # Turn 1 — confirm the winner itself -> enters GPS stage.
        r = run(finalize_winner(state, rt))
        state.update(r)
        # Turn 2 — native GPS share.
        state.update(gps_share_state_patch())
        r = run(finalize_winner(state, rt))
        state.update(r)
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        res.state_after = dict(state)
        return res
    res.state_after = dict(state)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    call_args = dict(rt.calls)
    win_args = None
    for t, a in rt.calls:
        if t == "select_winning_bid":
            win_args = a
    if win_args and win_args.get("delivery_lat") == lat and win_args.get("delivery_lon") == lon:
        res.status = "PASS"
        res.notes.append(
            "PASS = dispatch mechanism proven only (correct args threaded to select_winning_bid). "
            "This does NOT assert rejection/acceptance of out-of-bounds coordinates — that DB-layer "
            "property is out of this harness's reach, see grounding.critical_correction in the YAML."
        )
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(f"expected select_winning_bid(delivery_lat={lat}, delivery_lon={lon}), got {win_args}")
    return res


def drive_P0_GEO_008() -> ScenarioResult:
    return _drive_finalize_winner(
        "P0-GEO-008", "PROVEN_BY_EXECUTION (dispatch mechanism)",
        bid_id="BID-9001", phone="+22670000008", lat=5.31, lon=-4.02,
    )


def drive_P1_PROC_008() -> ScenarioResult:
    return _drive_finalize_winner(
        "P1-PROC-008", "PROVEN_BY_EXECUTION",
        bid_id="BID-1002", phone="+22670000027", lat=12.37, lon=-1.52,
    )


# =====================================================================
# P1-NEG-006 — real 3-turn negotiation: initiate -> view offers -> accept
# =====================================================================

def drive_P1_NEG_006() -> ScenarioResult:
    res = _result("P1-NEG-006", "PROVEN_BY_EXECUTION")
    rt = RecordingRuntime(responses={
        "search_products": {"status": "success", "results": [
            {"id": "PRD-500", "price": 200, "unit": "KG", "vendor_name": "Producteur X", "producer_id": "PROD-1"},
        ]},
        "initiate_negotiation_session": {
            "status": "PENDING", "message": "Négociation ouverte avec Producteur X.",
            "negotiation_id": "NEG-9001", "auction_id": "AUC-9001", "product_id": "PRD-500",
            "producer_id": "PROD-1", "buyer_offer": 180, "seller_minimum": 190,
        },
        "get_auction_bids": {"status": "success", "bids": [
            {"bid_id": "BID-7001", "producer": "Producteur X", "price": 195},
        ]},
        "select_winning_bid": {"status": "success", "summary_buyer": "Offre acceptée, livraison en cours."},
    })
    from ladini.graphs.agents.market_coach.flows.buyer.negotiation import (
        negotiation_gate,
    )
    state = _base_state(
        user_phone="+22670000044", user_role="BUYER", role="BUYER",
        current_goal="BUYER_NEGOTIATE_PRICE",
        transaction_payload={"product": "maïs", "price": 180},
    )
    res.state_before = dict(state)
    try:
        # Turn 1 — initiate negotiation.
        r = run(negotiation_gate(state, rt))
        state.update(r)
        # Turn 2 — menu selection "1" (view offers), resolved by real, non-LLM helper.
        state["transaction_payload"] = {"selection_index": "1"}
        r = run(negotiation_gate(state, rt))
        state.update(r)
        # Turn 3 — accept bid BID-7001 (bid_id seeded directly, same pattern as P1-PROC-026).
        state["transaction_payload"] = {"bid_id": "BID-7001"}
        r = run(negotiation_gate(state, rt))
        state.update(r)
    except Exception as exc:
        res.status = "TEST_SETUP_ERROR"
        res.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        res.state_after = dict(state)
        return res
    res.state_after = dict(state)
    res.actual_tool_calls = rt.call_names
    res.actual_tool_arguments = {t: a for t, a in rt.calls}
    win_args = None
    for t, a in rt.calls:
        if t == "select_winning_bid":
            win_args = a
    if win_args and win_args.get("phone") is None and win_args.get("delivery_lat") is None:
        res.status = "PASS"
        res.notes.append(
            "PASS confirms the PROVEN argument shape (phone=None, delivery_lat=None, delivery_lon=None) "
            "-- see grounding.new_finding_this_session in the YAML. Whether the real MCP layer rejects "
            "phone=None (PermissionDenied per the gateway's own comment) is BLOCKED_PENDING_CODE_TRACE, "
            "not verifiable by this no-real-DB harness."
        )
    elif "select_winning_bid" in rt.call_names:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(f"expected select_winning_bid(phone=None, delivery_lat=None, delivery_lon=None), got {win_args}")
    else:
        res.status = "FAIL_CORRECTNESS"
        res.failed_assertions.append(f"select_winning_bid was never called; tool_calls={rt.call_names}, status={state.get('status')}")
    return res


EXTRA_SET: Dict[str, Callable[[], ScenarioResult]] = {
    "P1-CAP-003": drive_P1_CAP_003,
    "P1-SALES-021": drive_P1_SALES_021,
    "P0-GEO-008": drive_P0_GEO_008,
    "P1-PROC-008": drive_P1_PROC_008,
    "P1-NEG-006": drive_P1_NEG_006,
}


if __name__ == "__main__":
    results = []
    for sid, fn in EXTRA_SET.items():
        try:
            r = fn()
        except Exception as exc:
            r = ScenarioResult(scenario_id=sid, status="TEST_SETUP_ERROR", grounding_status="?", error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        results.append(r)
        _print_result(r)
        if r.notes:
            print(f"  notes              : {r.notes}")

    print("\n\n=== EXTRA SUMMARY ===")
    for r in results:
        print(f"{r.scenario_id:20s} {r.status}")
