from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger("Agent.MarketplaceBackground")


class MarketplaceBackgroundAgent:
	"""System-facing async matching engine.

	This agent is intentionally non-conversational: it reads marketplace data,
	computes opportunities, and writes match suggestions.
	"""

	def __init__(self, mcp_session=None):
		self.db_host = mcp_session

	async def _call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
		if not self.db_host:
			return None
		args = arguments or {}
		try:
			if hasattr(self.db_host, "call_tool_sync") and callable(getattr(self.db_host, "call_tool_sync")):
				try:
					loop = asyncio.get_running_loop()
					sync_res = await loop.run_in_executor(None, lambda: self.db_host.call_tool_sync(tool_name, args))
				except RuntimeError:
					sync_res = self.db_host.call_tool_sync(tool_name, args)
				if isinstance(sync_res, dict) and "data" in sync_res:
					return sync_res.get("data")
				return sync_res

			res = await self.db_host.call_tool(tool_name, args)
			if isinstance(res, str):
				try:
					parsed = json.loads(res)
					return parsed.get("data", parsed)
				except Exception:
					return res
			return res
		except Exception as exc:
			logger.warning("MCP call failed for %s: %s", tool_name, exc)
			return None

	async def generate_matches_for_product(self, product_name: str, zone_id: str | None = None, limit: int = 10) -> List[Dict[str, Any]]:
		"""Search demand-side signals and persist match suggestions.

		Returns all created match records for observability.
		"""
		created: List[Dict[str, Any]] = []

		# 1) search active products / demand signals in current marketplace state
		candidates = await self._call_mcp_tool("search_products", {
			"product_name": product_name,
			"zone_id": zone_id,
			"limit": limit,
		}) or []
		if not isinstance(candidates, list):
			candidates = []

		# 2) also include open auctions as demand-side opportunities
		auctions = await self._call_mcp_tool("get_open_auctions", {
			"zone_id": zone_id,
			"limit": limit,
		}) or []
		if not isinstance(auctions, list):
			auctions = []

		# 3) persist normalized match suggestions
		for c in candidates:
			if not isinstance(c, dict):
				continue
			product_id = c.get("id") or c.get("product_id")
			if not product_id:
				continue
			score = 0.8 if zone_id and c.get("zone_id") == zone_id else 0.65
			saved = await self._call_mcp_tool("create_market_match", {
				"product_id": str(product_id),
				"buyer_id": str(c.get("buyer_id") or ""),
				"score": float(score),
				"status": "SUGGESTED",
				"meta_json": json.dumps({
					"source": "search_products",
					"candidate": c,
				}, ensure_ascii=False),
			})
			if saved:
				created.append(saved)

		for a in auctions:
			if not isinstance(a, dict):
				continue
			# Auction might not directly reference product_id, keep in meta and skip if absent.
			product_id = a.get("product_id")
			if not product_id:
				continue
			saved = await self._call_mcp_tool("create_market_match", {
				"product_id": str(product_id),
				"buyer_id": str(a.get("buyer_id") or ""),
				"score": 0.75,
				"status": "SUGGESTED",
				"meta_json": json.dumps({
					"source": "open_auction",
					"auction": a,
				}, ensure_ascii=False),
			})
			if saved:
				created.append(saved)

		return created

