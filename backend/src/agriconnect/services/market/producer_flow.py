"""Market — Producer Flow (résolveurs de contexte côté Producteur).

Contient le Context Resolver côté Producteur ainsi que les sous-résolveurs
spécialisés appelés via MCP :
  - `_resolve_auction`        : découverte proactive d'enchères à partir d'un produit
  - `_resolve_my_bids`        : portefeuille des bids actifs du producteur
  - `_resolve_bid`            : sélection d'un bid précis reçu pour acceptation
  - `_resolve_stock`          : identification d'un lot de stock
  - `_resolve_default_farm`   : auto-résolution de farm_id (1 ferme → autofill, N → menu)

Tous les helpers filtrent défensivement les `None` avant d'appeler FastMCP.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from agriconnect.graphs.agents.market_coach.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)

# Goals MCP qui exigent un farm_id en argument. Dérivé dynamiquement de
# INTENT_CONFIG : tout intent dont les `required` listent "farm_id" est
# candidat à l'auto-résolution. Aucun risque de fork de vocabulaire.
GOALS_NEEDING_FARM_ID = frozenset(
    intent_key
    for intent_key, cfg in INTENT_CONFIG.items()
    if "farm_id" in (cfg.get("required") or [])
)

logger = logging.getLogger("AgriConnect.Market.ProducerFlow")


# =====================================================================
# AUCTION DISCOVERY — Étape clef du PLACE_BID proactif
# =====================================================================

async def _resolve_auction(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Appelle `get_auctions` de manière proactive quand le produit est connu
    mais qu'aucun `auction_id` n'est encore résolu.
    """
    product = payload.get("product") or payload.get("product_name")
    zone = payload.get("zone_name") or payload.get("zone")

    kwargs: Dict[str, Any] = {"status": "OPEN", "view_mode": "MARKETPLACE"}
    if phone:
        kwargs["phone"] = str(phone)
    if product:
        kwargs["product_name"] = str(product)
    if zone:
        kwargs["zone_name"] = str(zone)

    logger.info("_resolve_auction: calling get_auctions with %s", kwargs)
    raw = await mc_runtime.call_db("get_auctions", **kwargs)
    result = ensure_dict(raw)
    
    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = result.get("message") or "Aucun marché disponible pour ce produit actuellement."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "available_mapping": {},
            "working_memory": {"available_mapping_kind": "auction"},
            "ag_ui_component": None,
        }

    mapping = result.get("mapping") or {}
    menu = result.get("formatted_menu") or "Marchés disponibles trouvés."
    
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
                "title": "Marchés disponibles",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "auction"},
            },
        },
    }


# =====================================================================
# BID PORTFOLIO — Suivi des bids émis par le producteur
# =====================================================================

async def _resolve_my_bids(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Récupère les offres/bids actifs émis par le producteur (CHECK_MY_BIDS)."""
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de continuer.",
            "ag_ui_component": None,
        }

    raw = await mc_runtime.call_db("get_my_active_bids", phone=str(phone))
    result = ensure_dict(raw)
    
    if not is_success_response(result):
        msg = result.get("message") or "Impossible de charger vos offres en cours."
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
            "final_response": "Vous n'avez aucune offre active pour le moment.",
            "available_mapping": {},
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    lines = ["📋 *Vos offres actives en cours :*"]
    for i, bid in enumerate(data, start=1):
        bid_id = str(bid.get("bid_id") or bid.get("id") or "")
        product_name = bid.get("product") or bid.get("product_name") or "?"
        price = bid.get("offered_price") or bid.get("price") or "?"
        status = bid.get("status") or "PENDING"
        lines.append(f"\n*{i}. {product_name}*\n💰 {price} CFA — Statut: {status}")
        mapping[str(i)] = bid_id

    menu = result.get("formatted_menu") or "\n".join(lines)
    candidates = [str(b.get("product") or "?") for b in data]
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": candidates,
        "available_mapping": mapping,
        "working_memory": {"available_mapping_kind": "bid", "bids_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": "Vos offres actives",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "bid"},
            },
        },
    }


# =====================================================================
# BID RESOLUTION — Offres reçues des acheteurs (ACCEPT_OFFER)
# =====================================================================

async def _resolve_bid(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Résout le `bid_id` d'une offre acheteur reçue pour acceptation ou traitement."""
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
            "final_response": "Aucune offre reçue sur vos marchés pour le moment.",
            "ag_ui_component": None,
        }

    idx = payload.get("selection_index")
    selected_value = payload.get("selected_value")

    # Si l'utilisateur a déjà choisi une option
    chosen = None
    if isinstance(idx, int) and 1 <= idx <= len(data):
        chosen = data[idx - 1] or {}
    elif selected_value:
        target = str(selected_value).strip().lower()
        chosen = next(
            (b for b in data if b and target in str(b.get("buyer_name") or "").lower()),
            None,
        )

    if chosen:
        bid_id = chosen.get("bid_id") or chosen.get("id")
        if not bid_id:
            return {
                "status": "ERROR",
                "validation_errors": ["bid_not_resolved"],
                "response_strategy": "ERROR",
                "final_response": "Désolé, l'identifiant technique de l'offre est manquant.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["bid_id"] = str(bid_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {"status": "PLANNING", "transaction_payload": new_payload, "ag_ui_component": None}

    # Sinon, on génère le catalogue de choix AG-UI complet
    mapping: Dict[str, str] = {}
    lines = ["📋 *Sélectionnez l'offre acheteur à accepter :*"]
    for i, b in enumerate(data, start=1):
        b_id = str(b.get("bid_id") or b.get("id") or "")
        buyer = b.get("buyer_name") or "Acheteur anonyme"
        product = b.get("product_name") or b.get("product") or "Produit"
        price = b.get("offered_price") or b.get("price") or "?"
        qty = b.get("quantity") or "?"
        lines.append(f"\n*{i}. {buyer}* pour *{product}*\n💰 {price} FCFA — Quantité: {qty}")
        mapping[str(i)] = b_id

    menu = "\n".join(lines)
    candidates = [f"{b.get('buyer_name', 'Acheteur')} ({b.get('product', 'Produit')})" for b in data]
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
                "title": "Offres acheteurs reçues",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "bid"},
            },
        },
    }


