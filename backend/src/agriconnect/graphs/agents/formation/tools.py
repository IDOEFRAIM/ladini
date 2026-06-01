"""MCP client tools for the Formation agent.

This module exposes a LangChain tool that queries the local MCP RAG server
through stdio transport.
"""

import asyncio
from pathlib import Path
from typing import Any

import mcp
from langchain_core.tools import tool
from mcp.client.sse import sse_client
import httpx
import os
from httpx import HTTPStatusError


# Server command: .\\.venv\\Scripts\\python.exe -m agriconnect.protocols.mcp.servers.rag_server
_WORKSPACE_ROOT = Path(__file__).resolve().parents[6]
_SERVER_COMMAND = str(_WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe")
_SERVER_ARGS = ["-m", "agriconnect.protocols.mcp.servers.rag_server"]


def _extract_tool_result(result: Any) -> str:
    """Normalize MCP tool results into plain text for agent consumption."""
    if result is None:
        return ""

    # Preferred: MCP CallToolResult with content blocks
    content = getattr(result, "content", None)
    if content:
        text_parts = []
        for block in content:
            block_text = getattr(block, "text", None)
            if block_text:
                text_parts.append(str(block_text))
        if text_parts:
            return "\n".join(text_parts)

    # Fallback for dict-like/other representations
    if isinstance(result, dict):
        if "structuredContent" in result:
            return str(result["structuredContent"])
        if "content" in result:
            return str(result["content"])
    return str(result)


async def _query_rag_async(query: str) -> str:
    """Open an MCP stdio session and call the `retrieve` tool."""
    # Connect to a remote MCP server over SSE transport. Port/host can be
    # overridden with environment variables `AGRICONNECT_MCP_HOST` and
    # `AGRICONNECT_MCP_PORT`.
    host = os.getenv("AGRICONNECT_MCP_HOST", "localhost")
    port = os.getenv("AGRICONNECT_MCP_PORT", "8000")
    sse_url = f"http://{host}:{port}/sse"
    try:
        async with sse_client(sse_url) as (read_stream, write_stream):
            async with mcp.ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                # Requested contract: call `retrieve` with {"query": query}
                result = await session.call_tool("retrieve", {"query": query})

                # Some MCP servers return unknown-tool as an error result instead of raising.
                if getattr(result, "isError", False):
                    text = _extract_tool_result(result)
                    if "Unknown tool" in text and "retrieve" in text:
                        result = await session.call_tool(
                            "search_agronomy_docs",
                            {"query": query, "level": "debutant", "top_k": 4},
                        )
                return _extract_tool_result(result)
    except Exception as exc:
        # Try to fetch /status for diagnostics and raise a clearer error.
        status_url = f"http://{host}:{port}/status"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(status_url)
                r.raise_for_status()
                srv_status = r.text
        except Exception as status_exc:
            srv_status = f"failed to fetch /status: {status_exc}"
        # If SSE isn't available, try the HTTP fallback `/call_tool`.
        call_url = f"http://{host}:{port}/call_tool"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    call_url,
                    json={"tool": "search_agronomy_docs", "arguments": {"query": query, "level": "debutant", "top_k": 4}},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("ok"):
                    return _extract_tool_result(data.get("data"))
                raise RuntimeError(f"HTTP call_tool error: {data.get('error')}")
        except Exception as http_exc:
            raise RuntimeError(
                f"Failed to connect to MCP SSE at {sse_url}: {exc}; status: {srv_status}; http fallback error: {http_exc}"
            ) from exc


@tool
async def query_rag(query: str) -> str:
    """Query the MCP RAG server with a natural-language question.

    The function connects to the local MCP `rag_server` using stdio transport,
    calls the `retrieve` tool with `{"query": query}`, and returns the tool
    response as text for downstream LLM reasoning.
    """
    return await _query_rag_async(query)


def query_rag_sync(query: str) -> str:
    """Synchronous helper when a non-async caller needs the same tool call."""
    return asyncio.run(_query_rag_async(query))


__all__ = ["query_rag", "query_rag_sync"]


async def invoke_query_rag(query: str) -> str:
    """Reliable async wrapper that always returns plain text from the RAG.

    This helper bypasses any LangChain Tool wrapper and calls the underlying
    async implementation directly so callers can treat RAG as a black box.
    """
    resp = await _query_rag_async(query)
    # Normalize output: prefer returning a dict {"text":..., "sources": [...]}
    if resp is None:
        return {"text": "", "sources": []}
    def _normalize_dict_payload(parsed: dict, fallback_text: str = "") -> dict:
        # Handle native RAG payload shape: {query, documents, context_text, total_found}
        if "context_text" in parsed or "documents" in parsed:
            text = parsed.get("context_text") or ""
            sources = parsed.get("documents") or []
            return {"text": text, "sources": sources}

        # Generic MCP/LLM shapes
        text = parsed.get("text") or parsed.get("content") or parsed.get("result") or fallback_text
        sources = parsed.get("sources") or parsed.get("metadata") or []
        return {"text": text, "sources": sources}

    # If underlying response already looks like JSON/dict, try to parse
    if isinstance(resp, str):
        s = resp.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                import json

                parsed = json.loads(s)
                # If parsed is a dict with textual content, normalize
                if isinstance(parsed, dict):
                    return _normalize_dict_payload(parsed, fallback_text=s)
            except Exception:
                # fall through to return raw text
                pass
        # plain string -> return as text
        return {"text": resp, "sources": []}
    if isinstance(resp, dict):
        return _normalize_dict_payload(resp, fallback_text=str(resp))
    return {"text": str(resp), "sources": []}


__all__.append("invoke_query_rag")


if __name__ == "__main__":
    import argparse
    import sys
    import traceback

    parser = argparse.ArgumentParser(description="Quick test for MCP RAG query tool")
    parser.add_argument("query", nargs="?", default="bonjour, pouvez-vous m'aider ?", help="Question to send to the RAG server")
    parser.add_argument("--verbose", action="store_true", help="Print exception trace on error")
    args = parser.parse_args()

    print("Testing query_rag_sync against local MCP RAG server...")
    try:
        resp = query_rag_sync(args.query)
        print("\n--- MCP RAG RESPONSE ---\n")
        if resp is None:
            print("<empty response>")
        else:
            print(resp)
        print("\n--- end ---\n")
    except Exception as exc:
        print("Error while querying MCP RAG server:", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
