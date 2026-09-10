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
import re as _re
from typing import Any, Dict, List, Optional

from ladini.core.formatting import fmt_num as _fmt_num
from ladini.domain.quantity_unit import (
    extract_unit_only_from_text as _extract_unit_only,
)
from ladini.domain.quantity_unit import (
    parse_quantity_unit_from_text as _parse_qty_unit,
)
from ladini.graphs.agents.market_coach.actions.common import load_entity_snapshot
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    resolve_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.services.domain.slot_enrichment import (
    extract_production_type_from_text as _extract_production_type,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    AuctionGateway,
    EscrowGateway,
    FarmGateway,
    OrderTrackingGateway,
    ProductGateway,
    StockGateway,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
    llm_deviation_reply,
)

# Goals MCP qui exigent un farm_id en argument. Dérivé dynamiquement de
# INTENT_CONFIG : tout intent dont les `required` listent "farm_id" est
# candidat à l'auto-résolution. Aucun risque de fork de vocabulaire.
GOALS_NEEDING_FARM_ID = frozenset(
    intent_key
    for intent_key, cfg in INTENT_CONFIG.items()
    if "farm_id" in (cfg.get("required") or [])
)

_STATEFUL_UPDATE_GOALS = frozenset(
    {
        "STOCK_ADJUST",
        "STOCK_REMOVE_PARTIAL",
        "STOCK_RECORD_MOVEMENT",
        "STOCK_DELETE",
    }
)

logger = logging.getLogger("Ladini.Market.ProducerFlow")


# =====================================================================
# AUCTION DISCOVERY — Étape clef du PLACE_BID proactif
# =====================================================================


