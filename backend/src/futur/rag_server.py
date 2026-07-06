from __future__ import annotations

import json
import logging
import os
import threading
import asyncio
from pathlib import Path
from typing import Any, Dict

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# Use FastMCP's built-in HTTP app creation (includes SSE support)


def _load_backend_env() -> None:
    """Load backend/.env when running the module directly.

    This allows `python -m agriconnect.protocols.mcp.servers.rag_server`
    to work without exporting DB vars manually.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return

    here = Path(__file__).resolve()
    backend_root = here.parents[5]
    env_file = backend_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


_load_backend_env()

from agriconnect.protocols.mcp.tools.agronomy import AgronomyTools

logger = logging.getLogger("MCP.Server.RAG")


def _safe_mcp_timeout_s() -> float:
    """Server-side hard timeout for MCP tool calls.

    Keep this lower than client request timeout to avoid `-32001 Request timed out`.
    """
    raw = (os.getenv("AGRICONNECT_MCP_TOOL_TIMEOUT_S", "12") or "12").strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid AGRICONNECT_MCP_TOOL_TIMEOUT_S=%r, fallback to 12s", raw)
        return 12.0
    return max(2.0, min(value, 60.0))


class AgriRAGMCPServer:
    """RAG MCP server wrapper using AgronomyTools (service-backed when available)."""

    def __init__(self, retriever: Any = None, retriever_factory: Any = None, compatibility_mode: bool = False) -> None:
        self._tools = AgronomyTools(retriever=retriever, retriever_factory=retriever_factory)
        self._compatibility_mode = compatibility_mode
        self._start_background_warmup()

    def _start_background_warmup(self) -> None:
        # Warm the retriever eagerly so first user query is less likely to stall.
        if os.getenv("AGRICONNECT_RAG_WARMUP", "1") != "1":
            return

        rag = getattr(self._tools, "_rag", None)
        if rag is None or not hasattr(rag, "_get_retriever"):
            return

        def _warmup() -> None:
            try:
                rag._get_retriever()
                logger.info("Background RAG warmup completed")
            except Exception as exc:
                logger.warning("Background RAG warmup skipped: %s", exc)

        threading.Thread(target=_warmup, name="rag-warmup", daemon=True).start()

    @staticmethod
    def _empty_docs_payload(query: str) -> Dict[str, Any]:
        return {
            "query": query,
            "documents": [],
            "context_text": "",
            "total_found": 0,
        }

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "search_agronomy_docs":
            try:
                raw = await self._tools.search_agronomy_docs(
                    query=arguments.get("query", ""),
                    level=arguments.get("level", "debutant"),
                    top_k=int(arguments.get("top_k", 4)),
                )
                payload = json.loads(raw) if isinstance(raw, str) else raw
                return {"ok": True, "data": payload}
            except Exception as exc:
                if not self._compatibility_mode:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "data": self._empty_docs_payload(arguments.get("query", ""))}

        if tool_name == "search_past_interactions":
            try:
                raw = await self._tools.search_past_interactions(
                    user_id=arguments.get("user_id", ""),
                    query=arguments.get("query", ""),
                    top_k=int(arguments.get("top_k", 3)),
                )
                payload = json.loads(raw) if isinstance(raw, str) else raw
                return {"ok": True, "data": payload}
            except Exception as exc:
                if not self._compatibility_mode:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "data": []}

        return {"ok": False, "error": f"Tool {tool_name} inconnu"}

    def call_tool_sync(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self.call_tool(tool_name, arguments))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass


async def rag_server_status() -> str:
    ready = False
    try:
        ready = bool(_SERVER and getattr(_SERVER._tools, "_rag", None))
    except Exception:
        ready = False
    state = "ready" if ready else "starting"
    return json.dumps({"status": state, "retriever_loaded": ready}, ensure_ascii=False)


# Initialize server eagerly; tool handlers still have hard timeouts.
_SERVER: AgriRAGMCPServer = AgriRAGMCPServer(compatibility_mode=True)
_MCP = FastMCP("AgriConnect RAG Server")

# The FastMCP HTTP app (including SSE mount) will be created when the
# server is run via `_MCP.run(transport="sse", ...)`. We register custom
# routes using `_MCP.custom_route(...)` so they'll be added to the HTTP app
# returned by FastMCP at runtime.


def _perform_startup_warmup() -> None:
    """Perform a synchronous warmup at process start to load models and indexes.

    This reduces the chance of early requests hitting the MCP timeout. The
    warmup timeout is configurable via `AGRICONNECT_RAG_WARMUP_TIMEOUT_S`.
    """
    try:
        raw = (os.getenv("AGRICONNECT_RAG_WARMUP", "1") or "1").strip()
        if raw != "1":
            return
        warmup_timeout = float(os.getenv("AGRICONNECT_RAG_WARMUP_TIMEOUT_S", "45") or "45")
    except Exception:
        warmup_timeout = 45.0

    try:
        rag = getattr(_SERVER._tools, "_rag", None)
        if rag is None:
            logger.info("No RagService available for warmup")
            return

        import asyncio

        async def _do_warmup() -> None:
            try:
                await asyncio.wait_for(rag.warmup(), timeout=warmup_timeout)
                logger.info("Startup warmup complete")
            except asyncio.TimeoutError:
                logger.warning("Startup warmup timed out after %.1fs", warmup_timeout)
            except Exception as exc:
                logger.warning("Startup warmup failed: %s", exc)

        asyncio.run(_do_warmup())
    except Exception as exc:
        logger.warning("Warmup process failed: %s", exc)


_perform_startup_warmup()


@_MCP.tool(name="search_agronomy_docs")
async def search_agronomy_docs(query: str, level: str = "debutant", top_k: int = 4) -> str:
    try:
        result = await asyncio.wait_for(
            _SERVER.call_tool(
                "search_agronomy_docs",
                {"query": query, "level": level, "top_k": top_k},
            ),
            timeout=_safe_mcp_timeout_s(),
        )
    except asyncio.TimeoutError:
        logger.warning("MCP tool timeout: search_agronomy_docs")
        payload = AgriRAGMCPServer._empty_docs_payload(query)
        payload["status"] = "timeout"
        return json.dumps(payload, ensure_ascii=False)

    if not result.get("ok"):
        raise RuntimeError(f"RAG search failed: {result.get('error', 'unknown error')}")
    return json.dumps(result["data"], ensure_ascii=False)


@_MCP.tool(name="search_past_interactions")
async def search_past_interactions(user_id: str, query: str, top_k: int = 3) -> str:
    try:
        result = await asyncio.wait_for(
            _SERVER.call_tool(
                "search_past_interactions",
                {"user_id": user_id, "query": query, "top_k": top_k},
            ),
            timeout=_safe_mcp_timeout_s(),
        )
    except asyncio.TimeoutError:
        logger.warning("MCP tool timeout: search_past_interactions")
        return json.dumps([], ensure_ascii=False)

    if not result.get("ok"):
        raise RuntimeError(f"RAG memory search failed: {result.get('error', 'unknown error')}")
    return json.dumps(result["data"], ensure_ascii=False)


@_MCP.custom_route("/message", methods=["POST"])
async def handle_message(request: Request):
    """Endpoint to receive arbitrary messages (for debugging / simple POST-ing).

    This route accepts JSON payloads and acknowledges receipt. The SSE
    transport (if available) implements the MCP wire protocol on `/sse`.
    """
    try:
        data = await request.json()
    except Exception:
        data = await request.body()
    logger.debug("/message received: %s", data)
    return {"ok": True}


@_MCP.custom_route("/status", methods=["GET"])
async def status(request):
    try:
        # Build a minimal, safe status payload without calling higher-level
        # helpers that may trigger complex initialization or I/O.
        ready = False
        try:
            ready = bool(_SERVER and getattr(_SERVER._tools, "_rag", None))
        except Exception as inner_exc:
            logger.exception("Error checking _SERVER._tools._rag: %s", inner_exc)
            return JSONResponse({"status": "error", "error": str(inner_exc)}, status_code=500)

        state = "ready" if ready else "starting"
        return JSONResponse({"status": state, "retriever_loaded": ready})
    except Exception as exc:
        logger.exception("Unexpected error in /status: %s", exc)
        return JSONResponse({"status": "error", "error": "internal"}, status_code=500)


@_MCP.custom_route("/call_tool", methods=["POST"])
async def call_tool_http(request: Request):
    """Simple HTTP fallback to call tools when SSE isn't usable.

    Expects JSON: {"tool": "tool_name", "arguments": {...}}
    Returns JSON: the same dict returned by `AgriRAGMCPServer.call_tool`.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    tool_name = payload.get("tool")
    arguments = payload.get("arguments", {}) or {}
    if not tool_name:
        return JSONResponse({"ok": False, "error": "missing tool name"}, status_code=400)

    try:
        result = await _SERVER.call_tool(tool_name, arguments)
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("HTTP call_tool failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


if __name__ == "__main__":
    # Run the FastMCP server using its run helper with SSE transport so the
    # MCP internals (lifespan, routes, SSE/message endpoints) are wired.
    host = os.getenv("AGRICONNECT_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("AGRICONNECT_MCP_PORT", "8000"))
    _MCP.run(transport="sse", path="/sse", host=host, port=port)