# =====================================================================
# FARM DEFAULT RESOLUTION — Auto-fill farm_id pour les intents qui l'exigent
# =====================================================================

async def _resolve_default_farm(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Résout `farm_id` automatiquement quand un seul existe, sinon propose un menu.

    Logique :
      - 0 ferme  → erreur (le producteur doit d'abord en créer une).
      - 1 ferme  → autofill silencieux dans `payload["farm_id"]`.
      - N fermes → ListMenu AG-UI (`expected_input=SELECTION`, `available_mapping`).

    Outil MCP appelé : `get_farms(producer_id=phone)`.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de résoudre votre exploitation.",
            "ag_ui_component": None,
        }

    raw = await mc_runtime.call_db("get_farms", phone=str(phone))
    farms = ensure_dict(raw).get("data") or []
    if not isinstance(farms, list):
        farms = []

    if not farms:
        return {
            "status": "ERROR",
            "validation_errors": ["no_farm_declared"],
            "response_strategy": "ERROR",
            "final_response": (
                "Vous n'avez pas encore déclaré d'exploitation. "
                "Dites-moi par exemple : « créer ma ferme Bagre 2ha sorgho »."
            ),
            "ag_ui_component": None,
        }

    if len(farms) == 1:
        chosen = farms[0]
        farm_id = chosen.get("id") or chosen.get("farm_id")
        if not farm_id:
            return {
                "status": "ERROR",
                "validation_errors": ["farm_id_unresolved"],
                "response_strategy": "ERROR",
                "final_response": "Erreur technique de résolution de votre exploitation.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["farm_id"] = str(farm_id)
        logger.info("[FarmAutofill] phone=%s → farm_id=%s (single farm)", phone, farm_id)
        return {"status": "PLANNING", "transaction_payload": new_payload, "ag_ui_component": None}

    # Plusieurs fermes : on tente la résolution par selection_index, sinon menu
    idx = payload.get("selection_index")
    if isinstance(idx, int) and 1 <= idx <= len(farms):
        chosen = farms[idx - 1]
        farm_id = chosen.get("id") or chosen.get("farm_id")
        if farm_id:
            new_payload = dict(payload)
            new_payload["farm_id"] = str(farm_id)
            new_payload.pop("selection_index", None)
            return {"status": "PLANNING", "transaction_payload": new_payload, "ag_ui_component": None}

    # Construction du menu AG-UI
    mapping: Dict[str, str] = {}
    candidates: List[str] = []
    lines = ["🌾 *Sur quelle exploitation enregistrer cette opération ?*"]
    for i, f in enumerate(farms, start=1):
        f_id = str(f.get("id") or f.get("farm_id") or "")
        name = f.get("name") or f"Ferme {i}"
        size = f.get("size") or f.get("surface_ha")
        suffix = f" ({size} ha)" if size else ""
        lines.append(f"{i}. *{name}*{suffix}")
        mapping[str(i)] = f_id
        candidates.append(f"{name}{suffix}")

    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": candidates,
        "available_mapping": mapping,
        "working_memory": {"available_mapping_kind": "farm"},
        "response_strategy": "SELECTION_MENU",
        "final_response": "\n".join(lines),
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": "Choisir l'exploitation",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "farm"},
            },
        },
    }


# =====================================================================
# STOCK RESOLUTION — Identification d'un lot précis
# =====================================================================

