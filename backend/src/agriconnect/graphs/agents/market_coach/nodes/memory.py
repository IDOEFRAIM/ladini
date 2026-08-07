from typing import Any, Dict, List, Optional
import unicodedata
from agriconnect.core.logger import get_logger
from agriconnect.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _normalize_quantity_to_kg,
    canonical_unit_label,
    merge_payload,
    normalize_slot_keys,
    slot_has_value,
)
from agriconnect.graphs.agents.market_coach.core.slots import build_alias_mirrors
from agriconnect.graphs.agents.market_coach.core.state_compaction import (
    build_compaction_patch,
)
from agriconnect.graphs.agents.market_coach.services.domain.slot_enrichment import (
    enrich_payload_from_text,
)

logger = get_logger("AgriConnect.MarketCoach.MemoryUpdate")

_ADD_TO_CART_DRAFT_FIELDS = {
    "product",
    "product_id",
    "quantity",
    "unit",
    "price",
    "zone",
    "selection_index",
    "selected_value",
    "resolved_id",
}


_PRIMARY_CANONICAL_UNITS = {"KG", "TONNE", "SAC", "UNITE", "PANIER", "TETE"}

# expected_input (slot précis en cours) → champs canoniques qu'une réponse
# ANSWER à ce slot a le droit de modifier. Tout le reste est du bruit/une
# hallucination LLM et doit être ignoré — voir le garde-fou dans `memory_update`.
_EXPECTED_INPUT_ALLOWED_FIELDS: Dict[str, frozenset] = {
    "PRODUCT": frozenset({"product", "product_id"}),
    "QUANTITY": frozenset({"quantity", "unit"}),
    "PRICE": frozenset({"price", "price_unit"}),
    "UNIT": frozenset({"unit"}),
    "LOCATION": frozenset({"zone"}),
    "DATE": frozenset({"estimated_available_at", "expected_harvest_date"}),
    "FARM_NAME": frozenset({"farm_name"}),
}

# _ALIAS_MIRRORS is now derived from core/slots.py (single source of truth).
_ALIAS_MIRRORS = build_alias_mirrors()

_CORRECTION_HISTORY_LIMIT = 5
_ORDER_MAPPING_KINDS = frozenset({"order", "order_list", "buyer_orders"})
_AUCTION_MAPPING_KINDS = frozenset({"auction", "buyer_auction_list", "auction_bids"})
_EPHEMERAL_WORKING_KEYS = frozenset({"payload_richness", "last_confidence", "step_index"})
_PRODUCT_CASCADE_FIELDS = (
    "quantity",
    "unit",
    "price",
    "currency",
    "is_negotiable",
    "variety",
    "quality_grade",
    "quantity_display",
    "original_quantity",
    "unit_conversion",
)
_UNIT_CASCADE_FIELDS = ("price",)
_CANONICAL_SLOT_ORDER = ("product", "quantity", "unit", "price", "zone")


_BUYER_REQUEST_SPECIALIZATIONS = frozenset({
    "BUYER_ADD_TO_CART",
    "BUYER_VIEW_CART",
    "BUYER_PREORDER_INIT",
    "BUYER_PREORDER_CONFIRM",
    "BUYER_NEGOTIATE_PRICE",
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_LIST_ORDERS",
    "BUYER_CANCEL_ORDER",
    "BUYER_LIST_AUCTIONS",
    "BUYER_CHECK_AUCTION_STATUS",
    "BUYER_CART_RESET",
})


def _is_goal_refinement(previous: str, incoming: str) -> bool:
    """BUYER_REQUEST → BUYER_ADD_TO_CART is a specialization, not a real change.

    The context_resolver bridges BUYER_REQUEST into specific goals (cart,
    preorder, negotiation). This must NOT trigger a payload reset.
    """
    if previous == "BUYER_REQUEST" and incoming in _BUYER_REQUEST_SPECIALIZATIONS:
        return True
    if incoming == "BUYER_REQUEST" and previous in _BUYER_REQUEST_SPECIALIZATIONS:
        return True
    return False


