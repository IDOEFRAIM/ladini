"""Unified DataGateway — single entry point for all agent DB/MCP operations.

Eliminates duplicated MCP/DB access patterns across Formation and Market agents.
Provides resilient execution with automatic fallback (MCP → DB direct).
"""
from __future__ import annotations

import inspect
import logging
import uuid
from contextlib import nullcontext
from typing import Any, Dict, Optional

from agriconnect.infrastructure.mcp.context import FarmerContext, mcp_context_scope

logger = logging.getLogger("AgriConnect.Agents.Gateway")


class DataGateway:
    """Facade providing resilient data access for all agents.

    Encapsulates:
      - MCP runtime calls (preferred path)
      - Direct AgriDatabaseService calls (fallback)
      - Automatic retry with fallback on failure
    """

    def __init__(
        self,
        *,
        mcp_runtime: Any = None,
        db_service: Any = None,
    ) -> None:
        self._mcp = mcp_runtime
        self._db = db_service

    @property
    def has_mcp(self) -> bool:
        return self._mcp is not None

    @property
    def has_db(self) -> bool:
        return self._db is not None

    @property
    def is_available(self) -> bool:
        return self.has_mcp or self.has_db

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def call(self, method_name: str, **kwargs: Any) -> Any:
        """Call a method with MCP-first, DB-fallback strategy.

        Strips None values from kwargs before calling.
        """
        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}

        # Try MCP first
        if self._mcp is not None:
            try:
                return await self._call_mcp(method_name, safe_kwargs)
            except Exception as mcp_exc:
                if self._db is None:
                    raise
                logger.debug(
                    "Gateway MCP failed for '%s', falling back to DB: %s",
                    method_name, mcp_exc,
                )

        # Fallback to direct DB
        if self._db is not None:
            return await self._call_db(method_name, safe_kwargs)

        raise RuntimeError(f"Gateway: no runtime available for '{method_name}'")

    async def call_mcp_only(self, method_name: str, **kwargs: Any) -> Any:
        """Call MCP only (no DB fallback). Raises if MCP unavailable."""
        if self._mcp is None:
            raise RuntimeError("MCP runtime unavailable")
        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return await self._call_mcp(method_name, safe_kwargs)

    async def call_db_only(self, method_name: str, **kwargs: Any) -> Any:
        """Call DB directly (no MCP). Raises if DB unavailable."""
        if self._db is None:
            raise RuntimeError("Database service unavailable")
        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return await self._call_db(method_name, safe_kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_mcp(self, method_name: str, kwargs: Dict[str, Any]) -> Any:
        runtime = self._mcp

        # Strategy 1: call_db (MarketRuntime style)
        call_db = getattr(runtime, "call_db", None)
        if callable(call_db):
            resp = call_db(method_name, **kwargs)
            return await resp if inspect.isawaitable(resp) else resp

        # Strategy 2: call_tool (MCP session)
        call_tool = getattr(runtime, "call_tool", None)
        if callable(call_tool):
            try:
                with self._context_scope(kwargs):
                    resp = call_tool(method_name, kwargs)
            except TypeError:
                with self._context_scope(kwargs):
                    resp = call_tool(tool_name=method_name, arguments=kwargs)
            return await resp if inspect.isawaitable(resp) else resp

        # Strategy 3: db_client pattern
        db_client = getattr(runtime, "db_client", None)
        if db_client is not None and hasattr(db_client, "call_tool"):
            with self._context_scope(kwargs):
                resp = db_client.call_tool(method_name, kwargs)
            return await resp if inspect.isawaitable(resp) else resp

        raise RuntimeError(f"Unsupported MCP runtime type for '{method_name}'")

    async def _call_db(self, method_name: str, kwargs: Dict[str, Any]) -> Any:
        method = getattr(self._db, method_name, None)
        if method is None:
            raise AttributeError(f"Database service missing method '{method_name}'")
        result = method(**kwargs)
        return await result if inspect.isawaitable(result) else result

    def _context_scope(self, kwargs: Dict[str, Any]):
        context = self._build_context(kwargs)
        if context is None:
            return nullcontext()
        return mcp_context_scope(context)

    def _build_context(self, kwargs: Dict[str, Any]) -> Optional[FarmerContext]:
        user_id = str(
            kwargs.get("user_id") or kwargs.get("producer_id") or kwargs.get("buyer_id") or ""
        ).strip()
        phone = str(kwargs.get("phone") or kwargs.get("user_phone") or "").strip()
        session_id = str(kwargs.get("session_id") or uuid.uuid4())
        if not user_id and not phone:
            return None
        return FarmerContext(
            user_id=user_id or phone,
            phone_number=phone or "unknown",
            session_id=session_id,
        )
