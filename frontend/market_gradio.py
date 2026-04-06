"""Gradio frontend to manually test MarketCoach agent (deterministic live mode).

Usage:
  python frontend/market_gradio.py

This script will:
- add the backend/src to PYTHONPATH so imports resolve from the repo
- instantiate a `MarketCoach` with either a real MCP server adapter (sync) or a MockMCP
- provide a simple UI to send queries and auto-confirm transactions for testing

The test producer id (PRODUCER_ID) is used as the default user profile id.
"""

import sys
import os
import json
import traceback

# Ensure backend package is importable when running from repo root
ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND_SRC = os.path.abspath(os.path.join(ROOT, "backend", "src"))
if BACKEND_SRC not in sys.path:
    sys.path.insert(0, BACKEND_SRC)

import gradio as gr
import requests

from agriconnect.graphs.nodes.market_coach import MarketCoach, PRODUCER_ID


# --- MCP adapter selection (prefer local MCP server sync API, fall back to mock) ---
class MockMCP:
    def __init__(self):
        self.calls = []

    def call_tool_sync(self, name: str, args: dict | None = None):
        args = args or {}
        self.calls.append((name, args))
        return {"ok": True, "data": {"mock": True, "tool": name, "args": args}}

    async def call_tool(self, name: str, args: dict | None = None):
        return self.call_tool_sync(name, args)


def get_mcp_client():
    # Try to use the in-process MCP server (AgriDBMCPServer) if available.
    try:
        from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer, runtime

        server = AgriDBMCPServer()

        class Adapter:
            def __init__(self, server):
                self._server = server

            def call_tool_sync(self, name: str, args: dict | None = None):
                return self._server.call_tool_sync(name, args or {})

            async def call_tool(self, name: str, args: dict | None = None):
                # run sync call in thread to avoid blocking event loop if needed
                import asyncio
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, lambda: self._server.call_tool_sync(name, args or {}))

        return Adapter(server)
    except Exception:
        return MockMCP()


# Instantiate coach with MCP adapter
mcp_client = get_mcp_client()
coach = MarketCoach(mcp_session=mcp_client)


def run_market_flow(user_text: str):
    try:
        logs = []
        # Initial state with test producer id
        state = {"user_query": user_text, "user_profile": {"user_id": PRODUCER_ID, "phone": "000000000"}}

        # analyze_node is async; run via agent bridge from sync context
        analysis = coach._run_async(coach.analyze_node(state))
        state.update(analysis)
        logs.append("ANALYZE: " + json.dumps(analysis, ensure_ascii=False))

        # validate_node is async
        validation = coach._run_async(coach.validate_node(state))
        state.update(validation)
        logs.append("VALIDATE: " + json.dumps(validation, ensure_ascii=False))

        proposal_out = {}
        # If agent asks for confirmation (legacy), convert to proposal flow.
        if state.get("waiting_for_confirmation"):
            logs.append("Converted waiting_for_confirmation -> PROPOSAL_READY (HITL)")
            # Build a simple proposal if not present
            tx = state.get("transaction_payload") or validation.get("transaction_payload")
            if not tx:
                return "No transaction payload produced.", "\n".join(logs), state
            proposal = state.get("proposed_action") or {
                "type": "proposal",
                "tool_name": "register_surplus_offer",
                "display_card": {"title": "Confirmation de mise en vente", "fields": []},
                "payload": tx,
            }
            state["proposed_action"] = proposal
            state["status"] = "PROPOSAL_READY"
            logs.append("PROPOSAL: " + json.dumps(proposal, ensure_ascii=False))
            # Return the serialized proposal so the UI can render the card
            proposal_out = proposal
            state["proposed_action"] = proposal
            return json.dumps(proposal, ensure_ascii=False), "\n".join(logs), state, proposal_out

        # Otherwise fetch market data path
        updates = coach._run_async(coach.fetch_data_node(state))
        state.update(updates)
        logs.append("FETCH_DATA: " + json.dumps(updates, ensure_ascii=False))

        composed = coach._run_async(coach.compose_node(state))
        logs.append("COMPOSE: " + json.dumps(composed, ensure_ascii=False))
        # If compose produced a proposal string (PROPOSAL_READY), try to extract it
        final_resp = composed.get("final_response", "(no response)")
        try:
            parsed = json.loads(final_resp)
            if isinstance(parsed, dict) and parsed.get("type") in ("proposal", "proposal_card"):
                proposal_out = parsed
                state["proposed_action"] = parsed
        except Exception:
            pass

        return final_resp, "\n".join(logs), state, proposal_out

    except Exception as e:
        tb = traceback.format_exc()
        return f"Error: {e}", tb, {}


