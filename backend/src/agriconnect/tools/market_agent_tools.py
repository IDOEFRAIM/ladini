import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Agent.MarketCoach.Tools")


class MarketAgentTools:
    """Helper container for MarketCoach utility logic."""

    def __init__(self, agent):
        self.agent = agent

    @staticmethod
    def _extract_items(raw: Any) -> List[Any]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("data", "items", "rows", "results"):
                value = raw.get(key)
                if isinstance(value, list):
                    return value
            return [raw]
        return []

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
        return [f"Lieu '{loc}' non trouve dans le registre officiel."]

    def filter_stocks_by_product(self, stocks: List[Any], product: str) -> List[Any]:
        relevant = []
        for stock in stocks:
            if hasattr(stock, "item_name"):
                stock_name = getattr(stock, "item_name", "")
            elif isinstance(stock, dict):
                stock_name = stock.get("item_name", "")
            else:
                stock_name = ""
            if product and product.lower() in str(stock_name).lower():
                relevant.append(stock)
        return relevant

    def serialize_stocks(self, stocks: List[Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for stock in stocks:
            if hasattr(stock, "model_dump"):
                try:
                    out.append(stock.model_dump())
                    continue
                except Exception:
                    pass
            if hasattr(stock, "dict"):
                try:
                    out.append(stock.dict())
                    continue
                except Exception:
                    pass
            if hasattr(stock, "to_dict"):
                try:
                    out.append(stock.to_dict())
                    continue
                except Exception:
                    pass
            if isinstance(stock, dict):
                out.append(stock)
            else:
                try:
                    out.append(vars(stock))
                except Exception:
                    out.append({"repr": str(stock)})
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
        # Price and trend lookups have been intentionally disabled.
        # The system no longer attempts to verify or suggest prices automatically.
        # Return a neutral informational payload so callers can render a message.
        if not product:
            return {}

        return {"info": "Recherche de prix désactivée dans cet environnement."}

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
            for product in products:
                name = str(product.get("name") or product.get("product_name") or "").strip().lower()
                if name and (name == target or target in name or name in target):
                    result["exists"] = True
                    result["product"] = product
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
        data: Dict[str, Any] = {}
        if not getattr(self.agent, "db_host", None) or not state.get("user_profile"):
            return data

        producer_id = self.agent._get_user_id(state)
        if not producer_id:
            return data

        all_stocks: List[Any] = []

        # First try direct API shape where producer_id doubles as farm identifier.
        try:
            direct = await self.agent.mcp_get_farm_stocks(str(producer_id))
            all_stocks.extend(self._extract_items(direct))
        except Exception as exc:
            logger.warning("Direct farm stock fetch failed: %s", exc)

        # Fallback: resolve farms from producer, then gather each farm stock.
        if not all_stocks:
            try:
                farms_raw = await self.agent.mcp_get_farms(str(producer_id))
                farms = self._extract_items(farms_raw)
                for farm in farms:
                    if not isinstance(farm, dict):
                        continue
                    farm_id = farm.get("id")
                    if not farm_id:
                        continue
                    try:
                        farm_stocks = await self.agent.mcp_get_farm_stocks(str(farm_id))
                        all_stocks.extend(self._extract_items(farm_stocks))
                    except Exception as stock_exc:
                        logger.warning("Stock fetch failed for farm %s: %s", farm_id, stock_exc)
            except Exception as farms_exc:
                logger.warning("MCP farm resolution failed: %s", farms_exc)

        if all_stocks:
            relevant = self.filter_stocks_by_product(all_stocks, product)
            if relevant:
                data["user_stock"] = self.serialize_stocks(relevant)

        return data
