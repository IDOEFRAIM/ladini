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
from agriconnect.tools.market_agent_tools import MarketAgentTools
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
    # Conversational draft memory
    pending_user_intent: str
    draft_data: Dict[str, Any]

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
        # Tools container to keep agent file small and focused
        self.tools = MarketAgentTools(self)

    # ------------------------------------------------------------------ #
    # NODES
    # ------------------------------------------------------------------ #
    
    async def transcribe_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Handles audio input. Returns partial state update."""
        audio_path = state.get("audio_file_path")
        if audio_path and audio_path.endswith(".wav"):
            logger.info(f"Processing audio: {audio_path}")
            # Mock transcription logic
            # return {"user_query": transcribed_text}
        return {} 

    async def analyze_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Analyzes intent and performs security checks."""
        query = state.get("user_query", "").strip()
        
        if not query:
            return {"status": "ERROR", "warnings": ["Empty query received."]}

        # 0) Load persisted user context (pending intent + draft data)
        profile = state.get("user_profile", {}) or {}
        user_id = profile.get("user_id") or profile.get("id") or profile.get("phone")
        persisted_ctx: Dict[str, Any] = {}
        if user_id and self.db_host:
            try:
                ctx = await self.mcp_get_user_context_state(str(user_id))
                if isinstance(ctx, dict):
                    persisted_ctx = ctx
            except Exception:
                persisted_ctx = {}

        # 1. Human-in-the-loop Confirmation Logic
        # Handle yes/no confirmations before moderation to avoid false scam flags
        # on short messages like "oui".
        if state.get("waiting_for_confirmation"):
            if re.search(r"\b(oui|ok|d'accord|c'est bon|valide|confirme)\b", query, re.IGNORECASE):
                return {
                    "intent": "CONFIRM_TRANSACTION",
                    "status": "CONFIRMED",
                    "security_status": "SAFE",
                    "waiting_for_confirmation": False,
                }
            if re.search(r"\b(non|annule|stop|pas bonne|erreur)\b", query, re.IGNORECASE):
                return {
                    "intent": "CANCEL_TRANSACTION",
                    "status": "CANCELLED",
                    "waiting_for_confirmation": False
                }

        # 2. Security Check (Fail-Fast)
        moderation = self._moderate_finance(query)
        if moderation.get("is_scam"):
            return {
                "security_status": "SCAM_DETECTED",
                "security_reason": moderation.get("reason"),
                "status": "SCAM_DETECTED"
            }

        # 3. Standard Intent Extraction
        # If we are awaiting missing info from the user (short answers like "gaoua" or "200"),
        # prefer a simple heuristic to map the reply to the missing field instead of calling the LLM.
        if state.get("status") == "MISSING_INFO":
            missing = state.get("missing_fields", []) or []
            # treat very short replies as direct answers
            tokens = query.split()
            if 0 < len(tokens) <= 4:
                # location fill
                if any("lieu" in m or "location" in m for m in missing):
                    return {"intent": state.get("intent"), "location": query, "status": "ANALYZED"}
                # price fill
                if any("prix" in m or "price" in m for m in missing):
                    m = re.search(r"(\d+\.?\d*)", query.replace(" ", ""))
                    price = float(m.group(1)) if m else None
                    return {"intent": state.get("intent"), "price_mentioned": price, "status": "ANALYZED"}
                # quantity fill
                if any("quantité" in m or "quantity" in m for m in missing):
                    m = re.search(r"(\d+\.?\d*)", query)
                    qty = float(m.group(1)) if m else None
                    return {"intent": state.get("intent"), "quantity_mentioned": qty, "status": "ANALYZED"}

        analysis = self._extract_market_intent(query)

        # Quick heuristic: ask to list user's products (capture many phrasings)
        ql = query.lower()
        if "produit" in ql or "produits" in ql:
            if re.search(r"\bmes\b|\bmoi\b|\bque\s*j(?:'|\s)?ai\b|connais|conna[iî]s|montre|liste", ql, re.IGNORECASE):
                return {
                    "intent": "LISTE_PRODUITS",
                    "product": None,
                    "location": None,
                    "price_mentioned": None,
                    "quantity_mentioned": None,
                    "unit_mentioned": None,
                    "security_status": "SAFE",
                    "status": "ANALYZED",
                }
        inferred_intent = analysis.get("intent")
        # Reuse pending intent from persisted state when the message is short/incomplete.
        if not inferred_intent and persisted_ctx.get("pending_intent"):
            inferred_intent = persisted_ctx.get("pending_intent")

        # Merge draft values as fallback when missing in current message
        draft_data = persisted_ctx.get("draft_data") if isinstance(persisted_ctx, dict) else {}
        if not isinstance(draft_data, dict):
            draft_data = {}
        if not inferred_intent:
            # If user provides product + quantity, default to surplus registration flow.
            has_product = bool(analysis.get("product"))
            has_qty = analysis.get("quantity") not in (None, "")
            if has_product and has_qty:
                inferred_intent = "REGISTER_SURPLUS"
        return {
            "intent": inferred_intent or "CHECK_PRICE",
            "product": analysis.get("product") or draft_data.get("product"),
            "location": analysis.get("location") or draft_data.get("location"),
            "price_mentioned": analysis.get("price") if analysis.get("price") is not None else draft_data.get("price_mentioned"),
            "quantity_mentioned": analysis.get("quantity") if analysis.get("quantity") is not None else draft_data.get("quantity_mentioned"),
            "unit_mentioned": analysis.get("unit") or draft_data.get("unit_mentioned"),
            "security_status": "SAFE",
            "status": "ANALYZED",
            "pending_user_intent": inferred_intent or persisted_ctx.get("pending_intent"),
            "draft_data": draft_data,
        }

    async def validate_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Validates business rules and normalizes data."""
        # Skip validation for non-transactional statuses
        if state.get("status") in ["SCAM_DETECTED", "ERROR", "CONFIRMED", "CANCELLED"]:
            return {}

        updates: Dict[str, Any] = {}

        intent = state.get("intent")
        # Accept common synonyms for selling intents (e.g. 'SELL')
        if intent not in ["REGISTER_SURPLUS", "SELL_OFFER", "BUY_OFFER", "SELL"]:
            # Fallback: if product + quantity are present, assume user wants to register an offer.
            if state.get("product") and state.get("quantity_mentioned") not in (None, ""):
                intent = "REGISTER_SURPLUS"
                updates["intent"] = intent
            else:
                return {}

        errors: List[str] = []
        missing: List[str] = []

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
        if state.get("price_mentioned") in (None, ""):
            missing.append("prix")
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
                    prof = await self.mcp_get_user_profile(user_id)
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
                "status": "WAITING_CONFIRMATION",
            })

            # Clear pending context once a complete transaction payload exists.
            try:
                uid = user_id
                if self.db_host and uid:
                    await self.mcp_upsert_user_context_state(
                        user_id=str(uid),
                        last_intent=intent or "REGISTER_SURPLUS",
                        pending_intent="",
                        draft_data={},
                    )
            except Exception:
                pass
        else:
            updates["status"] = "MISSING_INFO"
            # Persist progressive draft for incomplete conversational flows.
            try:
                profile = state.get("user_profile", {}) or {}
                uid = profile.get("user_id") or profile.get("id") or profile.get("phone")
                if self.db_host and uid:
                    draft = {
                        "product": state.get("product"),
                        "location": state.get("location"),
                        "price_mentioned": state.get("price_mentioned"),
                        "quantity_mentioned": state.get("quantity_mentioned"),
                        "unit_mentioned": state.get("unit_mentioned"),
                    }
                    await self.mcp_upsert_user_context_state(
                        user_id=str(uid),
                        last_intent=intent or state.get("intent") or "UNKNOWN",
                        pending_intent=intent or state.get("intent") or "UNKNOWN",
                        draft_data=draft,
                    )
                    updates["pending_user_intent"] = intent or state.get("intent") or "UNKNOWN"
                    updates["draft_data"] = draft
            except Exception:
                pass
        
        return updates

    async def _handle_transaction_execution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute transaction via MCP Shield (preferred) or local tool (fallback)."""
        data = {}
        success = False

        # Proactive behavior: verify product exists for producer and create it if missing.
        try:
            data["product_management"] = await self.tools.ensure_product_exists_for_offer(payload)
        except Exception as exc:
            data["product_management"] = {"checked": False, "error": str(exc)}

        # Prefer MCP path — validated, audited, permission-checked
        if self.db_host:
            try:
                # Prefer the wrapper to keep MCP access consistent and auditable
                result = await self.mcp_register_surplus_offer(
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

        # Event-driven decoupling: enqueue a background matching action.
        if success and self.db_host:
            try:
                await self.mcp_create_agent_action(
                    agent_name="MarketplaceBackgroundAgent",
                    action_type="RUN_MATCHING",
                    payload={
                        "product_name": payload.get("product"),
                        "location": payload.get("location"),
                        "user_id": payload.get("user_id"),
                        "quantity": payload.get("quantity"),
                    },
                    user_id=payload.get("user_id"),
                    priority="MEDIUM",
                )
                data["matching_event"] = "ENQUEUED"
            except Exception:
                data["matching_event"] = "FAILED_TO_ENQUEUE"

        # Persist conversation/audit via MCP when available
        try:
                if self.db_host:
                    q = json.dumps(payload, ensure_ascii=False)
                    r = json.dumps(data, ensure_ascii=False)
                    await self.mcp_persist_conversation(payload.get("user_id", "anon_user"), q, r, agent_type="MarketCoach")
        except Exception:
            pass

        return data

    def _get_user_id(self, state: MarketAgentState) -> Optional[str]:
        """Extract user ID from state profile."""
        user_profile = state.get("user_profile")
        if not user_profile:
            return None
        return user_profile.get("user_id") or user_profile.get("id")

    # Stock/product normalization and serialization helpers delegated to MarketTools

    # ensure_product_exists_for_offer delegated to MarketTools for clarity

    # ------------------------------------------------------------------ #
    # MCP TOOL HELPERS
    # ------------------------------------------------------------------ #
    async def _call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Call an MCP tool via the injected `db_host` and normalize result.

        Returns the `data` field when tools return GenericResult JSON, or
        the raw object when the backend adapter returns native objects.
        """
        if not self.db_host:
            return None
        args = arguments or {}
        try:
            # If the injected host exposes a synchronous call helper,
            # prefer it to avoid creating nested event loops in __main__.
            if hasattr(self.db_host, "call_tool_sync") and callable(getattr(self.db_host, "call_tool_sync")):
                try:
                    # If we're inside an active event loop, run the blocking sync call in an executor
                    try:
                        loop = asyncio.get_running_loop()
                        sync_res = await loop.run_in_executor(None, lambda: self.db_host.call_tool_sync(tool_name, args))
                    except RuntimeError:
                        # No running loop in this thread; call synchronously
                        sync_res = self.db_host.call_tool_sync(tool_name, args)

                    # call_tool_sync returns a dict like {"ok": True, "data": result}
                    if isinstance(sync_res, dict) and "data" in sync_res:
                        return sync_res.get("data")
                    return sync_res
                except Exception as exc:
                    logger.warning("MCP sync tool %s call failed: %s", tool_name, exc)
                    return None

            # Otherwise call the async API directly (we are in async nodes).
            res = await self.db_host.call_tool(tool_name, args)
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
    async def mcp_get_user_profile(self, user_id: str) -> Any:
        return await self._call_mcp_tool("get_user_profile", {"user_id": user_id})

    async def mcp_get_user_by_phone(self, phone: str) -> Any:
        return await self._call_mcp_tool("get_user_by_phone", {"phone": phone})

    async def mcp_identify_or_create_user(self, phone: str, name: str = "", zone_id: str = "") -> Any:
        return await self._call_mcp_tool("identify_or_create_user", {"phone": phone, "name": name, "zone_id": zone_id})

    async def mcp_prepare_transaction_staging(self, product_id: str, quantity_kg: float, price_fcfa_per_unit: float, buyer_phone: str, zone_id: str = "", source: str = "WHATSAPP") -> Any:
        return await self._call_mcp_tool("prepare_transaction_staging", {"product_id": product_id, "quantity_kg": quantity_kg, "price_fcfa_per_unit": price_fcfa_per_unit, "buyer_phone": buyer_phone, "zone_id": zone_id, "source": source})

    async def mcp_create_order(self, product_id: str, quantity: float, buyer_phone: str, **kwargs) -> Any:
        body = {"product_id": product_id, "quantity": quantity, "buyer_phone": buyer_phone}
        body.update(kwargs)
        return await self._call_mcp_tool("create_order", body)

    async def mcp_commit_staged_transaction(self, transaction_id: str, approved: bool = True) -> Any:
        return await self._call_mcp_tool("commit_staged_transaction", {"transaction_id": transaction_id, "approved": approved})

    async def mcp_create_agent_action(self, agent_name: str, action_type: str, payload: dict, user_id: str | None = None, priority: str = "MEDIUM") -> Any:
        return await self._call_mcp_tool("create_agent_action", {"agent_name": agent_name, "action_type": action_type, "payload": payload, "user_id": user_id, "priority": priority})

    async def mcp_get_pending_actions(self, agent_name: str = "", limit: int = 20) -> Any:
        return await self._call_mcp_tool("get_pending_actions", {"agent_name": agent_name, "limit": limit})

    async def mcp_update_action_status(self, action_id: str, new_status: str, admin_notes: str | None = None, validated_by_id: str | None = None) -> Any:
        return await self._call_mcp_tool("update_action_status", {"action_id": action_id, "new_status": new_status, "admin_notes": admin_notes, "validated_by_id": validated_by_id})

    async def mcp_search_products(self, product_name: str, zone_id: str | None = None, limit: int = 10) -> Any:
        return await self._call_mcp_tool("search_products", {"product_name": product_name, "zone_id": zone_id, "limit": limit})

    async def mcp_create_product(self, producer_id: str, name: str, price: float, quantity_for_sale: float, **kwargs) -> Any:
        body = {"producer_id": producer_id, "name": name, "price": price, "quantity_for_sale": quantity_for_sale}
        body.update(kwargs)
        return await self._call_mcp_tool("create_product", body)

    async def mcp_create_auction(self, buyer_id: str, sub_category_id: str, quantity: float, max_price_per_unit: float, deadline: str, **kwargs) -> Any:
        body = {"buyer_id": buyer_id, "sub_category_id": sub_category_id, "quantity": quantity, "max_price_per_unit": max_price_per_unit, "deadline": deadline}
        body.update(kwargs)
        return await self._call_mcp_tool("create_auction", body)

    async def mcp_place_bid(self, auction_id: str, producer_id: str, offered_price: float) -> Any:
        return await self._call_mcp_tool("place_bid", {"auction_id": auction_id, "producer_id": producer_id, "offered_price": offered_price})

    async def mcp_get_open_auctions(self, zone_id: str | None = None, limit: int = 20) -> Any:
        return await self._call_mcp_tool("get_open_auctions", {"zone_id": zone_id, "limit": limit})

    async def mcp_get_farms(self, producer_id: str) -> Any:
        return await self._call_mcp_tool("get_farms", {"producer_id": producer_id})

    async def mcp_get_stocks(self, farm_id: str) -> Any:
        return await self._call_mcp_tool("get_stocks", {"farm_id": farm_id})

    async def mcp_add_stock(self, farm_id: str, item_name: str, quantity: float, **kwargs) -> Any:
        body = {"farm_id": farm_id, "item_name": item_name, "quantity": quantity}
        body.update(kwargs)
        return await self._call_mcp_tool("add_stock", body)

    async def mcp_remove_stock(self, farm_id: str, item_name: str, quantity: float, **kwargs) -> Any:
        body = {"farm_id": farm_id, "item_name": item_name, "quantity": quantity}
        body.update(kwargs)
        return await self._call_mcp_tool("remove_stock", body)

    async def mcp_add_expense(self, farm_id: str, label: str, amount: float, **kwargs) -> Any:
        body = {"farm_id": farm_id, "label": label, "amount": amount}
        body.update(kwargs)
        return await self._call_mcp_tool("add_expense", body)

    async def mcp_get_expenses(self, farm_id: str, category: str | None = None, limit: int = 50) -> Any:
        return await self._call_mcp_tool("get_expenses", {"farm_id": farm_id, "category": category, "limit": limit})

    async def mcp_get_expense_summary(self, farm_id: str) -> Any:
        return await self._call_mcp_tool("get_expense_summary", {"farm_id": farm_id})

    async def mcp_get_producer_dashboard(self, producer_id: str) -> Any:
        return await self._call_mcp_tool("get_producer_dashboard", {"producer_id": producer_id})

    async def mcp_get_orders(self, buyer_id: str = "", buyer_phone: str = "", status: str = "", limit: int = 20) -> Any:
        return await self._call_mcp_tool("get_orders", {"buyer_id": buyer_id, "buyer_phone": buyer_phone, "status": status, "limit": limit})

    async def mcp_update_stock_with_movement(self, farm_id: str, item_name: str, quantity_change: float, reason: str) -> Any:
        return await self._call_mcp_tool("update_stock_with_movement", {"farm_id": farm_id, "item_name": item_name, "quantity_change": quantity_change, "reason": reason})

    async def mcp_persist_conversation(self, user_id: str, query_json: str, response_json: str, agent_type: str = "MarketCoach") -> Any:
        return await self._call_mcp_tool("persist_conversation", {"user_id": user_id, "query_json": query_json, "response_json": response_json, "agent_type": agent_type})

    async def mcp_register_surplus_offer(self, user_id: str, commodity: str, quantity: float, location: str | None = None) -> Any:
        body = {"user_id": user_id, "commodity": commodity, "quantity": quantity, "location": location}
        return await self._call_mcp_tool("register_surplus_offer", body)

    async def mcp_get_farm_stocks(self, farm_id: str) -> Any:
        return await self._call_mcp_tool("get_farm_stocks", {"farm_id": farm_id})

    async def mcp_list_products(self, producer_id: str) -> Any:
        return await self._call_mcp_tool("list_products", {"producer_id": producer_id})

    async def mcp_get_user_context_state(self, user_id: str) -> Any:
        return await self._call_mcp_tool("get_user_context_state", {"user_id": user_id})

    async def mcp_upsert_user_context_state(self, user_id: str, last_intent: str = "", pending_intent: str = "", draft_data: Dict[str, Any] | None = None) -> Any:
        body = {
            "user_id": user_id,
            "last_intent": last_intent,
            "pending_intent": pending_intent,
            "draft_data_json": json.dumps(draft_data or {}, ensure_ascii=False),
        }
        return await self._call_mcp_tool("upsert_user_context_state", body)

    async def mcp_list_products_human(self, producer_id: str) -> str:
        """Return a human-friendly textual summary of the producer's products.

        Normalises possible MCP responses and builds a readable bullet list.
        """
        raw = await self.mcp_list_products(producer_id)

        # Normalize raw response shape
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
                # Fallback: try to extract 'data' or treat as single product
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
                code = p.get("short_code") or p.get("id") or ""
                category = p.get("category_label")
                qty = p.get("quantity_for_sale") or p.get("quantity") or 0
                unit = p.get("unit") or "unités"
                price = p.get("price")
                price_str = f" — {price} /{unit}" if price is not None else ""
                cat_str = f" [{category}]" if category else ""
                code_str = f" ({code})" if code else ""
                lines.append(f"- {name}{code_str}{cat_str}: {qty} {unit}{price_str}")
            except Exception:
                lines.append("- (produit invalide)")

        return "\n".join(lines)

    async def _handle_user_stock_retrieval(self, state: MarketAgentState, product: str) -> Dict[str, Any]:
        """Delegate stock retrieval to MarketTools."""
        return await self.tools.handle_user_stock_retrieval(state, product)

    def _handle_market_data_retrieval(self, product: Optional[str]) -> Dict[str, Any]:
        """Delegate market data retrieval to MarketTools."""
        return self.tools.handle_market_data_retrieval(product)

    def fetch_data_node(self, state: MarketAgentState) -> Dict[str, Any]:
        """Executes transactions or fetches market data via MCP/Tools.

        Handles both async MCP calls and sync local-tool calls by running
        async helpers in the current or a new event loop.
        """
        status = state.get("status")

        if status in ["SCAM_DETECTED", "WAITING_CONFIRMATION", "MISSING_INFO", "CANCELLED", "ERROR"]:
            return {}

        updates: Dict[str, Any] = {}

        # If user confirmed a transaction, execute it
        if status == "CONFIRMED" and state.get("transaction_payload"):
            data = self._run_async(self._handle_transaction_execution(state["transaction_payload"]))
            updates["status"] = "COMPLETED_TRANSACTION"
            updates["waiting_for_confirmation"] = False
        else:
            # Special-case: user asked to list their products -> fetch via MCP
            intent = (state.get("intent") or "").upper()
            if intent in ("LISTE_PRODUITS", "LIST_PRODUCTS", "SHOW_PRODUCTS"):
                profile = state.get("user_profile", {}) or {}
                producer_id = profile.get("user_id") or profile.get("id") or PRODUCER_ID
                try:
                    human = self._run_async(self.mcp_list_products_human(producer_id))
                    data = {"products_human": human}
                except Exception as exc:
                    logger.warning("Failed to fetch human product list in fetch_data_node: %s", exc)
                    data = {}
                updates["status"] = "DATA_FETCHED"
            else:
                product = state.get("product")
                data = self._run_async(self.tools.handle_user_stock_retrieval(state, product)) if product else {}
                data.update(self.tools.handle_market_data_retrieval(product))
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
    # Unit and location helpers delegated to MarketTools

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
        # Default to kg when unit is missing to avoid over-scaling quantity.
        unit = state.get("unit_mentioned", "kg")

        if qty in (None, ""):
            missing.append("quantité")
            return updates, errors, missing

        factor = self.tools.unit_factor(unit)
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

        warnings = self.tools.location_warnings(loc)
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

        def _with_audit(response: str, out_status: Optional[str] = None) -> Dict[str, Any]:
            out: Dict[str, Any] = {"final_response": response}
            if out_status:
                out["status"] = out_status
            try:
                if self.db_host:
                    profile = state.get("user_profile", {}) or {}
                    user_id = profile.get("user_id") or profile.get("id") or profile.get("phone") or "anon_user"
                    query_json = json.dumps(
                        {
                            "user_query": state.get("user_query"),
                            "intent": state.get("intent"),
                            "transaction_payload": state.get("transaction_payload"),
                            "status": state.get("status"),
                        },
                        ensure_ascii=False,
                    )
                    response_json = json.dumps(
                        {
                            "final_response": response,
                            "status": out.get("status", state.get("status")),
                        },
                        ensure_ascii=False,
                    )
                    # Prefer synchronous MCP helper when in sync contexts to avoid nested event loops
                    try:
                        if hasattr(self.db_host, "call_tool_sync") and callable(getattr(self.db_host, "call_tool_sync")):
                            try:
                                self.db_host.call_tool_sync("persist_conversation", {"user_id": user_id, "query_json": query_json, "response_json": response_json, "agent_type": "MarketCoach"})
                            except Exception:
                                pass
                        else:
                            # If an event loop is running, schedule background persistence
                            try:
                                loop = asyncio.get_running_loop()
                                loop.create_task(self.mcp_persist_conversation(user_id, query_json, response_json, agent_type="MarketCoach"))
                            except RuntimeError:
                                # No running loop; skip persistence to avoid blocking
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            return out
        
        if status == "SCAM_DETECTED":
            return _with_audit(self._build_scam_alert(state))

        if status == "MISSING_INFO":
            missing = state.get("missing_fields", []) or []
            # Ask a short closed question for the most important missing field
            if "lieu" in missing or "location" in missing:
                return _with_audit("Où se trouve le stock (ville/centre) ?")
            if "quantité" in missing or "quantity" in missing:
                return _with_audit("Quelle est la quantité exacte (ex: 30) ?")
            if "prix" in missing or "price" in missing:
                return _with_audit("Quel est le prix unitaire en FCFA ?")
            # Fallback generic prompt
            missing_str = ", ".join(missing)
            return _with_audit(f"Pour finaliser, j'ai besoin de : {missing_str}. Pouvez-vous préciser ?")

        if status == "WAITING_CONFIRMATION":
            payload = state.get("transaction_payload", {})
            prod = payload.get("product") or state.get("product")
            qty = payload.get("quantity")
            loc = payload.get("location") or state.get("location") or "(localisation non précisée)"
            price = payload.get("price") or state.get("price_mentioned") or "Non précisé"
            response = (
                f"Le prix actuel est {price} FCFA.\n"
                f"Je peux enregistrer immédiatement {qty} kg de {prod} à {loc} pour vous. Voulez‑vous que je l'enregistre maintenant ? (Oui/Non)"
            )
            return _with_audit(response, "WAITING_CONFIRMATION")

        if status == "CANCELLED":
            return _with_audit("❌ Opération annulée.")

        if status == "COMPLETED_TRANSACTION":
            data = state.get("market_data", {}) or {}
            reg_status = data.get("registration_status") or data.get("status")
            mcp_res = data.get("mcp_result") or {}
            product_mgmt = data.get("product_management") or {}
            if reg_status and str(reg_status).upper().startswith("SUCCESS"):
                # Try to extract an id from MCP result
                offer_id = None
                if isinstance(mcp_res, dict):
                    offer_id = mcp_res.get("id") or mcp_res.get("offer_id") or mcp_res.get("surplus_id")
                payload = state.get("transaction_payload", {}) or {}
                msg = (
                    "✅ Offre enregistrée avec succès. "
                    f"Détails: produit={payload.get('product')}, quantité={payload.get('quantity')} kg, "
                    f"lieu={payload.get('location')}, prix={payload.get('price', 'Non précisé')} FCFA."
                )
                if offer_id:
                    msg += f" ID: {offer_id}."
                if isinstance(product_mgmt, dict) and product_mgmt.get("created"):
                    msg += " Le produit n'existait pas encore: je l'ai créé automatiquement dans votre catalogue."
                return _with_audit(msg, "COMPLETED")
            # If not successful, return details
            return _with_audit(f"Enregistrement échoué: {json.dumps(data, ensure_ascii=False)}", "COMPLETED")

        # Default / Completed
        # Special-case: user explicitly asked to list products
        intent = state.get("intent", "") or ""
        if str(intent).upper() in ("LISTE_PRODUITS", "LIST_PRODUCTS", "SHOW_PRODUCTS"):
            # Attempt to fetch producer id from profile
            profile = state.get("user_profile", {}) or {}
            producer_id = profile.get("user_id") or profile.get("id") or PRODUCER_ID
            # Prefer already-fetched human-readable list from fetch_data_node
            market = state.get("market_data", {}) or {}
            human_list = market.get("products_human")
            if human_list:
                return _with_audit(human_list, "COMPLETED")
            try:
                # mcp_list_products_human is async; run via bridge
                human_list = self._run_async(self.mcp_list_products_human(producer_id))
                return _with_audit(human_list, "COMPLETED")
            except Exception as exc:
                logger.warning("Failed to build human product list: %s", exc)
                return _with_audit("Désolé, impossible de récupérer vos produits pour le moment.", "COMPLETED")

        response = self._generate_market_response(state)
        return _with_audit(response, "COMPLETED")

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
                # Use the in-process MCP server adapter so the agent talks
                # to the MCP Shield (security, audit, permissions) instead
                # of calling the DB service directly.
                from agriconnect.protocols.mcp.infrastructure import AgriDBMCPServer, runtime

                class RealMCPAdapter:
                    def __init__(self):
                        # Instantiate the server-side proxy which resolves tools
                        # via the MCP tools registry and runtime.db.
                        self._server = AgriDBMCPServer()
                        # Ensure runtime (DB) is initialized for the MCP server.
                        try:
                            # Use the synchronous startup helper to initialise the
                            # DB/runtime when called from a non-async entrypoint.
                            runtime.start()
                        except Exception:
                            # Best-effort only; tool calls will fail if DB not ready
                            pass

                    async def call_tool(self, name: str, args: dict):
                        # Delegate to AgriDBMCPServer which applies preflight
                        # security checks and performs audit before executing
                        # the underlying DB service functions.
                        return await self._server.call_tool(name, args or {})

                mcp_client = RealMCPAdapter()
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

        # Run validation asynchronously
        validate_updates = await coach.validate_node(test_state)
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
            products = await coach.mcp_list_products(PRODUCER_ID)
            print(f"Products for {PRODUCER_ID}: {products}")
        except Exception as e:
            print("Failed to list products via MCP:", e)

    import asyncio

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass