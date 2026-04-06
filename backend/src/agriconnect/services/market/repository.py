import json
import logging
from typing import Any, Dict, List, Optional
import asyncio
import inspect

logger = logging.getLogger("AgriConnect.MarketRepository")

class MarketRepository:
    """Gateway to MCP tools and backend services.
    Decouples the agent from direct MCP tool calls and response normalization.
    """

    def __init__(self, mcp_client: Any):
        self.client = mcp_client

    @staticmethod
    def _unwrap_mcp_result(res: Any) -> Any:
        """Unwrap common MCP envelopes: {ok, data} or {status: ok, data}."""
        if isinstance(res, dict):
            if "data" in res and (res.get("ok") is True or str(res.get("status", "")).lower() == "ok"):
                return res.get("data")
        return res

    async def _call_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Call an MCP tool and normalize result."""
        if not self.client:
            return None
        
        args = arguments or {}
        try:
            # Check for sync adapter on client (legacy/testing support)
            sync_candidate = getattr(self.client, "call_tool_sync", None)
            has_explicit_sync_api = (
                sync_candidate is not None
                and callable(sync_candidate)
                and not inspect.iscoroutinefunction(sync_candidate)
                and (
                    getattr(type(self.client), "call_tool_sync", None) is not None
                    or "call_tool_sync" in getattr(self.client, "__dict__", {})
                )
            )

            if has_explicit_sync_api:
                # This path is discouraged in pure async contexts but kept for compatibility
                loop = asyncio.get_running_loop()
                sync_res = await loop.run_in_executor(
                    None,
                    lambda: sync_candidate(tool_name, args),
                )
                return self._unwrap_mcp_result(sync_res)

            # Standard async call
            maybe_res = self.client.call_tool(tool_name, args)
            res = await maybe_res if inspect.isawaitable(maybe_res) else maybe_res
        except Exception as e:
            logger.warning("MCP tool %s call failed: %s", tool_name, e)
            return None

        # Normalize JSON-string GenericResult responses
        if isinstance(res, str):
            try:
                parsed = json.loads(res)
                # Prefer returning the `data` payload when present
                return self._unwrap_mcp_result(parsed.get("data", parsed))
            except Exception:
                return res
        return self._unwrap_mcp_result(res)

    # --- User Management ---
    async def get_user_profile(self, user_id: str) -> Any:
        return await self._call_tool("get_user_profile", {"user_id": user_id})

    async def get_user_context_state(self, user_id: str) -> Any:
        return await self._call_tool("get_user_context_state", {"user_id": user_id})

    async def upsert_user_context_state(self, user_id: str, last_intent: str = "", pending_intent: str = "", draft_data: Dict[str, Any] | None = None) -> Any:
        body = {
            "user_id": user_id,
            "last_intent": last_intent,
            "pending_intent": pending_intent,
            "draft_data_json": json.dumps(draft_data or {}, ensure_ascii=False),
        }
        return await self._call_tool("upsert_user_context_state", body)

    # --- Product Management ---
    async def list_products(self, producer_id: str) -> Any:
        return await self._call_tool("list_products", {"producer_id": producer_id})
    
    async def list_products_human(self, producer_id: str) -> str:
        """Return a human-friendly textual summary of the producer's products."""
        raw = await self.list_products(producer_id)
        
        # Normalize response (handling different list wrappers)
        products = []
        try:
            if raw is None:
                products = []
            elif isinstance(raw, dict) and raw.get("ok") and isinstance(raw.get("data"), list):
                products = raw.get("data")
            elif isinstance(raw, dict) and isinstance(raw.get("data"), dict):
                # some tools may wrap data in dict with items under 'items' or similar
                maybe = raw["data"].get("items") if raw["data"].get("items") else []
                products = maybe
            elif isinstance(raw, list):
                products = raw
            else:
                # Fallback
                if isinstance(raw, dict) and "data" in raw:
                    d = raw["data"]
                    products = d if isinstance(d, list) else [d]
                else:
                    products = [raw]
        except Exception:
            products = []

        if not products:
            return "Vous n'avez aucun produit en stock pour le moment."

        lines = [f"Vous avez {len(products)} produit(s) en stock :"]
        for p in products:
            try:
                name = p.get("name") or p.get("label") or "(nom inconnu)"
                qty = p.get("quantity_for_sale") or p.get("quantity") or 0
                unit = p.get("unit") or "unités"
                price = p.get("price")
                price_str = f" — {price} /{unit}" if price is not None else ""
                lines.append(f"- {name}: {qty} {unit}{price_str}")
            except Exception:
                continue
        return "\n".join(lines)

    async def create_product(self, producer_id: str, name: str, price: float, quantity_for_sale: float, **kwargs) -> Any:
        body = {"producer_id": producer_id, "name": name, "price": price, "quantity_for_sale": quantity_for_sale}
        body.update(kwargs)
        return await self._call_tool("create_product", body)

    # --- Transaction Management ---
    async def register_surplus_offer(self, user_id: str, commodity: str, quantity: float, location: str | None = None, price: float | None = None) -> Any:
        body = {"user_id": user_id, "commodity": commodity, "quantity": quantity, "location": location}
        if price is not None:
            body["price"] = price # Optional but good to pass if tool supports it (future proof)
        return await self._call_tool("register_surplus_offer", body)

    async def create_agent_action(self, agent_name: str, action_type: str, payload: dict, user_id: str | None = None, priority: str = "MEDIUM") -> Any:
        return await self._call_tool("create_agent_action", {"agent_name": agent_name, "action_type": action_type, "payload": payload, "user_id": user_id, "priority": priority})
    
    async def persist_conversation(self, user_id: str, query_json: str, response_json: str, agent_type: str = "MarketCoach") -> Any:
        return await self._call_tool("persist_conversation", {"user_id": user_id, "query_json": query_json, "response_json": response_json, "agent_type": agent_type})

    async def execute_transaction(self, action_type: str, payload: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        """Handles execution logic routing."""
        try:
            if action_type == "CREATE_PRODUCT":
                res = await self.create_product(
                     producer_id=payload.get("user_id"),
                     name=payload.get("product"),
                     price=float(payload.get("price", 0)),
                     quantity_for_sale=float(payload.get("quantity", 0))
                )
                success = bool(res and (isinstance(res, dict) and (res.get("id") or res.get("product_id"))))
                return success, {"mcp_result": res, "product_management": {"created": True}}
            
            else: # REGISTER_SURPLUS / SELL_OFFER
                res = await self.register_surplus_offer(
                    user_id=payload.get("user_id"),
                    commodity=payload.get("product"),
                    quantity=float(payload.get("quantity", 0)),
                    location=payload.get("location"),
                    price=float(payload.get("price", 0)) if payload.get("price") else None
                )
                success = bool(res and (isinstance(res, dict) and (res.get("id") or res.get("offer_id") or res.get("surplus_id"))))
                return success, {"mcp_result": res}
                
        except Exception as e:
            logger.error(f"Transaction execution failed: {e}")
            return False, {"error": str(e)}

    # --- Market Data ---
    async def get_logistics_info(self, location: str) -> Dict[str, Any]:
        # Implementation could call a tool or service
        # For now, placeholder or migrate logic from tools
        return {}