"""Minimal real-code harness for backend/tests/evals YAML scenarios.

Built on top of the SAME deterministic, no-LLM, no-DB, no-network pattern
already established and used by this codebase's own test suite:

  - tests/conftest.py::StubRuntime / make_state
  - graphs/agents/market_coach/core/graph_builder.py::DemoRuntime,
    _run_producer_write_flow, demo_buyer_purchase_flow

Those existing helpers seed `current_goal`/`transaction_payload` directly and
chain REAL node functions together (validator -> context_resolver ->
confirmation_gate -> mcp_tool_executor -> final_response, or a flow-specific
function for handled_by_flow=True intents) — bypassing the LLM interpreter,
because there is no deterministic, offline way to run it. This harness does
the same thing, generalized to be driven by the YAML scenario files instead
of hand-written Python per scenario.

EXPLICIT SCOPE LIMITATION (do not remove this comment or hide this fact in
reports): because the LLM-based interpreter is bypassed exactly like the
rest of this repo's tests bypass it, THIS HARNESS DOES NOT EXERCISE THE
UNDERSTANDING DIMENSION (intent classification, entity extraction from raw
text). `expected_behavior.intent`/`.entities` fields in the YAML are used as
the DETERMINISTIC INPUT that seeds `current_goal`/`transaction_payload` for
a turn, not as something this harness independently derives from
`user_message` and then checks. Metrics genuinely exercised: Orchestration
(routing/confirmation/tool selection), Execution (tool arguments, MCP
success, state mutation), and the parts of Final Quality that are
deterministic (groundedness against the trace, not LLM-judged claims).

No LLM calls, no real database, no real MCP transport. Uses REAL node/flow
functions from `ladini.graphs.agents.market_coach`, imported and called
directly — never mocked at the node/flow level. Only the bottom-most `call_db`
boundary (the exact point where `_BaseGateway._call` hands off to
`mc_runtime.call_db`, see services/mcp/gateway.py:46-48) is a recording
double, matching this repo's own established testing philosophy
(conftest.py: "AUCUN réseau, AUCUN LLM réel, AUCUNE base de données").
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

DATASETS_DIR = Path(__file__).resolve().parents[1] / "datasets"


# =====================================================================
# Recording runtime — the ONLY test double in this harness, at the exact
# boundary MarketRuntime.call_db / _BaseGateway._call already establishes.
# =====================================================================

class RecordingRuntime:
    """Records every (tool_name, kwargs) pair passed through call_db.

    Configurable canned responses per tool. Default response is a generic
    success envelope — scenarios that need a specific shape (e.g. a search
    result, an error) pass `responses={tool_name: {...}}` or
    `responses={tool_name: Exception(...)}` to raise.
    """

    def __init__(self, responses: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._responses = responses or {}

    async def call_db(self, tool_name: str, **kwargs: Any) -> Any:
        self.calls.append((tool_name, dict(kwargs)))
        resp = self._responses.get(tool_name)
        if isinstance(resp, Exception):
            raise resp
        if resp is not None:
            return resp
        return {"status": "success", "data": {}, "message": f"{tool_name} ok"}

    @property
    def call_names(self) -> List[str]:
        return [c[0] for c in self.calls]


# =====================================================================
# Result types
# =====================================================================

@dataclass
class ScenarioResult:
    scenario_id: str
    status: str  # PASS | FAIL | BLOCKED | ERROR
    grounding_status: str
    actual_tool_calls: List[str] = field(default_factory=list)
    actual_tool_arguments: Dict[str, Any] = field(default_factory=dict)
    state_before: Dict[str, Any] = field(default_factory=dict)
    state_after: Dict[str, Any] = field(default_factory=dict)
    failed_assertions: List[str] = field(default_factory=list)
    block_reason: Optional[str] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def load_scenario(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# =====================================================================
# Small, reusable validation primitives — used by both the batch runners
# above and the harness's own pytest regression tests
# (tests/evals/runners/test_harness.py).
# =====================================================================

VALID_GATES = {
    "unauthorized_write", "confirmation_bypass", "cross_user_data_access",
    "permission_violation", "fabricated_transaction", "dangerous_tool_execution",
    "prompt_injection_failure", None,
}


def validate_gate(value: Any) -> bool:
    """True iff `value` is a member of the canonical Contract §7 gate enum, or None."""
    return value in VALID_GATES


def find_duplicate_scenario_ids(dataset_dir: Path) -> Dict[str, List[Path]]:
    """Returns {scenario_id: [paths]} for every scenario_id claimed by 2+ files."""
    seen: Dict[str, List[Path]] = {}
    for p in dataset_dir.rglob("*.yaml"):
        data = load_scenario(p)
        sid = data.get("scenario_id")
        if sid:
            seen.setdefault(sid, []).append(p)
    return {sid: paths for sid, paths in seen.items() if len(paths) > 1}


def assert_expected_tools_called(expected: List[str], actual: List[str]) -> List[str]:
    """Returns a list of failure messages (empty = pass). Every tool in
    `expected` must appear somewhere in `actual` (order not checked here —
    ordered-sequence checking is a separate, stricter helper below)."""
    failures = []
    for tool in expected:
        if tool not in actual:
            failures.append(f"expected tool {tool!r} was not called; actual calls={actual}")
    return failures


def assert_no_forbidden_tools(forbidden: List[str], actual: List[str]) -> List[str]:
    """Returns a list of failure messages (empty = pass) for any forbidden
    tool that DID appear in actual."""
    failures = []
    for tool in forbidden:
        if tool in actual:
            failures.append(f"forbidden tool {tool!r} WAS called; actual calls={actual}")
    return failures


def assert_state_assertion(expected: Dict[str, Any], actual_state: Dict[str, Any]) -> List[str]:
    """Dotted-key state assertion, e.g. {'preorder_workflow.phase': 'CONFIRMED'}.
    Returns a list of failure messages (empty = pass)."""
    failures = []
    for dotted_key, expected_value in expected.items():
        if dotted_key == "workspace":
            continue  # descriptive only, not a state key
        node: Any = actual_state
        for part in dotted_key.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        if expected_value == "NOT_NULL":
            if node is None:
                failures.append(f"expected {dotted_key!r} to be non-null, got None")
        elif expected_value == "UNCHANGED":
            continue  # caller must diff before/after separately; not checkable from actual_state alone
        elif node != expected_value:
            failures.append(f"expected {dotted_key!r} == {expected_value!r}, got {node!r}")
    return failures


def find_scenario(scenario_id: str) -> Path:
    for p in DATASETS_DIR.rglob("*.yaml"):
        data = load_scenario(p)
        if data.get("scenario_id") == scenario_id:
            return p
    raise FileNotFoundError(f"scenario_id {scenario_id!r} not found under {DATASETS_DIR}")


# =====================================================================
# Dispatch — which real function(s) handle a given intent.
#
# GENERIC_CHAIN_INTENTS: non-tunnel intents, PROVEN this session to be
#   dispatched via validator -> context_resolver -> confirmation_gate ->
#   mcp_tool_executor -> final_response (same pattern as
#   graph_builder.py::_run_producer_write_flow).
#
# FLOW_DISPATCH: handled_by_flow=True intents with a specific real function,
#   traced individually. Anything NOT in either table is BLOCKED — this
#   harness never falls back to guessing a dispatch strategy.
# =====================================================================

# (2026-09-13, Deep Intent Architecture Cleanup) : STOCK_DELETE/
# STOCK_UPDATE_LEVEL/STOCK_ADJUST/STOCK_REMOVE_PARTIAL/STOCK_RECORD_MOVEMENT/
# SALES_ACCEPT_CONTRACT/PROCUREMENT_ACCEPT_OFFER retirés — tous supprimés
# d'INTENT_CONFIG (tool_name fictif, jamais réellement exécutable).
GENERIC_CHAIN_INTENTS = {
    "STOCK_REGISTER_HARVEST",
    "SALES_PUBLISH_PRODUCT", "SALES_RECORD_DIRECT", "SALES_PLACE_BID",
    "FINANCE_LOG_EXPENSE",
    "PROCUREMENT_CREATE_REQUEST",
    "STOCK_GET_SUMMARY", "SALES_LIST_ORDERS",
}
# NOTE: SALES_UPDATE_PRODUCT / PRODUCTION_UPDATE_FUTURE (anciennement
# SALES_UPDATE_PRODUCTION) deliberately excluded — traced to
# _resolve_product_for_update / _resolve_cycle_for_update, a self-managed
# "mini state machine" per producer/flow.py, NOT the generic chain. See
# P1-SALES-021 result below for what actually happens when run.


async def _run_generic_chain(
    state: Dict[str, Any],
    runtime: RecordingRuntime,
    *,
    role: str,
    confirm: bool,
    with_role_guard: bool = False,
) -> Dict[str, Any]:
    """The generic non-tunnel WRITE/READ chain, proven via
    graph_builder.py::_run_producer_write_flow. Generalized here to accept
    either role (using buyer_context_resolver or producer_context_resolver)
    and to optionally prepend role_guard.

    IMPORTANT, discovered this session: nodes/role_guard.py's OWN docstring
    states role-based blocking was REMOVED from the graph — "il n'y a plus
    de role de session a faire respecter ici" — it now only fills a default
    display role, never blocks. with_role_guard=True still calls the REAL
    function (not a mock of what it "should" do) so that a scenario
    premised on role blocking gets an HONEST result reflecting current code,
    not an assumed one.
    """
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )
    from ladini.graphs.agents.market_coach.nodes.executor import (
        mcp_tool_executor,
    )
    from ladini.graphs.agents.market_coach.nodes.response_handlers import (
        final_response,
    )
    from ladini.graphs.agents.market_coach.nodes.role_guard import (
        make_role_guard,
    )
    from ladini.graphs.agents.market_coach.nodes.validation import validator

    if with_role_guard:
        role_guard = make_role_guard(role)
        rg = await role_guard(state, runtime)
        state.update(rg)

    v = await validator(state, runtime)
    state.update(v)
    if state.get("status") == "WAITING_INPUT":
        return state

    if role == "PRODUCER":
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            producer_context_resolver,
        )
        r = await producer_context_resolver(state, runtime)
    else:
        from ladini.graphs.agents.market_coach.flows.buyer.flow import (
            buyer_context_resolver,
        )
        r = await buyer_context_resolver(state, runtime)
    state.update(r)
    if str(state.get("status") or "").upper() in {"WAITING_INPUT", "ERROR", "COMPLETED"}:
        return state

    c1 = await confirmation_gate(state, runtime)
    state.update(c1)
    if state.get("status") != "WAITING_CONFIRMATION":
        # READ goal (auto-authorized) or already resolved another way.
        if state.get("status") == "EXECUTING":
            e = await mcp_tool_executor(state, runtime)
            state.update(e)
            resp = await final_response(state, runtime)
            state.update(resp)
        return state

    if not confirm:
        return state  # scenario only wants to observe the pending-confirmation state

    state["interpreted_event"] = "CONFIRM"
    c2 = await confirmation_gate(state, runtime)
    state.update(c2)
    if state.get("status") != "EXECUTING":
        return state  # e.g. rejected/aborted before reaching execution

    e = await mcp_tool_executor(state, runtime)
    state.update(e)
    resp = await final_response(state, runtime)
    state.update(resp)
    return state


async def _run_off_topic_during_confirmation(
    state: Dict[str, Any], runtime: RecordingRuntime, *, role: str, off_topic_text: str
) -> Dict[str, Any]:
    """Raises a confirmation, then sends an unrelated message instead of
    CONFIRM/REJECT — exercises confirmation_gate's deviation-note branch
    (nodes/confirmation_gate.py:161-209) directly, matching P0-SEC-001.
    """
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )
    from ladini.graphs.agents.market_coach.nodes.validation import validator

    v = await validator(state, runtime)
    state.update(v)
    if state.get("status") == "WAITING_INPUT":
        return state

    if role == "PRODUCER":
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            producer_context_resolver,
        )
        r = await producer_context_resolver(state, runtime)
    else:
        from ladini.graphs.agents.market_coach.flows.buyer.flow import (
            buyer_context_resolver,
        )
        r = await buyer_context_resolver(state, runtime)
    state.update(r)

    c1 = await confirmation_gate(state, runtime)
    state.update(c1)
    if state.get("status") != "WAITING_CONFIRMATION":
        return state

    # Second turn: unrelated message, no CONFIRM/REJECT event.
    state["normalized_text"] = off_topic_text
    state["user_query"] = off_topic_text
    state["interpreted_event"] = "UNKNOWN"
    c2 = await confirmation_gate(state, runtime)
    state.update(c2)
    return state


# =====================================================================
# GPS_SHARE — minimal representation, not a general event framework.
#
# PROVEN this session (order_tracking.py::finalize_winner,
# gps_delivery_gate.py::_get_stored_location): a native WhatsApp location
# share is NOT threaded through the graph state as delivery_lat/lon
# directly. The webhook persists it to the user's profile out-of-band, and
# the GPS-gated flows re-read it via ProfileGateway.get_user_by_phone
# (data.latitude/data.longitude) when `location_shared=True`. This helper
# captures exactly that real mechanism — two things, not an abstraction:
# (1) the state flags a real GPS-share turn sets, (2) the exact runtime
# response shape that mechanism reads from.
# =====================================================================

def gps_share_state_patch() -> Dict[str, Any]:
    """State fields a real native-location-share turn sets, per
    order_tracking.py:991 (`location_shared = bool(state.get("location_shared"))`)."""
    return {"location_shared": True, "interpreted_event": "GPS_SHARE"}


def gps_share_runtime_response(lat: float, lon: float) -> Dict[str, Any]:
    """The get_user_by_phone response shape _get_stored_location actually
    reads (gps_delivery_gate.py:59-64: result['data']['latitude'/'longitude'])."""
    return {"status": "success", "data": {"latitude": lat, "longitude": lon}}
