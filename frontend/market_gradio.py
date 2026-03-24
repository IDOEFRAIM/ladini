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

from agriconnect.graphs.nodes.market import MarketCoach, PRODUCER_ID


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

        # If agent asks for confirmation, auto-confirm and execute transaction
        if state.get("waiting_for_confirmation"):
            logs.append("Auto-confirming transaction and executing via MCP/tool...")
            # Ensure transaction_payload present
            tx = state.get("transaction_payload") or validation.get("transaction_payload")
            if not tx:
                return "No transaction payload produced.", "\n".join(logs)

            # Execute transaction (handles MCP path internally)
            exec_result = coach._run_async(coach._handle_transaction_execution(tx))
            logs.append("EXEC_RESULT: " + json.dumps(exec_result, ensure_ascii=False))
            # Attach exec results for compose
            state.setdefault("market_data", {}).update(exec_result)
            state["status"] = "COMPLETED_TRANSACTION"

            composed = coach.compose_node(state)
            logs.append("COMPOSE: " + json.dumps(composed, ensure_ascii=False))
            return composed.get("final_response", "(no response)"), "\n".join(logs)

        # Otherwise fetch market data path
        updates = coach.fetch_data_node(state)
        state.update(updates)
        logs.append("FETCH_DATA: " + json.dumps(updates, ensure_ascii=False))

        composed = coach.compose_node(state)
        logs.append("COMPOSE: " + json.dumps(composed, ensure_ascii=False))
        return composed.get("final_response", "(no response)"), "\n".join(logs)

    except Exception as e:
        tb = traceback.format_exc()
        return f"Error: {e}", tb


with gr.Blocks(title="AgriConnect MarketCoach Live Test") as demo:
    gr.Markdown("# AgriConnect — MarketCoach Live Test (Gradio)")
    gr.Markdown(f"Test Producer ID: **{PRODUCER_ID}**")

    with gr.Row():
        txt = gr.Textbox(label="User query", lines=3, value="Je veux enregistrer un surplus de 50 kg de maïs à Nouna")
        btn = gr.Button("Run flow (auto-confirm)")
        btn_list = gr.Button("Lister mes produits")

    out_resp = gr.Textbox(label="Agent response", lines=6)
    out_logs = gr.Textbox(label="Debug logs", lines=20)

    btn.click(fn=run_market_flow, inputs=[txt], outputs=[out_resp, out_logs])
    def run_list_products():
        logs = []
        try:
            res = coach._run_async(coach.mcp_list_products_human(PRODUCER_ID))
            logs.append(f"Called mcp_list_products_human for {PRODUCER_ID}")
            # res is a human-readable string ready for display
            return res, "\n".join(logs)
        except Exception as e:
            return "", f"Error calling list_products: {e}"

    btn_list.click(fn=run_list_products, inputs=[], outputs=[out_resp, out_logs])


if __name__ == "__main__":
    demo.launch(server_name="localhost", server_port=7862, share=False)