async def _resolve_auction(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Appelle `get_auctions` de manière proactive quand le produit est connu
    mais qu'aucun `auction_id` n'est encore résolu.
    """
    product = payload.get("product")
    zone = payload.get("zone")

    kwargs: Dict[str, Any] = {"status": "OPEN", "view_mode": "MARKETPLACE"}
    if phone:
        kwargs["phone"] = str(phone)
    if product:
        kwargs["product_name"] = str(product)
    if zone:
        kwargs["zone_name"] = str(zone)

    logger.info("_resolve_auction: calling get_auctions with %s", kwargs)
    auction_gw = AuctionGateway(mc_runtime)
    result = await auction_gw.search_open_auctions(**kwargs)

    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = (
            result.get("message")
            or "Aucun marché disponible pour ce produit actuellement."
        )
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

    candidates = [
        str(d.get("product") or "Produit") for d in (result.get("data") or [])
    ]
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "working_memory": {"auction_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Marchés disponibles",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="auction",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# BID PORTFOLIO — Suivi des bids émis par le producteur
# =====================================================================


async def _resolve_my_bids(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Récupère les offres/bids actifs émis par le producteur (CHECK_MY_BIDS)."""
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de continuer.",
            "ag_ui_component": None,
        }

    auction_gw = AuctionGateway(mc_runtime)
    result = await auction_gw.get_my_active_bids(str(phone))

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
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "working_memory": {"bids_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Vos offres actives",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="bid",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# BID RESOLUTION — Offres reçues des acheteurs (ACCEPT_OFFER)
# =====================================================================


async def _resolve_bid(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout le `bid_id` d'une offre acheteur reçue pour acceptation ou traitement."""
    kwargs: Dict[str, Any] = {"status": "OPEN"}
    if phone:
        kwargs["phone"] = str(phone)

    auction_gw = AuctionGateway(mc_runtime)
    data = (await auction_gw.get_auctions_bids(**kwargs)).get("data") or []

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
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

    # Sinon, on génère le catalogue de choix AG-UI complet
    mapping: Dict[str, str] = {}
    lines = ["📋 *Sélectionnez l'offre acheteur à accepter :*"]
    for i, b in enumerate(data, start=1):
        b_id = str(b.get("bid_id") or b.get("id") or "")
        buyer = b.get("buyer_name") or "Acheteur anonyme"
        product = b.get("product_name") or b.get("product") or "Produit"
        price = b.get("offered_price") or b.get("price") or "?"
        qty = b.get("quantity") or "?"
        lines.append(
            f"\n*{i}. {buyer}* pour *{product}*\n💰 {price} FCFA — Quantité: {qty}"
        )
        mapping[str(i)] = b_id

    menu = "\n".join(lines)
    candidates = [
        f"{b.get('buyer_name', 'Acheteur')} ({b.get('product', 'Produit')})"
        for b in data
    ]
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "working_memory": {"bids_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Offres acheteurs reçues",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="bid",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# FARM DEFAULT RESOLUTION — Auto-fill farm_id pour les intents qui l'exigent
# =====================================================================


async def _resolve_default_farm(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    *,
    state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Résout `farm_id` automatiquement quand un seul existe, sinon propose un menu.

    Logique :
      - Short-circuit: check ``user_farms_cache`` in state before network call.
      - 0 ferme  → erreur (le producteur doit d'abord en créer une).
      - 1 ferme  → autofill silencieux dans ``payload["farm_id"]``.
      - N fermes → ListMenu AG-UI (``expected_input=SELECTION``, ``available_mapping``).

    Outil MCP appelé : ``get_farms(producer_id=phone)`` — only if cache miss.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de résoudre votre exploitation.",
            "ag_ui_component": None,
        }

    _st = state or {}
    cached_farms = _st.get("user_farms_cache")
    if isinstance(cached_farms, list) and cached_farms:
        farms = cached_farms
        logger.info("[FarmResolve] Using cached farms (%d entries)", len(farms))
    else:
        farm_gw = FarmGateway(mc_runtime)
        farms = await farm_gw.list_farms_alt(str(phone))

    if not farms:
        # On ne bloque plus ici avec une erreur.
        # On retourne PLANNING pour laisser le noeud `ensure_farm_node`
        # tenter une création automatique ou le validator gérer le manque.
        return {"status": "PLANNING", "transaction_payload": payload}

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
        logger.info(
            "[FarmAutofill] phone=%s → farm_id=%s (single farm)", phone, farm_id
        )
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

    # Plusieurs fermes : on tente la résolution par selection_index, sinon menu
    idx = payload.get("selection_index")
    if isinstance(idx, int) and 1 <= idx <= len(farms):
        chosen = farms[idx - 1]
        farm_id = chosen.get("id") or chosen.get("farm_id")
        if farm_id:
            new_payload = dict(payload)
            new_payload["farm_id"] = str(farm_id)
            new_payload.pop("selection_index", None)
            return {
                "status": "PLANNING",
                "transaction_payload": new_payload,
                "ag_ui_component": None,
            }

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

    farm_menu_text = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": farm_menu_text,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Choisir l'exploitation",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="farm",
            preformatted_text=farm_menu_text,
        ),
    }


# =====================================================================
# STOCK RESOLUTION — Identification d'un lot précis
# =====================================================================


async def _resolve_stock(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout le `stock_id` depuis le produit cible ou via une liste de choix."""
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible d'accéder au stock.",
            "ag_ui_component": None,
        }

    stock_gw = StockGateway(mc_runtime)
    items = (await stock_gw.get_producer_stocks(str(phone))).get("data") or []

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
            it
            for it in items
            if str(it.get("item_name") or it.get("product_name") or "").lower()
            == str(product).strip().lower()
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
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

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
    candidates = [
        f"{m.get('item_name')} ({m.get('quantity')} {m.get('unit', 'KG')})"
        for m in matches
    ]
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "working_memory": {"stocks_menu": menu},
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Lots de stock disponibles",
            options=[
                MenuOption(index=str(i), label=c, value=mapping.get(str(i)))
                for i, c in enumerate(candidates, start=1)
            ],
            kind="stock",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# CLÔTURE PAIEMENT-À-LA-LIVRAISON (2026-09-04)
# =====================================================================


async def _resolve_order_for_delivery_payment(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout QUELLE commande le producteur vise pour
    `confirm_delivery_and_payment` — jamais "la dernière commande" (mandat
    §20 : un message comme "c'est payé" ne doit jamais muter une commande
    choisie par un simple `ORDER BY created_at DESC`). Même gabarit exact
    que `_resolve_stock` ci-dessus : auto-sélection si un seul candidat
    ACTIONNABLE existe, résolution par `selection_index` sur une réponse à
    un menu déjà affiché, sinon un nouveau menu numéroté strict.

    Ne montre QUE les commandes réellement actionnables par cette action
    précise — `status=="CONFIRMED"` ET `payment_status=="PENDING"` (jamais
    une commande déjà `COMPLETED`, ni une commande escrow `ESCROWED`/
    `PAID_OUT`, qui ne relève JAMAIS de ce chemin, voir
    `services/database/producer.py::confirm_delivery_and_payment`).
    Réutilise `get_producer_orders` tel quel (déjà audité/corrigé) plutôt
    que d'ajouter un paramètre de filtre supplémentaire : le filtrage
    `payment_status` se fait ici, côté conversation, sur les données déjà
    renvoyées.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible d'accéder à vos commandes.",
            "ag_ui_component": None,
        }

    gw = OrderTrackingGateway(mc_runtime)
    result = await gw.get_producer_orders(phone=str(phone), status="CONFIRMED")
    all_orders = result.get("data") or []
    candidates = [
        o
        for o in all_orders
        if str(o.get("payment_status") or "").upper() == "PENDING"
    ]

    if not candidates:
        return {
            "status": "ERROR",
            "validation_errors": ["no_order_pending_payment_at_delivery"],
            "response_strategy": "ERROR",
            "final_response": (
                "Aucune commande confirmée n'est actuellement en attente de "
                "livraison/paiement à la livraison."
            ),
            "ag_ui_component": None,
        }

    chosen = None
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        idx = payload.get("selection_index")
        try:
            idx_int = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx_int = None
        if idx_int is not None and 1 <= idx_int <= len(candidates):
            chosen = candidates[idx_int - 1]

    if chosen:
        order_id = chosen.get("order_id")
        new_payload = dict(payload)
        new_payload["order_id"] = str(order_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

    # Plus d'une commande éligible : menu de sélection strict — jamais de
    # choix implicite.
    mapping: Dict[str, str] = {}
    lines = ["📦 *Plusieurs commandes en attente. Choisissez le bon numéro :*"]
    options: List[MenuOption] = []
    for i, o in enumerate(candidates, start=1):
        ref = o.get("reference") or str(o.get("order_id") or "")[:8].upper()
        amount = o.get("total_amount")
        currency = o.get("currency") or "XOF"
        buyer = o.get("buyer_name") or "Acheteur"
        label = f"#{ref} — {amount} {currency} ({buyer})"
        lines.append(f"{i}. {label}")
        mapping[str(i)] = str(o.get("order_id"))
        options.append(MenuOption(index=str(i), label=label, value=mapping[str(i)]))

    menu = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Commandes en attente de livraison/paiement",
            options=options,
            kind="order_delivery_payment",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# ANNULATION PRODUCTEUR D'UNE COMMANDE CONFIRMÉE (2026-09-04, Phase 5)
# =====================================================================


async def _resolve_order_for_cancellation(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout QUELLE commande le producteur veut annuler.

    Même gabarit exact que `_resolve_order_for_delivery_payment` (dont ce
    flux est le pendant « je ne peux pas honorer ») : auto-sélection s'il
    n'y a qu'une commande annulable, résolution par `selection_index` sur
    une réponse à un menu déjà affiché, sinon menu numéroté strict —
    jamais de choix implicite sur une action destructrice. La confirmation
    explicite est ensuite assurée par le `confirmation_gate` générique.

    Candidates = exactement les mêmes que pour la clôture livraison/
    paiement (`status=="CONFIRMED"` ET `payment_status=="PENDING"`) : une
    commande escrow ne relève jamais de ce chemin, et une commande déjà
    `COMPLETED`/`CANCELLED` n'est plus annulable.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible d'accéder à vos commandes.",
            "ag_ui_component": None,
        }

    gw = OrderTrackingGateway(mc_runtime)
    result = await gw.get_producer_orders(phone=str(phone), status="CONFIRMED")
    all_orders = result.get("data") or []
    candidates = [
        o
        for o in all_orders
        if str(o.get("payment_status") or "").upper() == "PENDING"
    ]

    if not candidates:
        return {
            "status": "ERROR",
            "validation_errors": ["no_cancellable_order"],
            "response_strategy": "ERROR",
            "final_response": (
                "Aucune commande confirmée en attente de livraison — il n'y a "
                "rien à annuler."
            ),
            "ag_ui_component": None,
        }

    chosen = None
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        idx = payload.get("selection_index")
        try:
            idx_int = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx_int = None
        if idx_int is not None and 1 <= idx_int <= len(candidates):
            chosen = candidates[idx_int - 1]

    if chosen:
        new_payload = dict(payload)
        new_payload["order_id"] = str(chosen.get("order_id"))
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    options: List[MenuOption] = []
    lines = ["❌ *Quelle commande souhaitez-vous annuler ? Indiquez le numéro :*"]
    for i, o in enumerate(candidates, start=1):
        ref = o.get("reference") or str(o.get("order_id") or "")[:8].upper()
        amount = o.get("total_amount")
        currency = o.get("currency") or "XOF"
        buyer = o.get("buyer_name") or "Acheteur"
        label = f"#{ref} — {amount} {currency} ({buyer})"
        lines.append(f"{i}. {label}")
        mapping[str(i)] = str(o.get("order_id"))
        options.append(MenuOption(index=str(i), label=label, value=mapping[str(i)]))

    menu = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "available_mapping": mapping,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Commandes annulables",
            options=options,
            kind="order_cancellation",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# RETRAIT D'UN PRODUIT DU CATALOGUE (2026-09-04)
# =====================================================================


async def _resolve_product_for_unpublish(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout QUEL produit le producteur veut retirer de son catalogue.

    Même gabarit exact que `_resolve_stock` /
    `_resolve_order_for_delivery_payment` : auto-sélection s'il n'y a qu'un
    seul produit, résolution par `selection_index` sur une réponse à un menu
    déjà affiché, sinon menu numéroté strict — jamais de choix implicite
    ("mon dernier produit") sur une action destructrice. La confirmation
    explicite est assurée ensuite par `confirmation_gate` générique, et
    toute la règle métier (refus si commandes actives, archivage doux si
    historique, suppression physique sinon) reste dans
    `services/database/product.py::delete_product`.
    """
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible d'accéder à votre catalogue.",
            "ag_ui_component": None,
        }

    try:
        result = await ProductGateway(mc_runtime).get_my_products(str(phone))
    except Exception as exc:
        logger.error("SALES_UNPUBLISH_PRODUCT: get_my_products a échoué: %s", exc)
        return {
            "status": "ERROR",
            "validation_errors": ["catalog_unavailable"],
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger votre catalogue pour le moment. Réessayez dans un instant.",
            "ag_ui_component": None,
        }

    raw_items = (result or {}).get("data") or []
    # `get_my_products` est une lecture PARTAGÉE (menu de modification,
    # historique) : elle renvoie volontairement TOUT, y compris les produits
    # déjà archivés (`is_available=False`, cf. `delete_product`) et les
    # produits fantômes créés par `record_sale` pour ancrer une vente
    # directe. On filtre donc ici, côté conversation — jamais en modifiant la
    # lecture partagée — pour ne proposer que ce qui est réellement retirable
    # (même approche que `_resolve_order_for_delivery_payment`).
    items = [
        it
        for it in raw_items
        if isinstance(it, dict) and it.get("is_available", True)
    ] if isinstance(raw_items, list) else []
    if not items:
        return {
            "status": "ERROR",
            "validation_errors": ["empty_catalog"],
            "response_strategy": "ERROR",
            "final_response": "Aucun produit publié dans votre catalogue — il n'y a rien à retirer.",
            "ag_ui_component": None,
        }

    chosen = None
    if len(items) == 1:
        chosen = items[0]
    else:
        idx = payload.get("selection_index")
        try:
            idx_int = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx_int = None
        if idx_int is not None and 1 <= idx_int <= len(items):
            chosen = items[idx_int - 1]

    if chosen:
        product_id = chosen.get("id") or chosen.get("product_id")
        if not product_id:
            return {
                "status": "ERROR",
                "validation_errors": ["product_not_resolved"],
                "response_strategy": "ERROR",
                "final_response": "Erreur technique de résolution du produit.",
                "ag_ui_component": None,
            }
        new_payload = dict(payload)
        new_payload["product_id"] = str(product_id)
        new_payload.pop("selection_index", None)
        new_payload.pop("selected_value", None)
        return {
            "status": "PLANNING",
            "transaction_payload": new_payload,
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    options: List[MenuOption] = []
    lines = ["🗑️ *Quel produit souhaitez-vous retirer ? Indiquez le numéro :*"]
    for i, it in enumerate(items, start=1):
        pid = str(it.get("id") or it.get("product_id") or "")
        if not pid:
            continue
        name = it.get("name") or "Produit"
        qty = it.get("quantity_for_sale") or it.get("quantity")
        unit = str(it.get("unit") or "KG").upper()
        qty_str = f" — {qty} {unit}" if qty not in (None, "") else ""
        label = f"{name}{qty_str}"
        lines.append(f"{i}. *{name}*{qty_str}")
        mapping[str(i)] = pid
        options.append(MenuOption(index=str(i), label=label, value=pid))

    menu = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "available_mapping": mapping,
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Produits de mon catalogue",
            options=options,
            kind="catalog_product",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# PARSING DÉTERMINISTE DES CORRECTIONS (mise à jour catalogue/production)
# =====================================================================
# Ne JAMAIS réutiliser le slot générique `product` (rempli par le pipeline
# d'entités pour IDENTIFIER de quoi on parle, pas pour le RENOMMER) — sans
# ça "le kg d'oignon coûte 225 fcfa" faisait passer "oignon" comme NOUVEAU
# nom (bug vécu). On exige un signal explicite de renommage ("nom X",
# "renomme en X", "s'appelle X") avant d'accepter un changement de nom, et on
# parse tout le reste (prix/quantité/date/type) depuis le TEXTE BRUT, pas
# depuis les entités génériques du pipeline (qui avaient aussi halluciné une
# quantité=1 pour "le kg d'oignon...").

# Oui/non/annuler ne sont PAS détectés par mots-clés ici : les utilisateurs
# tapent des variantes infinies, avec fautes de frappe, tournures locales,
# politesse... Impossible à énumérer de façon fiable. On réutilise
# `interpreted_event` (CONFIRM/REJECT), déjà calculé en amont par
# `input_interpreter` via le LLM — c'est le SEUL endroit du pipeline dont le
# rôle est justement de classifier ce type de réponse, quelle que soit sa
# formulation. Seule l'extraction de champs (prix/quantité/nom/date) reste
# déterministe ici : c'est de l'extraction de données structurées (formats
# connus), pas de la classification de sentiment.

_PRICE_RE = _re.compile(
    r"(?:prix|co[uû]te?|coute|vaut)\D{0,12}?(\d+(?:[.,]\d+)?)"
    r"|(\d+(?:[.,]\d+)?)\s*(?:fcfa|f\s*cfa|francs?)\b",
    _re.IGNORECASE,
)
# Fallback pour "quantité 500" (nombre nu, sans mot d'unité reconnu qui suit —
# _parse_qty_unit exige une vraie unité et renvoie None sinon).
_BARE_QUANTITY_RE = _re.compile(r"quantit\w*\D{0,10}?(\d+(?:[.,]\d+)?)", _re.IGNORECASE)
_DATE_RE = _re.compile(r"(\d{4}-\d{2}-\d{2})")
# Complète _DATE_RE (ISO strict) pour une date en français ("13 décembre
# 2026", "13/12/2026") — sinon une correction de date en langage naturel était
# silencieusement ignorée (aucun champ extrait, l'utilisateur croyait avoir
# fourni la date alors qu'elle n'était jamais retenue).
_MONTHS_FR = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
_DATE_FR_RE = _re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS_FR) + r")\s+(\d{4})\b",
    _re.IGNORECASE,
)
_DATE_SLASH_RE = _re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
# Déclencheur de renommage EXPLICITE uniquement — jamais une simple mention du
# produit dans la phrase. Capture au plus 2 mots après le déclencheur, en
# s'arrêtant au premier mot de liaison/champ (ex: "et", "quantité", "prix") —
# empêche "le nom c'est mais et la quantité est 3632 kg" de capturer
# "c est mais et" au lieu de "mais".
_NAME_TRIGGER_RE = _re.compile(
    r"\bnom\b\s*(?:c'?est|c\s+est|est|:)?\s*", _re.IGNORECASE
)
_NAME_STOPWORDS = frozenset(
    {
        "et",
        "la",
        "le",
        "les",
        "un",
        "une",
        "des",
        "de",
        "du",
        "quantite",
        "quantité",
        "prix",
        "date",
        "unite",
        "unité",
        "type",
    }
)


def _extract_price_correction(text: str) -> Optional[float]:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        val = float(raw.replace(",", "."))
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_quantity_correction(text: str) -> tuple[Optional[float], Optional[str]]:
    """Retourne (quantity, unit). N'accepte la quantité QUE si une vraie unité
    a été reconnue par _parse_qty_unit — sinon "225 fcfa" (prix) serait
    confondu avec une quantité=225 (le parseur générique ne rejette pas un mot
    de fin non reconnu comme unité, il ignore juste l'unité)."""
    qty_result = _parse_qty_unit(text)
    if qty_result and qty_result.quantity is not None and qty_result.unit:
        return qty_result.quantity, qty_result.unit
    m = _BARE_QUANTITY_RE.search(text)
    if m:
        try:
            val = float(m.group(1).replace(",", "."))
            if val > 0:
                return val, None
        except (TypeError, ValueError):
            pass
    return None, None


def _extract_name_correction(text: str) -> Optional[str]:
    m = _NAME_TRIGGER_RE.search(text)
    if not m:
        return None
    tokens = _re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ'\-]+", text[m.end() :], _re.IGNORECASE)
    picked: List[str] = []
    for tok in tokens:
        if tok.lower() in _NAME_STOPWORDS:
            break
        picked.append(tok)
        if len(picked) >= 2:
            break
    return " ".join(picked) if picked else None


def _extract_date_correction(text: str) -> Optional[str]:
    m = _DATE_RE.search(text)
    if m:
        return m.group(1)
    m = _DATE_FR_RE.search(text)
    if m:
        day, month_name, year = m.groups()
        month = _MONTHS_FR.get(month_name.lower())
        if month:
            return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    m = _DATE_SLASH_RE.search(text)
    if m:
        day, month, year = m.groups()
        try:
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            pass
    return None


# Code de livraison escrow (Paydunya) : 4 chiffres exactement. Priorité au
# mot déclencheur "code" pour ne pas confondre avec un autre nombre à 4
# chiffres présent par hasard dans le message (ex: une quantité, une année) ;
# on retombe sur un nombre nu à 4 chiffres seulement si aucun mot-clé n'est
# trouvé — ce tunnel n'a aucun autre champ numérique avec lequel collisionner.
_OTP_CODE_RE = _re.compile(r"\bcode\D{0,8}?(\d{4})\b", _re.IGNORECASE)
_BARE_OTP_RE = _re.compile(r"\b(\d{4})\b")


def _extract_otp_code(text: str) -> Optional[str]:
    m = _OTP_CODE_RE.search(text)
    if m:
        return m.group(1)
    m = _BARE_OTP_RE.search(text)
    if m:
        return m.group(1)
    return None


def _parse_update_correction(text: str, *, allow_type_date: bool) -> Dict[str, Any]:
    """Extrait déterministiquement les champs mentionnés dans *text*.

    Utilisé à la fois en phase COLLECT (première saisie) et en phase CONFIRM
    (pour distinguer une VRAIE correction d'un simple "non" d'annulation).
    """
    fields: Dict[str, Any] = {}
    quantity, unit = _extract_quantity_correction(text)
    if quantity is not None:
        fields["quantity"] = quantity
        if unit:
            fields["unit"] = unit
    price = _extract_price_correction(text)
    if price is not None:
        fields["price"] = price
        # (2026-09-09) Incident réel : "non c'est 3500 FCFA PAR UNITE" ne
        # changeait jamais le suffixe du récap (resté "/KG") — cette
        # fonction n'extrayait qu'un NOMBRE de prix, jamais l'unité DE PRIX
        # elle-même quand elle accompagne le prix sans quantité ("500 kg"
        # aurait déjà été capté par `_extract_quantity_correction` ci-dessus
        # ; ici on ne comble QUE le cas où l'utilisateur reprécise la base
        # du prix seule, ex: "par unité"/"à l'unité"/"le kg"). `unit` est le
        # SEUL champ que `_format_pending_recap` affiche (prix ET quantité
        # partagent le même suffixe dans ce récap) — ne pas écraser une
        # unité déjà extraite par la quantité dans CE MÊME message.
        if "unit" not in fields:
            price_unit = _extract_unit_only(text)
            if price_unit:
                fields["unit"] = price_unit
    name = _extract_name_correction(text)
    if name:
        fields["product"] = name
    if allow_type_date:
        date = _extract_date_correction(text)
        if date:
            fields["estimated_available_at"] = date
        ptype = _extract_production_type(text)
        if ptype:
            fields["production_type"] = ptype
    return fields


# =====================================================================
# RÉCAP GÉNÉRIQUE — utilisé par les deux flux de mise à jour (cycle/produit)
# =====================================================================


def _format_pending_recap(pending: Dict[str, Any], *, noun: str) -> str:
    lines = [f"📝 *Récapitulatif de la modification ({noun})*"]
    if pending.get("product"):
        lines.append(f"- Nouveau nom : {pending['product']}")
    unit_disp = str(pending.get("unit") or "KG").upper()
    if pending.get("price") is not None:
        lines.append(f"- Nouveau prix : {_fmt_num(pending['price'])} FCFA/{unit_disp}")
    if pending.get("quantity") is not None:
        lines.append(
            f"- Nouvelle quantité : {_fmt_num(pending['quantity'])} {unit_disp}"
        )
    if (
        pending.get("unit")
        and pending.get("price") is None
        and pending.get("quantity") is None
    ):
        lines.append(f"- Nouvelle unité : {unit_disp}")
    if pending.get("estimated_available_at"):
        lines.append(
            f"- Nouvelle date de disponibilité : {pending['estimated_available_at']}"
        )
    if pending.get("production_type"):
        lines.append(f"- Nouveau type : {pending['production_type']}")
    lines.append(
        "\n👉 Répondez *oui* pour confirmer, envoyez une *correction* "
        "(ex: « prix 300 », « nom mil »), ou *non* pour annuler."
    )
    return "\n".join(lines)


# =====================================================================
# MISE À JOUR D'UNE PRODUCTION FUTURE (MarketOffer) — flux dédié isolé,
# auto-suffisant (SELECT → COLLECT → CONFIRM → ÉCRITURE), résilient aux
# corrections/rejets — cf. producer_auction_resolver (auctions.py) pour le
# même principe éprouvé côté enchères. Bypasse confirmation_gate/executor
# (goal câblé sur le tunnel "producer_update", voir core/goals.py) car ceux-ci
# font un reset générique total sur REJECT, incapables de distinguer une
# annulation d'une correction ("non, en fait c'est 973 kg").
# =====================================================================


async def _resolve_cycle_for_update(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    working: Dict[str, Any],
    text: str,
    event: str,
) -> Dict[str, Any]:
    if not phone:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de modifier une production.",
            "ag_ui_component": None,
        }

    phase = str(working.get("update_phase") or "").upper()
    cycle_id = payload.get("cycle_id") or working.get("update_cycle_id")
    pending: Dict[str, Any] = dict(working.get("update_pending") or {})

    def _clear_wm() -> Dict[str, Any]:
        return {
            **working,
            "update_cycle_id": None,
            "update_phase": None,
            "update_pending": None,
            "active_goal": None,
            "available_mapping_kind": None,
        }

    # ── CONFIRM : recap déjà affiché, on attend oui/correction/non ────
    if phase == "CONFIRM" and cycle_id and pending:
        correction = _parse_update_correction(text, allow_type_date=True)
        if correction:
            pending.update(correction)
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
                "response_strategy": "ASK_MISSING_FIELD",
                "current_goal": "SALES_UPDATE_PRODUCTION",
                "final_response": _format_pending_recap(pending, noun="lot"),
                "working_memory": {
                    **working,
                    "update_cycle_id": str(cycle_id),
                    "update_phase": "CONFIRM",
                    "update_pending": pending,
                    "active_goal": "SALES_UPDATE_PRODUCTION",
                },
                "ag_ui_component": None,
            }
        # Oui/non : classification déjà faite en amont par le LLM
        # (input_interpreter), pas de liste de mots-clés à maintenir ici.
        if event == "CONFIRM":
            gw_fields = dict(pending)
            gw_fields.pop("product", None)
            if "product" in pending:
                gw_fields["product_label"] = pending["product"]
            try:
                result = await StockGateway(mc_runtime).update_production(
                    str(phone), str(cycle_id), **gw_fields
                )
            except Exception as exc:
                logger.error(
                    "SALES_UPDATE_PRODUCTION: update_production a échoué: %s", exc
                )
                return {
                    "status": "COMPLETED",
                    "response_strategy": "ERROR",
                    "final_response": "Impossible d'enregistrer la modification pour le moment. Réessayez dans un instant.",
                    "working_memory": _clear_wm(),
                    "ag_ui_component": None,
                }
            if not is_success_response(result):
                return {
                    "status": "COMPLETED",
                    "response_strategy": "ERROR",
                    "final_response": (result or {}).get("message")
                    or "La modification n'a pas pu être enregistrée.",
                    "working_memory": _clear_wm(),
                    "ag_ui_component": None,
                }
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": (result or {}).get("message")
                or "✅ Production mise à jour.",
                "working_memory": _clear_wm(),
                "transaction_payload": {"__reset__": True},
                "ag_ui_component": None,
            }
        if event == "REJECT":
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": "❌ Modification annulée. Rien n'a été changé.",
                "working_memory": _clear_wm(),
                "ag_ui_component": None,
            }
        # Ni correction, ni CONFIRM, ni REJECT clair : on ré-affiche le récap
        # sans rien perdre, plutôt que de deviner via des mots-clés — mais on
        # accuse d'abord réception via le LLM (chantier résilience 2026-08 :
        # ce tunnel auto-suffisant posait son final_response directement,
        # court-circuitant l'adaptivité générique de ask.py/feedback.py qui
        # renvoie ce final_response précalculé tel quel).
        recap_text = _format_pending_recap(pending, noun="lot")
        note = await llm_deviation_reply(
            mc_runtime, text, f"un récapitulatif de modification à confirmer :\n{recap_text}",
            extra_instructions=(
                "Le récapitulatif ci-dessus reflète déjà les valeurs les plus "
                "récentes — accuse juste réception brièvement, ne demande pas "
                "de reformuler et ne redemande pas oui/non toi-même."
            ),
        )
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": "SALES_UPDATE_PRODUCTION",
            "final_response": f"{note}\n\n{recap_text}" if note else recap_text,
            "working_memory": {
                **working,
                "update_cycle_id": str(cycle_id),
                "update_phase": "CONFIRM",
                "update_pending": pending,
                "active_goal": "SALES_UPDATE_PRODUCTION",
            },
            "ag_ui_component": None,
        }

    # ── COLLECT : lot choisi, on attend au moins un champ à modifier ──
    if cycle_id:
        correction = _parse_update_correction(text, allow_type_date=True)
        if not correction:
            base_question = (
                "✏️ Que souhaitez-vous modifier sur ce lot ?\n"
                "Ex : « prix 400 », « nom maïs », « quantité 500 », « date 2026-12-31 »."
            )
            # Chantier résilience 2026-08 : uniquement sur une vraie déviation
            # (event UNKNOWN/OUT_OF_SCOPE) — ce bloc est AUSSI atteint sur
            # l'entrée fraîche juste après la sélection du lot (phase SELECT
            # -> COLLECT, `text` = l'index tapé), où appeler le LLM serait
            # inutile/à côté de la plaque. Même garde que
            # nodes/rendering/ask.py::render_ask_missing_field.
            note = None
            if event in {"UNKNOWN", "OUT_OF_SCOPE"} and text.strip():
                note = await llm_deviation_reply(
                    mc_runtime, text, "répondre à : quel champ modifier sur ce lot (prix/nom/quantité/date) ?",
                )
            return {
                # "PRODUCT" est un slot SOFT (tunnel_manager.py) : n'importe
                # quel NEW_TASK à confiance suffisante (ex: une correction
                # riche qui ressemble à une déclaration — "le nom c'est mil et
                # la quantité est 2243 kg, dispo le 13 décembre" — se lit
                # facilement comme DECLARE_CROP_CYCLE) pouvait faire dérailler
                # ce tunnel auto-suffisant vers un tout autre goal, perdant la
                # correction que _parse_update_correction sait pourtant bien
                # traiter. "UPDATE_FIELD" n'est reconnu par aucun slot
                # SOFT/HARD → tunnel_manager verrouille par défaut ; seuls les
                # goals de navigation critiques (mes commandes, etc.) peuvent
                # encore s'échapper.
                "status": "WAITING_INPUT",
                **set_pending_interaction(
                    InteractionKind.ENTER_FIELD,
                    field_name="update_field",
                    goal="SALES_UPDATE_PRODUCTION",
                ),
                "response_strategy": "ASK_MISSING_FIELD",
                "current_goal": "SALES_UPDATE_PRODUCTION",
                "working_memory": {
                    **working,
                    "update_cycle_id": str(cycle_id),
                    "update_phase": "COLLECT",
                    "active_goal": "SALES_UPDATE_PRODUCTION",
                    "available_mapping_kind": None,
                },
                "final_response": f"{note}\n\n{base_question}" if note else base_question,
                "ag_ui_component": None,
            }
        pending.update(correction)
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": "SALES_UPDATE_PRODUCTION",
            "final_response": _format_pending_recap(pending, noun="lot"),
            "working_memory": {
                **working,
                "update_cycle_id": str(cycle_id),
                "update_phase": "CONFIRM",
                "update_pending": pending,
                "active_goal": "SALES_UPDATE_PRODUCTION",
                "available_mapping_kind": None,
            },
            "ag_ui_component": None,
        }

    # ── SELECT : aucun lot choisi → liste des productions ─────────────
    # NE JAMAIS confondre une erreur technique (outil MCP indisponible, panne
    # transitoire) avec une liste réellement vide — sinon l'utilisateur reçoit
    # "aucune production" alors que le vrai problème est une erreur backend
    # (bug vécu : le service MCP HTTP tournait sur un code plus ancien que le
    # nouvel outil list_producer_productions → refus silencieux interprété à
    # tort comme "liste vide").
    try:
        result = await StockGateway(mc_runtime).list_productions(str(phone))
    except Exception as exc:
        logger.error("SALES_UPDATE_PRODUCTION: list_productions a échoué: %s", exc)
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger vos productions pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }
    if str((result or {}).get("status") or "").lower() not in ("success", "ok"):
        logger.error(
            "SALES_UPDATE_PRODUCTION: list_productions status=%s message=%s",
            (result or {}).get("status"),
            (result or {}).get("message"),
        )
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger vos productions pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }
    items = (result or {}).get("data") or []
    if not isinstance(items, list) or not items:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "Vous n'avez aucune production future à modifier pour le moment.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    options: List[MenuOption] = []
    lines = ["✏️ *Quel lot souhaitez-vous modifier ? Indiquez le numéro :*"]
    for i, it in enumerate(items, start=1):
        cid = str(it.get("cycle_id") or "")
        if not cid:
            continue
        name = it.get("product_label") or "Production"
        qty = it.get("quantity")
        unit = str(it.get("unit") or "KG").upper()
        price = it.get("price")
        qty_str = f" — {qty} {unit}" if qty not in (None, "") else ""
        price_str = f" — {price} FCFA/{unit}" if price not in (None, "") else ""
        lines.append(f"{i}. *{name}*{qty_str}{price_str}")
        mapping[str(i)] = cid
        options.append(MenuOption(index=str(i), label=f"{name}{qty_str}", value=cid))

    menu = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "current_goal": "SALES_UPDATE_PRODUCTION",
        "final_response": menu,
        "available_mapping": mapping,
        "working_memory": {
            **working,
            "active_goal": "SALES_UPDATE_PRODUCTION",
            "available_mapping_kind": "cycle",
            "update_phase": "SELECT",
            "update_pending": None,
        },
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Productions à modifier",
            options=options,
            kind="cycle",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# MISE À JOUR D'UN PRODUIT CATALOGUE — flux dédié isolé, symétrique au
