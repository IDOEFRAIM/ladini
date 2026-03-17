import asyncio
import json
import logging
import re
import operator
import hashlib
from typing import Any, Dict, List, Optional, TypedDict, Annotated

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver 


from agriconnect.graphs.prompts import (
        MARKET_EXTRACT_INTENT_TEMPLATE,
        MARKET_MODERATE_FINANCE_TEMPLATE,
        MARKET_SYSTEM_PROMPT_TEMPLATE,
        MARKET_USER_PROMPT_TEMPLATE,
    )
from agriconnect.rag.components import get_groq_sdk
from agriconnect.tools.market import AgrimarketTool
from agriconnect.agents.base import BaseAgent

logger = logging.getLogger("Agent.MarketCoach")

# Test producer id used for manual __main__ runs
PRODUCER_ID = "fa987f63-fafa-4147-9676-52c9af0edc75"

class MarketAgentState(TypedDict, total=False):
    # Core User Data
    user_query: str
    user_profile: Dict[str, Any]
    user_level: str

    # Intent & Entities
    intent: str  # CHECK_PRICE, SELL_OFFER, BUY_OFFER, SCAM_CHECK, REGISTER_SURPLUS, CONFIRM_TRANSACTION, CANCEL_TRANSACTION
    product: str
    location: str
    price_mentioned: Optional[float]
    quantity_mentioned: Optional[float]
    unit_mentioned: Optional[str]
    normalized_quantity_kg: Optional[float]
    
    # Process Data
    market_data: Dict[str, Any]
    scam_analysis: Dict[str, Any]
    final_response: str
    status: str
    
    # Robustness & Logic (Using operator.add for accumulation)
    warnings: Annotated[List[str], operator.add]
    missing_fields: List[str]
    validation_errors: Annotated[List[str], operator.add]
    
    # Transaction State
    waiting_for_confirmation: bool
    transaction_payload: Dict[str, Any]
    transaction_hash: str
    audio_file_path: Optional[str]
    
    # Security
    security_status: str
    security_reason: str
    # HITL / Hand-off flags
    requires_human: bool
    handoff_to: str
    clarification_needed: str

