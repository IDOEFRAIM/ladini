"""Buyer flow shared helpers — entity extraction, state builders, constants."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.flows.common.menu_text import (
    render_numbered_menu,
)
from agriconnect.graphs.agents.market_coach.services.domain.buyer_common import (
    SUPPORT_FOOTER,
    with_support_footer,
)
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
    SOURCE_TYPE_LABELS,
)

logger = logging.getLogger("AgriConnect.Market.BuyerFlow")

# =====================================================================
# GOAL SETS — aliases de compat ; source canonique : core/goals.py
# (dérivés d'INTENT_CONFIG, anti-drift). Ne jamais redéfinir localement.
# =====================================================================

from agriconnect.graphs.agents.market_coach.core.goals import (  # noqa: E402
    BUYER_CART_GOALS as CART_GOALS,
    BUYER_PREORDER_GOALS as PREORDER_GOALS,
    BUYER_NEGOTIATION_GOALS as NEGOTIATION_GOALS,
    BUYER_ORDER_TRACKING_GOALS as ORDER_TRACKING_GOALS,
    BUYER_AUCTION_TRACKING_GOALS as AUCTION_TRACKING_GOALS,
)

READ_ONLY_INTENTS = frozenset({
    "BUYER_VIEW_CART", "BUYER_LIST_ORDERS", "BUYER_CHECK_ORDER_STATUS",
    "BUYER_LIST_AUCTIONS", "BUYER_CHECK_AUCTION_STATUS",
})

ESCALATE_KEYWORDS = frozenset({
    "appel", "appels", "appel d'offres", "appel doffres",
    "appel d offre", "lancer appel",
})
CONFIRM_KEYWORDS = frozenset({"oui", "yes", "ok"})
DECLINE_KEYWORDS = frozenset({"non", "no", "aucun", "aucune", "annuler"})

# =====================================================================
# PRODUCT INFERENCE
# =====================================================================

_PRODUCT_HINT_PATTERN = re.compile(
    r"(?:\bde\b|\bdu\b|\bdes\b|d')\s+"
    r"([a-zàâçéèêëîïôûùüÿñæœ'\-]+(?:\s+[a-zàâçéèêëîïôûùüÿñæœ'\-]+)?)",
    re.IGNORECASE,
)
_PRODUCT_STOP_WORDS = frozenset({
    "kg", "kgs", "kilo", "kilos", "kilogramme", "kilogrammes", "tonne", "tonnes",
    "sac", "sacs", "panier", "paniers", "de", "du", "des", "d", "le", "la", "les",
    "un", "une", "au", "aux", "en", "pour", "avec", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "et", "ou",
})


def infer_product_from_text(state: Dict[str, Any]) -> Optional[str]:
    """Extract the most likely product name from normalized user text."""
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
    if not text:
        return None

    hint = _PRODUCT_HINT_PATTERN.search(text)
    if hint:
        candidate = hint.group(1).strip(" .,;!?")
        if candidate and candidate.lower() not in _PRODUCT_STOP_WORDS:
            return candidate

    tokens = re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ'\-]+", text.lower())
    for token in reversed(tokens):
        if token not in _PRODUCT_STOP_WORDS and not token.isdigit():
            return token
    return None


# =====================================================================
# ENTITY RESOLUTION — consistent product/quantity/unit extraction
# =====================================================================

def resolve_product(
    payload: Dict[str, Any],
    stable_entities: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Resolve product name from payload → stable entities → text inference → memory."""
    product = (
        payload.get("product")
        or stable_entities.get("product")
    )
    if not product:
        product = infer_product_from_text(state)
        if product:
            payload["product"] = product
            payload["_product_from_text"] = True
    if not product:
        working = state.get("working_memory") or {}
        last = working.get("buyer_request_last_product")
        if last:
            product = str(last)
            payload.setdefault("product", product)
    return product