# flux cycles ci-dessus. Même principe auto-suffisant (SELECT → COLLECT →
# CONFIRM → ÉCRITURE), résilient aux corrections/rejets.
# =====================================================================


async def _resolve_product_for_update(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    working: Dict[str, Any],
    text: str,
    event: str,
) -> Dict[str, Any]:
    """Mini-machine à états pour SALES_UPDATE_PRODUCT (produit catalogue).

    Miroir de ``_resolve_cycle_for_update`` mais pour la table Product
    (oignon, tomates…) au lieu de MarketOffer (productions futures).
    """
    if not phone:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de modifier un produit.",
            "ag_ui_component": None,
        }

    phase = str(working.get("update_phase") or "").upper()
    product_id = payload.get("product_id") or working.get("update_product_id")
    pending: Dict[str, Any] = dict(working.get("update_pending") or {})

    def _clear_wm() -> Dict[str, Any]:
        return {
            **working,
            "update_product_id": None,
            "update_phase": None,
            "update_pending": None,
            "active_goal": None,
            "available_mapping_kind": None,
        }

    # ── CONFIRM : recap déjà affiché, on attend oui/correction/non ────
    if phase == "CONFIRM" and product_id and pending:
        correction = _parse_update_correction(text, allow_type_date=False)
        if correction:
            pending.update(correction)
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
                "response_strategy": "ASK_MISSING_FIELD",
                "current_goal": "SALES_UPDATE_PRODUCT",
                "final_response": _format_pending_recap(pending, noun="produit"),
                "working_memory": {
                    **working,
                    "update_product_id": str(product_id),
                    "update_phase": "CONFIRM",
                    "update_pending": pending,
                    "active_goal": "SALES_UPDATE_PRODUCT",
                },
                "ag_ui_component": None,
            }
        # Oui/non : classification déjà faite en amont par le LLM
        # (input_interpreter), pas de liste de mots-clés à maintenir ici.
        if event == "CONFIRM":
            gw_fields = dict(pending)
            if "product" in pending:
                gw_fields["name"] = gw_fields.pop("product")
            try:
                result = await ProductGateway(mc_runtime).update_product(
                    str(phone), str(product_id), **gw_fields
                )
            except Exception as exc:
                logger.error("SALES_UPDATE_PRODUCT: update_product a échoué: %s", exc)
                return {
                    "status": "COMPLETED",
                    "response_strategy": "ERROR",
                    "final_response": "Impossible d'enregistrer la modification pour le moment. Réessayez dans un instant.",
                    "working_memory": _clear_wm(),
                    "ag_ui_component": None,
                }
            if not is_success_response(result):
                return {
                    "status": "COMPLETED",
                    "response_strategy": "ERROR",
                    "final_response": (result or {}).get("message")
                    or "La modification n'a pas pu être enregistrée.",
                    "working_memory": _clear_wm(),
                    "ag_ui_component": None,
                }
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": (result or {}).get("message")
                or "✅ Produit mis à jour.",
                "working_memory": _clear_wm(),
                "transaction_payload": {"__reset__": True},
                "ag_ui_component": None,
            }
        if event == "REJECT":
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": "❌ Modification annulée. Rien n'a été changé.",
                "working_memory": _clear_wm(),
                "ag_ui_component": None,
            }
        # Ni correction, ni CONFIRM, ni REJECT clair : on ré-affiche le récap
        # — accuse d'abord réception via le LLM (chantier résilience 2026-08,
        # même correctif que le miroir _resolve_cycle_for_update ci-dessus).
        recap_text = _format_pending_recap(pending, noun="produit")
        note = await llm_deviation_reply(
            mc_runtime, text, f"un récapitulatif de modification à confirmer :\n{recap_text}",
            extra_instructions=(
                "Le récapitulatif ci-dessus reflète déjà les valeurs les plus "
                "récentes — accuse juste réception brièvement, ne demande pas "
                "de reformuler et ne redemande pas oui/non toi-même."
            ),
        )
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": "SALES_UPDATE_PRODUCT",
            "final_response": f"{note}\n\n{recap_text}" if note else recap_text,
            "working_memory": {
                **working,
                "update_product_id": str(product_id),
                "update_phase": "CONFIRM",
                "update_pending": pending,
                "active_goal": "SALES_UPDATE_PRODUCT",
            },
            "ag_ui_component": None,
        }

    # ── COLLECT : produit choisi, on attend au moins un champ à modifier ──
    if product_id:
        correction = _parse_update_correction(text, allow_type_date=False)
        if not correction:
            base_question = (
                "✏️ Que souhaitez-vous modifier sur ce produit ?\n"
                "Ex : « prix 400 », « nom maïs », « quantité 500 »."
            )
            # Voir le commentaire miroir dans _resolve_cycle_for_update : le
            # LLM n'est appelé que sur une vraie déviation (event UNKNOWN/
            # OUT_OF_SCOPE), jamais sur l'entrée fraîche juste après la
            # sélection du produit (phase SELECT -> COLLECT).
            note = None
            if event in {"UNKNOWN", "OUT_OF_SCOPE"} and text.strip():
                note = await llm_deviation_reply(
                    mc_runtime, text, "répondre à : quel champ modifier sur ce produit (prix/nom/quantité) ?",
                )
            return {
                # Voir le commentaire miroir dans _resolve_cycle_for_update :
                # "PRODUCT" est interruptible (slot SOFT), ce qui laissait une
                # correction riche dérailler ce tunnel vers un autre goal.
                "status": "WAITING_INPUT",
                **set_pending_interaction(
                    InteractionKind.ENTER_FIELD,
                    field_name="update_field",
                    goal="SALES_UPDATE_PRODUCT",
                ),
                "response_strategy": "ASK_MISSING_FIELD",
                "current_goal": "SALES_UPDATE_PRODUCT",
                "working_memory": {
                    **working,
                    "update_product_id": str(product_id),
                    "update_phase": "COLLECT",
                    "active_goal": "SALES_UPDATE_PRODUCT",
                    "available_mapping_kind": None,
                },
                "final_response": f"{note}\n\n{base_question}" if note else base_question,
                "ag_ui_component": None,
            }
        pending.update(correction)
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": "SALES_UPDATE_PRODUCT",
            "final_response": _format_pending_recap(pending, noun="produit"),
            "working_memory": {
                **working,
                "update_product_id": str(product_id),
                "update_phase": "CONFIRM",
                "update_pending": pending,
                "active_goal": "SALES_UPDATE_PRODUCT",
                "available_mapping_kind": None,
            },
            "ag_ui_component": None,
        }

    # ── SELECT : aucun produit choisi → liste du catalogue ────────────
    try:
        result = await ProductGateway(mc_runtime).get_my_products(str(phone))
    except Exception as exc:
        logger.error("SALES_UPDATE_PRODUCT: get_my_products a échoué: %s", exc)
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger votre catalogue pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }
    if str((result or {}).get("status") or "").lower() not in ("success", "ok"):
        logger.error(
            "SALES_UPDATE_PRODUCT: get_my_products status=%s message=%s",
            (result or {}).get("status"),
            (result or {}).get("message"),
        )
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger votre catalogue pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }
    items = (result or {}).get("data") or []
    if not isinstance(items, list) or not items:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "Votre catalogue de produits est actuellement vide.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    options: List[MenuOption] = []
    lines = ["✏️ *Quel produit souhaitez-vous modifier ? Indiquez le numéro :*"]
    for i, it in enumerate(items, start=1):
        pid = str(it.get("id") or it.get("product_id") or "")
        if not pid:
            continue
        name = it.get("name") or "Produit"
        qty = it.get("quantity_for_sale") or it.get("quantity")
        unit = str(it.get("unit") or "KG").upper()
        price = it.get("price")
        qty_str = f" — {qty} {unit}" if qty not in (None, "") else ""
        price_str = f" — {price} FCFA/{unit}" if price not in (None, "") else ""
        lines.append(f"{i}. *{name}*{qty_str}{price_str}")
        mapping[str(i)] = pid
        options.append(MenuOption(index=str(i), label=f"{name}{qty_str}", value=pid))

    menu = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "current_goal": "SALES_UPDATE_PRODUCT",
        "final_response": menu,
        "available_mapping": mapping,
        "working_memory": {
            **working,
            "active_goal": "SALES_UPDATE_PRODUCT",
            "available_mapping_kind": "catalog_product",
            "update_phase": "SELECT",
            "update_pending": None,
        },
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Produits à modifier",
            options=options,
            kind="catalog_product",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# ESCROW (Paydunya) — confirmation de livraison par code producteur