async def _resolve_stock(mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Résout le `stock_id` depuis le produit cible ou via une liste de choix."""
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible d'accéder au stock.",
            "ag_ui_component": None,
        }

    raw = await mc_runtime.call_db("get_producer_stocks", phone=str(phone))
    items = ensure_dict(raw).get("data") or []
    
    if not isinstance(items, list) or not items:
        return {
            "status": "ERROR",
            "validation_errors": ["empty_inventory"],
            "response_strategy": "ERROR",
            "final_response": "Votre inventaire de stock est actuellement vide.",
            "ag_ui_component": None,
        }

    product = payload.get("product")
    if product:
        matches = [
            it for it in items
            if str(it.get("item_name") or it.get("product_name") or "").lower() == str(product).strip().lower()
        ]
    else:
        matches = list(items)

    if not matches:
        return {
            "status": "ERROR",
            "validation_errors": ["product_not_in_stock"],
            "response_strategy": "ERROR",
            "final_response": f"Aucun lot trouvé dans votre stock pour : {product or 'ce produit'}.",
            "ag_ui_component": None,
        }

    # Sélection automatique ou manuelle
    chosen = None
    if len(matches) == 1:
        chosen = matches[0]
    else:
        idx = payload.get("selection_index")
        if isinstance(idx, int) and 1 <= idx <= len(matches):
            chosen = matches[idx - 1]

    if chosen:
        stock_id = chosen.get("stock_id") or chosen.get("id")
        if not stock_id:
            return {
                "status": "ERROR",
                "validation_errors": ["stock_not_resolved"],
                "response_strategy": "ERROR",
                "final_response": "Erreur technique de résolution du lot de stock.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["stock_id"] = str(stock_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {"status": "PLANNING", "transaction_payload": new_payload, "ag_ui_component": None}

    # Plus d'un lot disponible : construction du menu de sélection strict
    mapping: Dict[str, str] = {}
    lines = ["📋 *Plusieurs lots correspondent. Choisissez le bon numéro :*"]
    for i, m in enumerate(matches, start=1):
        s_id = str(m.get("stock_id") or m.get("id") or "")
        name = m.get("item_name") or m.get("product_name") or "Produit"
        qty = m.get("quantity") or "0"
        unit = m.get("unit") or "KG"
        lines.append(f"{i}. *{name}* — {qty} {unit}")
        mapping[str(i)] = s_id

    menu = "\n".join(lines)
    candidates = [f"{m.get('item_name')} ({m.get('quantity')} {m.get('unit', 'KG')})" for m in matches]
    return {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "expected_candidates": candidates,
        "available_mapping": mapping,
        "working_memory": {"available_mapping_kind": "stock", "stocks_menu": menu, "stocks_cache": matches},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": "Lots de stock disponibles",
                "options": [{"index": str(i), "label": c} for i, c in enumerate(candidates, start=1)],
                "metadata": {"kind": "stock"},
            },
        },
    }


# =====================================================================
# NODE 7 — CONTEXT RESOLVER (PRODUCER)
# =====================================================================

async def producer_context_resolver(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Nœud LangGraph : Convertit les expressions textuelles en IDs système."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")

    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, session impossible.",
            "ag_ui_component": None,
        }

    # 0. Auto-résolution `farm_id` pour les intents qui le requièrent (déclaratif).
    #    Doit s'exécuter EN PREMIER : les autres branches peuvent en dépendre.
    if goal in GOALS_NEEDING_FARM_ID and not payload.get("farm_id"):
        farm_resolution = await _resolve_default_farm(mc_runtime, str(phone), payload)
        # Si l'auto-fill a réussi, on continue le flow ; sinon on retourne le menu/erreur.
        if farm_resolution.get("status") != "PLANNING":
            return farm_resolution
        payload = farm_resolution.get("transaction_payload") or payload

    # 1. Découverte de marchés : producteur cherche un appel d'offres pour bidder
    if goal in {"SALES_PLACE_BID", "MARKET_GET_REQUESTS"} and not payload.get("auction_id"):
        return await _resolve_auction(mc_runtime, str(phone), payload)

    # 2. Consultation des propres propositions du producteur
    if goal == "MARKET_GET_MY_PROPOSALS":
        return await _resolve_my_bids(mc_runtime, str(phone), payload)

    # 3. Validation finale du contrat (cas où le producteur doit désigner un bid précis)
    if goal == "SALES_ACCEPT_CONTRACT" and not payload.get("bid_id"):
        return await _resolve_bid(mc_runtime, str(phone), payload)

    # 4. Gestion des stocks physiques (mouvements / ajustements / suppressions partielles)
    if goal in {"STOCK_ADJUST", "STOCK_REMOVE_PARTIAL", "STOCK_RECORD_MOVEMENT", "STOCK_DELETE"} and not payload.get("stock_id"):
        return await _resolve_stock(mc_runtime, str(phone), payload)

    # Fallback par défaut si toutes les informations sont résolues
    return {"status": "PLANNING", "ag_ui_component": None}


__all__ = [
    "_resolve_auction",
    "_resolve_my_bids",
    "_resolve_bid",
    "_resolve_stock",
    "producer_context_resolver",
]