def additional_products_hint(payload: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Message à ajouter quand l'utilisateur a mentionné plusieurs produits
    dans le même message (ex: "œufs et laitue").

    L'interprète (`interpreter/routing.py`) ne met QUE le premier produit
    dans `product` — jamais fusionné en une seule chaîne — et liste le reste
    dans `additional_products`. Le système traite les produits UN PAR UN
    (jamais de recherche simultanée sur un terme composite) : sans cet
    avertissement, les produits supplémentaires disparaîtraient silencieusement.
    Voir [[buyer-search-fuzzy-match-safety-2026-08]]."""
    extras = payload.get("additional_products") or (state.get("extracted_entities") or {}).get("additional_products")
    if not extras or not isinstance(extras, (list, tuple)):
        return ""
    names = [str(p).strip() for p in extras if str(p or "").strip()]
    if not names:
        return ""
    joined = ", ".join(f"*{n}*" for n in names)
    return f"\n\n📝 _J'ai aussi noté {joined} — redites son nom une fois qu'on aura fini ici pour le chercher aussi._"


def resolve_quantity(
    payload: Dict[str, Any],
    stable_entities: Dict[str, Any],
    *,
    allow_stable_fallback: bool = True,
) -> Any:
    """Resolve quantity from payload → stable entities, normalizing key."""
    quantity = payload.get("quantity")
    if quantity in (None, "", 0) and allow_stable_fallback:
        quantity = stable_entities.get("quantity")
        if quantity not in (None, "", 0):
            payload["quantity"] = quantity
            payload["_auto_quantity_fill"] = True
    return quantity


def resolve_unit(
    payload: Dict[str, Any],
    stable_entities: Dict[str, Any],
) -> Optional[str]:
    """Resolve unit from payload → stable entities."""
    unit = (
        payload.get("unit")
        or stable_entities.get("unit")
    )
    if unit:
        payload.setdefault("unit", unit)
    return unit


# =====================================================================
# DRAFT MANAGEMENT
# =====================================================================

_EXPECTED_INPUT_FROM_FIELD = {
    "product": "PRODUCT",
    "quantity": "QUANTITY",
    "unit": "UNIT",
}

_DRAFT_FIELD_PAIRS = (
    ("product", None),
    ("quantity", None),
    ("unit", None),
)


def missing_draft_field(draft: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the first missing field name in a draft, or None."""
    if not draft or draft.get("__reset__"):
        return None
    for primary, fallback in _DRAFT_FIELD_PAIRS:
        value = draft.get(primary)
        if value in (None, "", 0, [], {}):
            alt = draft.get(fallback) if fallback else None
            if alt in (None, "", 0, [], {}):
                return primary
    return None


def draft_requires_completion(draft: Optional[Dict[str, Any]]) -> bool:
    return missing_draft_field(draft) is not None


def draft_expected_input(draft: Optional[Dict[str, Any]]) -> str:
    missing = missing_draft_field(draft)
    if not missing:
        return "NONE"
    return _EXPECTED_INPUT_FROM_FIELD.get(missing, "NONE")


def draft_block_response(draft: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a WAITING_INPUT state patch asking for the next missing draft field."""
    summary = CartDomainService.format_pending_draft(draft)
    prompt = summary or (
        "✏️ *Ajout en cours*. Indiquez le produit, la quantité et l'unité "
        "avant de confirmer la précommande."
    )
    return {
        "status": "WAITING_INPUT",
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": prompt,
        "expected_input": draft_expected_input(draft),
        "ag_ui_component": None,
    }


def capture_cart_draft(state: Dict[str, Any], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Capture partial slot data into a draft payload snapshot."""
    draft = dict(state.get("draft_payload") or {})
    changed = False
    for key in ("product", "quantity", "unit"):
        value = payload.get(key)
        if value in (None, "", 0, [], {}):
            continue
        if draft.get(key) != value:
            draft[key] = value
            changed = True
    if changed:
        return draft
    if draft and not draft.get("__reset__"):
        return draft
    return None


# =====================================================================
# STATE RESET HELPERS
# =====================================================================

def clear_active_goal(state: Dict[str, Any], *, clear_cart_snapshot: bool = False) -> Dict[str, Any]:
    """Return a working_memory patch with the goal/lock cleared."""
    working = dict(state.get("working_memory") or {})
    working["active_goal"] = None
    working["locked_intent"] = None
    if clear_cart_snapshot:
        # None-overwrite OBLIGATOIRE : `working_memory` est réduit par
        # `merge_dict`, donc RETIRER (pop) une clé du patch retourné ne la
        # supprime PAS de l'état — l'ancienne valeur est conservée au merge.
        # Avec le pop, l'instantané de panier survivait à l'annulation/
        # validation d'une précommande et pouvait RESSUSCITER un panier
        # abandonné (lu par flows/buyer/preorder.py via `last_active_cart`).
        working["last_active_cart"] = None
    return working


def error_response(message: str) -> Dict[str, Any]:
    """Standard error state patch."""
    return {
        "status": "ERROR",
        "validation_errors": ["missing_user_phone"],
        "response_strategy": "ERROR",
        "final_response": with_support_footer(message),
        "ag_ui_component": None,
    }


def phone_missing_error() -> Dict[str, Any]:
    return error_response("Numéro de téléphone introuvable, impossible de continuer.")


# =====================================================================
# MENU UTILITIES
# =====================================================================

PREORDER_ACTION_OPTIONS = [
    MenuOption(index="1", label="✅ Confirmer la commande", value="PREORDER_CONFIRM"),
    MenuOption(index="2", label="↩️ Annuler et revenir au panier", value="PREORDER_CANCEL"),
    MenuOption(index="3", label="➕ Ajouter d'autres produits", value="PREORDER_ADD_MORE"),
]

NEGOTIATION_ACTION_OPTIONS = [
    MenuOption(index="1", label="📥 Voir les offres reçues", value="NEGOTIATION_VIEW_OFFERS"),
    MenuOption(index="2", label="🔁 Proposer un autre prix", value="NEGOTIATION_COUNTER"),
    MenuOption(index="3", label="❌ Abandonner", value="NEGOTIATION_ABORT"),
]

CART_ACTION_KEYWORDS = {
    "precommander": "PREORDER",
    "précommander": "PREORDER",
    "precommande": "PREORDER",
    "précommande": "PREORDER",
    "confirmer": "PREORDER",
    "valider": "PREORDER",
    "ajouter": "ADD_MORE",
    "ajoute": "ADD_MORE",
    "ajouter encore": "ADD_MORE",
    "annuler": "CANCEL",
}


def resolve_menu_value_by_index(options: Sequence[MenuOption], index: Any) -> Optional[str]:
    """Find the value associated with a given menu index."""
    if index in (None, ""):
        return None
    idx_str = str(index)
    for opt in options:
        if opt.index == idx_str:
            return str(opt.value if opt.value is not None else opt.index)
    return None


def preorder_choice_from_index(index: Any) -> Optional[str]:
    return resolve_menu_value_by_index(PREORDER_ACTION_OPTIONS, index)


def negotiation_choice_from_index(index: Any) -> Optional[str]:
    return resolve_menu_value_by_index(NEGOTIATION_ACTION_OPTIONS, index)


def render_interactive_menu(options: Sequence[MenuOption], header: Optional[str] = None) -> str:
    """Build a numbered menu string for WhatsApp / AG-UI."""
    return render_numbered_menu(options, header=header)


def preorder_action_menu(preorder_id: str, **extra_meta: Any) -> MenuRequest:
    """Build the standard preorder actions menu."""
    return MenuRequest(
        title="Précommande créée",
        options=list(PREORDER_ACTION_OPTIONS),
        kind="preorder_action",
        metadata={"preorder_id": str(preorder_id), **extra_meta},
        preformatted_text=render_interactive_menu(PREORDER_ACTION_OPTIONS, "Choisissez une option :"),
    )


def negotiation_action_menu(auction_id: str, **extra_meta: Any) -> MenuRequest:
    """Build the standard negotiation actions menu."""
    return MenuRequest(
        title="Négociation",
        options=list(NEGOTIATION_ACTION_OPTIONS),
        kind="negotiation_action",
        metadata={"auction_id": str(auction_id), **extra_meta},
    )


def detect_cart_action(state: Dict[str, Any]) -> Optional[str]:
    """Détecte une action panier à partir du VERDICT DU LLM (pas de mots-clés).

    Avant : balayage par SOUS-CHAÎNE d'une liste figée de mots français
    (`CART_ACTION_KEYWORDS`) — « je ne veux pas annuler » et « surtout ne pas
    valider » déclenchaient donc ANNULER / VALIDER, la négation étant tout
    simplement ignorée. Rallonger la liste ne corrige rien : il faudrait
    modéliser la négation, les fautes de frappe et toutes les tournures.

    Le LLM produit déjà exactement ce signal (`interpreted_event` = CONFIRM /
    REJECT, en tenant compte de la négation et des formulations libres) : on
    s'appuie dessus. Le contexte (panier non vide, menu panier affiché) reste
    vérifié, lui, de façon déterministe.
    """
    expected = str(state.get("expected_input") or "").upper().strip() or "SELECTION"
    if expected not in {"SELECTION", "NONE", ""}:
        return None

    event = str(state.get("interpreted_event") or "").upper().strip()
    if event not in {"CONFIRM", "REJECT"}:
        return None

    cart = state.get("active_cart") or []
    working = state.get("working_memory") or {}
    mapping_kind = str(
        working.get("available_mapping_kind") or working.get("available_mapping_meta") or ""
    ).lower().strip()
    in_cart_context = mapping_kind in {"cart", "preorder_action"}

    if event == "REJECT":
        return "CANCEL" if in_cart_context else None

    # CONFIRM : valider le panier (précommander) si un panier existe.
    if cart:
        return "PREORDER"
    return "PREORDER" if in_cart_context else None


def read_only_intent(intent: Optional[str]) -> bool:
    return str(intent or "").upper() in READ_ONLY_INTENTS


__all__ = [
    "CART_GOALS", "PREORDER_GOALS", "NEGOTIATION_GOALS",
    "ORDER_TRACKING_GOALS", "AUCTION_TRACKING_GOALS", "READ_ONLY_INTENTS",
    "ESCALATE_KEYWORDS", "CONFIRM_KEYWORDS", "DECLINE_KEYWORDS",
    "infer_product_from_text", "resolve_product", "additional_products_hint", "resolve_quantity", "resolve_unit",
    "missing_draft_field", "draft_requires_completion", "draft_expected_input",
    "draft_block_response", "capture_cart_draft",
    "clear_active_goal", "error_response", "phone_missing_error",
    "PREORDER_ACTION_OPTIONS", "NEGOTIATION_ACTION_OPTIONS",
    "resolve_menu_value_by_index", "preorder_choice_from_index",
    "negotiation_choice_from_index", "render_interactive_menu",
    "preorder_action_menu", "negotiation_action_menu",
    "detect_cart_action", "read_only_intent",
    "SOURCE_TYPE_LABELS", "logger",
]
