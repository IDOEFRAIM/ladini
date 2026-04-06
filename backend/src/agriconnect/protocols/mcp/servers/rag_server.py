from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict

from fastmcp import FastMCP

from agriconnect.protocols.mcp.tools.agronomy import AgronomyTools

logger = logging.getLogger("MCP.Server.RAG")


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
    return json.dumps({"status": "ok", "retriever_loaded": bool(getattr(_SERVER._tools, "_rag", None))}, ensure_ascii=False)


_SERVER = AgriRAGMCPServer(compatibility_mode=False)
_MCP = FastMCP("AgriConnect RAG Server")


@_MCP.tool(name="search_agronomy_docs")
async def search_agronomy_docs(query: str, level: str = "debutant", top_k: int = 4) -> str:
    result = await _SERVER.call_tool(
        "search_agronomy_docs",
        {"query": query, "level": level, "top_k": top_k},
    )
    if not result.get("ok"):
        raise RuntimeError(f"RAG search failed: {result.get('error', 'unknown error')}")
    return json.dumps(result["data"], ensure_ascii=False)


@_MCP.tool(name="search_past_interactions")
async def search_past_interactions(user_id: str, query: str, top_k: int = 3) -> str:
    result = await _SERVER.call_tool(
        "search_past_interactions",
        {"user_id": user_id, "query": query, "top_k": top_k},
    )
    if not result.get("ok"):
        raise RuntimeError(f"RAG memory search failed: {result.get('error', 'unknown error')}")
    return json.dumps(result["data"], ensure_ascii=False)


if __name__ == "__main__":
    _MCP.run()
