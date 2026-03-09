"""Thin flow module assembling the LangGraph using decomposed node modules.

This module provides a small `build_graph` helper that mirrors the
previous `MessageResponseFlow.build_graph` behavior but imports nodes
from the new layout. It keeps runtime behavior unchanged while making
the top-level flow easier to read and maintain.

Fan-out / Fan-in (LangGraph Send)
----------------------------------
Instead of a single ``PARALLEL_EXPERTS`` node backed by a ThreadPoolExecutor,
the routing function emits ``Send("RUN_EXPERT", ...)`` objects — one per
selected expert.  LangGraph runs them concurrently and the ``operator.add``
reducer on ``expert_responses`` merges results before ``SYNTHESIZE``.
"""
from langgraph.graph import StateGraph, END
from agriconnect.graphs.state import GlobalAgriState


def build_graph(flow_instance):
    workflow = StateGraph(GlobalAgriState)

    workflow.add_node("ANALYZE", flow_instance.analyze_needs)
    workflow.add_node("EXECUTE_CHAT", flow_instance.execute_chat)
    # RUN_EXPERT replaces both SOLO_AGENT and PARALLEL_EXPERTS.
    # route_flow() returns Send("RUN_EXPERT", …) — one per expert.
    workflow.add_node("RUN_EXPERT", flow_instance.run_expert_node)
    # HITL gate: suspends execution via interrupt() when requires_validation=True
    workflow.add_node("HITL_GATE", flow_instance.hitl_gate)
    workflow.add_node("SYNTHESIZE", flow_instance.synthesize_results)

    workflow.add_node("GENERATE_AUDIO", flow_instance.generate_audio)
    workflow.add_node("PERSIST", flow_instance.persist)
    workflow.add_node("REJECT", flow_instance.execute_rejection)

    workflow.set_entry_point("ANALYZE")

    # route_flow returns either a plain string or a list[Send].
    # The path_map lists every node reachable from ANALYZE.
    workflow.add_conditional_edges(
        "ANALYZE",
        flow_instance.route_flow,
        ["EXECUTE_CHAT", "RUN_EXPERT", "REJECT"],
    )

    # Fan-in: all RUN_EXPERT branches → HITL_GATE → SYNTHESIZE
    workflow.add_edge("RUN_EXPERT", "HITL_GATE")
    workflow.add_edge("HITL_GATE", "SYNTHESIZE")

    for node in ["EXECUTE_CHAT", "SYNTHESIZE", "REJECT"]:
        workflow.add_edge(node, "GENERATE_AUDIO")

    workflow.add_edge("GENERATE_AUDIO", "PERSIST")
    workflow.add_edge("PERSIST", END)

    return workflow


__all__ = ["build_graph"]
