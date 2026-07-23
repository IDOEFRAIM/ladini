from __future__ import annotations

from typing import Any, Dict

import asyncio

from agriconnect.graphs.agents.market_coach.actions.tool_provider import (
    MCPToolProvider,
    ToolProvider,
)


class _DummyRuntime:
    def __init__(self) -> None:
        self.last_call: Dict[str, Any] = {}

    async def call_db(self, tool_name: str, **kwargs: Any):  # type: ignore[override]
        self.last_call = {"tool": tool_name, "args": dict(kwargs)}
        # Echo-style JSON-like result
        return {"tool": tool_name, "args": dict(kwargs), "ok": True}


async def _run_provider() -> None:
    runtime = _DummyRuntime()
    provider: ToolProvider = MCPToolProvider(runtime=runtime)
    result = await provider.execute("echo_tool", {"a": 1, "b": 2})
    assert result["tool"] == "echo_tool"
    assert result["args"] == {"a": 1, "b": 2}
    assert result["ok"] is True
    assert runtime.last_call["tool"] == "echo_tool"
    assert runtime.last_call["args"] == {"a": 1, "b": 2}


def test_mcp_tool_provider_executes_via_runtime() -> None:
    # Simple wrapper to run the async helper under pytest without
    # introducing extra dependencies.
    asyncio.run(_run_provider())
