"""BuyerContext — Sous-structure typée propre au tunnel transactionnel Acheteur.

Contient EXCLUSIVEMENT les champs métier qui n'ont aucun sens hors du flow
acheteur (panier, précommande, négociation, alternatives produit). Hérité par
`MarketAgentState` via composition de `TypedDict` : les clés restent à plat
dans l'état runtime, donc tous les `state.get("active_cart")` historiques
fonctionnent à l'identique. Seule la signature typée est isolée par domaine.

Cart Line Structure (enriched):
  {
    product_id: str,
    name: str,
    quantity: float,
    unit: str,
    price: float,
    line_total: float,
    producer_id: Optional[str],
    vendor_name: Optional[str],
    source_type: "DIRECT" | "AUCTION" | "PROCUREMENT",
    is_auction: bool,
    notification_id: Optional[str],
    notification_status: "PENDING_RESPONSE" | "RESPONDED" | None,
    status: "DRAFT" | "VALIDATED" | "CONFIRMED",
  }
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing_extensions import Annotated, TypedDict

from agriconnect.agents.reducers import merge_dict, replace_list, replace_value


class BuyerContext(TypedDict, total=False):
    """Champs spécifiques au domaine Acheteur.

    Reducers conservés à l'identique du `MarketAgentState` historique pour
    garantir que la fusion dans LangGraph reste **strictement** la même.
    """

    # Lignes de commande temporaires. Enriched with:
    # source_type ("DIRECT"|"AUCTION"|"PROCUREMENT"), is_auction (bool),
    # notification_id (str|None), notification_status (str|None), vendor_name.
    # Remplacée intégralement par cart_management (jamais accumulée en silence).
    active_cart: Annotated[List[Dict[str, Any]], replace_list]

    # Métadonnées agrégées du panier (total, devise, nb_items).
    cart_meta: Annotated[Dict[str, Any], merge_dict]

    # État de la session de négociation en cours :
    # {session_id, product_id, producer_id, buyer_offer, seller_minimum,
    #  current_bid_id, auction_id, status, last_updated}
    negotiation_context: Annotated[Dict[str, Any], merge_dict]

    # Workflow de précommande. `phase` ∈
    # {CART, PREORDER_DRAFTED, AWAITING_CONFIRM, CONFIRMED}.
    # Pivot déterministe du routage buyer (cf. graph_builder).
    preorder_workflow: Annotated[Dict[str, Any], merge_dict]

    # Alternatives intelligentes alimentées en cas d'échec d'un outil MCP
    # (stock insuffisant, prix refusé). Consommées par final_response.
    fallback_recommendations: Annotated[List[Dict[str, Any]], replace_list]

    # Dernière commande confirmée (snapshot utilisé pour l'accusé de réception).
    # Contient : order_id, order_number, total_amount, currency, items.
    last_order_summary: Annotated[Dict[str, Any], merge_dict]

    # Contexte de suivi conversationnel des commandes.
    # {order_focus, last_status, last_interaction_ts, menu_generated_at}
    # Permet la mémoire de la dernière commande consultée, la fenêtre
    # temporelle du menu WhatsApp (30 min) et la proactivité contextuelle.
    order_tracking_context: Annotated[Dict[str, Any], merge_dict]

    # Vendor selection context: when multiple vendors are available for a
    # product, stores the pending selection state before cart insertion.
    # {product_name, vendors: [...], selected_vendor_id, available_mapping_kind}
    vendor_selection_context: Annotated[Optional[Dict[str, Any]], replace_value]

    # (2026-08-30) Pricing-tier selection context — voir domain/pricing_tiers.py
    # + flows/buyer/cart.py. Un produit à `pricing_tiers` (5L bidon / 10L
    # bidon...) exige de savoir LEQUEL avant l'ajout au panier ; ce contexte
    # survit entre tours tant que le palier n'est pas encore choisi, même
    # pattern que vendor_selection_context.
    # {product_id, tiers: [...]}
    tier_selection_context: Annotated[Optional[Dict[str, Any]], replace_value]


__all__ = ["BuyerContext"]
