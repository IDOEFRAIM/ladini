import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Agent.MarketCoach.Tools")


class MarketAgentTools:
    """Helper container for `MarketCoach` logic extracted from graph node.

    Keeps graph node focused on orchestration while utility logic stays here.
    """

    def __init__(self, agent):
        self.agent = agent

    def unit_factor(self, unit: Optional[str]) -> int:
        try:
            unit_clean = str(unit).lower().replace("s", "")
        except Exception:
            return 100
        for key, val in getattr(self.agent, "UNIT_REGISTRY", {}).items():
            if key in unit_clean:
                return val
        return 100

    def location_warnings(self, loc: str) -> List[str]:
        if not loc:
            return []
        loc_l = loc.lower()
        valid_cities = getattr(self.agent, "VALID_CITIES", [])
        if any(valid in loc_l for valid in valid_cities):
            return []
        return [f"Lieu '{loc}' non trouvé dans le registre officiel."]

    def filter_stocks_by_product(self, stocks: List[Any], product: str) -> List[Any]:
        relevant = []
        for s in stocks:
            s_name = getattr(s, "item_name", "") if hasattr(s, "item_name") else s.get("item_name", "")
            if product.lower() in s_name.lower():
                relevant.append(s)
        return relevant

    def serialize_stocks(self, stocks: List[Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for s in stocks:
            if hasattr(s, "model_dump"):
                try:
                    out.append(s.model_dump())
                    continue
                except Exception:
                    pass
            if hasattr(s, "dict") or hasattr(s, "model_dump"):
                try:
                    if hasattr(s, "model_dump"):
                        out.append(s.model_dump())
                    else:
                        out.append(s.dict())
                    continue
                except Exception:
                    pass
            if hasattr(s, "to_dict"):
                try:
                    out.append(s.to_dict())
                    continue
                except Exception:
                    pass
            if isinstance(s, dict):
                out.append(s)
            else:
                try:
                    out.append(vars(s))
                except Exception:
                    out.append({"repr": str(s)})
        return out

    def extract_products_list(self, raw_products: Any) -> List[Dict[str, Any]]:
        if raw_products is None:
            return []
        if isinstance(raw_products, list):
            return [p for p in raw_products if isinstance(p, dict)]
        if isinstance(raw_products, dict):
            for key in ("products", "items", "rows", "data"):
                val = raw_products.get(key)
                if isinstance(val, list):
                    return [p for p in val if isinstance(p, dict)]
            if raw_products.get("name") or raw_products.get("product_name"):
                return [raw_products]
        return []

    def handle_market_data_retrieval(self, product: Optional[str]) -> Dict[str, Any]:
        data = {}
        if not product:
            return data
        prices = None
        try:
            prices = self.agent.tool.get_commodity_price(product)
        except Exception as e:
            logger.warning("Local tool get_commodity_price failed: %s", e)
        if prices:
            data["prices"] = prices
        try:
            data["trends"] = self.agent.tool.analyze_market_trends(product)
        except Exception as e:
            logger.warning("Local tool analyze_market_trends failed: %s", e)
            data.setdefault("trends", {})
        return data

    async def ensure_product_exists_for_offer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"checked": False, "exists": False, "created": False}
        if not getattr(self.agent, "db_host", None):
            return result

        producer_id = payload.get("user_id")
        product_name = payload.get("product")
        if not producer_id or not product_name:
            return result

        try:
            listed = await self.agent.mcp_list_products(str(producer_id))
            result["checked"] = True
            products = self.extract_products_list(listed)
            target = str(product_name).strip().lower()
            for p in products:
                name = str(p.get("name") or p.get("product_name") or "").strip().lower()
                if name and (name == target or target in name or name in target):
                    result["exists"] = True
                    result["product"] = p
                    return result
        except Exception as exc:
            result["list_error"] = str(exc)

        try:
            qty = float(payload.get("quantity") or 0)
        except Exception:
            qty = 0.0
        try:
            price = float(payload.get("price") or 0)
        except Exception:
            price = 0.0

        try:
            created = await self.agent.mcp_create_product(
                producer_id=str(producer_id),
                name=str(product_name),
                price=max(price, 0.0),
                quantity_for_sale=max(qty, 0.0),
                unit="KG",
            )
            result["created"] = True
            result["created_product"] = created
        except Exception as exc:
            result["create_error"] = str(exc)

        return result

    async def handle_user_stock_retrieval(self, state: Dict[str, Any], product: str) -> Dict[str, Any]:
        data = {}
        if not getattr(self.agent, "db_host", None) or not state.get("user_profile"):
            return data
        uid = self.agent._get_user_id(state)
        if not uid:
            return data
        try:
            stocks = await self.agent.mcp_get_farm_stocks(uid)
            relevant = self.filter_stocks_by_product(stocks, product)
            if relevant:
                data["user_stock"] = self.serialize_stocks(relevant)
        except Exception as e:
            logger.warning("MCP Stock Check failed: %s", e)
        return data
