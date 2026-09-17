"""Market — Producer Flow (résolveurs de contexte côté Producteur).

Contient le Context Resolver côté Producteur ainsi que les sous-résolveurs
spécialisés appelés via MCP :
  - `_resolve_auction`        : découverte proactive d'enchères à partir d'un produit
  - `_resolve_my_bids`        : portefeuille des bids actifs du producteur
  - `_resolve_default_farm`   : auto-résolution de farm_id (1 ferme → autofill, N → menu)

Tous les helpers filtrent défensivement les `None` avant d'appeler FastMCP.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any, Callable, Dict, List, Optional

from ladini.core.formatting import fmt_num as _fmt_num
from ladini.domain.quantity_unit import (
    extract_deterministic_pricing_tiers,
    extract_single_pricing_tier_correction,
    find_matching_tier_index,
)
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


# (2026-09-13, Deep Intent Architecture Cleanup) : `_resolve_bid` supprimée
# — exclusivement rattachée à `SALES_ACCEPT_CONTRACT` (accepter une offre
# reçue), goal supprimé d'INTENT_CONFIG (contrat outil fictif, voir
# interpreter/intent.py). Son seul appelant, la branche de dispatch dans
# `producer_context_resolver`, était déjà mort avant même cette suppression.


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


# (2026-09-13, Deep Intent Architecture Cleanup) : `_resolve_stock`
# supprimée — exclusivement rattachée à STOCK_ADJUST/STOCK_REMOVE_PARTIAL/
# STOCK_RECORD_MOVEMENT/STOCK_DELETE, tous supprimés d'INTENT_CONFIG
# (tool_name `*_by_id` fictif, aucune méthode DB réelle — voir
# interpreter/intent.py). Son seul appelant, la branche de dispatch dans
# `producer_context_resolver`, était déjà mort avant même cette suppression.


# =====================================================================
# RÉSOLUTION D'UN CANDIDAT APRÈS MENU NUMÉROTÉ (2026-09-15)
# =====================================================================
# Incident réel : "annuler" -> menu à 2 commandes -> réponse "2" -> LE MÊME
# MENU se réaffichait, quel que soit le numéro tapé (reproductible sur
# _resolve_order_for_cancellation/_resolve_order_for_confirmation/
# _resolve_order_for_delivery_payment/_resolve_product_for_unpublish — les
# 4 résolveurs qui affichent un menu numéroté "sinon" après auto-sélection).
#
# Racine : `nodes/memory.py` résout DÉJÀ la sélection numérique en un VRAI
# identifiant métier (`order_id`/`product_id`...) dès que le
# `mapping_kind` du menu est reconnu (`_ORDER_MAPPING_KINDS`/
# "catalog_product") — MAIS efface `selection_index` dans le MÊME
# mouvement une fois cette résolution faite (comportement voulu : une fois
# traduit, le nombre brut n'a plus de raison de survivre). Ces 4
# résolveurs, eux, ne lisaient QUE `selection_index` — jamais
# l'identifiant déjà résolu — donc le tour suivant les trouvait tous les
# deux vides : aucune commande/produit ne pouvait jamais être identifié
# au-delà du cas à un seul candidat (auto-sélection, qui ne passe jamais
# par ce chemin).
def _resolve_selected_candidate(
    payload: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    payload_id_key: str,
    candidate_id_fn: "Callable[[Dict[str, Any]], Any]",
) -> Optional[Dict[str, Any]]:
    """Retrouve dans `candidates` celui désigné par le tour courant.
    Vérifie D'ABORD l'identifiant déjà résolu par `nodes/memory.py` sous
    `payload[payload_id_key]` (le cas nominal, désormais le SEUL qui
    survit après un menu réel) ; retombe sur `selection_index` brut
    UNIQUEMENT en repli dégradé (résolution générique indisponible ce
    tour, ex. snapshot manquant) — jamais l'inverse, pour ne pas
    réintroduire une dépendance silencieuse au nombre brut là où
    l'identifiant réel est déjà connu."""
    raw_id = payload.get(payload_id_key)
    if raw_id not in (None, ""):
        raw_id_str = str(raw_id)
        for c in candidates:
            if str(candidate_id_fn(c)) == raw_id_str:
                return c
    idx = payload.get("selection_index")
    try:
        idx_int = int(idx) if idx is not None else None
    except (TypeError, ValueError):
        idx_int = None
    if idx_int is not None and 1 <= idx_int <= len(candidates):
        return candidates[idx_int - 1]
    return None


