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
from agriconnect.protocols.mcp.servers.agri_db_server import AgriDBMCPServer

logger = logging.getLogger("Agent.MarketCoach")

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

class MarketCoach:
    def __init__(self, llm_client=None):
        self.model_planner = "llama-3.1-8b-instant"
        self.model_answer = "llama-3.3-70b-versatile"
        self.tool = AgrimarketTool()
        
        # Initialize MCP Database Server
        try:
            self.db_server = AgriDBMCPServer()
        except Exception as e:
            logger.error("Failed to init MCP DB Server: %s", e)
            self.db_server = None

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
        if intent not in ["REGISTER_SURPLUS", "SELL", "BUY_OFFER"]:
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
            user_id = state.get("user_profile", {}).get("phone", "anon_user")
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

    def _handle_transaction_execution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute transaction and return data fragment."""
        data = {}
        success = self.tool.register_surplus_offer(
            payload["product"], 
            payload["quantity"],
            payload["location"]
        )
        
        data["registration_status"] = "SUCCESS" if success else "OFFLINE_SAVED"
        if payload.get("location"):
            data["logistics"] = self.tool.get_logistics_info(payload["location"])
        
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
        return [s.dict() if hasattr(s, "dict") else s for s in stocks]

    async def _handle_user_stock_retrieval(self, state: MarketAgentState, product: str) -> Dict[str, Any]:
        """Retrieve user stock from MCP server."""
        data = {}
        if not self.db_server or not state.get("user_profile"):
            return data
        
        uid = self._get_user_id(state)
        if not uid:
            return data
        
        try:
            stocks = await self.db_server.call_tool("get_farm_stocks", arguments={"farm_id": uid})
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
        """Executes transactions or fetches market data via MCP/Tools."""
        status = state.get("status")
        
        if status in ["SCAM_DETECTED", "WAITING_CONFIRMATION", "MISSING_INFO", "CANCELLED", "ERROR"]:
            return {}

        updates = {}

        if status == "CONFIRMED" and state.get("transaction_payload"):
            data = self._handle_transaction_execution(state["transaction_payload"])
            updates["status"] = "COMPLETED_TRANSACTION"
        else:
            product = state.get("product")
            data = self._handle_user_stock_retrieval(state, product) if product else {}
            data.update(self._handle_market_data_retrieval(product))
            updates["status"] = "DATA_FETCHED"
        
        updates["market_data"] = data
        return updates

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

    def _execute_transaction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Perform registration and return market_data fragment."""
        result: Dict[str, Any] = {}
        success = False
        try:
            success = self.tool.register_surplus_offer(
                payload.get("product"),
                payload.get("quantity"),
                payload.get("location"),
            )
        except Exception:
            success = False

        result["registration_status"] = "SUCCESS" if success else "OFFLINE_SAVED"
        if payload.get("location"):
            try:
                result["logistics"] = self.tool.get_logistics_info(payload.get("location"))
            except Exception:
                result["logistics"] = {}
        return result

    def _retrieve_product_market(self, product: Optional[str]) -> Dict[str, Any]:
        """Fetch market data for a product using the tool shim."""
        data: Dict[str, Any] = {}
        if not product:
            return data
        try:
            prices = self.tool.get_commodity_price(product)
            if prices:
                data["prices"] = prices
        except Exception:
            data["prices"] = []
        try:
            data["trends"] = self.tool.analyze_market_trends(product)
        except Exception:
            data["trends"] = {}
        return data

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

        workflow.set_entry_point("transcribe")
        workflow.add_edge("transcribe", "analyze")
        

        workflow.add_conditional_edges("analyze", self.route_analysis)
        
        
            
        workflow.add_conditional_edges("validate", self.route_validation)
        
        workflow.add_edge("fetch_data", "compose")
        workflow.add_edge("compose", END)
        
        return workflow.compile(checkpointer=checkpointer)

if __name__ == "__main__":
    # Persistence setup (In-Memory for testing, replace with Postgres/Sqlite for Prod)
    memory = MemorySaver()
    coach = MarketCoach()
    app = coach.build(checkpointer=memory)

    # Simulating a conversation thread
    thread_id = "user_123_phone_number"
    config = {"configurable": {"thread_id": thread_id}}

    print("🚀 Démarrage AgriConnect Market Coach (Persistent Mode)...\n")

    # Step 1: Initial Request
    print("--- User: J'ai 50 sacs de maïs à vendre à Nouna ---")
    initial_state = {
        "user_query": "J'ai 50 sacs de maïs à vendre à Nouna",
        "user_profile": {"niveau": "débutant"},
    }
    result = app.invoke(initial_state, config=config)
    print(f"Agent: {result.get('final_response')}\n")

    # Step 2: User Confirms (Persistence Check)
    print("--- User: Oui, c'est bon ---")
    follow_up_state = {
        "user_query": "Oui, c'est bon",
        # We don't need to resend profile/intent, memory handles it
    }
    result = app.invoke(follow_up_state, config=config)
    print(f"Agent: {result.get('final_response')}\n")
    print(f"Status Final: {result.get('status')}")