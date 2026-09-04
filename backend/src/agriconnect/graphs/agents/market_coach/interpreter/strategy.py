"""Market — Intent router (response_strategy).

Ce module isole la logique d'aiguillage AG-UI (routing) afin d'alléger
`shared_core.py`.

Contrat:
- `response_strategy(state, mc_runtime) -> updates`
- N'émet pas d'appels MCP (pur routing déterministe).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    CART_TUNNEL_KINDS,
    get_pending_interaction,
    to_tunnel_category,
)
from agriconnect.graphs.agents.market_coach.core.slots import SLOT_FILLING_INPUTS

logger = logging.getLogger("AgriConnect.Market.IntentRouter")


async def response_strategy(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Routeur AG-UI agentic — détermine la stratégie de réponse en tenant
    compte de la décision cognitive, de la progression, et du contexte."""
    status = str(state.get("status") or "").upper().strip()
    working = state.get("working_memory") or {}
    current_goal = (
        state.get("current_goal")
        or working.get("active_goal")
        or working.get("locked_intent")
    )
    # (2026-09-02, "no legacy shim") : source UNIQUE — dérivé de
    # `pending_interaction`, plus jamais lu directement depuis l'état. Un
    # seul point de traduction : toutes les comparaisons plus bas
    # (`== "SELECTION"`, `== "CONFIRMATION"`, `in SLOT_FILLING_INPUTS`...)
    # consomment cette même variable dans le même vocabulaire qu'avant.
    expected_input = to_tunnel_category(get_pending_interaction(state))
    interpreted_event = state.get("interpreted_event")
    missing_fields = state.get("missing_fields") or []
    last_missing_field = state.get("last_missing_field")
    existing_strategy = str(state.get("response_strategy") or "").upper().strip()
    if state.get("slot_enrichment_force_clarification"):
        reasons = state.get("clarification_reasons") or []
        logger.warning(
            "[ResponseStrategy] slot enrichment forced clarification | reasons=%s",
            reasons,
        )
        updates: Dict[str, Any] = {
            "response_strategy": "CLARIFICATION",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
            "clarification_reasons": reasons,
        }
        return updates
    if existing_strategy == "ONBOARDING" or state.get("is_onboarding"):
        return {
            "response_strategy": "ONBOARDING",
            "status": "WAITING_INPUT"
            if state.get("is_onboarding")
            else status or "WAITING_INPUT",
            "ag_ui_component": state.get("ag_ui_component"),
        }
    cognitive = state.get("cognitive_decision") or {}
    cognitive_action = cognitive.get("action", "")

    # Priority 1: security + global commands (interrupt/cancel) must beat missing_fields.
    if state.get("security_status") == "SCAM_DETECTED":
        return {
            "response_strategy": "ERROR",
            "status": "BLOCKED",
            "ag_ui_component": None,
        }

    if (
        state.get("interruption_detected")
        or interpreted_event == "INTERRUPTION"
        or existing_strategy == "INTERRUPTION_HANDLER"
    ):
        # If upstream nodes already produced a concrete UI (menu/form), keep it.
        if existing_strategy in {
            "SELECTION_MENU",
            "ASK_MISSING_FIELD",
            "ERROR",
            "CONFIRMATION",
            "SUCCESS",
        }:
            updates: Dict[str, Any] = {"response_strategy": existing_strategy}
            if existing_strategy == "CONFIRMATION":
                updates["status"] = "WAITING_CONFIRMATION"
            if existing_strategy == "ASK_MISSING_FIELD":
                updates["status"] = "WAITING_INPUT"
            return updates
        if expected_input == "SELECTION" and (
            state.get("pending_menu") or state.get("expected_candidates")
        ):
            return {
                "response_strategy": "SELECTION_MENU",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            }
        if expected_input == "CONFIRMATION":
            return {
                "response_strategy": "CONFIRMATION",
                "status": "WAITING_CONFIRMATION",
                "ag_ui_component": None,
            }
        return {"response_strategy": "INTERRUPTION_HANDLER", "ag_ui_component": None}

    # Global explicit cancel/refusal should never be blocked by missing_fields.
    if interpreted_event == "REJECT":
        return {
            "response_strategy": "CLARIFICATION",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
        }

    # (2026-09-02, refonte state canonique — G-1, défense en profondeur) :
    # un tunnel panier (producteur/palier/quantité) ENCORE actif — dérivé à
    # neuf ce tour depuis vendor_selection_context/tier_selection_context,
    # jamais périmable — gagne TOUJOURS sur n'importe quel autre signal, y
    # compris expected_input=="CONFIRMATION" resté périmé d'un tour
    # antérieur. Le fix primaire est au niveau du routeur
    # (core/router.py::_cart_guard, désormais laissé passer vers
    # cart_management dans ce cas précis) ; cette garde protège les chemins
    # qui atteignent quand même response_strategy directement (ex:
    # to_confirmation/context_resolver, hors du guard cart). C'est la
    # correction structurelle du bug réel "celui de 10 l" → "Que voulez-vous
    # confirmer exactement ?".
    pending = get_pending_interaction(state)
    if pending.kind in CART_TUNNEL_KINDS:
        return {
            "response_strategy": "SELECTION_MENU",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
        }

    # --- COGNITIVE GUARD DECISIONS take priority (after global commands) ---
    if cognitive_action == "recover_active_tunnel":
        # Prefer re-showing menus/confirmations instead of verbose coaching.
        if expected_input == "SELECTION" and (
            state.get("pending_menu") or state.get("expected_candidates")
        ):
            return {
                "response_strategy": "SELECTION_MENU",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            }
        if expected_input == "CONFIRMATION":
            return {
                "response_strategy": "CONFIRMATION",
                "status": "WAITING_CONFIRMATION",
                "ag_ui_component": None,
            }
        return {
            "response_strategy": "RECOVERY",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
        }
    if cognitive_action == "abandon_tunnel_max_retries":
        return {"response_strategy": "CLARIFICATION", "ag_ui_component": None}

    # Un partage de position WhatsApp natif n'a pas de texte à classifier —
    # interpreted_event vaut donc TOUJOURS UNKNOWN pour ces tours, y compris
    # quand le resolver vient de conclure avec succès (ex: confirm_preorder_draft/
    # select_winning_bid après le gate GPS — voir
    # [[gps-delivery-burkina-faso-2026-08]]). Sans cette exclusion, ce bloc
    # écrasait un response_strategy=SUCCESS déjà posé par le resolver avec
    # CONFIRMATION/CLARIFICATION à partir d'un expected_input désormais
    # périmé — la commande était confirmée en base mais l'utilisateur
    # recevait "Je n'ai pas bien saisi".
    if interpreted_event in {"UNKNOWN", "OUT_OF_SCOPE"} and not state.get(
        "location_shared"
    ):
        if expected_input == "SELECTION" and state.get("expected_candidates"):
            return {
                "response_strategy": "SELECTION_MENU",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            }
        if expected_input in SLOT_FILLING_INPUTS:
            return {
                "response_strategy": "ASK_MISSING_FIELD",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            }
        if expected_input == "CONFIRMATION":
            return {
                "response_strategy": "CONFIRMATION",
                "status": "WAITING_CONFIRMATION",
                "ag_ui_component": None,
            }
        # Outside any slot-filling context, never fall through to SUCCESS based on a stale
        # previous status/execution_result. UNKNOWN should always clarify.
        return {
            "response_strategy": "CLARIFICATION",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
        }

    # Now it's safe to enforce slot-filling.
    if missing_fields or last_missing_field:
        return {
            "response_strategy": "ASK_MISSING_FIELD",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
        }

    # Respect upstream-set strategy (ex: buyer_flow nodes returning menus).
    if existing_strategy in {
        "SELECTION_MENU",
        "ASK_MISSING_FIELD",
        "ERROR",
        "CONFIRMATION",
        "SUCCESS",
    }:
        updates: Dict[str, Any] = {"response_strategy": existing_strategy}
        if state.get("ag_ui_component") is not None:
            updates["ag_ui_component"] = state.get("ag_ui_component")
        if existing_strategy == "CONFIRMATION":
            updates["status"] = "WAITING_CONFIRMATION"
        if existing_strategy == "ASK_MISSING_FIELD":
            updates["status"] = "WAITING_INPUT"
        return updates

    # Respect upstream-set RECOVERY (e.g. from cognitive_guard)
    if existing_strategy == "RECOVERY":
        return {
            "response_strategy": "RECOVERY",
            "status": "WAITING_INPUT",
            "ag_ui_component": None,
        }

    if status in {"BLOCKED", "ERROR"}:
        return {"response_strategy": "ERROR", "ag_ui_component": None}

    if status == "COMPLETED" or (not current_goal and state.get("execution_result")):
        return {"response_strategy": "SUCCESS", "ag_ui_component": None}

    if status == "WAITING_CONFIRMATION" or expected_input == "CONFIRMATION":
        return {
            "response_strategy": "CONFIRMATION",
            "status": "WAITING_CONFIRMATION",
            "ag_ui_component": None,
        }

    if status == "WAITING_INPUT" or expected_input not in {None, "NONE"}:
        if expected_input == "SELECTION" and state.get("expected_candidates"):
            return {
                "response_strategy": "SELECTION_MENU",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            }
        # (2026-08-31) Incident réel : `expected_input` porte le SENTINELLE
        # littéral `"NONE"` (une chaîne non vide, donc vraie en Python) quand
        # rien n'est réellement attendu — `bool("NONE")` vaut `True`. Ce
        # check nu confondait donc TOUT tour où `validator` renvoie
        # légitimement `status="WAITING_INPUT"` sans rien à demander (son
        # repli `if not goal:` — aucun goal actif, simple clarification)
        # avec un vrai champ manquant, escaladant à tort vers
        # `ASK_MISSING_FIELD`. `render_ask_missing_field` retombait alors
        # sur un `last_missing_field`/`detected_intent` PÉRIMÉS d'un tour
        # sans rapport, produisant une question incohérente (ex: demander un
        # prix après l'enregistrement d'une récolte) — dont la réponse de
        # l'utilisateur était ensuite silencieusement perdue. Même exclusion
        # explicite du sentinelle que la condition juste au-dessus
        # (`expected_input not in {None, "NONE"}`), pour rester cohérent au
        # sein de cette même fonction.
        if (
            state.get("missing_fields")
            or state.get("last_missing_field")
            or expected_input not in (None, "", "NONE")
        ):
            return {
                "response_strategy": "ASK_MISSING_FIELD",
                "status": "WAITING_INPUT",
                "ag_ui_component": None,
            }

    return {"response_strategy": "CLARIFICATION", "ag_ui_component": None}