# =====================================================================
# CLÔTURE PAIEMENT-À-LA-LIVRAISON (2026-09-04)
# =====================================================================


async def _resolve_order_for_delivery_payment(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout QUELLE commande le producteur vise pour
    `confirm_delivery_and_payment` — jamais "la dernière commande" (mandat
    §20 : un message comme "c'est payé" ne doit jamais muter une commande
    choisie par un simple `ORDER BY created_at DESC`). Même gabarit que les
    autres résolveurs de ce module : auto-sélection si un seul candidat
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
        chosen = _resolve_selected_candidate(
            payload, candidates, "order_id", lambda c: c.get("order_id")
        )

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

    Candidates (2026-09-13, élargi pour la confirmation explicite
    producteur) : `status in {"CONFIRMED", "PENDING_PRODUCER_CONFIRMATION"}`
    ET `payment_status=="PENDING"` — refuser une commande pas encore
    confirmée est désormais un cas d'usage légitime, au même titre
    qu'annuler après confirmation. Une commande escrow ne relève jamais de
    ce chemin, et une commande déjà `COMPLETED`/`CANCELLED` n'est plus
    annulable.
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
    # (2026-09-13, confirmation explicite producteur) : annulable AUSSI
    # avant confirmation — refuser une commande reçue est un cas d'usage
    # légitime et distinct, désormais accepté au même titre qu'annuler après
    # confirmation (voir `services/database/producer.py::
    # cancel_confirmed_order`, qui accepte maintenant les deux statuts).
    result_confirmed = await gw.get_producer_orders(phone=str(phone), status="CONFIRMED")
    result_pending = await gw.get_producer_orders(
        phone=str(phone), status="PENDING_PRODUCER_CONFIRMATION"
    )
    all_orders = (result_confirmed.get("data") or []) + (
        result_pending.get("data") or []
    )
    candidates = [
        o
        for o in all_orders
        if str(o.get("status") or "").upper() == "PENDING_PRODUCER_CONFIRMATION"
        or str(o.get("payment_status") or "").upper() == "PENDING"
    ]

    if not candidates:
        return {
            "status": "ERROR",
            "validation_errors": ["no_cancellable_order"],
            "response_strategy": "ERROR",
            "final_response": (
                "Aucune commande confirmée ou en attente de votre "
                "confirmation — il n'y a rien à annuler."
            ),
            "ag_ui_component": None,
        }

    chosen = None
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = _resolve_selected_candidate(
            payload, candidates, "order_id", lambda c: c.get("order_id")
        )

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
# CONFIRMATION EXPLICITE D'UNE COMMANDE REÇUE (2026-09-13)
# =====================================================================


async def _resolve_order_for_confirmation(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout QUELLE commande le producteur veut confirmer.

    Même gabarit exact que `_resolve_order_for_cancellation` : auto-sélection
    s'il n'y a qu'une commande en attente, résolution par `selection_index`
    sur une réponse à un menu déjà affiché, sinon menu numéroté strict —
    jamais de choix implicite. La confirmation explicite (gate) suit ensuite,
    identique au reste du parcours WRITE.

    Candidates = uniquement `status=="PENDING_PRODUCER_CONFIRMATION"` —
    contrairement à l'annulation (qui couvre aussi `CONFIRMED`), une commande
    déjà confirmée n'a plus de raison d'être reconfirmée."""
    if not phone:
        return {
            "status": "ERROR",
            "validation_errors": ["missing_user_phone"],
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable, impossible d'accéder à vos commandes.",
            "ag_ui_component": None,
        }

    gw = OrderTrackingGateway(mc_runtime)
    result = await gw.get_producer_orders(
        phone=str(phone), status="PENDING_PRODUCER_CONFIRMATION"
    )
    candidates = result.get("data") or []

    if not candidates:
        return {
            "status": "ERROR",
            "validation_errors": ["no_confirmable_order"],
            "response_strategy": "ERROR",
            "final_response": (
                "Aucune commande en attente de votre confirmation pour le "
                "moment."
            ),
            "ag_ui_component": None,
        }

    chosen = None
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = _resolve_selected_candidate(
            payload, candidates, "order_id", lambda c: c.get("order_id")
        )

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
    lines = ["✅ *Quelle commande souhaitez-vous confirmer ? Indiquez le numéro :*"]
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
            title="Commandes en attente de confirmation",
            options=options,
            kind="order_confirmation",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# ACTION SUR UNE COMMANDE EN ATTENTE (2026-09-15) — confirmer/annuler,
# tunnel auto-suffisant fusionnant PRODUCER_CONFIRM_ORDER/PRODUCER_CANCEL_
# ORDER.
# =====================================================================
# Incident réel RÉPÉTÉ (2026-09-14 puis 2026-09-15, même bug) : un
# producteur qui vient de voir sa liste de ventes
# ("Tapez *confirmer*... ou *annuler*...") tape le mot NU — sans numéro,
# sans phrase. Deux correctifs successifs se sont avérés insuffisants :
# d'abord enrichir le LABEL de l'intent (interpreter/intent.py) avec des
# exemples de phrasé, puis donner un signal d'état en CONTEXTE au prompt
# LLM (`producer_order_action_hint`) — dans les deux cas, la classification
# NEW_TASK d'un mot NU sans aucun autre ancrage reste une supposition
# libre, jamais garantie, quelle que soit la qualité du prompt.
#
# La vraie source de fiabilité de ce codebase pour "oui"/"non" est
# AILLEURS : `expected_input == "CONFIRMATION"` +
# `_CONFIRM_EXACT_PHRASES`/`_REJECT_EXACT_PHRASES`
# (interpreter/routing.py) — un filet DÉTERMINISTE déjà éprouvé partout
# ailleurs (mise à jour produit/production, désignation d'un gagnant
# d'enchère…), mais qui exige qu'un `PendingInteraction` soit DÉJÀ posé —
# jamais qu'il soit deviné après coup. `flows/buyer/order_tracking.py::
# list_orders` pose maintenant ce `PendingInteraction` PROACTIVEMENT (un
# verrou, pas une classification) dès qu'il affiche une liste avec
# EXACTEMENT une vente 🟡 en attente — le tour suivant n'a donc plus RIEN à
# classifier.
#
# "confirmer" et "annuler" désignent deux ACTIONS réellement différentes
# (`confirm_order_by_producer` / `cancel_confirmed_order`, pas un simple
# CONFIRM/REJECT d'une même action) : ce résolveur lit `event`
# (CONFIRM/REJECT), JAMAIS `detected_intent`, pour choisir entre elles —
# cohérent avec le contrat du fast-path déterministe (il ne fait qu'échoer
# le goal déjà verrouillé, jamais en inventer un — voir
# `TestGoalLockOnlyValidatedOnTheLlmPath`,
# tests/architecture/test_fastpath_normal_path_equivalence.py) : peu
# importe lequel des deux goals reste affiché, l'action réelle vient
# toujours de `event`.


def _pending_order_action_recap(order_id: Any) -> str:
    return (
        f"📦 *Commande #{str(order_id)[:8].upper()}*\n"
        "Tapez *confirmer* pour l'accepter, ou *annuler* si vous ne pouvez "
        "pas l'honorer."
    )


async def _finalize_pending_order_action(
    mc_runtime: MarketRuntime,
    phone: str,
    order_id: str,
    event: str,
    working: Dict[str, Any],
) -> Dict[str, Any]:
    """Exécute l'action réellement demandée pour la commande déjà
    verrouillée — `event` (CONFIRM/REJECT), jamais le nom du goal courant,
    décide entre confirmer et annuler (voir docstring de section)."""

    def _clear_wm() -> Dict[str, Any]:
        return {
            **working,
            "pending_order_action_id": None,
            "pending_order_action_phase": None,
            "active_goal": None,
        }

    gw = OrderTrackingGateway(mc_runtime)

    if event == "CONFIRM":
        try:
            result = await gw.confirm_order_by_producer(phone, str(order_id))
        except Exception as exc:
            logger.error(
                "PRODUCER_CONFIRM_ORDER: confirm_order_by_producer a échoué: %s",
                exc,
            )
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": (
                    "Impossible de confirmer la commande pour le moment. "
                    "Réessayez dans un instant."
                ),
                "working_memory": _clear_wm(),
                "ag_ui_component": None,
            }
        if not is_success_response(result):
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": (result or {}).get("message")
                or "La commande n'a pas pu être confirmée.",
                "working_memory": _clear_wm(),
                "ag_ui_component": None,
            }
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (result or {}).get("message") or "✅ Commande confirmée.",
            "working_memory": _clear_wm(),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    if event == "REJECT":
        try:
            result = await gw.cancel_confirmed_order(
                phone,
                str(order_id),
                reason="Producteur indisponible pour honorer cette commande.",
            )
        except Exception as exc:
            logger.error(
                "PRODUCER_CANCEL_ORDER: cancel_confirmed_order a échoué: %s", exc
            )
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": (
                    "Impossible d'annuler la commande pour le moment. "
                    "Réessayez dans un instant."
                ),
                "working_memory": _clear_wm(),
                "ag_ui_component": None,
            }
        if not is_success_response(result):
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": (result or {}).get("message")
                or "La commande n'a pas pu être annulée.",
                "working_memory": _clear_wm(),
                "ag_ui_component": None,
            }
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (result or {}).get("message") or "❌ Commande annulée.",
            "working_memory": _clear_wm(),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    # Ni CONFIRM ni REJECT clair (texte incompris) : ré-affiche le rappel,
    # verrou inchangé.
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(
            InteractionKind.CONFIRM_ACTION, context_ref="confirmation"
        ),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "PRODUCER_CONFIRM_ORDER",
        "final_response": _pending_order_action_recap(order_id),
        "working_memory": {
            **working,
            "pending_order_action_id": str(order_id),
            "pending_order_action_phase": "CONFIRM",
            "active_goal": "PRODUCER_CONFIRM_ORDER",
        },
        "ag_ui_component": None,
    }


async def _resolve_pending_order_action(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    working: Dict[str, Any],
    event: str,
    goal: str,
) -> Dict[str, Any]:
    """Point d'entrée PRODUCER_CONFIRM_ORDER/PRODUCER_CANCEL_ORDER — voir
    le docstring de section ci-dessus pour le contexte complet."""
    if not phone:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": (
                "Numéro de téléphone introuvable, impossible d'agir sur "
                "cette commande."
            ),
            "ag_ui_component": None,
        }

    phase = str(working.get("pending_order_action_phase") or "").upper()
    stored_order_id = working.get("pending_order_action_id")

    if phase == "CONFIRM" and stored_order_id:
        return await _finalize_pending_order_action(
            mc_runtime, phone, str(stored_order_id), event, working
        )

    # Pas encore verrouillé sur une commande précise : résout QUELLE
    # commande (menu numéroté si plusieurs, auto-sélection sinon) —
    # réutilise les résolveurs existants pour la recherche de candidats.
    if goal == "PRODUCER_CANCEL_ORDER":
        select_result = await _resolve_order_for_cancellation(mc_runtime, phone, payload)
    else:
        select_result = await _resolve_order_for_confirmation(mc_runtime, phone, payload)

    resolved_order_id = (select_result.get("transaction_payload") or {}).get("order_id")
    if select_result.get("status") == "PLANNING" and resolved_order_id:
        # Commande identifiée (auto-sélection, ou réponse numérique à un
        # menu déjà affiché) : bascule en phase CONFIRM auto-suffisante —
        # jamais confirmation_gate/mcp_tool_executor génériques (l'action à
        # exécuter dépend de `event` au tour SUIVANT, pas d'un seul goal
        # verrouillé — voir docstring de section).
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(
                InteractionKind.CONFIRM_ACTION, context_ref="confirmation"
            ),
            "response_strategy": "ASK_MISSING_FIELD",
            "current_goal": goal,
            "final_response": _pending_order_action_recap(resolved_order_id),
            "working_memory": {
                **working,
                "pending_order_action_id": str(resolved_order_id),
                "pending_order_action_phase": "CONFIRM",
                "active_goal": goal,
            },
            "ag_ui_component": None,
        }
    return select_result


# =====================================================================
# RETRAIT D'UN PRODUIT DU CATALOGUE (2026-09-04)
# =====================================================================


async def _resolve_product_for_unpublish(
    mc_runtime: MarketRuntime, phone: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Résout QUEL produit le producteur veut retirer de son catalogue.

    Même gabarit exact que `_resolve_order_for_delivery_payment` :
    auto-sélection s'il n'y a qu'un seul produit, résolution par
    `selection_index` sur une réponse à un menu
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
        chosen = _resolve_selected_candidate(
            payload, items, "product_id", lambda c: c.get("id") or c.get("product_id")
        )

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


def _apply_tier_correction(
    current_tiers: List[Dict[str, Any]], described: Dict[str, Any]
) -> Optional[List[Dict[str, Any]]]:
    idx = find_matching_tier_index(
        current_tiers, described["quantity"], described["unit"]
    )
    if idx is None:
        return None
    merged = [dict(t) for t in current_tiers]
    merged[idx]["price"] = described["price"]
    return merged


def _extract_pricing_tier_update(
    text: str, current_tiers: Optional[List[Dict[str, Any]]]
) -> Optional[List[Dict[str, Any]]]:
    """Reconnaît une correction ciblant UN OU PLUSIEURS paliers précis d'un
    produit déjà multi-tarifs — ex: "prix bidon de 20 L à 70 000 fcfa" (un
    seul palier), ou "bidon de 5 L à 12000 fcfa et bidon de 20 L à 80000
    fcfa" (2 paliers corrigés dans le même message — incident réel
    2026-09-14 : seul le cas à un seul palier fonctionnait, un producteur
    voulant corriger 2 tarifs à la fois n'avait aucun moyen de le faire en
    un message, le message tombait hors de toute reconnaissance déterministe
    et le tunnel se terminait par une clarification générique sans rapport).
    Retourne la liste COMPLÈTE des paliers (ceux visés remplacés, les autres
    inchangés) — jamais un delta partiel, car `update_product_price_and_qty`
    REMPLACE tout `pricing_tiers` fourni (voir le commentaire au site
    d'appel).

    Distinction cruciale entre `None` et `[]` : `None` signifie "ce texte ne
    ressemble à AUCUN motif de palier" — l'appelant peut alors retomber sans
    risque sur l'extraction scalaire (prix/quantité simples). `[]` signifie
    "un motif de palier A ÉTÉ détecté mais n'a pas pu être résolu" (palier
    introuvable, ou un seul des paliers d'un message à plusieurs) —
    l'appelant NE DOIT PAS retomber sur l'extraction scalaire dans ce cas :
    elle mal-interpréterait la quantité/le prix du palier visé comme une
    correction scalaire plate (incident identifié en écrivant ce correctif :
    "bidon de 5 L à 12000 fcfa et bidon de 99 L à 1 fcfa", le second palier
    introuvable, produisait silencieusement quantity=5/price=12000 au lieu
    de signaler l'échec — jamais de résultat partiel, soit TOUTES les
    corrections demandées s'appliquent, soit aucune)."""
    if not current_tiers:
        return None

    single = extract_single_pricing_tier_correction(text)
    if single:
        result = _apply_tier_correction(current_tiers, single)
        return result if result is not None else []

    # Plusieurs paliers dans le même message ("... et ...", virgules) :
    # réutilise le MÊME parseur déterministe multi-clauses que la
    # publication d'un produit multi-tarifs (`extract_deterministic_
    # pricing_tiers`, domain/quantity_unit.py) — motif identique (une paire
    # quantité+unité et une paire prix+devise par clause), jamais une
    # seconde regex à maintenir en parallèle.
    described_list = extract_deterministic_pricing_tiers(text)
    if not described_list:
        return None
    merged = [dict(t) for t in current_tiers]
    for described in described_list:
        idx = find_matching_tier_index(
            merged, described["quantity"], described["unit"]
        )
        if idx is None:
            return []
        merged[idx]["price"] = described["price"]
    return merged


def _parse_update_correction(
    text: str,
    *,
    allow_type_date: bool,
    current_tiers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Extrait déterministiquement les champs mentionnés dans *text*.

    Utilisé à la fois en phase COLLECT (première saisie) et en phase CONFIRM
    (pour distinguer une VRAIE correction d'un simple "non" d'annulation).

    `current_tiers` (produit catalogue uniquement, jamais les productions
    futures) : quand fourni et non vide, une correction de palier ("prix
    bidon de 20 L à 70 000 fcfa") est reconnue EN PRIORITÉ et retournée
    seule (`{"pricing_tiers": [...]}`) — sans elle, "prix bidon de 20 L à
    70 000 fcfa" serait mal capté comme un prix scalaire de 20 par
    `_extract_price_correction` (son motif "prix" + premier nombre proche
    ne distingue pas la quantité du palier du prix réel plus loin dans la
    phrase) : incident réel (2026-09-14) confirmé.
    """
    tier_update = _extract_pricing_tier_update(text, current_tiers)
    if tier_update is not None:
        # `[]` = motif de palier détecté mais non résolu (voir docstring de
        # `_extract_pricing_tier_update`) : ne JAMAIS retomber sur
        # l'extraction scalaire ci-dessous, qui mal-interpréterait la
        # quantité/le prix du palier visé.
        return {"pricing_tiers": tier_update} if tier_update else {}

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
    # `pricing_tiers` (2026-09-14) : `_extract_pricing_tier_update` retourne
    # TOUJOURS la liste COMPLÈTE (jamais un delta, voir son docstring) — le
    # récap doit donc afficher chaque palier tel qu'il sera écrit en base,
    # pour que le producteur puisse vérifier qu'un SEUL a changé et que les
    # autres sont restés intacts avant de confirmer.
    tiers = pending.get("pricing_tiers")
    if isinstance(tiers, list) and tiers:
        lines.append("- Tarifs par conditionnement :")
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            t_qty, t_unit, t_price = (
                tier.get("quantity"),
                tier.get("unit"),
                tier.get("price"),
            )
            if t_qty in (None, "") or not t_unit or t_price in (None, ""):
                continue
            packaging = tier.get("packaging")
            label = f"{_fmt_num(t_qty)} {t_unit}" + (
                f" ({packaging})" if packaging else ""
            )
            lines.append(f"  • {label} — {_fmt_num(t_price)} FCFA")
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
                "current_goal": "PRODUCTION_UPDATE_FUTURE",
                "final_response": _format_pending_recap(pending, noun="lot"),
                "working_memory": {
                    **working,
                    "update_cycle_id": str(cycle_id),
                    "update_phase": "CONFIRM",
                    "update_pending": pending,
                    "active_goal": "PRODUCTION_UPDATE_FUTURE",
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
                    "PRODUCTION_UPDATE_FUTURE: update_production a échoué: %s", exc
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
            "current_goal": "PRODUCTION_UPDATE_FUTURE",
            "final_response": f"{note}\n\n{recap_text}" if note else recap_text,
            "working_memory": {
                **working,
                "update_cycle_id": str(cycle_id),
                "update_phase": "CONFIRM",
                "update_pending": pending,
                "active_goal": "PRODUCTION_UPDATE_FUTURE",
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
                # facilement comme PRODUCTION_DECLARE_FUTURE) pouvait faire dérailler
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
                    goal="PRODUCTION_UPDATE_FUTURE",
                ),
                "response_strategy": "ASK_MISSING_FIELD",
                "current_goal": "PRODUCTION_UPDATE_FUTURE",
                "working_memory": {
                    **working,
                    "update_cycle_id": str(cycle_id),
                    "update_phase": "COLLECT",
                    "active_goal": "PRODUCTION_UPDATE_FUTURE",
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
            "current_goal": "PRODUCTION_UPDATE_FUTURE",
            "final_response": _format_pending_recap(pending, noun="lot"),
            "working_memory": {
                **working,
                "update_cycle_id": str(cycle_id),
                "update_phase": "CONFIRM",
                "update_pending": pending,
                "active_goal": "PRODUCTION_UPDATE_FUTURE",
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
        logger.error("PRODUCTION_UPDATE_FUTURE: list_productions a échoué: %s", exc)
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger vos productions pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_wm(),
            "ag_ui_component": None,
        }
    if str((result or {}).get("status") or "").lower() not in ("success", "ok"):
        logger.error(
            "PRODUCTION_UPDATE_FUTURE: list_productions status=%s message=%s",
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
        "current_goal": "PRODUCTION_UPDATE_FUTURE",
        "final_response": menu,
        "available_mapping": mapping,
        "working_memory": {
            **working,
            "active_goal": "PRODUCTION_UPDATE_FUTURE",
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
            "update_product_tiers": None,
            "active_goal": None,
            "available_mapping_kind": None,
        }

    # Palier(s) de prix DÉJÀ publiés sur ce produit (2026-09-14) — nécessaire
    # pour comprendre "prix bidon de 20 L à 70 000 fcfa" comme une correction
    # d'UN palier précis (voir `_extract_pricing_tier_update` plus bas) :
    # `update_product_price_and_qty` REMPLACE tout `pricing_tiers` fourni
    # (jamais un patch partiel côté serveur, voir
    # `services/database/product.py::update_product_price_and_qty`), donc il
    # faut connaître les AUTRES paliers pour les préserver dans la liste
    # envoyée. Récupéré UNE SEULE fois (mis en cache dans `working_memory`,
    # jamais refetché à chaque tour de ce tunnel) — `[]` si le produit n'a
    # aucun palier ou si la récupération échoue (une simple correction
    # scalaire prix/quantité/nom reste possible dans ce cas, seule la
    # correction PAR PALIER est alors indisponible).
    current_tiers = working.get("update_product_tiers")
    if current_tiers is None and product_id:
        current_tiers = []
        try:
            _products = await ProductGateway(mc_runtime).get_my_products(str(phone))
            _items = (_products or {}).get("data") or []
            _match = next(
                (
                    it
                    for it in _items
                    if str(it.get("id") or it.get("product_id") or "")
                    == str(product_id)
                ),
                None,
            )
            if _match:
                current_tiers = _match.get("pricing_tiers") or []
        except Exception as exc:
            logger.warning(
                "SALES_UPDATE_PRODUCT: récupération des paliers existants a échoué: %s",
                exc,
            )
        working = {**working, "update_product_tiers": current_tiers}

    # ── CONFIRM : recap déjà affiché, on attend oui/correction/non ────
    if phase == "CONFIRM" and product_id and pending:
        correction = _parse_update_correction(
            text, allow_type_date=False, current_tiers=current_tiers
        )
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
        correction = _parse_update_correction(
            text, allow_type_date=False, current_tiers=current_tiers
        )
        if not correction:
            base_question = (
                "✏️ Que souhaitez-vous modifier sur ce produit ?\n"
                "Ex : « prix 400 », « nom maïs », « quantité 500 »."
                + (
                    " Pour un palier précis : « prix bidon de 20 L à 70000 fcfa »."
                    if current_tiers
                    else ""
                )
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
        if event != "UPDATE":
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
    if goal == "PRODUCTION_UPDATE_FUTURE":
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

    # 3bis. Clôture paiement-à-la-livraison (2026-09-04) — résout QUELLE
    # commande est visée (jamais "la dernière", mandat §20) avant de
    # tomber sur confirmation_gate/mcp_tool_executor génériques.
    if goal == "PRODUCER_CONFIRM_DELIVERY_PAYMENT" and not payload.get("order_id"):
        return await _resolve_order_for_delivery_payment(mc_runtime, str(phone), payload)

    # 3ter/3quater. Action (confirmer/annuler) sur une commande reçue —
    # tunnel auto-suffisant fusionné (2026-09-15, voir docstring de section
    # `_resolve_pending_order_action` : la classification NEW_TASK d'un
    # "confirmer"/"annuler" nu s'est avérée structurellement peu fiable,
    # remplacée par un verrou CONFIRM_ACTION posé PROACTIVEMENT par
    # `flows/buyer/order_tracking.py::list_orders`). Jamais
    # confirmation_gate/mcp_tool_executor génériques pour ces deux goals.
    if goal in ("PRODUCER_CANCEL_ORDER", "PRODUCER_CONFIRM_ORDER"):
        working = state.get("working_memory") or {}
        return await _resolve_pending_order_action(
            mc_runtime, str(phone), payload, working, event, goal
        )

    # Fallback par défaut si toutes les informations sont résolues
    patch: Dict[str, Any] = {"status": "PLANNING", "ag_ui_component": None}
    if snapshot_patch:
        patch.update(snapshot_patch)
        patch["transaction_payload"] = payload
    return patch


__all__ = [
    "_resolve_auction",
    "_resolve_my_bids",
    "_resolve_order_for_delivery_payment",
    "_resolve_order_for_cancellation",
    "_resolve_order_for_confirmation",
    "_resolve_pending_order_action",
    "_resolve_product_for_unpublish",
    "producer_context_resolver",
]