def _mirror_aliases(container: Dict[str, Any]) -> None:
    for canonical, aliases in _ALIAS_MIRRORS.items():
        value = container.get(canonical)
        for alias in aliases:
            if slot_has_value(value):
                container[alias] = value
            else:
                container.pop(alias, None)




def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _format_value(value: Any) -> str:
    if not slot_has_value(value):
        return "∅"
    if isinstance(value, float):
        return ("%g" % value)
    return str(value)


def _resolve_unit_value(value: Any) -> Optional[str]:
    if not slot_has_value(value):
        return None
    raw = str(value).strip().upper()
    if not raw:
        return None
    canonical = canonical_unit_label(raw, raw)
    canonical = str(canonical or "").upper().strip()
    if canonical in _PRIMARY_CANONICAL_UNITS:
        return canonical
    return None


async def memory_update(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Met à jour la mémoire de transaction, résout les sélections AG-UI,
    hérite les entités stables pour continuité inter-tours, et injecte
    les défauts profil (zone) quand l'utilisateur ne les spécifie pas."""
    extracted_raw: Dict[str, Any] = state.get("extracted_entities") or {}
    payload_source: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    stable_source: Dict[str, Any] = dict(state.get("stable_entities") or {})
    working_source: Dict[str, Any] = dict(state.get("working_memory") or {})
    working: Dict[str, Any] = {
        key: value for key, value in working_source.items() if key not in _EPHEMERAL_WORKING_KEYS
    }
    draft_source: Dict[str, Any] = dict(state.get("draft_payload") or {})

    extracted = normalize_slot_keys(extracted_raw)
    payload = normalize_slot_keys(payload_source)
    stable = normalize_slot_keys(stable_source)
    draft_payload = normalize_slot_keys(draft_source)

    # --- STALE TRANSIENT KEYS CLEANUP ---
    # transaction_payload uses merge_dict reducer → post_response_cleanup
    # cannot delete keys. Clear stale selection/resolved values here at the
    # start of every turn unless the fresh extracted_entities carries them.
    _interpreted_event_upper = str(state.get("interpreted_event") or "").upper().strip()
    if _interpreted_event_upper != "SELECTION":
        for _transient in ("selection_index", "selected_value", "resolved_id"):
            if _transient not in extracted:
                payload.pop(_transient, None)
    onboarding_profile = dict(state.get("onboarding_profile") or {})

    def _rehydrate_onboarding_slot(source_key: str, target_key: Optional[str] = None) -> None:
        key = target_key or source_key
        value = onboarding_profile.get(source_key)
        if slot_has_value(value) and not slot_has_value(payload.get(key)):
            payload[key] = value

    _rehydrate_onboarding_slot("phone")
    _rehydrate_onboarding_slot("name")
    _rehydrate_onboarding_slot("role")
    _rehydrate_onboarding_slot("zone_name")
    _rehydrate_onboarding_slot("zone_id")

    existing_corrections = working_source.get("recent_corrections") or {}
    if isinstance(existing_corrections, dict):
        recent_corrections = dict(existing_corrections)
    else:
        # Legacy format (list of dicts) → flatten
        recent_corrections = {}
        for entry in existing_corrections:
            if isinstance(entry, dict):
                for key, value in entry.items():
                    recent_corrections[key] = value
    payload_reset = False
    product_slot_changed = False
    clear_vendor_ctx = False

    def _record_correction(field: str, old: Any, new: Any) -> None:
        if not field:
            return
        change_value = f"{_format_value(old)} -> {_format_value(new)}"
        if field in recent_corrections:
            recent_corrections.pop(field, None)
        elif len(recent_corrections) >= _CORRECTION_HISTORY_LIMIT:
            first_key = next(iter(recent_corrections))
            recent_corrections.pop(first_key, None)
        recent_corrections[field] = change_value

    def _reset_payload(reason: str) -> None:
        nonlocal payload_reset, payload, clear_vendor_ctx
        if payload:
            logger.info("[MemoryUpdate] Payload reset (%s)", reason)
        payload.clear()
        payload_reset = True
        clear_vendor_ctx = True

    def _cascade_clear(fields) -> None:
        for field in fields:
            payload.pop(field, None)

    def _clean_upper(value: Any) -> Optional[str]:
        if not slot_has_value(value):
            return None
        trimmed = str(value).strip().upper()
        return trimmed or None

    def _apply_slot(field: str, value: Any) -> None:
        nonlocal product_slot_changed, clear_vendor_ctx
        if not slot_has_value(value):
            return
        current_value = payload.get(field)
        if not slot_has_value(current_value):
            payload[field] = value
            return
        if _values_equal(current_value, value):
            return
        _record_correction(field, current_value, value)
        if field == "product":
            _cascade_clear(_PRODUCT_CASCADE_FIELDS)
            stable.pop("product", None)
            stable.pop("quantity", None)
            stable.pop("price", None)
            stable.pop("unit", None)
            stable.pop("stock_id", None)
            product_slot_changed = True
            clear_vendor_ctx = True
        elif field == "unit":
            _cascade_clear(_UNIT_CASCADE_FIELDS)
            stable.pop("unit", None)
        payload[field] = value

    def _normalize_menu_text(value: str | None) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        normalized = normalized.lower()
        return "".join(ch for ch in normalized if ch.isalnum())

    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    expected_input = str(state.get("expected_input") or "NONE").upper().strip()
    form_step_state = str(state.get("form_step") or "").upper().strip()
    form_completed = form_step_state == "COMPLETE"
    onboarding_active = bool(
        state.get("is_onboarding")
        or interpreted_event == "ONBOARDING_INPUT"
        or str(state.get("detected_intent") or "").upper().strip() == "ONBOARDING"
        or str(state.get("response_strategy") or "").upper().strip() == "ONBOARDING"
    )

    if expected_input == "CANCELLATION_REASON":
        reason_text = state.get("normalized_text") or state.get("user_query")
        if isinstance(reason_text, str):
            reason_text = reason_text.strip()
            if reason_text:
                payload["cancel_reason"] = reason_text

    if expected_input == "UNIT":
        unit_candidates = [
            extracted.get("unit"),
            payload.get("unit"),
            stable.get("unit"),
            state.get("normalized_text"),
            state.get("user_query"),
        ]
        resolved_unit = None
        for candidate in unit_candidates:
            resolved_unit = _resolve_unit_value(candidate)
            if resolved_unit:
                break
        if resolved_unit:
            payload["unit"] = resolved_unit
            extracted["unit"] = resolved_unit
            # Avoid treating the unit answer as a product change.
            extracted.pop("product", None)

    incoming_role = _clean_upper(extracted.get("role"))
    if not incoming_role and not onboarding_active:
        incoming_role = _clean_upper(state.get("user_role"))
    if incoming_role == "UNKNOWN":
        incoming_role = None
    previous_role = _clean_upper(payload.get("role"))
    if previous_role and incoming_role and previous_role != incoming_role and not onboarding_active:
        _record_correction("role", previous_role, incoming_role)
        _reset_payload("role_change")
    elif previous_role and incoming_role and previous_role != incoming_role and onboarding_active:
        _record_correction("role", previous_role, incoming_role)
    if incoming_role:
        payload["role"] = incoming_role
    extracted.pop("role", None)

    detected_intent = (
        state.get("current_goal")
        or state.get("detected_intent")
        or extracted.get("intent")
    )
    incoming_intent = _clean_upper(detected_intent)
    if incoming_intent == "UNKNOWN":
        incoming_intent = None
    previous_intent = _clean_upper(payload.get("intent"))
    if previous_intent and incoming_intent and previous_intent != incoming_intent and not onboarding_active:
        if not _is_goal_refinement(previous_intent, incoming_intent):
            _record_correction("intent", previous_intent, incoming_intent)
            if not form_completed:
                _reset_payload("intent_change")
    if incoming_intent and not onboarding_active:
        payload["intent"] = incoming_intent
    extracted.pop("intent", None)

    # FAILLE 1 — Sanctuarise the active business goal.
    # Never overwrite an active tunnel goal with None/UNKNOWN because the user replied briefly ("1", "ok", "oui").
    incoming_goal = state.get("current_goal")
    previous_goal = working.get("active_goal") or working.get("locked_intent")
    in_tunnel = bool(previous_goal and expected_input not in {"", "NONE"})
    if (
        previous_goal
        and (incoming_goal in (None, "", "UNKNOWN") or interpreted_event in {"UNKNOWN", "ANSWER"})
        and (in_tunnel or str(state.get("status") or "").upper() in {"WAITING_INPUT", "WAITING_CONFIRMATION"})
    ):
        current_goal = previous_goal
    else:
        current_goal = incoming_goal

    goal_upper = str(current_goal or "").upper()

    if goal_upper == "BUYER_ADD_TO_CART" and draft_payload and not draft_payload.get("__reset__"):
        # `draft_payload` is in `_DRAFT_SAFE_GOALS` (state_cleaner_node), so it
        # is NEVER purged between two separate BUYER_ADD_TO_CART attempts as
        # long as the goal name doesn't change — only on FAILED/COMPLETED
        # status. If the user abandons adding product A mid-flow (no
        # FAILED/COMPLETED transition) and later starts adding product B, the
        # stale draft for A would otherwise resurrect onto B's payload. Guard:
        # only inherit the persisted draft if it's for the SAME product this
        # turn is extracting (i.e. a genuine continuation of the same
        # in-progress add), not a fresh add for a different one.
        incoming_product = extracted.get("product")
        draft_product = draft_payload.get("product")
        if incoming_product and draft_product and not _values_equal(draft_product, incoming_product):
            logger.info(
                "[MemoryUpdate] Stale cart draft for '%s' discarded (new product '%s')",
                draft_product, incoming_product,
            )
        else:
            payload = merge_payload(draft_payload, payload)

    # Un tour CONFIRM ne fournit AUCUNE information nouvelle par définition
    # ("je confirme", "oui") — toute entité que l'interpréteur a pu extraire
    # pour un message aussi court/ambigu est du bruit (voire une
    # hallucination du LLM sur un few-shot du prompt système), jamais une
    # correction volontaire. Sans cette garde, `_apply_slot("product", ...)`
    # traitait un tel bruit comme un changement de produit légitime : il
    # écrasait le `product` que `form_node` venait JUSTE de figer dans
    # `transaction_payload` au moment de la confirmation, ET videait
    # quantity/unit/price/zone au passage (cascade de `_apply_slot` sur
    # changement de produit) — cause du bug où la commande exécutée portait
    # un tout autre produit que celui confirmé dans le récap.
    #
    # REJECT est différent : "non" seul ne porte en effet aucune info, mais
    # "non c'est 200 tonnes" / "non je vends X au prix de Y" est un refus AVEC
    # correction explicite — un pattern conversationnel très courant. Bloquer
    # inconditionnellement REJECT ici faisait perdre silencieusement toute
    # correction accompagnant un refus (le récap restait identique malgré une
    # reformulation limpide de l'utilisateur). On ne bloque donc REJECT que
    # si l'interpréteur n'a RÉELLEMENT rien extrait (cas du "non" bref) —
    # sinon les valeurs explicitement redonnées sont appliquées normalement.
    _extracted_has_real_values = any(slot_has_value(v) for v in extracted.values())
    _skip_merge = interpreted_event == "CONFIRM" or (
        interpreted_event == "REJECT" and not _extracted_has_real_values
    )

    # Garde-fou anti-hallucination pendant la collecte d'un slot PRÉCIS
    # (expected_input ∈ {DATE, PRICE, QUANTITY, UNIT, LOCATION, PRODUCT,
    # FARM_NAME}) : une réponse OU une correction visant ce slot ne doit jamais
    # pouvoir réécrire silencieusement des champs SANS RAPPORT déjà validés
    # (product/quantity/price...). Un LLM en repli dégradé (ex: llama-3.1-8b
    # après un 429 sur le modèle principal) hallucine des valeurs plausibles
    # pour TOUT le schéma JSON même quand une seule info a été donnée — observé
    # en prod DEUX fois :
    #   1) réponse à la DATE ("d'ici le 21 décembre") → "78 boeufs à 425000"
    #      remplacé par "5900 poussins allemand à 2400" ;
    #   2) correction de PRIX ("l'unité coûte plutôt 36500") pendant la DATE →
    #      "14 chèvres" remplacé par "36500 poussins allemands".
    # Le cas (2) passait par `event=UPDATE`, NON couvert par la 1re version de
    # ce garde (ANSWER seul) — d'où l'extension à UPDATE ici.
    #
    # IMPORTANT — pourquoi CONFIRMATION reste libre : `_EXPECTED_INPUT_ALLOWED_
    # FIELDS` n'a PAS de clé "CONFIRMATION"/"SELECTION"/"NONE" → `allowed_for_
    # slot` vaut None → ce bloc est un no-op. Une correction au moment de la
    # confirmation ("non c'est 200 tonnes", récap complet sous les yeux) peut
    # donc toujours toucher n'importe quel champ, comme voulu. Le narrowing ne
    # mord QUE pendant la collecte d'un slot isolé. INTERRUPTION est
    # volontairement exclu : il porte les entités d'un NOUVEAU goal, pas une
    # correction du goal courant.
    #
    # Même principe que le narrowing déjà appliqué par `agents/forms.py` et
    # `nodes/form_node.py` pour les formulaires DRY — ici pour le chemin
    # générique (validator/context_resolver) qui n'en bénéficiait pas.
    # Extraction composée du fast-path déterministe (routing.py) : un message
    # comme "775 kg d'oignon et le kg coûte 175 fcfa" répond au slot QUANTITY
    # en cours ET fournit price/price_unit — par correspondance exacte de
    # tokens (unité/devise), pas une supposition du LLM. Contrairement au LLM
    # en repli dégradé (source du garde-fou ci-dessous), cette extraction ne
    # peut PAS halluciner un champ sans rapport : elle ne remplit jamais que
    # quantity/unit/price/price_unit, les 4 champs qu'elle sait reconnaître
    # explicitement dans le texte. Sans cette exception, price/price_unit
    # étaient silencieusement rejetés comme "hors-scope" pendant la collecte
    # de QUANTITY — l'agent redemandait alors le prix au tour suivant alors
    # que l'utilisateur venait de le donner.
    _trusted_compound_path = (
        str((state.get("raw_analysis") or {}).get("path") or "")
        == "fast_path_slot_numeric_compound_answer"
    )

    if interpreted_event in {"ANSWER", "UPDATE"} and not _skip_merge and not _trusted_compound_path:
        allowed_for_slot = _EXPECTED_INPUT_ALLOWED_FIELDS.get(expected_input)
        if allowed_for_slot is not None:
            for key in list(extracted.keys()):
                if key in allowed_for_slot:
                    continue
                dropped = extracted.pop(key, None)
                if slot_has_value(dropped):
                    logger.warning(
                        "[MemoryUpdate] %s attendu=%s : champ hors-sujet '%s'=%r ignoré "
                        "(hors-scope du slot en cours — probable bruit/hallucination LLM)",
                        interpreted_event, expected_input, key, dropped,
                    )

    if not _skip_merge:
        for field in _CANONICAL_SLOT_ORDER:
            value = extracted.pop(field, None)
            _apply_slot(field, value)

        for key, value in list(extracted.items()):
            if not slot_has_value(value):
                continue
            payload[key] = value

    # --- ENTITY INHERITANCE : stable_entities → payload pour continuité ---
    # Pendant un tunnel, si le payload manque un champ stable, on l'injecte.
    # Cela évite de redemander le produit si l'utilisateur l'a déjà dit.
    if current_goal:
        skip_product_inheritance = payload_reset or product_slot_changed
        for key in ("product", "unit", "zone"):
            if slot_has_value(payload.get(key)) or not slot_has_value(stable.get(key)):
                continue
            if key == "product" and skip_product_inheritance:
                continue
            payload[key] = stable[key]
            logger.debug("[MemoryUpdate] Inherited stable entity %s=%s", key, stable[key])

    # --- VENDOR CONTEXT RECOVERY for cart tunnel ---
    # When the buyer is selecting a vendor, the product and quantity live in
    # vendor_selection_context but may be absent from payload (reset between
    # turns). Recover them so the validator doesn't block with "product missing".
    if goal_upper in ("BUYER_ADD_TO_CART", "BUYER_VIEW_CART"):
        vendor_ctx = state.get("vendor_selection_context")
        if isinstance(vendor_ctx, dict) and not vendor_ctx.get("__reset__"):
            if not slot_has_value(payload.get("product")):
                chosen = vendor_ctx.get("chosen_vendor")
                recovered_product = (
                    (chosen.get("name") if isinstance(chosen, dict) else None)
                    or vendor_ctx.get("product")
                )
                if slot_has_value(recovered_product):
                    payload["product"] = recovered_product
                    logger.info("[MemoryUpdate] Recovered product '%s' from vendor_selection_context", recovered_product)
            if not slot_has_value(payload.get("quantity")):
                recovered_qty = vendor_ctx.get("requested_quantity")
                if slot_has_value(recovered_qty):
                    payload["quantity"] = recovered_qty
                    logger.info("[MemoryUpdate] Recovered quantity '%s' from vendor_selection_context", recovered_qty)
            if not slot_has_value(payload.get("unit")):
                recovered_unit = vendor_ctx.get("requested_unit")
                if slot_has_value(recovered_unit):
                    payload["unit"] = recovered_unit

    # --- ZONE INJECTION from user profile ---
    if not slot_has_value(payload.get("zone")):
        profile_zone = state.get("zone") or state.get("zone_name")
        if slot_has_value(profile_zone):
            payload["zone"] = str(profile_zone).strip()

    # --- SLOT ENRICHMENT: text-based extraction (single pass) ---
    normalized_text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
    if normalized_text and current_goal:
        payload = await enrich_payload_from_text(payload, normalized_text, current_goal, mc_runtime)

    # --- AG-UI: free-text resolution of ListMenu labels ---
    user_free_text = (
        extracted.get("normalized_text")
        or payload.get("normalized_text")
        or state.get("normalized_text")
        or state.get("user_query")
        or ""
    )
    user_free_text = str(user_free_text or "").strip()
    candidates = state.get("expected_candidates") or []
    if (
        user_free_text
        and candidates
        and "selection_index" not in payload
        and "selected_value" not in payload
        and expected_input == "SELECTION"
    ):
        normalized_user = _normalize_menu_text(user_free_text)
        label_index_map = {
            _normalize_menu_text(label): str(i)
            for i, label in enumerate(candidates, start=1)
        }
        matched_index = label_index_map.get(normalized_user)
        if matched_index:
            payload["selection_index"] = matched_index
            extracted["selection_index"] = matched_index

    # --- AG-UI: Selection index resolution via available_mapping ---
    sel_idx = extracted.get("selection_index") or payload.get("selection_index")
    sel_val = extracted.get("selected_value") or payload.get("selected_value")
    mapping = state.get("available_mapping") or {}
    mapping_kind = working.get("available_mapping_kind")
    session_id = str(state.get("session_id") or state.get("user_phone") or "")
    snapshot_id = (
        working.get("menu_snapshot_id")
        or state.get("menu_snapshot_id")
    )

    if sel_idx is not None or sel_val is not None:
        resolved_id = None
        if mapping:
            if sel_idx is not None:
                resolved_id = mapping.get(str(sel_idx))
            if resolved_id is None and sel_val is not None:
                resolved_id = mapping.get(str(sel_val))
        if resolved_id is None and snapshot_id:
            selection_token = sel_idx if sel_idx is not None else sel_val
            try:
                resolved_id = menu_snapshot_store.resolve(session_id, snapshot_id, selection_token)
            except Exception as snap_exc:
                logger.warning(
                    "[MemoryUpdate] menu_snapshot_store.resolve failed (snapshot=%s token=%s): %s",
                    snapshot_id,
                    selection_token,
                    snap_exc,
                )

        if resolved_id:
            resolved_str = str(resolved_id)
            if mapping_kind in _AUCTION_MAPPING_KINDS:
                payload["auction_id"] = resolved_str
            elif mapping_kind == "bid":
                payload["bid_id"] = resolved_str
            elif mapping_kind == "stock":
                payload["stock_id"] = resolved_str
            elif mapping_kind == "cycle":
                # Sélection d'une production future (MarketOffer) dans la liste
                # des stocks → cible de SALES_UPDATE_PRODUCTION (mise à jour de lot).
                payload["cycle_id"] = resolved_str
            elif mapping_kind == "catalog_product":
                # Sélection d'un produit du catalogue → cible de SALES_UPDATE_PRODUCT.
                payload["product_id"] = resolved_str
            elif mapping_kind == "farm":
                payload["farm_id"] = resolved_str
            elif mapping_kind in _ORDER_MAPPING_KINDS:
                payload["order_id"] = resolved_str
            elif mapping_kind == "intent_disambiguation":
                pass
            elif mapping_kind == "product_vendor":
                # cart_management resolves the vendor by integer position in
                # vendor_selection_context.vendors — it needs the original numeric
                # selection_index, NOT the resolved UUID.  Do NOT inject the UUID and
                # do NOT clear selection_index here; cart_management pops it itself
                # once it has successfully located the vendor.
                pass
            else:
                payload["resolved_id"] = resolved_str
            # For product_vendor, keep selection_index so cart_management can use it.
            # Clearing it here would cause cart_management to skip vendor resolution
            # and re-show the vendor menu on every turn (infinite loop).
            if mapping_kind != "product_vendor":
                payload.pop("selection_index", None)
                payload.pop("selected_value", None)

    # --- Update stable entities uniquement après complétion ---
    status = str(state.get("status") or "").upper()
    goal_status = str(state.get("goal_status") or "").upper()
    strategy = str(state.get("response_strategy") or "").upper()
    transaction_closed = strategy == "SUCCESS" or status in {"COMPLETED", "SUCCESS"} or goal_status == "COMPLETED"

    updated_stable = dict(stable)
    if transaction_closed:
        for k in ("product", "unit", "movement_type", "zone"):
            candidate = payload.get(k) or extracted.get(k)
            if slot_has_value(candidate):
                updated_stable[k] = candidate

    # --- Conversation metrics ---
    working["last_event"] = state.get("interpreted_event")
    working["last_confidence"] = float(state.get("interpreter_confidence") or 0.0)
    entity_count = sum(1 for v in payload.values() if slot_has_value(v))
    working["payload_richness"] = entity_count
    if current_goal and current_goal != "DISAMBIGUATION_PENDING":
        working["active_goal"] = current_goal
        working["locked_intent"] = current_goal
        working["step_index"] = 0

    existing_draft = draft_payload if draft_payload and not draft_payload.get("__reset__") else {}

    draft_patch: Dict[str, Any] | None = None
    if goal_upper == "BUYER_ADD_TO_CART":
        if not transaction_closed:
            tracked = {
                key: payload.get(key)
                for key in _ADD_TO_CART_DRAFT_FIELDS
                if slot_has_value(payload.get(key))
            }
            if tracked:
                draft_patch = merge_payload(existing_draft, tracked)
        elif existing_draft:
            draft_patch = {"__reset__": True}
    elif transaction_closed and existing_draft:
        draft_patch = {"__reset__": True}

    # Let downstream nodes acknowledge corrections once; response_handlers will clear it.
    if recent_corrections:
        working["recent_corrections"] = recent_corrections
    else:
        working["recent_corrections"] = None

    payload = _normalize_quantity_to_kg(payload)
    _mirror_aliases(payload)
    _mirror_aliases(updated_stable)
    if draft_patch and not draft_patch.get("__reset__"):
        _mirror_aliases(draft_patch)
    elif existing_draft:
        _mirror_aliases(existing_draft)

    result = {
        "transaction_payload": payload,
        "stable_entities": updated_stable,
        "working_memory": working,
        "current_goal": current_goal,
    }

    if clear_vendor_ctx:
        result["vendor_selection_context"] = None

    if draft_patch is not None:
        result["draft_payload"] = draft_patch
    elif existing_draft:
        result["draft_payload"] = existing_draft

    compaction_patch = build_compaction_patch(state, tracking_strategy="drop")
    if compaction_patch:
        result.update(compaction_patch)

    return result


