"""Validator node — pure validation of transaction_payload completeness.

Extraction is handled upstream by the SlotResolver (memory_update) via
slot_enrichment.  This node only:
1. Reads the already-resolved transaction_payload.
2. Checks completeness against INTENT_CONFIG required fields.
3. Validates value constraints (price > 0, quantity > 0, etc.).
4. Produces structured missing_fields / validation_errors.
5. Delegates prompt generation to response_handlers.final_response.
"""

from typing import Any, Dict, List

from agriconnect.core.logger import get_logger
from agriconnect.graphs.agents.market_coach.core.goals import BUYER_CART_GOALS
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    set_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.core.slots import (
    field_priority,
    get_slot,
)
from agriconnect.graphs.agents.market_coach.core.state_compaction import (
    build_compaction_patch,
)
from agriconnect.graphs.agents.market_coach.interpreter.contracts import (
    CONTRACTS,
    enforce_contract,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import (
    _label_for_field,
)
from agriconnect.graphs.agents.market_coach.utils import (
    _AUTO_RESOLVABLE_FIELDS,
    MarketRuntime,
    _compute_progress,
    _normalize_quantity_to_kg,
    canonical_unit_label,
    normalize_slot_keys,
)

logger = get_logger("AgriConnect.MarketCoach.Validator")


def _canonical_field_name(field: str) -> str:
    normalized = normalize_slot_keys({field: True})
    return next(iter(normalized.keys()), field)


def _canonicalize_required_fields(fields: List[str]) -> List[str]:
    canonical: List[str] = []
    seen: set[str] = set()
    for raw_field in fields:
        canonical_field = _canonical_field_name(raw_field)
        if canonical_field not in seen:
            canonical.append(canonical_field)
            seen.add(canonical_field)
    return canonical


def _finalize_validator_response(
    state: Dict[str, Any], response: Dict[str, Any]
) -> Dict[str, Any]:
    cleanup = build_compaction_patch(state)
    if cleanup:
        response.update(cleanup)
    return response


#: Champs porteurs d'un identifiant TECHNIQUE (UUID/clé interne). Ils sont
#: résolus par un menu de sélection ou un résolveur dédié — jamais saisis par
#: l'utilisateur. `farm_id` est exclu : il est auto-provisionné en amont
#: (ensure_farm_node), donc jamais réclamé non plus.
_TECHNICAL_ID_SUFFIXES = ("_id",)


def _is_technical_id_field(field: str) -> bool:
    name = str(field or "").strip().lower()
    if not name or name in _AUTO_RESOLVABLE_FIELDS:
        return False
    return name.endswith(_TECHNICAL_ID_SUFFIXES)


def _missing_fields_for_goal(
    payload: Dict[str, Any], required_fields: List[str]
) -> List[str]:
    missing = [
        f
        for f in required_fields
        if f not in _AUTO_RESOLVABLE_FIELDS and payload.get(f) in (None, "", [], {})
    ]
    missing.sort(key=lambda f: field_priority(f))
    return missing


def _apply_slot_defaults(
    payload: Dict[str, Any], missing: List[str], goal_upper: str
) -> List[str]:
    """Apply registered default values for missing slots. Returns updated missing list."""
    remaining = []
    for f in missing:
        slot_def = get_slot(f)
        if slot_def and slot_def.default_value is not None:
            payload[f] = slot_def.default_value
            logger.info(
                "[Validator] %s: %s missing — defaulting to %s",
                goal_upper,
                f,
                slot_def.default_value,
            )
        else:
            remaining.append(f)
    return remaining


async def validator(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Validates transaction_payload completeness for the current goal.

    Payload is already enriched by the SlotResolver (memory_update +
    slot_enrichment).  This node focuses on validation and completeness
    checks only.
    """
    goal = state.get("current_goal")
    payload: Dict[str, Any] = normalize_slot_keys(
        dict(state.get("transaction_payload") or {})
    )
    goal_upper = str(goal or "").upper()
    goal_config = INTENT_CONFIG.get(goal_upper) or {}
    required_for_goal = _canonicalize_required_fields(
        list(goal_config.get("required") or [])
    )

    if not payload and state.get("extracted_entities"):
        payload = normalize_slot_keys(dict(state.get("extracted_entities")))

    working_memory = state.get("working_memory") or {}

    previous_goal = working_memory.get("active_goal") or working_memory.get(
        "locked_intent"
    )
    prev_goal_upper = str(previous_goal or "").upper()
    goal_changed = bool(previous_goal and goal and prev_goal_upper != goal_upper)

    _BUYER_CART_CONTINUITY = frozenset(
        {
            "BUYER_REQUEST",
            "BUYER_ADD_TO_CART",
            "SEARCH_PRODUCTS",
        }
    )
    strip_structural = False
    if goal_upper in {"BUYER_LIST_ORDERS", "BUYER_VIEW_CART"}:
        strip_structural = True
    elif goal_changed:
        strip_structural = not (
            prev_goal_upper in _BUYER_CART_CONTINUITY
            and goal_upper in _BUYER_CART_CONTINUITY
        )
    if strip_structural:
        for structural_key in ("quantity", "unit", "price"):
            payload.pop(structural_key, None)

    if not goal:
        return _finalize_validator_response(
            state,
            {
                "status": "WAITING_INPUT",
                "response_strategy": "CLARIFICATION",
                "missing_fields": [],
                "validation_errors": [],
                "transaction_payload": payload,
                "ag_ui_component": None,
            },
        )

    # -----------------------------------------------------------------
    # WRITE TUNNELS — FARM & STOCKS (alias normalization)
    # -----------------------------------------------------------------
    if goal == "FARM_CREATE":
        if payload.get("location") and not payload.get("zone"):
            payload["zone"] = payload.get("location")
        if payload.get("size") and not payload.get("surface"):
            payload["surface"] = payload.get("size")

    # Pass-through to context resolver for goals that resolve IDs dynamically
    _RESOLVER_PASSTHROUGH = {
        "MARKET_BROWSE_REQUESTS": ("auction_id", ["product"]),
        "MARKET_MY_REQUESTS": ("auction_id", ["product"]),
        "SALES_PLACE_BID": ("auction_id", ["product"]),
        "MARKET_GET_MY_PROPOSALS": ("bid_id", []),
        "SALES_ACCEPT_CONTRACT": ("bid_id", []),
        "PROCUREMENT_ACCEPT_OFFER": ("bid_id", []),
        # Ces deux-là ONT un résolveur dédié côté acheteur (flows/buyer/flow.py :
        # `resolve_received_bids` et `resolve_buyer_bid_pick`, qui affichent un
        # menu de sélection) mais n'étaient PAS déclarés ici : le validateur
        # bloquait donc AVANT, en réclamant `auction_id`/`bid_id` — un UUID que
        # l'utilisateur ne peut pas connaître, avec expected_input=NONE (donc
        # aucune réponse n'était interprétable). Impasse conversationnelle.
        "MARKET_GET_REQUEST_DETAIL": ("auction_id", []),
        "PROCUREMENT_SELECT_WINNER": ("bid_id", []),
        # cycle_id / product_id sont résolus par le flux de sélection dédié
        # (_resolve_cycle_for_update / _resolve_product_for_update), jamais
        # demandés comme UUID brut à l'utilisateur.
        "SALES_UPDATE_PRODUCTION": ("cycle_id", []),
        "SALES_UPDATE_PRODUCT": ("product_id", []),
        # (2026-09-04, Product Completeness Phase 2) : MÊME impasse que celle
        # décrite juste au-dessus, réintroduite par le chantier F1 —
        # `PRODUCER_CONFIRM_DELIVERY_PAYMENT` déclare `required=["order_id"]`
        # et possède bien un résolveur dédié
        # (`flows/producer/flow.py::_resolve_order_for_delivery_payment`),
        # mais sans cette entrée le validateur bloquait AVANT lui en
        # réclamant un UUID de commande que le producteur ne peut pas
        # connaître (`missing_fields` non vide -> le routeur retombait sur
        # `to_strategy`, le résolveur n'était JAMAIS atteint). Idem pour
        # `SALES_UNPUBLISH_PRODUCT` (`_resolve_product_for_unpublish`).
        "PRODUCER_CONFIRM_DELIVERY_PAYMENT": ("order_id", []),
        "SALES_UNPUBLISH_PRODUCT": ("product_id", []),
        # (2026-09-04, Phase 5) : idem pour l'annulation producteur —
        # `_resolve_order_for_cancellation` affiche la liste des commandes
        # annulables, l'UUID n'est jamais demandé.
        "PRODUCER_CANCEL_ORDER": ("order_id", []),
    }
    passthrough = _RESOLVER_PASSTHROUGH.get(goal_upper)
    if passthrough:
        id_field, hint_fields = passthrough
        needs_resolver = not payload.get(id_field)
        if goal_upper == "SALES_PLACE_BID":
            needs_resolver = needs_resolver and bool(payload.get("product"))
        if needs_resolver:
            return _finalize_validator_response(
                state,
                {
                    "status": "PLANNING",
                    "missing_fields": [],
                    "last_missing_field": None,
                    "completed_fields": [k for k in hint_fields if payload.get(k)],
                    "validation_errors": [],
                    "transaction_payload": payload,
                    "ag_ui_component": None,
                    **clear_pending_interaction("resolver_passthrough"),
                },
            )

    # -----------------------------------------------------------------
    # COMPLETENESS + VALIDATION
    # -----------------------------------------------------------------
    missing = _missing_fields_for_goal(payload, required_for_goal)
    missing = _apply_slot_defaults(payload, missing, goal_upper)
    completed = [f for f in required_for_goal if f not in missing]

    errors: List[str] = []
    warnings: List[str] = []

    price = payload.get("price")
    if price is not None:
        try:
            pf = float(price)
            if pf <= 0:
                errors.append("Le prix doit être supérieur à 0.")
            elif pf > 10_000_000:
                warnings.append(f"Prix très élevé ({pf:,.0f} FCFA). Vérifiez.")
        except (TypeError, ValueError):
            errors.append("Le prix indiqué n'est pas un nombre valide.")

    qty = payload.get("quantity")
    if qty is not None:
        try:
            qf = float(qty)
            if qf <= 0:
                errors.append("La quantité doit être supérieure à 0.")
            elif qf > 100_000:
                warnings.append(
                    f"Quantité très importante ({qf:,.0f}). Vérifiez l'unité."
                )
        except (TypeError, ValueError):
            errors.append("La quantité indiquée n'est pas un nombre valide.")

    payload = _normalize_quantity_to_kg(payload)

    unit_raw = str(payload.get("unit") or "KG").upper()
    unit_display = canonical_unit_label(
        payload.get("unit_display") or payload.get("original_unit") or unit_raw
    )
    if qty is not None and unit_display == "TONNE":
        try:
            if float(qty) > 500:
                warnings.append("500+ tonnes semble excessif. Vérifiez l'unité.")
        except (TypeError, ValueError):
            pass

    # -----------------------------------------------------------------
    # CONTRATS PYDANTIC (défense en profondeur post-LLM, Phase 2)
    # Valident uniquement les VALEURS présentes — la présence reste le
    # travail d'INTENT_CONFIG.required (enforce_contract ignore `missing`).
    # -----------------------------------------------------------------
    if goal_upper in CONTRACTS:
        contract_payload = dict(payload)
        contract_payload.setdefault(
            "phone", state.get("user_phone") or state.get("phone") or ""
        )
        contract_ok, contract_msg, contract_field = enforce_contract(
            goal_upper, contract_payload
        )
        if not contract_ok:
            label = _label_for_field(goal, contract_field) if contract_field else None
            errors.append(
                f"{label} : valeur invalide."
                if label
                else (contract_msg or "Entrée invalide.")
            )
            if (
                contract_field
                and contract_field != "phone"
                and contract_field not in missing
            ):
                # Re-demander le champ fautif comme s'il manquait.
                payload.pop(contract_field, None)
                missing.insert(0, contract_field)
            logger.info(
                "[Validator] Contrat %s rejeté (champ=%s): %s",
                goal_upper,
                contract_field,
                contract_msg,
            )

    progress = _compute_progress(goal, payload)

    if missing or errors:
        first_missing = missing[0] if missing else None

        # ── FILET : NE JAMAIS RÉCLAMER UN IDENTIFIANT TECHNIQUE ──────────
        # Un champ `*_id` est résolu par un menu de sélection ou un résolveur
        # dédié, JAMAIS saisi par l'utilisateur (personne ne connaît un UUID).
        # Si un tel champ reste manquant ici, c'est qu'aucun résolveur ne
        # couvre ce goal : demander l'ID produisait une IMPASSE — l'agent
        # affichait « Étape 1/1 : identifiant enchère » avec
        # expected_input=NONE, donc même une réponse correcte de l'utilisateur
        # ne pouvait être rattachée au champ. On termine proprement le tour
        # avec un message actionnable plutôt que de piéger la conversation.
        # On inspecte TOUS les champs manquants, pas seulement le premier :
        # `STOCK_UPDATE_LEVEL` exige ['stock_id', 'quantity'] et `quantity` est
        # prioritaire — l'agent demandait donc la quantité d'abord et ne butait
        # sur l'UUID qu'AU TOUR SUIVANT. Collecter des slots qui mènent à une
        # impasse est inutile : si un identifiant non résoluble manque, on
        # clarifie TOUT DE SUITE.
        blocking_id = next((f for f in missing if _is_technical_id_field(f)), None)
        if blocking_id:
            logger.warning(
                "[Validator] %s : champ technique '%s' manquant sans résolveur — "
                "clarification au lieu de réclamer un identifiant à l'utilisateur.",
                goal_upper,
                blocking_id,
            )
            return _finalize_validator_response(
                state,
                {
                    "status": "COMPLETED",
                    "goal_status": "COMPLETED",
                    "missing_fields": [],
                    "last_missing_field": None,
                    "validation_errors": [],
                    "response_strategy": "CLARIFICATION",
                    "final_response": (
                        "Je n'ai pas assez d'éléments pour identifier l'élément concerné. "
                        "Dites-moi de quoi il s'agit en toutes lettres (par exemple "
                        "*voir mon stock*, *mes commandes* ou *mes appels d'offres*), "
                        "puis choisissez dans la liste."
                    ),
                    "transaction_payload": payload,
                    "is_certified": False,
                    "confirmation_summary": None,
                    "execution_authorized": False,
                    "ag_ui_component": None,
                    **clear_pending_interaction("blocking_technical_id"),
                },
            )

        hint = None
        if progress and first_missing:
            total = progress.get("total", 0)
            filled = progress.get("filled", 0)
            label = _label_for_field(goal, first_missing)
            hint = f"Étape {filled + 1}/{total} : {label}"

        return _finalize_validator_response(
            state,
            {
                "status": "WAITING_INPUT",
                "goal_status": "WAITING_INPUT",
                "missing_fields": missing,
                "completed_fields": completed,
                "validation_errors": errors + warnings,
                "last_missing_field": first_missing,
                "response_strategy": "ASK_MISSING_FIELD",
                "transaction_payload": payload,
                "conversation_progress": progress,
                "proactive_hint": hint,
                "is_certified": False,
                "confirmation_summary": None,
                "execution_authorized": False,
                "ag_ui_component": None,
                # (2026-09-02) `get_pending_interaction()` donne toujours la
                # priorité au tunnel panier (build_selection_context) sur ce
                # champ quand les deux existent — écrire ENTER_FIELD ici
                # même pour un goal panier est donc sans danger, jamais lu
                # tant qu'un menu producteur/palier est réellement actif.
                **set_pending_interaction(
                    InteractionKind.ENTER_FIELD,
                    goal=goal_upper,
                    field_name=first_missing,
                ),
            },
        )

    is_cart_goal = goal_upper in BUYER_CART_GOALS

    result: Dict[str, Any] = {
        "status": "PROCESSING" if is_cart_goal else "PLANNING",
        "missing_fields": [],
        "last_missing_field": None,
        "completed_fields": completed,
        "validation_errors": warnings,
        "transaction_payload": payload,
        "conversation_progress": progress,
        "proactive_hint": None,
        "final_response": None,
        "ag_ui_component": None,
        **clear_pending_interaction("validated_complete"),
    }

    return _finalize_validator_response(state, result)