# =====================================================================
# Flux auto-suffisant à UN SEUL échange : contrairement aux tunnels de mise
# à jour ci-dessus, pas besoin de SELECT/COLLECT/CONFIRM — le code à 4
# chiffres lui-même sert de sélecteur (verify_delivery_otp cherche parmi les
# commandes ESCROWED de CE producteur), donc un seul message suffit ("livré,
# code 1234"). "OTP_CODE" est un slot HARD (tunnel_manager.py) — seul un
# NEW_TASK/INTERRUPTION à confiance suffisante peut en sortir, comme pour du
# CONFIRMATION classique, ce qui est le bon niveau de protection pour un flux
# qui débloque de l'argent.


async def _resolve_delivery_otp(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    working: Dict[str, Any],
    text: str,
    event: str,
) -> Dict[str, Any]:
    if not phone:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible de confirmer la livraison.",
            "ag_ui_component": None,
        }

    code = payload.get("otp_code") or _extract_otp_code(text)
    if not code:
        return {
            "status": "WAITING_INPUT",
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": "PRODUCER_CONFIRM_DELIVERY_OTP",
            "working_memory": {
                **working,
                "active_goal": "PRODUCER_CONFIRM_DELIVERY_OTP",
            },
            "final_response": (
                "📦 Quel est le code de livraison à 4 chiffres transmis par l'acheteur ?\n"
                "Ex : « code 1234 » ou juste « 1234 »."
            ),
            "ag_ui_component": None,
            # (2026-09-02) Sans ceci, TunnelManager (migré pour dériver sa
            # catégorie d'interruption depuis `pending_interaction`, pas
            # `expected_input` directement) ne verrait plus ce tunnel comme
            # actif et perdrait la protection ALWAYS_UNBREAKABLE sur un flux
            # qui débloque de l'argent.
            **set_pending_interaction(
                InteractionKind.VERIFY_OTP, goal="PRODUCER_CONFIRM_DELIVERY_OTP"
            ),
        }

    try:
        result = await EscrowGateway(mc_runtime).verify_delivery_otp(phone, str(code))
    except Exception as exc:
        logger.error(
            "PRODUCER_CONFIRM_DELIVERY_OTP: verify_delivery_otp a échoué: %s", exc
        )
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible de vérifier le code pour le moment. Réessayez dans un instant.",
            "working_memory": {**working, "active_goal": None},
            "ag_ui_component": None,
            **resolve_pending_interaction(),
        }

    if not is_success_response(result):
        # Code invalide : on ré-explique et on reste dans le tunnel plutôt que
        # d'abandonner — le producteur a probablement fait une faute de frappe.
        return {
            "status": "WAITING_INPUT",
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": "PRODUCER_CONFIRM_DELIVERY_OTP",
            "final_response": (result or {}).get("message")
            or "Code invalide. Vérifiez le code à 4 chiffres transmis par l'acheteur.",
            "working_memory": {
                **working,
                "active_goal": "PRODUCER_CONFIRM_DELIVERY_OTP",
            },
            "ag_ui_component": None,
            **set_pending_interaction(
                InteractionKind.VERIFY_OTP, goal="PRODUCER_CONFIRM_DELIVERY_OTP"
            ),
        }

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (result or {}).get("message")
        or "✅ Code valide ! Livraison confirmée. Vos fonds sont débloqués.",
        "working_memory": {**working, "active_goal": None},
        "transaction_payload": {"__reset__": True},
        "ag_ui_component": None,
        **resolve_pending_interaction(),
    }


