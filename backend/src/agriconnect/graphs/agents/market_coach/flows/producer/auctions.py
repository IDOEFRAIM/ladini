"""Producer — Auctions & Bids (cycle complet côté PRODUCTEUR).

Machine à états déterministe, miroir du suivi acheteur (``order_tracking.py``) :

    parcourir les appels d'offres  →  choisir une enchère
        →  proposer un prix  →  soumettre l'offre (place_bid)  →  suivi

Zéro heuristique LLM ici : le routage repose sur ``current_goal``,
``expected_input`` et ``working_memory.available_mapping_kind``. Chaque
appel externe passe par ``AuctionGateway`` (typed) et est défensif.

Goals pris en charge :
  - ``MARKET_GET_REQUESTS``     : découverte des enchères (par catégorie).
  - ``SALES_PLACE_BID``         : sélection d'une enchère + dépôt d'offre.
  - ``MARKET_GET_MY_PROPOSALS`` : suivi de l'état de mes offres.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)

logger = logging.getLogger("AgriConnect.Market.ProducerFlow.Auctions")


# Mots-clés qui demandent la vue « tout le marché » plutôt que « mes catégories ».
_ALL_SCOPE_TOKENS = (
    "toutes", "tout", "tous", "marche", "marché", "autres", "autre", "elargir", "élargir",
)

# Kinds d'état AG-UI portés par ``available_mapping_kind``.
_KIND_PICK = "producer_auction_pick"   # menu d'enchères → sélection = choisir une enchère


# =====================================================================
# HELPERS
# =====================================================================

def _text_of(state: Dict[str, Any]) -> str:
    return str(state.get("normalized_text") or state.get("user_query") or "").strip()


def _wants_all_scope(state: Dict[str, Any]) -> bool:
    low = _text_of(state).lower()
    return any(tok in low for tok in _ALL_SCOPE_TOKENS)


def _extract_price(state: Dict[str, Any]) -> Optional[float]:
    """Récupère un prix depuis toutes les sources plausibles (résilience)."""
    payload = state.get("transaction_payload") or {}
    candidates = [
        payload.get("price"),
        payload.get("offered_price"),
        (state.get("extracted_entities") or {}).get("price"),
        (state.get("stable_entities") or {}).get("price"),
    ]
    for c in candidates:
        if c in (None, "", [], {}):
            continue
        try:
            val = float(c)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    # Fallback : premier nombre du texte (« 300 », « 300 fcfa », « 300f »).
    m = re.search(r"(\d+(?:[.,]\d+)?)", _text_of(state))
    if m:
        try:
            val = float(m.group(1).replace(",", "."))
            if val > 0:
                return val
        except ValueError:
            pass
    return None


def _resolve_selected_auction_id(state: Dict[str, Any]) -> Optional[str]:
    """Convertit une sélection (index ou valeur) en ``auction_id`` via le mapping."""
    payload = state.get("transaction_payload") or {}
    direct = payload.get("auction_id") or payload.get("selected_value")
    if direct and _looks_like_uuid(str(direct)):
        return str(direct)

    idx = payload.get("selection_index")
    if idx is None:
        idx = (state.get("extracted_entities") or {}).get("selection_index")
    if idx is None:
        raw = _text_of(state)
        if raw.isdigit():
            idx = raw
    if idx is not None:
        mapping = state.get("available_mapping") or {}
        resolved = mapping.get(str(idx))
        if resolved:
            return str(resolved)
    # selected_value non-UUID : peut être un auction_id direct malgré tout.
    if direct:
        return str(direct)
    return None


def _looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{16,36}", value or ""))


def _error(message: str) -> Dict[str, Any]:
    return {
        "status": "ERROR",
        "response_strategy": "ERROR",
        "final_response": message,
        "ag_ui_component": None,
    }


def _clear_pick_wm(state: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    wm = dict(state.get("working_memory") or {})
    wm.pop("available_mapping_kind", None)
    wm.pop("auction_menu", None)
    wm.update(extra)
    return wm


# =====================================================================
# 1. DISCOVERY — lister les enchères (par catégorie)
# =====================================================================

async def browse_auctions(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible de charger le marché.")

    scope = "ALL" if _wants_all_scope(state) else "MATCHABLE"
    payload = state.get("transaction_payload") or {}
    product = payload.get("product")

    gw = AuctionGateway(mc_runtime)
    result = await gw.get_producer_auctions(phone, scope=scope, product_name=product)

    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = result.get("message") or "Aucun appel d'offres ouvert pour le moment."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "available_mapping": {},
            "working_memory": _clear_pick_wm(state),
            "ag_ui_component": None,
        }

    mapping = result.get("mapping") or {}
    menu = result.get("formatted_menu") or "Appels d'offres disponibles."
    data = result.get("data") or []

    options = [
        MenuOption(
            index=str(i),
            label=f"{d.get('product')} — {d.get('max_price')} FCFA",
            value=mapping.get(str(i)),
        )
        for i, d in enumerate(data, start=1)
    ]

    wm = dict(state.get("working_memory") or {})
    wm["available_mapping_kind"] = _KIND_PICK
    wm["auction_scope"] = result.get("scope") or scope

    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "available_mapping": mapping,
        "expected_candidates": [str(d.get("product") or "") for d in data],
        "working_memory": wm,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Appels d'offres",
            options=options,
            kind="auction",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# 2. PICK + PRICE — choisir une enchère puis proposer un prix
# =====================================================================

async def ask_bid_price(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
) -> Dict[str, Any]:
    """Affiche le détail de l'enchère choisie et demande le prix proposé."""
    gw = AuctionGateway(mc_runtime)
    detail: Dict[str, Any] = {}
    try:
        detail = await gw.get_auction_bids(auction_id=str(auction_id))
    except Exception as exc:
        logger.warning("ask_bid_price: get_auction_bids failed: %s", exc)

    info = (detail or {}).get("auction") or {}
    product = info.get("product") or "ce produit"
    qty = info.get("quantity")
    unit = info.get("unit") or ""
    max_price = info.get("max_price")

    ctx_lines = [f"📦 *Enchère sélectionnée : {product}*"]
    if qty is not None:
        ctx_lines.append(f"⚖️ Quantité demandée : {qty:g} {unit}")
    if max_price is not None:
        ctx_lines.append(f"💰 Prix plafond acheteur : *{max_price:g} FCFA/{unit or 'unité'}*")
    ctx_lines.append("\n💬 *Quel prix proposez-vous ?* (par unité, en FCFA)")

    payload = dict(state.get("transaction_payload") or {})
    payload["auction_id"] = str(auction_id)
    payload.pop("selection_index", None)
    payload.pop("selected_value", None)

    wm = _clear_pick_wm(state, pending_bid_auction=str(auction_id))

    return {
        "status": "WAITING_INPUT",
        "expected_input": "PRICE",
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": "\n".join(ctx_lines),
        "current_goal": "SALES_PLACE_BID",
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def submit_bid(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    price: float,
) -> Dict[str, Any]:
    """Dépose l'offre (place_bid) et confirme au producteur."""
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible d'enregistrer votre offre.")

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.place_bid(auction_id=str(auction_id), phone=phone, offered_price=price)
    except Exception as exc:
        logger.exception("submit_bid: place_bid failed: %s", exc)
        return _error("Impossible d'enregistrer votre offre pour le moment. Réessayez dans un instant.")

    if not is_success_response(result):
        msg = result.get("message") or "Votre offre n'a pas pu être enregistrée."
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": msg,
            "working_memory": _clear_pick_wm(state, pending_bid_auction=None),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    price_txt = f"{float(price):g}"
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"✅ Votre offre de *{price_txt} FCFA* a été transmise à l'acheteur.\n\n"
            "📊 Tapez *mes offres* pour suivre son état (acceptée / en attente)."
        ),
        "working_memory": _clear_pick_wm(state, pending_bid_auction=None),
        "transaction_payload": {"__reset__": True},
        "ag_ui_component": None,
    }