with gr.Blocks(title="AgriConnect MarketCoach Live Test") as demo:
    gr.Markdown("# AgriConnect — MarketCoach Live Test (Gradio)")
    gr.Markdown(f"Test Producer ID: **{PRODUCER_ID}**")

    with gr.Row():
        txt = gr.Textbox(label="User query", lines=3, value="Je veux enregistrer un surplus de 50 kg de maïs à Nouna")
        btn = gr.Button("Run flow (auto-confirm)")
        btn_list = gr.Button("Lister mes produits")

    out_resp = gr.Textbox(label="Agent response", lines=6)
    out_logs = gr.Textbox(label="Debug logs", lines=20)
    # Store last conversation/state for HITL actions
    state_store = gr.State({})
    proposal_json = gr.JSON(value={}, label="PROPOSED_ACTION")

    btn.click(fn=run_market_flow, inputs=[txt], outputs=[out_resp, out_logs, state_store, proposal_json])
    def run_list_products():
        logs = []
        try:
            res = coach._run_async(coach.repo.list_products_human(PRODUCER_ID))
            logs.append(f"Called list_products_human for {PRODUCER_ID}")
            # res is a human-readable string ready for display
            return res, "\n".join(logs)
        except Exception as e:
            return "", f"Error calling list_products: {e}"

    btn_list.click(fn=run_list_products, inputs=[], outputs=[out_resp, out_logs])

    # Approve / Reject handlers for proposals
    def approve_proposal(state_dict: dict):
        logs = []
        if not state_dict:
            return "No proposal stored.", "No state available."
        proposal = state_dict.get("proposed_action") or state_dict.get("proposed_action")
        if not proposal:
            return "No proposal found.", ""
        payload = proposal.get("payload") or {}
        # Try server-side commit endpoint first
        try:
            resp = requests.post("http://localhost:8003/agent/commit_proposal", json={"proposal": proposal}, timeout=3)
            if resp.status_code == 200:
                logs.append("Committed via server endpoint")
                data = resp.json()
                return json.dumps(data, ensure_ascii=False), "\n".join(logs)
            else:
                logs.append(f"Server commit returned {resp.status_code}, falling back to local execution")
        except Exception as e:
            logs.append(f"Server commit unreachable: {e}; falling back to local execution")

        # Local execution fallback (use coach internals)
        try:
            action = payload.get("action_type")
            success, result = coach._run_async(coach.repo.execute_transaction(action, payload))
            exec_result = result if success else {"error": str(result)}

            logs.append("EXEC_RESULT: " + json.dumps(exec_result, ensure_ascii=False))
            # Build final composed message
            state_dict.setdefault("market_data", {}).update(exec_result)
            state_dict["status"] = "COMPLETED_TRANSACTION"
            composed = coach._run_async(coach.compose_node(state_dict))
            logs.append("COMPOSE: " + json.dumps(composed, ensure_ascii=False))
            return composed.get("final_response", "(no response)"), "\n".join(logs)
        except Exception as e:
            tb = traceback.format_exc()
            return f"Execution error: {e}", tb

    def reject_proposal(state_dict: dict):
        logs = []
        if not state_dict:
            return "No proposal to reject.", ""
        # Optionally notify server about rejection
        try:
            requests.post("http://localhost:8003/agent/reject_proposal", json={"proposal": state_dict.get("proposed_action")}, timeout=2)
            logs.append("Notified server of rejection (if endpoint exists)")
        except Exception:
            pass
        return "Opération annulée.", "\n".join(logs)

    approve_btn = gr.Button("Approuver la proposition")
    reject_btn = gr.Button("Refuser la proposition")

    approve_btn.click(fn=approve_proposal, inputs=[state_store], outputs=[out_resp, out_logs])
    reject_btn.click(fn=reject_proposal, inputs=[state_store], outputs=[out_resp, out_logs])


if __name__ == "__main__":
    demo.launch(server_name="localhost", server_port=7862, share=False)