# =====================================================================
# NODE 7 — CONTEXT RESOLVER (PRODUCER)
# =====================================================================


async def producer_context_resolver(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Nœud LangGraph : Convertit les expressions textuelles en IDs système."""
    goal = (state.get("current_goal") or "").upper()
    event = str(state.get("interpreted_event") or "").upper().strip()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")

    async def _maybe_load_snapshot(
        current_payload: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        if state.get("original_entity") not in (None, "", [], {}):
            return None
        if event != "UPDATE" and goal not in _STATEFUL_UPDATE_GOALS:
            return None

        entity_kind = None
        entity_id = None
        if current_payload.get("product_id") not in (None, "", [], {}):
            entity_kind = "product"
            entity_id = current_payload.get("product_id")
        elif current_payload.get("stock_id") not in (None, "", [], {}):
            entity_kind = "stock"
            entity_id = current_payload.get("stock_id")
        elif current_payload.get("auction_id") not in (None, "", [], {}):
            entity_kind = "auction"
            entity_id = current_payload.get("auction_id")
        elif current_payload.get("order_id") not in (None, "", [], {}):
            entity_kind = "order"
            entity_id = current_payload.get("order_id")
        elif current_payload.get("preorder_id") not in (None, "", [], {}):
            entity_kind = "preorder"
            entity_id = current_payload.get("preorder_id")

        if not entity_kind or not entity_id:
            return None

        patch = await load_entity_snapshot(
            mc_runtime,
            goal,
            str(entity_id),
            current_payload,
            phone=str(phone),
            entity_kind=entity_kind,
        )
        if str(patch.get("status") or "").upper() == "ERROR":
            return patch

        logger.info(
            "[StatefulUpdate] Snapshot loaded kind=%s id=%s goal=%s",
            entity_kind,
            entity_id,
            goal,
        )
        return patch

    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, session impossible.",
            "ag_ui_component": None,
        }

    snapshot_patch = await _maybe_load_snapshot(payload)
    if snapshot_patch:
        if str(snapshot_patch.get("status") or "").upper() == "ERROR":
            return snapshot_patch
        payload = snapshot_patch.get("transaction_payload") or payload

    # 0. Auto-résolution `farm_id` pour les intents qui le requièrent (déclaratif).
    #    Doit s'exécuter EN PREMIER : les autres branches peuvent en dépendre.
    if goal in GOALS_NEEDING_FARM_ID and not payload.get("farm_id"):
        farm_resolution = await _resolve_default_farm(mc_runtime, str(phone), payload)
        # Si l'auto-fill a réussi, on continue le flow ; sinon on retourne le menu/erreur.
        if farm_resolution.get("status") != "PLANNING":
            return farm_resolution
        payload = farm_resolution.get("transaction_payload") or payload

    # 1-2. Cycle enchères producteur (découverte par catégorie → bid → suivi).
    #      Délégué à la machine à états dédiée (flows/producer/auctions.py).
    if goal in {"SALES_PLACE_BID", "MARKET_BROWSE_REQUESTS", "MARKET_GET_MY_PROPOSALS"}:
        from ladini.graphs.agents.market_coach.flows.producer.auctions import (
            producer_auction_resolver,
        )

        state_for_auction = dict(state)
        state_for_auction["transaction_payload"] = payload
        return await producer_auction_resolver(state_for_auction, mc_runtime)

    # 2b. Mise à jour d'une production future (MarketOffer) : flux auto-suffisant
    #     (sélection → collecte → confirmation → écriture directe).
    if goal == "SALES_UPDATE_PRODUCTION":
        working = state.get("working_memory") or {}
        raw_text = str(
            state.get("normalized_text") or state.get("user_query") or ""
        ).strip()
        return await _resolve_cycle_for_update(
            mc_runtime, str(phone), payload, working, raw_text, event
        )

    # 2c. Mise à jour d'un produit du catalogue : même UX que ci-dessus, sur
    #     la table Product (oignon, tomates…) au lieu de MarketOffer.
    if goal == "SALES_UPDATE_PRODUCT":
        working = state.get("working_memory") or {}
        raw_text = str(
            state.get("normalized_text") or state.get("user_query") or ""
        ).strip()
        return await _resolve_product_for_update(
            mc_runtime, str(phone), payload, working, raw_text, event
        )

    # 2c-bis. Retrait d'un produit du catalogue (2026-09-04, Product
    #     Completeness Phase 2) : résout QUEL produit est visé, puis laisse
    #     confirmation_gate/mcp_tool_executor génériques exécuter
    #     `delete_product` (dont toute la règle métier vit côté DB).
    if goal == "SALES_UNPUBLISH_PRODUCT" and not payload.get("product_id"):
        return await _resolve_product_for_unpublish(mc_runtime, str(phone), payload)

    # 2d. Escrow (Paydunya) : le producteur transmet le code de livraison à
    #     4 chiffres pour débloquer ses fonds — flux auto-suffisant à un
    #     seul échange (voir _resolve_delivery_otp).
    if goal == "PRODUCER_CONFIRM_DELIVERY_OTP":
        working = state.get("working_memory") or {}
        raw_text = str(
            state.get("normalized_text") or state.get("user_query") or ""
        ).strip()
        return await _resolve_delivery_otp(
            mc_runtime, str(phone), payload, working, raw_text, event
        )

    # 3. Validation finale du contrat (cas où le producteur doit désigner un bid précis)
    if goal == "SALES_ACCEPT_CONTRACT" and not payload.get("bid_id"):
        return await _resolve_bid(mc_runtime, str(phone), payload)

    # 3bis. Clôture paiement-à-la-livraison (2026-09-04) — résout QUELLE
    # commande est visée (jamais "la dernière", mandat §20) avant de
    # tomber sur confirmation_gate/mcp_tool_executor génériques.
    if goal == "PRODUCER_CONFIRM_DELIVERY_PAYMENT" and not payload.get("order_id"):
        return await _resolve_order_for_delivery_payment(mc_runtime, str(phone), payload)

    # 3ter. Annulation producteur d'une commande confirmée (Phase 5) — même
    # résolution de cible que ci-dessus, jamais "la dernière commande".
    if goal == "PRODUCER_CANCEL_ORDER" and not payload.get("order_id"):
        return await _resolve_order_for_cancellation(mc_runtime, str(phone), payload)

    # 4. Gestion des stocks physiques (mouvements / ajustements / suppressions partielles)
    if goal in {
        "STOCK_ADJUST",
        "STOCK_REMOVE_PARTIAL",
        "STOCK_RECORD_MOVEMENT",
        "STOCK_DELETE",
    } and not payload.get("stock_id"):
        stock_resolution = await _resolve_stock(mc_runtime, str(phone), payload)
        if stock_resolution.get("status") != "PLANNING":
            return stock_resolution

        merged: Dict[str, Any] = dict(stock_resolution)
        payload = stock_resolution.get("transaction_payload") or payload
        snapshot_patch = await _maybe_load_snapshot(payload)
        if snapshot_patch:
            if str(snapshot_patch.get("status") or "").upper() == "ERROR":
                return snapshot_patch
            merged.update(snapshot_patch)
        else:
            merged["transaction_payload"] = payload
        return merged

    # Fallback par défaut si toutes les informations sont résolues
    patch: Dict[str, Any] = {"status": "PLANNING", "ag_ui_component": None}
    if snapshot_patch:
        patch.update(snapshot_patch)
        patch["transaction_payload"] = payload
    return patch


__all__ = [
    "_resolve_auction",
    "_resolve_my_bids",
    "_resolve_bid",
    "_resolve_stock",
    "_resolve_order_for_delivery_payment",
    "_resolve_order_for_cancellation",
    "_resolve_product_for_unpublish",
    "producer_context_resolver",
]