# =====================================================================
# 3. TRACK — suivre l'état de mes offres
# =====================================================================

async def track_my_bids(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible de charger vos offres.")

    gw = AuctionGateway(mc_runtime)
    result = await gw.get_my_active_bids(phone)

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": result.get("message") or "Impossible de charger vos offres en cours.",
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not data:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "Vous n'avez encore fait aucune proposition.\n\n"
                "🛒 Tapez *voir les enchères* pour trouver des acheteurs."
            ),
            "ag_ui_component": None,
        }

    menu = result.get("formatted_menu") or "Vos offres."
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": menu,
        "ag_ui_component": None,
    }


# =====================================================================
# ORCHESTRATOR
# =====================================================================

async def producer_auction_resolver(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Aiguille le cycle enchères producteur selon le goal et l'état AG-UI."""
    goal = str(state.get("current_goal") or "").upper().strip()
    expected = str(state.get("expected_input") or "").upper().strip()
    working = state.get("working_memory") or {}
    mapping_kind = str(working.get("available_mapping_kind") or "").strip()
    payload = state.get("transaction_payload") or {}

    # (A) Suivi des offres émises.
    if goal == "MARKET_GET_MY_PROPOSALS":
        return await track_my_bids(state, mc_runtime)

    # (B) On attend un prix → tenter le dépôt de l'offre.
    auction_id = payload.get("auction_id") or working.get("pending_bid_auction")
    if auction_id and (expected == "PRICE" or goal == "SALES_PLACE_BID"):
        price = _extract_price(state)
        if price is not None:
            return await submit_bid(state, mc_runtime, str(auction_id), price)
        if expected == "PRICE":
            # Toujours en attente d'un prix valide : on re-demande sans repartir de zéro.
            return {
                "status": "WAITING_INPUT",
                "expected_input": "PRICE",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": "💬 Indiquez un *prix* valide en FCFA (ex: 300).",
                "ag_ui_component": None,
            }

    # (C) Sélection d'une enchère dans le menu → demander le prix.
    if mapping_kind == _KIND_PICK or payload.get("selection_index") is not None:
        picked = _resolve_selected_auction_id(state)
        if picked:
            # Prix déjà fourni en amont ? on dépose directement.
            price = _extract_price(state)
            if price is not None:
                return await submit_bid(state, mc_runtime, picked, price)
            return await ask_bid_price(state, mc_runtime, picked)

    # (D) Entrée par défaut : afficher les enchères.
    return await browse_auctions(state, mc_runtime)


__all__ = [
    "producer_auction_resolver",
    "browse_auctions",
    "ask_bid_price",
    "submit_bid",
    "track_my_bids",
]
