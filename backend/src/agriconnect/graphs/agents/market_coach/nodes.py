"""MarketCoach nodes as pure functions with a runtime builder."""

import asyncio
import json
import logging
import concurrent.futures
from functools import partial
from typing import Any, Dict, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from .prompts import MARKET_SYSTEM_PROMPT_TEMPLATE, MARKET_USER_PROMPT_TEMPLATE
from .state import MarketAgentState
from agriconnect.services.market.repository import MarketRepository
from agriconnect.services.market.intent import IntentService
from agriconnect.services.market.validation import ValidationService
from agriconnect.services.market.transaction import TransactionService
from agriconnect.services.market.security import SecurityService
from agriconnect.tools.market import AgrimarketTool

logger = logging.getLogger("Agent.MarketCoach")

USER_ID = "e7b2dc50-f3a3-462c-8f7b-178f34f1a005"
PRODUCER_ID = "716c76d6-e4a1-445f-b8ce-35a5c5274ddf"


class MarketRuntime:
	repo: MarketRepository
	intent_svc: IntentService
	validator: ValidationService
	tx_svc: TransactionService
	security: SecurityService
	_tool: AgrimarketTool
	llm: Any
	model_answer: str = "llama-3.3-70b-versatile"
	_persist_tasks: set

	def __init__(self, llm_client=None, mcp_session=None):
		self.repo = MarketRepository(mcp_session)
		self.intent_svc = IntentService(llm_client)
		self.validator = ValidationService()
		self.tx_svc = TransactionService()
		self.security = SecurityService(llm_client)
		self._persist_tasks = set()
		self.llm = llm_client
		self.tool = AgrimarketTool()


def build_runtime(llm_client=None, mcp_session=None) -> MarketRuntime:
	return MarketRuntime(llm_client=llm_client, mcp_session=mcp_session)


def _resolve_user_id(state: MarketAgentState) -> str:
	profile = state.get("user_profile", {}) or {}
	return str(profile.get("user_id") or "anon_user")


async def analyze_node(state: MarketAgentState, runtime: MarketRuntime) -> Dict[str, Any]:
	query = state.get("user_query", "")
	sec_result = runtime.security.moderate_content(query)
	if sec_result.get("is_scam"):
		return {
			"status": "SCAM_DETECTED",
			"security_status": "SCAM_DETECTED",
			"security_reason": sec_result.get("reason"),
		}

	analysis = runtime.intent_svc.analyze(query)
	return {
		"intent": analysis.get("intent"),
		"product": analysis.get("product"),
		"location": analysis.get("location"),
		"price_mentioned": analysis.get("price"),
		"quantity_mentioned": analysis.get("quantity"),
		"unit_mentioned": analysis.get("unit"),
		"status": "ANALYZED",
		"security_status": "SAFE",
	}


async def validate_node(state: MarketAgentState, runtime: MarketRuntime) -> Dict[str, Any]:
	status = state.get("status")
	if status in ["SCAM_DETECTED", "ERROR"]:
		return {}

	updates = runtime.validator.validate_request(state)
	intent = state.get("intent")
	has_errors = bool(updates.get("validation_errors"))
	is_missing = bool(updates.get("missing_fields"))

	if not has_errors and not is_missing and intent in ["REGISTER_SURPLUS", "CREATE_PRODUCT"]:
		payload = runtime.tx_svc.construct_payload(intent, state, updates)
		proposal = runtime.tx_svc.create_proposal(payload)
		tx_hash = runtime.tx_svc.build_transaction_hash(payload)
		updates.update(
			{
				"transaction_payload": payload,
				"proposed_action": proposal,
				"transaction_hash": tx_hash,
				"status": "PROPOSAL_READY",
				"waiting_for_confirmation": False,
				"pending_user_intent": "",
				"draft_data": {},
			}
		)
	elif is_missing:
		updates["status"] = "MISSING_INFO"
		updates["pending_user_intent"] = intent
		updates["draft_data"] = {
			"product": state.get("product"),
			"quantity": state.get("quantity_mentioned"),
			"location": state.get("location"),
			"price": state.get("price_mentioned"),
			"unit": state.get("unit_mentioned"),
		}

	return updates


