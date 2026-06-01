"""Marketplace v3 sub-graph nodes with local, independent business logic.

This module implements the same behaviour as the old `MarketplaceAgentV3`
class but exposes pure async node functions and a `build_runtime`
factory that returns a compiled LangGraph workflow bound to a runtime.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from functools import partial
from langgraph.graph import END, StateGraph

from .state import MarketplaceState
from agriconnect.tools.marketplace_v3 import MarketplaceToolV3
from agriconnect.tools.intelligence import IntelligenceTool
from agriconnect.services.database.database_service import AgriDatabaseService
from agriconnect.services.persistence import AgriPersister
from agriconnect.infrastructure.mcp.runtime import runtime

logger = logging.getLogger("Agent.Marketplace.v3")

_FINANCIAL_THRESHOLD_FCFA: int = 100_000

INTENTS = [
	"REGISTER_STOCK",
	"SELL_PRODUCT",
	"CHECK_STOCK",
	"CHECK_ORDERS",
	"UPDATE_STOCK",
	"REMOVE_STOCK",
	"FIND_BUYERS",
	"FIND_PRODUCTS",
	"CREATE_ORDER",
	"CHECK_AUCTIONS",
	"PLACE_BID",
	"ADD_EXPENSE",
	"CHECK_EXPENSES",
	"DASHBOARD",
	"REGISTER_CROP_CYCLE",
	"CHECK_PRICE",
	"MANAGE_CLIENTS",
	"HELP",
]


@dataclass
class MarketplaceConfig:
	llm_client: Any = None
	db_service: Optional[AgriDatabaseService] = None
	mcp_session: Any = None
	mcp_client: Any = None
	ctx: Any = None
	mcp_shield: Any = None


@dataclass
class MarketplaceRuntime:
	tool: MarketplaceToolV3
	intel: IntelligenceTool
	persister: Optional[AgriPersister]
	llm: Any
	model_planner: str = "llama-3.1-8b-instant"


def build_runtime(config: Optional[MarketplaceConfig] = None, **overrides: Any) -> MarketplaceRuntime:
	cfg = config or MarketplaceConfig()
	for key, value in overrides.items():
		if hasattr(cfg, key):
			setattr(cfg, key, value)

	mcp_client = cfg.mcp_client or cfg.mcp_session
	svc = None if mcp_client else (cfg.db_service or runtime.db)
	if not mcp_client and not svc:
		svc = AgriDatabaseService()

	if cfg.ctx and hasattr(cfg.ctx, "db"):
		persister = AgriPersister(cfg.ctx.db, getattr(cfg.ctx, "memory", None))
	else:
		persister = None
		logger.warning("Marketplace workflow: running without persistence (Context missing)")

	try:
		from agriconnect.rag.components import get_groq_sdk

		llm = cfg.llm_client if cfg.llm_client else get_groq_sdk()
	except Exception as exc:
		logger.warning("LLM init failed for marketplace: %s", exc)
		llm = None

	return MarketplaceRuntime(
		tool=MarketplaceToolV3(db_service=svc, mcp_client=mcp_client),
		intel=IntelligenceTool(db_service=svc),
		persister=persister,
		llm=llm,
	)


async def identify_user_node(state: MarketplaceState, runtime: MarketplaceRuntime) -> Dict[str, Any]:
	state = dict(state)
	phone = state.get("user_phone", "")
	zone_id = state.get("zone_id")

	if not phone:
		return {"status": "ERROR", "final_response": "NumÃ©ro de tÃ©lÃ©phone requis pour les transactions."}

	try:
		user = await runtime.tool.identify_or_create_user(phone, zone_id=zone_id)
		out = {"user_profile": user, "producer_id": user.get("producer_id")}
		if out.get("producer_id"):
			farm = await runtime.tool.get_or_create_farm(out["producer_id"], zone_id=zone_id)
			out["farm_id"] = farm["id"]
		trust = await runtime.intel.get_trust_score(user["id"])
		out["trust_score"] = trust
		out["status"] = "IDENTIFIED"
		return out
	except Exception as e:
		logger.exception("Identification Critical Failure: %s", e)
		return {"status": "ERROR", "final_response": "Erreur systÃ¨me critique lors de l'identification."}


async def parse_intent_node(state: MarketplaceState, runtime: MarketplaceRuntime) -> Dict[str, Any]:
	if state.get("status") == "ERROR":
		return {}

	query = state.get("user_query", "")
	if not query:
		return {"intent": "HELP"}

	intents_str = "|".join(i for i in INTENTS)
	prompt = (
		"Tu es l'IA Transactionnelle AgriConnect. Analyse la demande.\n"
		f"Query: {query}\n\n"
		"Respond ONLY with valid JSON:\n"
		"{\n"
		f'  "intent": "ONE_OF: {intents_str}",\n'
		'  "product": "normalized product name or null",\n'
		'  "quantity": number or null,\n'
		'  "unit": "kg/sac/tonne" or null,\n'
		'  "price": number or null,\n'
		'  "category": "Cereals/Legumes/Fruits/etc" or null,\n'
		'  "description": "details or null",\n'
		'  "expense_amount": number or null,\n'
		'  "expense_category": "Category or null"\n'
		"}\n"
	)

	try:
		resp = runtime.llm.chat.completions.create(
			model=runtime.model_planner,
			messages=[{"role": "user", "content": prompt}],
			temperature=0,
			response_format={"type": "json_object"},
		)
		parsed = json.loads(resp.choices[0].message.content)
		intent = parsed.get("intent", "HELP")
		if intent not in INTENTS:
			intent = "HELP"
		return {"intent": intent, "parsed": parsed}
	except Exception as e:
		logger.error("Intent Parsing Failed: %s", e)
		return {"intent": "HELP", "errors": [str(e)]}


async def validate_action_node(state: MarketplaceState, runtime: MarketplaceRuntime) -> Dict[str, Any]:
	intent = state.get("intent")
	parsed = state.get("parsed", {})
	warnings = []

	if intent in ("SELL_PRODUCT", "PLACE_BID") and parsed.get("price") and parsed.get("product"):
		try:
			market_price = await runtime.intel.check_market_price(parsed["product"], state.get("zone_id"))
			if market_price:
				pct_diff = abs(parsed["price"] - market_price) / market_price
				if pct_diff > 0.5:
					warnings.append(f"Prix anormalement Ã©loignÃ© du marchÃ© ({market_price} FCFA).")
					return {"validation_warnings": warnings, "requires_human_review": True}
		except Exception as e:
			logger.warning("Price Check Circuit Open: %s", e)
			warnings.append("VÃ©rification prix marchÃ© indisponible.")

	amt = parsed.get("amount") or (parsed.get("price", 0) * parsed.get("quantity", 0))
	if amt > _FINANCIAL_THRESHOLD_FCFA:
		warnings.append(f"Transaction > {_FINANCIAL_THRESHOLD_FCFA:,} FCFA requiert validation.")
		return {"validation_warnings": warnings, "requires_human_review": True}

	return {"validation_warnings": warnings}


async def execute_action_node(state: MarketplaceState, runtime: MarketplaceRuntime) -> Dict[str, Any]:
	intent = state.get("intent")
	user_id = state.get("user_profile", {}).get("id")
	parsed = state.get("parsed", {})

	res = {"success": False, "message": "Non traitÃ©"}
	try:
		if intent == "REGISTER_STOCK":
			res = await runtime.tool.add_stock(
				farm_id=state.get("farm_id"),
				item_name=parsed.get("product"),
				quantity=parsed.get("quantity"),
				unit=parsed.get("unit") or "kg",
			)
		elif intent == "SELL_PRODUCT":
			res = await runtime.tool.create_product(
				producer_id=state.get("producer_id"),
				name=parsed.get("product"),
				quantity_for_sale=parsed.get("quantity"),
				unit=parsed.get("unit") or "kg",
				price=parsed.get("price"),
				category_label=parsed.get("category"),
				description=parsed.get("description"),
			)
			_audit(runtime, "OFFER_CREATED", user_id, parsed)
		elif intent == "ADD_EXPENSE":
			res = await runtime.tool.add_expense(
				farm_id=state.get("farm_id"),
				label=parsed.get("expense_label") or "DÃ©pense",
				amount=parsed.get("expense_amount"),
				category=parsed.get("expense_category"),
			)
			_audit(runtime, "EXPENSE_LOGGED", user_id, parsed)
		elif intent == "CHECK_PRICE":
			price = await runtime.intel.check_market_price(parsed.get("product"), state.get("zone_id"))
			res = {"success": True, "price": price, "product": parsed.get("product")}
		elif intent == "DASHBOARD":
			res = await runtime.tool.get_dashboard(state.get("producer_id"))
		else:
			res = {"success": True, "message": "Commande reÃ§ue (simulation)."}

		return {"action_result": res, "status": ("SUCCESS" if res.get("success") else "FAILURE")} 
	except Exception as e:
		logger.exception("Execution Failed: %s", e)
		return {"status": "ERROR", "errors": [str(e)]}


def _audit(runtime: MarketplaceRuntime, action: str, user_id: str, payload: Dict[str, Any]):
	if runtime.persister:
		try:
			runtime.persister.db.log_audit_action(
				agent_name="MarketplaceAgentV3",
				action_type=action,
				user_id=user_id,
				protocol="TRANSACTION",
				payload=payload,
				resource="market_db",
				confidence=1.0,
			)
		except Exception:
			logger.debug("Audit write failed (ignored)")


async def confirm_node(state: MarketplaceState, runtime: MarketplaceRuntime) -> Dict[str, Any]:
	res = state.get("action_result", {})
	intent = state.get("intent")
	status = state.get("status")

	if status == "SUCCESS":
		if intent == "CHECK_PRICE":
			msg = f"Prix marchÃ© estimÃ© pour {res.get('product')}: {res.get('price')} FCFA/kg."
		elif intent == "SELL_PRODUCT":
			msg = "Offre de vente publiÃ©e avec succÃ¨s."
		elif intent == "ADD_EXPENSE":
			msg = "DÃ©pense enregistrÃ©e."
		else:
			msg = f"OpÃ©ration {intent} rÃ©ussie."
	elif status == "ERROR":
		msg = "Une erreur technique est survenue. Veuillez rÃ©essayer plus tard."
	else:
		msg = "Je n'ai pas pu finaliser votre demande."

	return {"final_response": msg}


async def audit_node(state: MarketplaceState, runtime: MarketplaceRuntime) -> Dict[str, Any]:
	if runtime.persister is None:
		return {}
	if state.get("status") != "SUCCESS":
		return {}

	try:
		user_id = state.get("user_profile", {}).get("user_id", "anonymous")
		runtime.persister.db.log_audit_action(
			agent_name="MarketplaceAgentV3",
			action_type="ACTION_EXECUTED",
			user_id=user_id,
			protocol="TRANSACTION",
			payload={
				"query": state.get("user_query"),
				"intent": state.get("intent"),
				"result": state.get("action_result"),
			},
			resource="market_db",
			confidence=0.9,
		)
	except Exception as exc:
		logger.warning("Audit write failed: %s", exc)

	return {}


def compile_graph(config: Optional[MarketplaceConfig] = None, **overrides: Any):
	runtime = build_runtime(config=config, **overrides)
	workflow = StateGraph(MarketplaceState)
	workflow.add_node("IDENTIFY", partial(identify_user_node, runtime=runtime))
	workflow.add_node("PARSE", partial(parse_intent_node, runtime=runtime))
	workflow.add_node("VALIDATE", partial(validate_action_node, runtime=runtime))
	workflow.add_node("EXECUTE", partial(execute_action_node, runtime=runtime))
	workflow.add_node("CONFIRM", partial(confirm_node, runtime=runtime))
	workflow.add_node("AUDIT", partial(audit_node, runtime=runtime))
	workflow.set_entry_point("IDENTIFY")
	workflow.add_edge("IDENTIFY", "PARSE")
	workflow.add_edge("PARSE", "VALIDATE")
	workflow.add_edge("VALIDATE", "EXECUTE")
	workflow.add_edge("EXECUTE", "CONFIRM")
	workflow.add_edge("CONFIRM", "AUDIT")
	workflow.add_edge("AUDIT", END)
	return workflow.compile()


__all__ = [
	"MarketplaceConfig",
	"MarketplaceRuntime",
	"build_runtime",
	"identify_user_node",
	"parse_intent_node",
	"validate_action_node",
	"execute_action_node",
	"confirm_node",
	"audit_node",
	"MarketplaceState",
	"INTENTS",
	"compile_graph",
]