class MarketCoach(BaseAgent):
    """Agent prix de marché — hérite de BaseAgent pour HITL/handoff."""

    _capabilities = [
        "CHECK_PRICE", "SELL_OFFER", "BUY_OFFER",
        "SCAM_CHECK", "MARKET_ANALYSIS",
    ]

    def __init__(self, llm_client=None, mcp_session=None):
        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"
        self.tool = AgrimarketTool()

        # MCP access is provided by the Orchestrator (Host) via DI.
        # Experts NEVER create their own MCP clients.
        self.mcp_session = mcp_session  # MCPSessionManager | None
        self.db_server = mcp_session    # backward-compat alias
        # Prefer using the injected session manager as the DB host for MCP calls.
        # This ensures calls go through the Shield (validation, HITL, audit).
        self.db_host = mcp_session

        #efra mets cadans les tools,c est plus facile a maintenir,on aura juste a faire les calcul idoine
        self.UNIT_REGISTRY = {
            "sac": 100,      
            "sac_100": 100,
            "sac_50": 50,
            "tin": 15,       
            "panier": 25,
            "tonnes": 1000,
            "kg": 1
        }
        ##note a moi meme,on fetch avec la db
        self.VALID_CITIES = [
            "ouagadougou", "bobo-dioulasso", "bobo", "koudougou", "ouahigouya", 
            "kaya", "banfora", "pouytenga", "fada", "fada n'gourma", "dédougou", "nouna"
        ]

        try:
            self.llm = llm_client if llm_client else get_groq_sdk()
        except Exception as exc:
            logger.error("LLM Initialization failed: %s", exc)
            self.llm = None

    # ------------------------------------------------------------------ #
    # NODES
    # ------------------------------------------------------------------ #
    
    def transcribe_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Handles audio input. Returns partial state update."""
        audio_path = state.get("audio_file_path")
        if audio_path and audio_path.endswith(".wav"):
            logger.info(f"Processing audio: {audio_path}")
            # Mock transcription logic
            # return {"user_query": transcribed_text}
        return {} 

    def analyze_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Analyzes intent and performs security checks."""
        query = state.get("user_query", "").strip()
        
        if not query:
            return {"status": "ERROR", "warnings": ["Empty query received."]}

        # 1. Security Check (Fail-Fast)
        moderation = self._moderate_finance(query)
        if moderation.get("is_scam"):
            return {
                "security_status": "SCAM_DETECTED",
                "security_reason": moderation.get("reason"),
                "status": "SCAM_DETECTED"
            }

        # 2. Human-in-the-loop Confirmation Logic
        if state.get("waiting_for_confirmation"):
            if re.search(r"\b(oui|ok|d'accord|c'est bon|valide|confirme)\b", query, re.IGNORECASE):
                return {
                    "intent": "CONFIRM_TRANSACTION",
                    "status": "CONFIRMED",
                    "security_status": "SAFE"
                }
            if re.search(r"\b(non|annule|stop|pas bonne|erreur)\b", query, re.IGNORECASE):
                return {
                    "intent": "CANCEL_TRANSACTION",
                    "status": "CANCELLED",
                    "waiting_for_confirmation": False
                }

        # 3. Standard Intent Extraction
        analysis = self._extract_market_intent(query)
        return {
            "intent": analysis.get("intent", "CHECK_PRICE"),
            "product": analysis.get("product"),
            "location": analysis.get("location"),
            "price_mentioned": analysis.get("price"),
            "quantity_mentioned": analysis.get("quantity"),
            "unit_mentioned": analysis.get("unit"),
            "security_status": "SAFE",
            "status": "ANALYZED"
        }

    def validate_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Validates business rules and normalizes data."""
        # Skip validation for non-transactional statuses
        if state.get("status") in ["SCAM_DETECTED", "ERROR", "CONFIRMED", "CANCELLED"]:
            return {}

        intent = state.get("intent")
        if intent not in ["REGISTER_SURPLUS", "SELL_OFFER", "BUY_OFFER"]:
            return {}

        errors: List[str] = []
        missing: List[str] = []
        updates: Dict[str, Any] = {}

        # 1. Product
        missing.extend(self._validate_product(state))

        # 2. Quantity & Unit
        qty_updates, qty_errors, qty_missing = self._validate_quantity(state)
        updates.update(qty_updates)
        errors.extend(qty_errors)
        missing.extend(qty_missing)

        # 3. Location
        loc_missing, loc_warnings = self._validate_location(state)
        missing.extend(loc_missing)
        if loc_warnings:
            updates.setdefault("warnings", []).extend(loc_warnings)

        # 4. Price
        errors.extend(self._validate_price(state))

        # Final Decision
        updates["missing_fields"] = missing
        updates["validation_errors"] = errors

        if not missing and not errors:
            profile = state.get("user_profile", {}) or {}
            user_id = profile.get("user_id") or profile.get("id") or profile.get("phone") or "anon_user"
            # Enrich profile from MCP when possible
            try:
                if self.db_host and (not profile.get("name") or not profile.get("zone_id")):
                    prof = self.mcp_get_user_profile(user_id)
                    if prof:
                        # prof may be GenericResult.data or raw dict
                        if isinstance(prof, dict) and prof.get("user_id"):
                            profile.update(prof)
                        elif isinstance(prof, dict):
                            profile.update(prof)
                        updates.setdefault("user_profile", {}).update(profile)
            except Exception:
                pass
            payload = {
                "product": state.get("product"),
                "quantity": updates.get("normalized_quantity_kg", 0),
                "price": state.get("price_mentioned"),
                "location": state.get("location"),
                "user_id": user_id,
            }
            payload_str = json.dumps(payload, sort_keys=True)
            tx_hash = hashlib.md5(payload_str.encode()).hexdigest()

            updates.update({
                "transaction_payload": payload,
                "transaction_hash": tx_hash,
                "waiting_for_confirmation": True,
            })
        else:
            updates["status"] = "MISSING_INFO"
        
        return updates

    async def _handle_transaction_execution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute transaction via MCP Shield (preferred) or local tool (fallback)."""
        data = {}
        success = False

        # Prefer MCP path — validated, audited, permission-checked
        if self.db_host:
            try:
                # Prefer the wrapper to keep MCP access consistent and auditable
                result = self.mcp_register_surplus_offer(
                    payload.get("user_id"),
                    payload["product"],
                    payload["quantity"],
                    payload.get("location"),
                )
                success = True
                data["mcp_result"] = result
            except Exception as exc:
                logger.warning("MCP register_surplus_offer failed, fallback to local tool: %s", exc)

        # Fallback to local AgrimarketTool
        if not success:
            try:
                success = self.tool.register_surplus_offer(
                    payload["product"],
                    payload["quantity"],
                    payload["location"],
                )
            except Exception:
                success = False

        data["registration_status"] = "SUCCESS" if success else "OFFLINE_SAVED"
        if payload.get("location"):
            data["logistics"] = self.tool.get_logistics_info(payload["location"])

        # Persist conversation/audit via MCP when available
        try:
            if self.db_host:
                q = json.dumps(payload, ensure_ascii=False)
                r = json.dumps(data, ensure_ascii=False)
                self.mcp_persist_conversation(payload.get("user_id", "anon_user"), q, r, agent_type="MarketCoach")
        except Exception:
            pass

        return data

    def _get_user_id(self, state: MarketAgentState) -> Optional[str]:
        """Extract user ID from state profile."""
        user_profile = state.get("user_profile")
        if not user_profile:
            return None
        return user_profile.get("user_id") or user_profile.get("id")

    def _filter_stocks_by_product(self, stocks: List[Any], product: str) -> List[Any]:
        """Filter stocks matching the product name."""
        relevant = []
        for s in stocks:
            s_name = getattr(s, "item_name", "") if hasattr(s, "item_name") else s.get("item_name", "")
            if product.lower() in s_name.lower():
                relevant.append(s)
        return relevant

    def _serialize_stocks(self, stocks: List[Any]) -> List[Dict[str, Any]]:
        """Convert stocks to serializable format."""
        out: List[Dict[str, Any]] = []
        for s in stocks:
            if hasattr(s, "model_dump"):
                try:
                    out.append(s.model_dump())
                    continue
                except Exception:
                    pass
            if hasattr(s, "dict"):
                try:
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

    # ------------------------------------------------------------------ #
    # MCP TOOL HELPERS
    # ------------------------------------------------------------------ #
    def _call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Call an MCP tool via the injected `db_host` and normalize result.

        Returns the `data` field when tools return GenericResult JSON, or
        the raw object when the backend adapter returns native objects.
        """
        if not self.db_host:
            return None
        args = arguments or {}
        try:
            res = self._run_async(self.db_host.call_tool(tool_name, args))
        except Exception as e:
            logger.warning("MCP tool %s call failed: %s", tool_name, e)
            return None

        # Normalize JSON-string GenericResult responses
        if isinstance(res, str):
            try:
                parsed = json.loads(res)
                # Prefer returning the `data` payload when present
                return parsed.get("data", parsed)
            except Exception:
                return res

        return res

    # Thin wrappers for commonly available MCP tools (exposed for reuse)
    def mcp_get_user_profile(self, user_id: str) -> Any:
        return self._call_mcp_tool("get_user_profile", {"user_id": user_id})

    def mcp_get_user_by_phone(self, phone: str) -> Any:
        return self._call_mcp_tool("get_user_by_phone", {"phone": phone})

    def mcp_identify_or_create_user(self, phone: str, name: str = "", zone_id: str = "") -> Any:
        return self._call_mcp_tool("identify_or_create_user", {"phone": phone, "name": name, "zone_id": zone_id})

    def mcp_prepare_transaction_staging(self, product_id: str, quantity_kg: float, price_fcfa_per_unit: float, buyer_phone: str, zone_id: str = "", source: str = "WHATSAPP") -> Any:
        return self._call_mcp_tool("prepare_transaction_staging", {"product_id": product_id, "quantity_kg": quantity_kg, "price_fcfa_per_unit": price_fcfa_per_unit, "buyer_phone": buyer_phone, "zone_id": zone_id, "source": source})

    def mcp_create_order(self, product_id: str, quantity: float, buyer_phone: str, **kwargs) -> Any:
        body = {"product_id": product_id, "quantity": quantity, "buyer_phone": buyer_phone}
        body.update(kwargs)
        return self._call_mcp_tool("create_order", body)

    def mcp_commit_staged_transaction(self, transaction_id: str, approved: bool = True) -> Any:
        return self._call_mcp_tool("commit_staged_transaction", {"transaction_id": transaction_id, "approved": approved})

    def mcp_create_agent_action(self, agent_name: str, action_type: str, payload: dict, user_id: str | None = None, priority: str = "MEDIUM") -> Any:
        return self._call_mcp_tool("create_agent_action", {"agent_name": agent_name, "action_type": action_type, "payload": payload, "user_id": user_id, "priority": priority})

    def mcp_get_pending_actions(self, agent_name: str = "", limit: int = 20) -> Any:
        return self._call_mcp_tool("get_pending_actions", {"agent_name": agent_name, "limit": limit})

    def mcp_update_action_status(self, action_id: str, new_status: str, admin_notes: str | None = None, validated_by_id: str | None = None) -> Any:
        return self._call_mcp_tool("update_action_status", {"action_id": action_id, "new_status": new_status, "admin_notes": admin_notes, "validated_by_id": validated_by_id})

    def mcp_search_products(self, product_name: str, zone_id: str | None = None, limit: int = 10) -> Any:
        return self._call_mcp_tool("search_products", {"product_name": product_name, "zone_id": zone_id, "limit": limit})

    def mcp_create_product(self, producer_id: str, name: str, price: float, quantity_for_sale: float, **kwargs) -> Any:
        body = {"producer_id": producer_id, "name": name, "price": price, "quantity_for_sale": quantity_for_sale}
        body.update(kwargs)
        return self._call_mcp_tool("create_product", body)

    def mcp_create_auction(self, buyer_id: str, sub_category_id: str, quantity: float, max_price_per_unit: float, deadline: str, **kwargs) -> Any:
        body = {"buyer_id": buyer_id, "sub_category_id": sub_category_id, "quantity": quantity, "max_price_per_unit": max_price_per_unit, "deadline": deadline}
        body.update(kwargs)
        return self._call_mcp_tool("create_auction", body)

    def mcp_place_bid(self, auction_id: str, producer_id: str, offered_price: float) -> Any:
        return self._call_mcp_tool("place_bid", {"auction_id": auction_id, "producer_id": producer_id, "offered_price": offered_price})

    def mcp_get_open_auctions(self, zone_id: str | None = None, limit: int = 20) -> Any:
        return self._call_mcp_tool("get_open_auctions", {"zone_id": zone_id, "limit": limit})

    def mcp_get_farms(self, producer_id: str) -> Any:
        return self._call_mcp_tool("get_farms", {"producer_id": producer_id})

    def mcp_get_stocks(self, farm_id: str) -> Any:
        return self._call_mcp_tool("get_stocks", {"farm_id": farm_id})

    def mcp_add_stock(self, farm_id: str, item_name: str, quantity: float, **kwargs) -> Any:
        body = {"farm_id": farm_id, "item_name": item_name, "quantity": quantity}
        body.update(kwargs)
        return self._call_mcp_tool("add_stock", body)

    def mcp_remove_stock(self, farm_id: str, item_name: str, quantity: float, **kwargs) -> Any:
        body = {"farm_id": farm_id, "item_name": item_name, "quantity": quantity}
        body.update(kwargs)
        return self._call_mcp_tool("remove_stock", body)

    def mcp_add_expense(self, farm_id: str, label: str, amount: float, **kwargs) -> Any:
        body = {"farm_id": farm_id, "label": label, "amount": amount}
        body.update(kwargs)
        return self._call_mcp_tool("add_expense", body)

    def mcp_get_expenses(self, farm_id: str, category: str | None = None, limit: int = 50) -> Any:
        return self._call_mcp_tool("get_expenses", {"farm_id": farm_id, "category": category, "limit": limit})

    def mcp_get_expense_summary(self, farm_id: str) -> Any:
        return self._call_mcp_tool("get_expense_summary", {"farm_id": farm_id})

    def mcp_get_producer_dashboard(self, producer_id: str) -> Any:
        return self._call_mcp_tool("get_producer_dashboard", {"producer_id": producer_id})

    def mcp_get_orders(self, buyer_id: str = "", buyer_phone: str = "", status: str = "", limit: int = 20) -> Any:
        return self._call_mcp_tool("get_orders", {"buyer_id": buyer_id, "buyer_phone": buyer_phone, "status": status, "limit": limit})

    def mcp_update_stock_with_movement(self, farm_id: str, item_name: str, quantity_change: float, reason: str) -> Any:
        return self._call_mcp_tool("update_stock_with_movement", {"farm_id": farm_id, "item_name": item_name, "quantity_change": quantity_change, "reason": reason})

    def mcp_persist_conversation(self, user_id: str, query_json: str, response_json: str, agent_type: str = "MarketCoach") -> Any:
        return self._call_mcp_tool("persist_conversation", {"user_id": user_id, "query_json": query_json, "response_json": response_json, "agent_type": agent_type})

    def mcp_register_surplus_offer(self, user_id: str, commodity: str, quantity: float, location: str | None = None) -> Any:
        body = {"user_id": user_id, "commodity": commodity, "quantity": quantity, "location": location}
        return self._call_mcp_tool("register_surplus_offer", body)

    def mcp_get_farm_stocks(self, farm_id: str) -> Any:
        return self._call_mcp_tool("get_farm_stocks", {"farm_id": farm_id})

    def mcp_list_products(self, producer_id: str) -> Any:
        return self._call_mcp_tool("list_products", {"producer_id": producer_id})


    async def _handle_user_stock_retrieval(self, state: MarketAgentState, product: str) -> Dict[str, Any]:
        """Retrieve user stock via MCP Shield (permission-checked, masked)."""
        data = {}
        if not self.db_host or not state.get("user_profile"):
            return data
        
        uid = self._get_user_id(state)
        if not uid:
            return data
        
        try:
            # Use wrapper to fetch farm stocks via MCP Shield.
            stocks = self.mcp_get_farm_stocks(uid)
            relevant = self._filter_stocks_by_product(stocks, product)
            if relevant:
                data["user_stock"] = self._serialize_stocks(relevant)
        except Exception as e:
            logger.warning("MCP Stock Check failed: %s", e)
        
        return data

    def _handle_market_data_retrieval(self, product: Optional[str]) -> Dict[str, Any]:
        """Retrieve market prices and trends for a product."""
        data = {}
        if not product:
            return data
        
        prices = self.tool.get_commodity_price(product)
        if prices:
            data["prices"] = prices
        data["trends"] = self.tool.analyze_market_trends(product)
        
        return data

    def fetch_data_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Executes transactions or fetches market data via MCP/Tools.

        Handles both async MCP calls and sync local-tool calls by running
        async helpers in the current or a new event loop.
        """
        status = state.get("status")

        if status in ["SCAM_DETECTED", "WAITING_CONFIRMATION", "MISSING_INFO", "CANCELLED", "ERROR"]:
            return {}

        updates: Dict[str, Any] = {}

        if status == "CONFIRMED" and state.get("transaction_payload"):
            data = self._run_async(self._handle_transaction_execution(state["transaction_payload"]))
            updates["status"] = "COMPLETED_TRANSACTION"
        else:
            product = state.get("product")
            data = self._run_async(self._handle_user_stock_retrieval(state, product)) if product else {}
            data.update(self._handle_market_data_retrieval(product))
            updates["status"] = "DATA_FETCHED"

        updates["market_data"] = data
        return updates

    # ------------------------------------------------------------------ #
    # ASYNC BRIDGE
    # ------------------------------------------------------------------ #

    @staticmethod
    def _run_async(coro) -> Any:
        """Run an async coroutine from a sync context safely."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an existing event loop (e.g. LangGraph async runner)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=15)
        else:
            return asyncio.run(coro)

    # ------------------------------------------------------------------ #
    # EXTRA HELPERS (extracted to reduce cyclomatic complexity)
    # ------------------------------------------------------------------ #
    def _unit_factor(self, unit: Optional[str]) -> int:
        """Return multiplication factor for given unit string."""
        try:
            unit_clean = str(unit).lower().replace("s", "")
        except Exception:
            return 100
        for key, val in self.UNIT_REGISTRY.items():
            if key in unit_clean:
                return val
        return 100

    def _location_warnings(self, loc: str) -> List[str]:
        """Return warnings list if location not recognized."""
        if not loc:
            return []
        loc_l = loc.lower()
        if any(valid in loc_l for valid in self.VALID_CITIES):
            return []
        return [f"Lieu '{loc}' non trouvé dans le registre officiel."]

    # NOTE: _execute_transaction and _retrieve_product_market were duplicates
    # of _handle_transaction_execution and _handle_market_data_retrieval.
    # They have been removed to keep a single code-path per operation.

    # ------------------------------------------------------------------ #
    # VALIDATION HELPERS
    # ------------------------------------------------------------------ #
    def _validate_product(self, state: MarketAgentState) -> List[str]:
        """Return list of missing product-related fields."""
        missing: List[str] = []
        if not state.get("product"):
            missing.append("produit (maïs, sorgho...)")
        return missing

    def _validate_quantity(self, state: MarketAgentState) -> tuple[Dict[str, Any], List[str], List[str]]:
        """Validate and normalize quantity; returns (updates, errors, missing)."""
        updates: Dict[str, Any] = {}
        errors: List[str] = []
        missing: List[str] = []

        qty = state.get("quantity_mentioned")
        unit = state.get("unit_mentioned", "sac")

        if qty in (None, ""):
            missing.append("quantité")
            return updates, errors, missing

        factor = self._unit_factor(unit)
        try:
            updates["normalized_quantity_kg"] = float(qty) * factor
        except (ValueError, TypeError):
            errors.append("Quantité invalide (doit être un nombre).")

        return updates, errors, missing

    def _validate_location(self, state: MarketAgentState) -> tuple[List[str], List[str]]:
        """Return (missing_fields, warnings) for location."""
        missing: List[str] = []
        warnings: List[str] = []
        loc = state.get("location", "")
        if not loc:
            missing.append("lieu")
            return missing, warnings

        warnings = self._location_warnings(loc)
        return missing, warnings

    def _validate_price(self, state: MarketAgentState) -> List[str]:
        """Validate price field and return list of errors."""
        errors: List[str] = []
        price = state.get("price_mentioned")
        if price is None:
            return errors
        if not isinstance(price, (int, float)):
            errors.append("Prix invalide.")
            return errors
        if price < 0:
            errors.append("Prix invalide.")
        return errors

    def compose_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Generates the final response based on status."""
        status = state.get("status")
        
        if status == "SCAM_DETECTED":
            return {"final_response": self._build_scam_alert(state)}

        if status == "MISSING_INFO":
            missing = ", ".join(state.get("missing_fields", []))
            return {"final_response": f"Pour finaliser, j'ai besoin de : {missing}. Pouvez-vous préciser ?"}

        if status == "WAITING_CONFIRMATION":
            payload = state.get("transaction_payload", {})
            response = (
                f"📝 **Récapitulatif** :\n"
                f"- {payload.get('product')} : {payload.get('quantity')} kg\n"
                f"- Lieu : {payload.get('location')}\n"
                f"- Prix : {payload.get('price', 'Non précisé')} FCFA\n\n"
                "Je confirme l'enregistrement ? (Oui/Non)"
            )
            return {"final_response": response}

        if status == "CANCELLED":
            return {"final_response": "❌ Opération annulée."}

        # Default / Completed
        response = self._generate_market_response(state)
        return {"final_response": response, "status": "COMPLETED"}

    # ------------------------------------------------------------------ #
    # HELPERS
    # ------------------------------------------------------------------ #

    def _moderate_finance(self, query: str) -> Dict[str, Any]:
        if not self.llm: return {"is_scam": False}
        try:
            formatted = MARKET_MODERATE_FINANCE_TEMPLATE.format(query=query)
            resp = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": formatted}],
                temperature=0,
                response_format={"type": "json_object"} 
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("Moderation failed: %s", e)
            return {"is_scam": False}

    def _extract_market_intent(self, query: str) -> Dict[str, Any]:
        if not self.llm: return {"intent": "CHECK_PRICE"}
        try:
            formatted = MARKET_EXTRACT_INTENT_TEMPLATE.format(query=query)
            resp = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": formatted}],
                temperature=0,
                response_format={"type": "json_object"}
            )
            return json.loads(resp.choices[0].message.content)
        except Exception:
            return {"intent": "CHECK_PRICE"}

    def _build_scam_alert(self, state: MarketAgentState) -> str:
        reason = state.get("security_reason", "Suspicious activity detected.")
        return f"🚨 **ALERTE SÉCURITÉ**\n{reason}\nRefusez tout transfert d'argent."

    def _generate_market_response(self, state: MarketAgentState) -> str:
        data = state.get("market_data", {})
        if not self.llm:
            return "Données récupérées (Mode hors ligne)."
            
        system_content = MARKET_SYSTEM_PROMPT_TEMPLATE.format(
            market_data=json.dumps(data, ensure_ascii=False),
            logistics_data=json.dumps(data.get("logistics", {}), ensure_ascii=False)
        )
        try:
            resp = self.llm.chat.completions.create(
                model=self.model_answer,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": MARKET_USER_PROMPT_TEMPLATE.format(query=state.get('user_query'))}
                ],
                temperature=0.2
            )
            return resp.choices[0].message.content
        except Exception:
            return "Désolé, service indisponible."

    @staticmethod
    def route_analysis(state):
        status = state.get("status")
        if status == "SCAM_DETECTED":
            return "compose" # Fast track to alert
        if status == "CONFIRMED":
            return "fetch_data" # Fast track to execution
        if status == "CANCELLED":
            return "compose" # Fast track to cancel msg
        if status == "ERROR":
            return END
        return "validate"

    @staticmethod
    def route_validation(state):
        if state.get("status") in ["MISSING_INFO", "WAITING_CONFIRMATION"]:
            return "compose" # Ask user for details/confirm
        return "fetch_data" # Proceed to fetch/execute
    # ------------------------------------------------------------------ #
    # BUILD
    # ------------------------------------------------------------------ #

    def build(self, checkpointer=None):
        """Builds the StateGraph with optional persistence."""
        workflow = StateGraph(MarketAgentState)
        
        workflow.add_node("transcribe", self.transcribe_node)
        workflow.add_node("analyze", self.analyze_node)
        workflow.add_node("validate", self.validate_node)
        workflow.add_node("fetch_data", self.fetch_data_node)
        workflow.add_node("compose", self.compose_node)

        #workflow.set_entry_point("transcribe")
        #workflow.add_edge("transcribe", "analyze")
        workflow.set_entry_point("analyze")

        workflow.add_conditional_edges("analyze", self.route_analysis)
        
        
            
        workflow.add_conditional_edges("validate", self.route_validation)
        
        workflow.add_edge("fetch_data", "compose")
        workflow.add_edge("compose", END)
        
        return workflow.compile(checkpointer=checkpointer)