async def fetch_data_node(state: MarketAgentState, runtime: MarketRuntime) -> Dict[str, Any]:
	status = state.get("status")
	intent = state.get("intent")

	if status == "CONFIRMED" and state.get("transaction_payload"):
		payload = state.get("transaction_payload")
		success, result = await runtime.repo.execute_transaction(payload.get("action_type"), payload)
		if success:
			state.update({"status": "COMPLETED_TRANSACTION", "market_data": result})
			return {"status": "COMPLETED_TRANSACTION", "market_data": result}
		return {"status": "ERROR", "market_data": {"error": result.get("error")}}

	if intent in ["CHECK_PRICE", "LIST_PRODUCTS", "LISTE_PRODUITS", "SHOW_PRODUCTS"]:
		data = {}
		up_intent = str(intent).upper()
		if up_intent in ("LISTE_PRODUITS", "LIST_PRODUCTS", "SHOW_PRODUCTS"):
			pid = state.get("user_profile", {}).get("user_id")
			human_list = await runtime.repo.list_products_human(pid) if pid else "Identifiant producteur manquant pour lister les produits."
			data = {"products_human": human_list}
		else:
			product = state.get("product")
			if product:
				try:
					data = runtime.tool.get_commodity_price(str(product))
				except Exception:
					data = {}
			else:
				data = {}
		return {"status": "DATA_FETCHED", "market_data": data}

	return {}


async def compose_node(state: MarketAgentState, runtime: MarketRuntime) -> Dict[str, Any]:
	status = state.get("status")
	context = _build_response_context(state)

	try:
		if status == "SCAM_DETECTED":
			response = runtime.security.build_alert_message(state.get("security_reason", ""))
		elif runtime.llm and status not in {"PROPOSAL_READY", "MISSING_INFO", "COMPLETED_TRANSACTION"}:
			response = await _generate_llm_response(state, runtime, context)
		else:
			response = _format_fallback_response(status, context)
	except Exception:
		logger.exception("Erreur lors de la génération de la réponse")
		response = "Désolé, j'ai rencontré une erreur technique. Pouvez-vous reformuler ?"

	_persist_non_blocking(runtime, state, response)
	return {"final_response": response}


def _build_response_context(state: MarketAgentState) -> Dict[str, Any]:
	details = {
		"produit (maïs, sorgho...)": "le produit (ex: riz)",
		"quantité": "la quantité (ex: 40 kg)",
		"prix": "le prix par kg (ex: 300 FCFA)",
		"lieu": "le lieu (ex: Gaoua)",
	}
	missing = state.get("missing_fields", []) or []
	wanted = [details.get(m, m) for m in missing]

	known_parts = []
	if state.get("product"):
		known_parts.append(f"produit: {state.get('product')}")
	if state.get("quantity_mentioned") is not None:
		known_parts.append(f"quantité: {state.get('quantity_mentioned')} {state.get('unit_mentioned') or 'kg'}")
	if state.get("price_mentioned") is not None:
		known_parts.append(f"prix: {state.get('price_mentioned')} FCFA")
	if state.get("location"):
		known_parts.append(f"lieu: {state.get('location')}")

	return {
		"intent": state.get("intent"),
		"status": state.get("status"),
		"data": {
			"product": state.get("product"),
			"quantity": state.get("quantity_mentioned"),
			"unit": state.get("unit_mentioned", "kg"),
			"price": state.get("price_mentioned"),
			"location": state.get("location"),
			"missing": missing,
			"wanted": wanted,
			"known_parts": known_parts,
			"market_data": state.get("market_data", {}),
			"proposed_action": state.get("proposed_action"),
			"transaction_payload": state.get("transaction_payload") or {},
		},
	}


async def _safe_persist(runtime: MarketRuntime, state: MarketAgentState, response: str) -> None:
	try:
		uid = state.get("user_profile", {}).get("user_id") or "anon"
		query = state.get("user_query", "")
		await runtime.repo.persist_conversation(uid, query, response, agent_type="MarketCoach")
	except Exception as e:
		logger.warning("Échec de l'audit/persistance : %s", e)


def _persist_non_blocking(runtime: MarketRuntime, state: MarketAgentState, response: str) -> None:
	try:
		task = asyncio.create_task(_safe_persist(runtime, state, response))
		runtime._persist_tasks.add(task)
		task.add_done_callback(runtime._persist_tasks.discard)
	except Exception as e:
		logger.debug("Persistance asynchrone non planifiée: %s", e)


