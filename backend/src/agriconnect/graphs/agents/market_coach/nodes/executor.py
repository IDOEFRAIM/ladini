from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from agriconnect.core.logger import get_logger

logger = get_logger("AgriConnect.MarketCoach.Executor")

from agriconnect.graphs.agents.market_coach.registry import (
    get_action,
    load_all_actions,
)
from agriconnect.graphs.agents.market_coach.actions.tool_provider import MCPToolProvider, ToolProvider
from agriconnect.workspace.context_guard import ContextGuard
from agriconnect.agents.task_handler import TaskHandler, GoalState
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    get_required_fields,
    intent_requires_farm,
)
from agriconnect.graphs.agents.market_coach.flows.producer.farm_logic import ensure_farm_node
from agriconnect.graphs.agents.market_coach.core.base import FARM_CRITICAL_GOALS, _AUTO_FARM_NOTICE
from agriconnect.graphs.agents.market_coach.core.slots import resolve_canonical
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)


from agriconnect.graphs.agents.market_coach.services.mcp.schema_resolver import (
    get_tool_schema as _get_mcp_tool_schema,
    build_resolved_tool_args as _build_resolved_tool_args,
    MissingRequiredMCPArgs,
    sanitize_mcp_args as _sanitize_mcp_args,
    mask_pii_args as _mask_pii_args,
    ascii_fold_value as _ascii_fold_value,
)
from agriconnect.graphs.agents.market_coach.services.mcp.error_translation import (
    GENERIC_TECHNICAL_ERROR as _GENERIC_TECHNICAL_ERROR,
    translate_mcp_error as _translate_mcp_error,
)
from agriconnect.graphs.agents.market_coach.services.mcp.post_success import (
    post_success_suggestion as _post_success_suggestion,
)

load_all_actions()


def _now() -> float:
    return time.time()


# =====================================================================
# FARM AUTO-PROVISIONING
# =====================================================================