if __name__ == "__main__":
    # Persistence setup (In-Memory for testing, replace with Postgres/Sqlite for Prod)
    memory = MemorySaver()

    async def _main():
        # Try to initialize a real DB-backed service and expose a small
        # `call_tool(name, args)` adapter. If DB isn't available we fall
        # back to a lightweight mock that records calls.
        mcp_client = None
        use_real_db = False
        try:
            from agriconnect.core.database import init_db, check_connection
            from agriconnect.services.database.database_service import AgriDatabaseService

            init_db()
            ok = await check_connection()
            if ok:
                svc = AgriDatabaseService()

                class RealMCPAdapter:
                    def __init__(self, svc):
                        self._svc = svc

                    async def call_tool(self, name: str, args: dict):
                        if name == "register_surplus_offer":
                            return await self._svc.create_surplus_offer(
                                args.get("user_id"),
                                args.get("commodity"),
                                args.get("quantity"),
                                zone_id=None,
                                location=args.get("location"),
                                channel="cli",
                            )
                        if name == "get_farm_stocks":
                            return await self._svc.get_stocks(args.get("farm_id"))
                        if name == "list_products":
                            return await self._svc.list_products(args.get("producer_id"))
                        raise NotImplementedError(f"Tool {name} not implemented in RealMCPAdapter")

                mcp_client = RealMCPAdapter(svc)
                use_real_db = True
                print("Using real DB for __main__ test (AgriDatabaseService).")
        except Exception as e:
            print("Real DB unavailable or init failed, using MockMCP for __main__ test:", e)

        if not mcp_client:
            class MockMCP:
                def __init__(self):
                    self.calls = []

                async def call_tool(self, name: str, args: dict):
                    self.calls.append((name, args))
                    return {"id": "mock-record-1", "status": "created", "args": args}

            mcp_client = MockMCP()

        coach = MarketCoach(mcp_session=mcp_client)
        app = coach.build(checkpointer=memory)

        # Simulating a conversation thread
        thread_id = "user_123_phone_number"
        config = {"configurable": {"thread_id": thread_id}}

        print("🚀 Démarrage AgriConnect Market Coach (Deterministic Test Mode)...\n")

        # Deterministic test: skip LLM-driven analysis and directly validate
        # a REGISTER_SURPLUS intent so we reliably assert confirmation flow
        # and persistence. This avoids background fetches that touch the DB.
        print("--- Simulating REGISTER_SURPLUS (50 kg maïs, Nouna) ---")
        test_state = {
            "intent": "REGISTER_SURPLUS",
            "product": "maïs",
            "quantity_mentioned": 50,
            "unit_mentioned": "kg",
            "location": "Nouna",
            "user_profile": {"user_id": PRODUCER_ID, "phone": "000000000"},
        }

        # Run validation synchronously (validate_node is sync)
        validate_updates = coach.validate_node(test_state)
        print(f"Validation updates: {validate_updates}")

        # Expect the agent to ask for confirmation
        if validate_updates.get("waiting_for_confirmation"):
            print("Agent requested confirmation (OK).")
        else:
            print("Agent did NOT request confirmation — failing test expectation.")

        # Build the transaction payload (use normalized_quantity_kg if present)
        payload = validate_updates.get("transaction_payload") or {
            "product": test_state["product"],
            "quantity": validate_updates.get("normalized_quantity_kg", 50),
            "location": test_state["location"],
            "user_id": PRODUCER_ID,
        }

        print("--- Simulating user confirmation and executing transaction... ---")
        # Execute transaction (this will call MCP adapter)
        exec_result = await coach._handle_transaction_execution(payload)
        print(f"Execution result: {exec_result}")

        if use_real_db:
            print("Real DB used — verify the `surplus_offers` table for PRODUCER_ID to confirm persistence.")

        # --- Simulate user asking to list their products via MCP tools ---
        print("\n--- User: Montre-moi mes produits en stock ---")
        try:
            products = coach.mcp_list_products(PRODUCER_ID)
            print(f"Products for {PRODUCER_ID}: {products}")
        except Exception as e:
            print("Failed to list products via MCP:", e)

    import asyncio

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass