from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agriconnect.infrastructure.mcp.base import MCPToolSpec

logger = logging.getLogger("MCP.Tools.Agronomy")


class AgronomyTools:
    """Stateless MCP tool set for agronomy operations."""

    name = "agronomy"

    def __init__(self, retriever: Any = None, retriever_factory: Any = None, session_factory: Any = None) -> None:
        self._rag = None
        self._data = None
        try:
            from agriconnect.services.rag_service import RagService

            self._rag = RagService(retriever=retriever, retriever_factory=retriever_factory)
        except Exception as exc:
            logger.warning("RagService unavailable, using degraded mode: %s", exc)

        try:
            from agriconnect.services.data_service import DataService

            self._data = DataService(
                retriever=retriever,
                retriever_factory=retriever_factory,
                session_factory=session_factory,
            )
        except Exception as exc:
            logger.warning("DataService unavailable, using degraded mode: %s", exc)

    async def search_agronomy_docs(self, query: str, level: str = "debutant", top_k: int = 4, ctx=None) -> str:
        if ctx:
            await ctx.info(f"search_agronomy_docs q='{query}' level={level}")
        if self._rag is None:
            return json.dumps({"query": query, "documents": [], "context_text": "", "total_found": 0}, ensure_ascii=False)
        payload = await self._rag.search_documents(query=query, level=level, top_k=top_k)
        return self._rag.dumps(payload)

    async def search_past_interactions(self, user_id: str, query: str, top_k: int = 3, ctx=None) -> str:
        if ctx:
            await ctx.info(f"search_past_interactions user={user_id}")
        if self._rag is None:
            return "[]"
        payload = await self._rag.search_memory(user_id=user_id, query=query, top_k=top_k)
        return self._rag.dumps(payload)

    async def rag_status(self, ctx=None) -> str:
        if ctx:
            await ctx.info("rag_status")
        if self._rag is None:
            return json.dumps({"status": "degraded", "reason": "rag_service_unavailable"}, ensure_ascii=False)
        return self._rag.dumps(await self._rag.status())

    async def search_pest_disease(self, crop: str, symptoms: str, top_k: int = 3, ctx=None) -> str:
        if self._data is None:
            return json.dumps({"crop": crop, "matches": []}, ensure_ascii=False)
        return await self._data.search_pest_disease(crop=crop, symptoms=symptoms, top_k=top_k, ctx=ctx)

    async def semantic_search_episodes(self, user_id: str, query: str, top_k: int = 5, ctx=None) -> str:
        if self._data is None:
            return json.dumps({"user_id": user_id, "episodes": []}, ensure_ascii=False)
        return await self._data.semantic_search_episodes(user_id=user_id, query=query, top_k=top_k, ctx=ctx)

    def get_tools(self) -> List[MCPToolSpec]:
        return [
            MCPToolSpec(name="search_agronomy_docs", handler=self.search_agronomy_docs),
            MCPToolSpec(name="search_past_interactions", handler=self.search_past_interactions),
            MCPToolSpec(name="search_pest_disease", handler=self.search_pest_disease),
            MCPToolSpec(name="semantic_search_episodes", handler=self.semantic_search_episodes),
        ]

    async def ping(self) -> Dict[str, Any]:
        return {
            "status": "ready" if self._rag is not None else "degraded",
            "rag": await self._rag.ping() if self._rag is not None else {"status": "down"},
            "data": await self._data.ping() if self._data is not None else {"status": "down"},
        }