async def _auto_provision_farm_if_needed(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    payload: Dict[str, Any],
    tool_name: str,
    tool_schema: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    goal = (state.get("current_goal") or "").upper()
    if goal not in FARM_CRITICAL_GOALS:
        return payload, {}

    farm_required = "farm_id" in (tool_schema.get("required") or [])
    farm_missing = payload.get("farm_id") in (None, "", [], {})
    if not farm_required or not farm_missing:
        return payload, {}

    updates = await ensure_farm_node(state, mc_runtime) or {}

    if "transaction_payload" in updates:
        updated_payload = dict(updates["transaction_payload"])
    else:
        updated_payload = dict(payload)
        stable_farm = state.get("stable_entities", {}).get("farm_id")
        if stable_farm:
            updated_payload["farm_id"] = stable_farm

    status = str(updates.get("status") or "").upper()

    # FILET ULTIME : `ensure_farm_node` peut renvoyer {} (garde `farm_creation_attempted`
    # déjà posée par son passage en nœud du graphe) ou échouer sans produire de
    # farm_id. Plutôt que de laisser l'exécuteur réclamer un `farm_id` (UUID) à
    # l'utilisateur, on garantit une ferme via `get_or_create_farm` (idempotent,
    # crée producteur+ferme si absent). On NE le fait PAS si ensure_farm_node
    # demande explicitement une sélection multi-fermes (WAITING_INPUT).
    if status != "WAITING_INPUT" and updated_payload.get("farm_id") in (None, "", [], {}):
        phone = state.get("user_phone") or payload.get("phone")
        if phone:
            try:
                from agriconnect.graphs.agents.market_coach.services.mcp.gateway import FarmGateway
                farm_data = await FarmGateway(mc_runtime).get_or_create_farm(phone=str(phone).strip())
                fid = str((farm_data or {}).get("id") or (farm_data or {}).get("farm_id") or "")
                if fid:
                    updated_payload["farm_id"] = fid
                    updates = dict(updates)
                    updates["transaction_payload"] = updated_payload
                    updates.setdefault("auto_farm_notice", _AUTO_FARM_NOTICE)
                    logger.info("[Executor] farm garanti via get_or_create_farm: %s", fid)
            except Exception as exc:
                logger.warning("[Executor] get_or_create_farm (filet ultime) a échoué: %s", exc)

    if not updates:
        return payload, {}

    if status != "WAITING_INPUT":
        updates.setdefault("available_mapping", {})
        wm_patch = dict(updates.get("working_memory") or {})
        wm_patch.setdefault("available_mapping_kind", None)
        updates["working_memory"] = wm_patch

    return updated_payload, updates


# =====================================================================
# TASK HANDLER ADAPTERS
# =====================================================================

class _RuntimeStorageAdapter:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def ensure_profile(self, phone: str) -> None:
        phone = str(phone or "").strip()
        if not phone:
            raise ValueError("phone requis pour la validation")
        from agriconnect.graphs.agents.market_coach.services.mcp.gateway import ProfileGateway
        await ProfileGateway(self.runtime).identify_or_create_user(phone)


class _NoopExecutor:
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload


def _extract_float(*values: Any) -> Optional[float]:
    for value in values:
        if value is None:
            continue
        try:
            clean = str(value).strip().replace(",", ".")
            if not clean:
                continue
            return float(clean)
        except (TypeError, ValueError):
            continue
    return None


def _build_task_payload(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = dict(state.get("transaction_payload") or {})
    phone = state.get("user_phone") or payload.get("producer_id") or payload.get("phone")
    goal = (state.get("current_goal") or payload.get("goal") or "").upper()
    if not phone or not goal:
        return None

    price = _extract_float(
        payload.get("price"),
        state.get("price"),
    )
    quantity = _extract_float(
        payload.get("quantity_for_sale"),
        payload.get("quantity"),
        state.get("quantity"),
    )
    unit = (
        payload.get("unit")
        or state.get("unit")
        or "KG"
    )

    required_fields = get_required_fields(goal)

    context_payload = dict(payload)
    context_payload.setdefault("transaction_payload", dict(payload))
    context_payload.setdefault("current_goal", goal)

    return {
        "phone": str(phone),
        "goal": goal,
        "price": price,
        "quantity": quantity,
        "unit": unit,
        "required_fields": required_fields,
        "context": context_payload,
    }


# =====================================================================
# NODE 9 — MCP TOOL EXECUTOR
# =====================================================================

async def mcp_tool_executor(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """MCP executor with self-healing, transient retry, error translation,
    and post-success suggestions.
    """
    state_side_effects: Dict[str, Any] = {}
    payload: Dict[str, Any] = deepcopy(state.get("transaction_payload") or {})

    def _apply_side_effects(result: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(result)
        explicit_keys = set(merged.keys())
        side_effects = dict(state_side_effects)

        if "transaction_payload" not in explicit_keys:
            merged["transaction_payload"] = deepcopy(side_effects.pop("transaction_payload", payload))
        else:
            side_effects.pop("transaction_payload", None)

        for key, value in side_effects.items():
            if key in explicit_keys:
                continue
            merged[key] = value

        status = str(merged.get("status") or "").upper()
        if status != "WAITING_INPUT":
            merged.setdefault("available_mapping", {})
            wm_patch = dict(merged.get("working_memory") or {})
            wm_patch.setdefault("available_mapping_kind", None)
            merged["working_memory"] = wm_patch

        return merged

    allowed, guard_msg = ContextGuard.ensure_agent(state, expected_agent="market")
    if not allowed:
        logger.warning("ContextGuard: tool execution blocked (market)")
        return _apply_side_effects({
            "status": "WAITING_INPUT",
            "response_strategy": "CLARIFICATION",
            "final_response": guard_msg,
            "execution_authorized": False,
            "validation_errors": ["agent_mismatch"],
            "ag_ui_component": None,
        })

    if not state.get("execution_authorized"):
        logger.warning("Executor invoqué sans autorisation — refus d'écriture")
        return _apply_side_effects({
            "status": "ERROR",
            "validation_errors": ["execution_not_authorized"],
            "response_strategy": "ERROR",
            "ag_ui_component": None,
        })

    goal = (state.get("current_goal") or "").upper()
    phone = state.get("user_phone")
    retry_count = int(state.get("retry_count") or 0)

    if not goal:
        logger.error("Aucun goal défini — exécution impossible")
        return _apply_side_effects({
            "status": "ERROR",
            "validation_errors": ["no_goal_defined"],
            "response_strategy": "ERROR",
            "selected_tool": None,
            "selected_tool_args": {},
            "ag_ui_component": None,
        })

    registration = get_action(goal)
    if registration is None:
        raise RuntimeError("Action non découverte. Migration incomplète.")

    _is_read_only = not registration.is_write

    # --- PRE-FLIGHT FARM ENRICHMENT --------------------------------------
    # TaskHandler valide les champs requis AVANT l'appel MCP. Si farm_id est
    # requis mais absent, on déclenche l'auto-provisionnement immédiatement
    # pour éviter de bloquer la FSM en HUMAN_INTERVENTION avant même l'exécution.
    if intent_requires_farm(goal) and not payload.get("farm_id"):
        preflight_payload, preflight_updates = await _auto_provision_farm_if_needed(
            state,
            mc_runtime,
            payload,
            tool_name="preflight_farm",
            tool_schema={"required": ["farm_id"]},
        )
        if preflight_updates:
            state_side_effects.update(preflight_updates)
            payload = dict(preflight_payload)
            waiting = str(preflight_updates.get("status") or "").upper() == "WAITING_INPUT"
            if waiting:
                return _apply_side_effects(preflight_updates)
        else:
            payload = dict(preflight_payload)
        state["transaction_payload"] = dict(payload)

    if not _is_read_only:
        task_payload = _build_task_payload(state)
        if task_payload:
            handler = TaskHandler(
                storage=_RuntimeStorageAdapter(mc_runtime),
                executor=_NoopExecutor(),
            )
            # La pré-étape du TaskHandler touche l'infrastructure
            # (`ensure_profile` -> identify_or_create_user via MCP). Si MCP ou
            # Postgres est injoignable, elle LÈVE — et l'exception traversait le
            # nœud LangGraph, terminant le tour sur un CRASH au lieu d'un état
            # exploitable.
            #
            # On NE court-circuite PAS pour autant : cette étape n'est qu'une
            # VALIDATION préalable. En cas de panne d'infra, on journalise et on
            # poursuit vers l'appel outil principal, qui possède déjà la
            # machinerie complète (détection d'erreur transitoire, retries avec
            # backoff non bloquant, traduction du message utilisateur). Couper
            # ici priverait l'utilisateur de ces retries pour une panne
            # passagère.
            handler_state = None
            try:
                handler_state = await handler.handle(task_payload, retry_seed=retry_count)
            except Exception as exc:
                logger.warning(
                    "[Executor] Pré-validation TaskHandler indisponible pour %s (%s) — "
                    "poursuite vers l'appel outil qui gère retries/traduction.",
                    goal, exc,
                )

            # `handler_state is None` = pré-validation indisponible : on saute
            # ses verdicts et on laisse l'appel outil principal trancher.
            if handler_state is not None and handler_state.goal_state == GoalState.WAITING_INPUT:
                missing = handler_state.metadata.get("missing_fields", [])
                logger.info("[Executor] TaskHandler INCOMPLETE for %s — missing: %s", goal, missing)
                return _apply_side_effects({
                    "status": "WAITING_INPUT",
                    "response_strategy": "ASK_CLARIFICATION",
                    "missing_fields": missing,
                    "validation_errors": [f"missing:{f}" for f in missing],
                    "execution_authorized": False,
                    "ag_ui_component": None,
                })
            if handler_state is not None and handler_state.goal_state == GoalState.ERROR_RECOVERY:
                prompt = handler_state.metadata.get("clarification_prompt") or "Merci de préciser les informations manquantes."
                return _apply_side_effects({
                    "status": "ERROR",
                    "response_strategy": "ASK_CLARIFICATION",
                    "final_response": prompt,
                    "execution_authorized": False,
                    "validation_errors": ["payload_invalid"],
                    "ag_ui_component": None,
                })
            if handler_state is not None and handler_state.goal_state == GoalState.HUMAN_INTERVENTION:
                return _apply_side_effects({
                    "status": "HUMAN_INTERVENTION",
                    "response_strategy": "ESCALATE",
                    "final_response": "Je transmets cette demande à un agent humain.",
                    "execution_authorized": False,
                    "validation_errors": ["manual_takeover"],
                    "ag_ui_component": None,
                })

    dispatch_handler = registration.handler

    try:
        tool_name, tool_args = dispatch_handler(state, payload)
        tool_name = str(tool_name)
        tool_args = dict(tool_args or {})
    except ValueError as ve:
        missing_field = str(ve).replace("Missing required field: ", "").strip()
        logger.info("[SelfHeal] Dispatcher ValueError for %s: missing '%s' — attempting repair", goal, missing_field)

        repair_sources = [
            state.get("stable_entities") or {},
            state.get("working_memory") or {},
            state.get("extracted_entities") or {},
        ]
        repaired_value = None
        for source in repair_sources:
            candidate = source.get(missing_field)
            if candidate not in (None, "", [], {}):
                repaired_value = candidate
                break

        if repaired_value is not None:
            payload[missing_field] = repaired_value
            logger.info("[SelfHeal] Repaired '%s' = %r from state — retrying dispatcher", missing_field, repaired_value)
            try:
                tool_name, tool_args = dispatch_handler(state, payload)
                tool_name = str(tool_name)
                tool_args = dict(tool_args or {})
            except Exception as exc2:
                logger.error("[SelfHeal] Dispatcher still fails after repair: %s", exc2)
                return _apply_side_effects({
                    "status": "ERROR",
                    "validation_errors": [f"dispatcher_error_after_repair: {exc2}"],
                    "response_strategy": "ERROR",
                    "final_response": _GENERIC_TECHNICAL_ERROR,
                    "selected_tool": None,
                    "selected_tool_args": {},
                    "ag_ui_component": None,
                })
        else:
            logger.warning("[SelfHeal] Cannot repair '%s' — routing to ASK_MISSING_FIELD", missing_field)
            return _apply_side_effects({
                "status": "WAITING_INPUT",
                "missing_fields": [missing_field],
                "last_missing_field": missing_field,
                "response_strategy": "ASK_MISSING_FIELD",
                "validation_errors": [],
                "selected_tool": None,
                "selected_tool_args": {},
                "ag_ui_component": None,
            })
    except Exception as exc:
        logger.error("Le dispatcher pour %s a échoué: %s", goal, exc)
        return _apply_side_effects({
            "status": "ERROR",
            "validation_errors": [f"dispatcher_error: {exc}"],
            "response_strategy": "ERROR",
            "final_response": _GENERIC_TECHNICAL_ERROR,
            "selected_tool": None,
            "selected_tool_args": {},
            "ag_ui_component": None,
        })

    # NOTE (refonte double-rôle) : le blocage par "rôle de session" a été
    # retiré ici — tout utilisateur peut déclencher un outil producteur OU
    # acheteur selon le goal classé pour CE tour (voir `core/router.py`
    # `_goal_domain`). La sécurité sur les actions sensibles (paiement, OTP
    # de livraison...) se fait désormais par ownership au niveau de la
    # méthode DB elle-même (vérifie que l'appelant est la partie prenante de
    # LA COMMANDE ciblée), pas par un rôle global figé.

    tool_schema = await _get_mcp_tool_schema(mc_runtime, tool_name)
    payload, farm_updates = await _auto_provision_farm_if_needed(
        state,
        mc_runtime,
        payload,
        tool_name,
        tool_schema,
    )
    if farm_updates:
        state_side_effects.update(farm_updates)
    else:
        state_side_effects.setdefault("transaction_payload", deepcopy(payload))

    try:
        resolved_args = _build_resolved_tool_args(
            tool_name=tool_name,
            schema=tool_schema,
            state=state,
            payload=payload,
            initial_args=tool_args or {},
        )
    except MissingRequiredMCPArgs as exc:
        logger.warning("[Executor] %s", str(exc))

        # MCP arg → slot conversationnel à redemander. Les alias de SLOT
        # (name/product_name → product, quantity_for_sale → quantity…) sont
        # résolus par le registre central (core/slots.py) — ne pas les
        # redupliquer ici. Seule la résolution d'IDENTITÉ (producer_id/user_id
        # → phone), qui n'est PAS un alias de slot, reste un overlay local.
        _MCP_IDENTITY_TO_SLOT = {"producer_id": "phone", "user_id": "phone"}
        missing_slots = [
            _MCP_IDENTITY_TO_SLOT.get(m) or resolve_canonical(m)
            for m in (exc.missing_args or [])
        ]
        missing_slots = [m for m in missing_slots if m]

        first_missing = missing_slots[0] if missing_slots else None
        return _apply_side_effects({
            "status": "WAITING_INPUT",
            "execution_authorized": False,
            "validation_errors": [f"missing_required_args: {', '.join(exc.missing_args)}"],
            "selected_tool": tool_name,
            "selected_tool_args": {},
            "missing_fields": missing_slots,
            "last_missing_field": first_missing,
            "response_strategy": "ASK_MISSING_FIELD",
            "ag_ui_component": None,
        })

    resolved_args = _sanitize_mcp_args(resolved_args)
    resolved_args = _ascii_fold_value(resolved_args)

    logger.info(
        "MCP_EXEC_AUDIT | tool=%s | args=%s",
        tool_name,
        json.dumps(_mask_pii_args(resolved_args), default=str, ensure_ascii=False),
    )
    history = list(state.get("tool_execution_history") or [])

    provider: ToolProvider = MCPToolProvider(runtime=mc_runtime)

    _MCP_MAX_TRANSIENT_RETRIES = 2
    _TRANSIENT_MARKERS = ("timeout", "connection", "unavailable", "temporary", "503", "502")
    last_exc: Optional[Exception] = None

    for attempt in range(1, _MCP_MAX_TRANSIENT_RETRIES + 1):
        try:
            result = await provider.execute(tool_name, resolved_args)
            success = is_success_response(result)

            history.append({
                "tool": tool_name,
                "args": resolved_args,
                "success": success,
                "raw": result,
                "ts": _now(),
                "attempt": attempt,
            })

            if not success:
                err_msg = result.get("message") or result.get("error") or "Transaction rejetée par le système"
                logger.warning("Tool %s rejeté par le MCP (tentative %d): %s", tool_name, attempt, err_msg)
                user_msg = _translate_mcp_error(str(err_msg))
                return _apply_side_effects({
                    "status": "ERROR",
                    "execution_result": result,
                    "selected_tool": tool_name,
                    "selected_tool_args": resolved_args,
                    "tool_execution_history": history,
                    "retry_count": retry_count,
                    "validation_errors": [str(err_msg)],
                    "response_strategy": "ERROR",
                    "final_response": user_msg,
                    "ag_ui_component": None,
                })

            proactive = _post_success_suggestion(goal, payload)

            success_state: Dict[str, Any] = {
                "status": "COMPLETED",
                "goal_status": "COMPLETED",
                "execution_result": result,
                "selected_tool": tool_name,
                "selected_tool_args": resolved_args,
                "tool_execution_history": history,
                "retry_count": retry_count,
                "is_certified": False,
                "execution_authorized": False,
                "waiting_for_confirmation": False,
                "transaction_payload": {},
                "missing_fields": [],
                "completed_fields": [],
                "expected_input": "NONE",
                "current_goal": None,
                "response_strategy": "SUCCESS",
                "proactive_hint": proactive,
                "ag_ui_component": None,
                "final_response": None,
                "active_form": None,
                "form_data": {},
                "form_step": None,
                "confirmation_summary": None,
                "available_mapping": {},
            }

            wm_reset = dict(state.get("working_memory") or {})
            for key in (
                "active_goal",
                "locked_intent",
                "recent_corrections",
                "buyer_request_waiting_choice",
                "buyer_request_catalog_checked",
                "buyer_request_last_product",
                "vendor_selection_context",
                "auction_menu",
                "bids_menu",
                "generic_menu",
            ):
                wm_reset[key] = None
            success_state["working_memory"] = wm_reset

            if isinstance(result.get("mapping"), dict):
                success_state["available_mapping"] = result["mapping"]
                kind = "auction" if "auction" in tool_name else "bid"
                wm_next = dict(success_state.get("working_memory") or {})
                wm_next["available_mapping_kind"] = kind
                success_state["working_memory"] = wm_next

            return _apply_side_effects(success_state)

        except Exception as exc:
            last_exc = exc
            exc_lower = str(exc).lower()
            is_transient = any(m in exc_lower for m in _TRANSIENT_MARKERS)
            history.append({
                "tool": tool_name,
                "args": resolved_args,
                "success": False,
                "error": str(exc),
                "ts": _now(),
                "attempt": attempt,
                "transient": is_transient,
            })
            if is_transient and attempt < _MCP_MAX_TRANSIENT_RETRIES:
                logger.warning(
                    "[Executor] Transient error on %s (attempt %d/%d): %s — retrying",
                    tool_name, attempt, _MCP_MAX_TRANSIENT_RETRIES, exc,
                )
                await asyncio.sleep(0.5 * attempt)
                continue
            break

    logger.exception("Le call MCP %s a crashé après %d tentatives: %s", tool_name, _MCP_MAX_TRANSIENT_RETRIES, last_exc)
    return _apply_side_effects({
        "status": "ERROR",
        "selected_tool": tool_name,
        "selected_tool_args": resolved_args,
        "tool_execution_history": history,
        "retry_count": retry_count + 1,
        "validation_errors": [f"mcp_crash: {last_exc}"],
        "execution_authorized": False,
        "response_strategy": "ERROR",
        "final_response": _translate_mcp_error(str(last_exc)),
        "ag_ui_component": None,
    })