def _format_fallback_response(status: str, context: Dict[str, Any]) -> str:
	data = context.get("data", {})

	if status == "PROPOSAL_READY":
		proposal = data.get("proposed_action")
		if isinstance(proposal, dict):
			return json.dumps(proposal, ensure_ascii=False)
		return json.dumps(data, ensure_ascii=False)

	if status == "MISSING_INFO":
		known_txt = f"J'ai déjà noté {', '.join(data.get('known_parts', []))}. " if data.get("known_parts") else ""
		wanted = data.get("wanted") or data.get("missing") or ["les informations nécessaires"]
		return f"{known_txt}Pour finaliser, il me faut encore : {', '.join(wanted)}."

	if status == "COMPLETED_TRANSACTION":
		payload = data.get("transaction_payload") or {}
		prod = payload.get("product", "produit")
		qty = payload.get("quantity", 0)
		unit = payload.get("unit", "kg")
		return f"✅ Opération réussie. {prod} ({qty} {unit}) a été enregistré."

	if status == "DATA_FETCHED":
		market_data = data.get("market_data") or {}
		if isinstance(market_data, dict) and "products_human" in market_data:
			return market_data["products_human"]
		if not market_data:
			if context.get("intent") == "CHECK_PRICE":
				return "Je peux vérifier le prix du marché, mais il me faut au moins le produit (et idéalement le lieu). Exemple: 'prix du riz à Gaoua'."
			return "Je n'ai pas trouvé de données exploitables pour cette demande. Pouvez-vous reformuler avec produit, quantité, prix et lieu ?"
		return json.dumps(market_data, ensure_ascii=False, indent=2)

	if status == "SCAM_DETECTED":
		return "Demande bloquée pour raison de sécurité."

	return "J'ai bien reçu votre demande, mais il me manque encore du contexte pour finaliser. Pouvez-vous préciser le produit, la quantité, le prix et le lieu ?"


async def _generate_llm_response(state: MarketAgentState, runtime: MarketRuntime, context: Dict[str, Any] | None = None) -> str:
	if not runtime.llm:
		fallback_context = context or _build_response_context(state)
		return _format_fallback_response(state.get("status", ""), fallback_context)

	if state.get("status") != "DATA_FETCHED":
		fallback_context = context or _build_response_context(state)
		return _format_fallback_response(state.get("status", ""), fallback_context)

	ctx = context or _build_response_context(state)
	data = ctx.get("data", {}).get("market_data", {})
	market_data_s = json.dumps(data, ensure_ascii=False)

	system_content = MARKET_SYSTEM_PROMPT_TEMPLATE.format(
		market_data=market_data_s,
		logistics_data="{}",
	)

	try:
		user_q = MARKET_USER_PROMPT_TEMPLATE.format(query=state.get("user_query", ""))
		resp = runtime.llm.chat.completions.create(
			model=runtime.model_answer,
			messages=[
				{"role": "system", "content": system_content},
				{"role": "user", "content": user_q},
			],
			temperature=0.2,
		)
		return resp.choices[0].message.content
	except Exception:
		return "Désolé, service temporairement indisponible."


def _run_async(coro) -> Any:
	try:
		loop = asyncio.get_running_loop()
	except RuntimeError:
		loop = None

	if loop and loop.is_running():
		with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
			try:
				return pool.submit(asyncio.run, coro).result(timeout=30)
			except concurrent.futures.TimeoutError:
				logger.warning("Async helper timed out in _run_async")
				return {}
	return asyncio.run(coro)


def route_analysis(state):
	status = state.get("status")
	if status == "SCAM_DETECTED":
		return "compose"
	if status == "CONFIRMED":
		return "fetch_data"
	return "validate"


def route_validation(state):
	if state.get("status") in ["MISSING_INFO", "PROPOSAL_READY"]:
		return "compose"
	return "fetch_data"


def build(checkpointer=None, llm_client=None, mcp_session=None):
	workflow = StateGraph(MarketAgentState)

	runtime = build_runtime(llm_client=llm_client, mcp_session=mcp_session)

	workflow.add_node("analyze", partial(analyze_node, runtime=runtime))
	workflow.add_node("validate", partial(validate_node, runtime=runtime))
	workflow.add_node("fetch_data", partial(fetch_data_node, runtime=runtime))
	workflow.add_node("compose", partial(compose_node, runtime=runtime))

	workflow.set_entry_point("analyze")
	workflow.add_conditional_edges("analyze", route_analysis)
	workflow.add_conditional_edges("validate", route_validation)
	workflow.add_edge("fetch_data", "compose")
	workflow.add_edge("compose", END)
	return workflow.compile(checkpointer=checkpointer or MemorySaver())


__all__ = ["build", "USER_ID", "PRODUCER_ID", "MarketAgentState"]
