"""Definitive round-trip proof for `tier_selection_context` persistence.

Every previous fix in this saga (declaring the LangGraph channel, registering
it as DURABLE in `core/state_profile.py`, removing the fragile product_id
re-match in `cart.py`) was verified only at the NODE level — `cart_management`
called directly with a hand-built `state` dict. That NEVER exercises the
actual save/flush/reload cycle a real turn goes through, so none of those
tests could have caught a genuine checkpointer-layer bug.

This test replicates the REAL orchestrator sequence exactly (see
`orchestrator.py::_SessionGuard.attach/detach` + `_flush_workspace`):
  attach_workspace → aput (staged in RAM, mid-turn) → finalize_for_persistence
  (the flush-time prune) → store.save → detach_workspace → NEXT turn reads via
  aget_tuple (falls back to the store, since nothing is attached anymore).

If `tier_selection_context` survives THIS exact sequence, the persistence
layer is proven innocent and any remaining live bug is elsewhere (state
merge in a node, or the interpreter/goal_planner routing before cart.py is
even reached).
"""
from __future__ import annotations

from ladini.workspace.checkpointer import WorkspaceCheckpointer, _SerializedValue
from ladini.workspace.models import Workspace
from tests.conftest import run
from tests.integration.test_checkpointer_state_machine import (
    config_for,
    make_checkpoint,
    make_checkpointer,
)


def _decoded_channel_values(cp: WorkspaceCheckpointer, workspace: Workspace) -> dict:
    for bucket in (workspace.agent_state.get("namespaces") or {}).values():
        checkpoints = bucket.get("checkpoints") or {}
        if not checkpoints:
            continue
        latest_id = max(checkpoints.keys())
        encoded = checkpoints[latest_id].get("checkpoint")
        if isinstance(encoded, dict):
            return _SerializedValue(**encoded).decode(cp.serde).get("channel_values", {})
    return {}


class TestTierSelectionContextSurvivesARealTurnBoundary:
    def test_full_attach_aput_flush_save_detach_reload_cycle(self):
        cp, store = make_checkpointer()
        thread_id = "+22601479800"
        ws = Workspace(workspace_id=thread_id, agent_state={"namespaces": {}})

        # --- Turn N: cart_management showed the tier menu, LangGraph's
        # final checkpoint for this turn carries tier_selection_context. ---
        cp.attach_workspace(ws)
        tiers = [
            {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 500.0,
             "packaging": "bidon", "base_unit_quantity": 5.0, "min_order_quantity": 1},
            {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 900.0,
             "packaging": "bidon", "base_unit_quantity": 10.0, "min_order_quantity": 1},
        ]
        checkpoint = make_checkpoint(
            "cp-turnN",
            channel_values={
                "current_goal": "BUYER_ADD_TO_CART",
                "expected_input": "SELECTION",
                "transaction_payload": {"product": "lait", "quantity": 40},
                "tier_selection_context": {"product_id": "P-LAIT", "tiers": tiers},
                "vendor_selection_context": None,
            },
        )
        run(cp.aput(config_for(thread_id), checkpoint, {}, {}))

        # --- Flush (exactly what the orchestrator does before detaching) ---
        cp.finalize_for_persistence(ws)
        run(store.save(ws))
        cp.detach_workspace(thread_id)

        # Sanity: confirm what actually landed in the fake DB before even
        # touching aget_tuple — isolates "pruning dropped it" from "aget_tuple
        # failed to read it back".
        saved_ws = store._rows[thread_id]
        saved_channels = _decoded_channel_values(cp, saved_ws)
        assert "tier_selection_context" in saved_channels, (
            "tier_selection_context was pruned/lost during finalize_for_persistence "
            "+ store.save — the bug is in the WRITE path"
        )
        assert saved_channels["tier_selection_context"]["tiers"] == tiers

        # --- Turn N+1: buyer replies "2". Orchestrator is NOT attached yet
        # (fresh task pulled off the Celery queue) — aget_tuple must fall
        # back to the store. ---
        tup = run(cp.aget_tuple(config_for(thread_id)))
        assert tup is not None, "aget_tuple found no checkpoint at all — full wipe"
        reloaded = tup.checkpoint["channel_values"]
        assert "tier_selection_context" in reloaded, (
            "tier_selection_context was lost on RELOAD (aget_tuple) even though "
            "it was correctly saved — the bug is in the READ path"
        )
        assert reloaded["tier_selection_context"]["tiers"] == tiers
        assert reloaded["tier_selection_context"]["product_id"] == "P-LAIT"

    def test_survives_two_consecutive_turns_like_the_live_incident(self):
        """Mirrors the exact live sequence: menu shown (turn N), '2' sent and
        STILL sees the stale-looking state (turn N+1), reproducing the
        "search_products fires again" symptom by checking whether
        vendor_selection_context (which the live logs showed WAS being lost)
        and tier_selection_context (which should NOT be) diverge."""
        cp, store = make_checkpointer()
        thread_id = "+22601479800"
        ws = Workspace(workspace_id=thread_id, agent_state={"namespaces": {}})

        cp.attach_workspace(ws)
        tiers = [{"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 900.0,
                   "packaging": "bidon", "base_unit_quantity": 10.0, "min_order_quantity": 1}]
        run(cp.aput(config_for(thread_id), make_checkpoint(
            "cp-1",
            channel_values={
                "tier_selection_context": {"product_id": "P-LAIT", "tiers": tiers},
                # Reproduces the live observation: vendor_selection_context
                # was never re-seeded on the "quantity already known" path.
                "vendor_selection_context": None,
            },
        ), {}, {}))
        cp.finalize_for_persistence(ws)
        run(store.save(ws))
        cp.detach_workspace(thread_id)

        # Next turn picks the workspace back up from the store (this is what
        # WorkspaceResolver does — re-attach for the new turn).
        reloaded_ws = run(store.get(thread_id))
        cp.attach_workspace(reloaded_ws)
        tup = run(cp.aget_tuple(config_for(thread_id)))
        assert tup is not None
        channels = tup.checkpoint["channel_values"]
        assert channels.get("tier_selection_context", {}).get("tiers") == tiers, (
            "tier_selection_context did not survive the exact save->reload "
            "sequence the live incident's second turn goes through"
        )
