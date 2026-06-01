"""Market — Buyer Flow (CREATE_AUCTION, ACCEPT_BID).

Côté ACHETEUR du marché AgriConnect. Architecture miroir du producer_flow,
adaptée à la perspective de l'acheteur :

  - Création d'un appel d'offres : `CREATE_AUCTION` → outil MCP `create_auction`
    (pas de résolveur préalable nécessaire, le payload contient déjà tout).

  - Examen des bids reçus : `GET_AUCTIONS_BIDS` / `GET_AUCTION_BIDS` →
    récupère les offres déposées sur les enchères de l'acheteur, présente
    un menu numéroté et stocke le mapping dans `state["available_mapping"]`.

  - Sélection d'un bid gagnant : `ACCEPT_BID` / `ACCEPT_OFFER` / `SELECT_WINNING_BID`
    → après affichage du menu, résolution du `bid_id` via l'index ou la valeur.

Tous les appels MCP filtrent défensivement les `None`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)

logger = logging.getLogger("AgriConnect.Market.BuyerFlow")


# =====================================================================
# OWN AUCTIONS — Liste des appels d'offres lancés par l'acheteur
# =====================================================================

async def _resolve_own_auctions(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Récupère les enchères créées par l'acheteur (view_mode='MY_OWN').

    Permet à l'acheteur de naviguer dans ses propres appels d'offres pour
    consulter les offres reçues.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de continuer.",
            "ag_ui_component": None,
        }

    kwargs: Dict[str, Any] = {
        "phone": str(phone),
        "status": "OPEN",
        "view_mode": "MY_OWN",
    }
    product = payload.get("product") or payload.get("product_name")
    if product:
        kwargs["product_name"] = str(product)

    logger.info("_resolve_own_auctions: calling get_auctions with %s", kwargs)
    raw = await mc_runtime.call_db("get_auctions", **kwargs)
    result = ensure_dict(raw)
    
    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = result.get("message") or "Vous n'avez aucun appel d'offres ouvert pour l'instant."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "available_mapping": {},
            "working_memory": {"available_mapping_kind": "auction"},
            "ag_ui_component": None,
        }

    mapping = result.get("mapping") or {}
    menu = result.get("formatted_menu") or "Vos appels d'offres ouverts ont été trouvés."
    
    candidates = [str(d.get("product") or "Produit") for d in (result.get("data") or [])]
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": candidates,
        "available_mapping": mapping,
        "working_memory": {"available_mapping_kind": "auction", "auction_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": "Vos appels d'offres",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "auction"},
            },
        },
    }


# =====================================================================
# RECEIVED BIDS — Offres déposées sur les enchères de l'acheteur
# =====================================================================

async def _resolve_received_bids(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Récupère les bids déposés sur les enchères de l'acheteur."""
    kwargs: Dict[str, Any] = {"status": "OPEN"}
    if phone:
        kwargs["phone"] = str(phone)

    raw = await mc_runtime.call_db("get_auctions_bids", **kwargs)
    result = ensure_dict(raw)
    
    if not is_success_response(result):
        msg = result.get("message") or "Impossible de charger les offres reçues."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "available_mapping": {},
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not data:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "Aucune offre n'a encore été déposée sur vos enchères.",
            "available_mapping": {},
            "ag_ui_component": None,
        }

    # Construction du menu et du mapping côté acheteur (par fournisseur)
    mapping: Dict[str, str] = {}
    lines = ["📥 *Offres reçues sur vos enchères :*"]
    for i, bid in enumerate(data, start=1):
        bid_id = str(bid.get("bid_id") or bid.get("id") or "")
        producer_name = bid.get("producer_name") or bid.get("seller_name") or "Producteur"
        price = bid.get("offered_price") or bid.get("price") or "?"
        product_name = bid.get("product") or bid.get("product_name") or "?"
        status = bid.get("status") or "PENDING"
        lines.append(f"\n*{i}. {producer_name}* — {product_name}\n💰 {price} FCFA — Statut: {status}")
        mapping[str(i)] = bid_id

    menu = result.get("formatted_menu") or "\n".join(lines)
    candidates = [f"{b.get('producer_name') or b.get('seller_name') or 'Producteur'} ({b.get('product') or b.get('product_name') or 'Produit'})" for b in data]
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": candidates,
        "available_mapping": mapping,
        "working_memory": {"available_mapping_kind": "bid", "bids_menu": menu, "bids_cache": data},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": "Offres reçues",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "bid"},
            },
        },
    }


# =====================================================================
# BID PICK — Sélection d'un bid à accepter / désigner gagnant
# =====================================================================

async def _resolve_buyer_bid_pick(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Résout le `bid_id` requis pour l'acceptation à partir de l'index sélectionné."""
    if payload.get("bid_id"):
        return {"status": "PLANNING", "ag_ui_component": None}

    kwargs: Dict[str, Any] = {"status": "OPEN"}
    if phone:
        kwargs["phone"] = str(phone)

    raw = await mc_runtime.call_db("get_auctions_bids", **kwargs)
    data = ensure_dict(raw).get("data") or []

    if not isinstance(data, list) or not data:
        return {
            "status": "ERROR",
            "validation_errors": ["no_open_bids"],
            "response_strategy": "ERROR",
            "final_response": "Aucune offre disponible à accepter.",
            "ag_ui_component": None,
        }

    idx = payload.get("selection_index")
    selected_value = payload.get("selected_value")

    chosen = None
    if isinstance(idx, int) and 1 <= idx <= len(data):
        chosen = data[idx - 1] or {}
    elif selected_value:
        target = str(selected_value).strip().lower()
        chosen = next(
            (b for b in data if b and (target in str(b.get("producer_name") or "").lower() or target in str(b.get("seller_name") or "").lower())),
            None,
        )

    if chosen:
        bid_id = chosen.get("bid_id") or chosen.get("id")
        if not bid_id:
            return {
                "status": "ERROR",
                "validation_errors": ["bid_not_resolved"],
                "response_strategy": "ERROR",
                "final_response": "Identifiant de l'offre introuvable sur l'élément sélectionné.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["bid_id"] = str(bid_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {"status": "PLANNING", "transaction_payload": new_payload, "ag_ui_component": None}

    # Fallback : ré-affiche le catalogue si aucune sélection valide n'est interceptée
    return await _resolve_received_bids(mc_runtime, phone, payload)


# =====================================================================
# NODE 7 — CONTEXT RESOLVER (BUYER)
# =====================================================================

async def buyer_context_resolver(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Nœud LangGraph : Résout les expressions textuelles en IDs techniques pour l'Acheteur."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")

    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de continuer.",
            "ag_ui_component": None,
        }

    # Consultation brute des offres reçues sur ses marchés
    if goal == "MARKET_GET_REQUEST_DETAIL":
        return await _resolve_received_bids(mc_runtime, str(phone), payload)

    # Acceptation / Sélection finale d'une offre
    if goal in {"PROCUREMENT_ACCEPT_OFFER", "PROCUREMENT_SELECT_WINNER"} and not payload.get("bid_id"):
        return await _resolve_buyer_bid_pick(mc_runtime, str(phone), payload)

    # Par défaut, bascule sur la planification si aucun ID n'est manquant
    return {"status": "PLANNING", "ag_ui_component": None}


__all__ = [
    "_resolve_own_auctions",
    "_resolve_received_bids",
    "_resolve_buyer_bid_pick",
    "buyer_context_resolver",
]