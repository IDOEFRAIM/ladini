"""Action handlers for the Sales & Marketplace domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import (
    coalesce_entity_value,
    is_update_mode,
    normalize_quantity_to_kg,
    require,
    require_current_entity,
    require_phone,
    to_float,
)

_EMPTY_SLOT_VALUES = (None, "", [], {})


def _pick_first_value(*candidates: Any) -> Any:
    for candidate in candidates:
        if candidate not in _EMPTY_SLOT_VALUES:
            return candidate
    return None


def _resolve_mass_payload(payload: Mapping[str, Any]) -> Tuple[float, str]:
    quantity_raw = _pick_first_value(
        payload.get("original_quantity"),
        payload.get("quantity_display"),
        payload.get("quantity"),
        payload.get("quantity_mentioned"),
    )
    if quantity_raw is None:
        quantity_raw = require(payload, "quantity_mentioned")

    unit_raw = _pick_first_value(
        payload.get("original_unit"),
        payload.get("unit_display"),
        payload.get("unit"),
        payload.get("unit_mentioned"),
    )

    try:
        qty_value = float(str(quantity_raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"Quantité invalide: {quantity_raw!r}")

    qty_kg, canonical_unit = normalize_quantity_to_kg(qty_value, unit_raw)
    return qty_kg, canonical_unit

@register_action("SALES_GET_CATALOG", mode="READ")
def prep_sales_get_catalog(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation du catalogue, des stocks et des productions futures.

    Outil MCP : get_stocks(phone) — retourne un snapshot complet
    regroupant catalogue, inventaire par ferme et `upcoming_cycles`.
    """
    phone = require_phone(state)
    return "get_stocks", {"phone": phone}


@register_action("MARKET_GET_REQUESTS", mode="READ")
def prep_market_get_requests(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la liste des appels d'offres du marché."""
    phone = require_phone(state)
    args: Dict[str, Any] = {
        "phone": phone,
        "status": payload.get("status") or "OPEN",
        "view_mode": "MARKETPLACE"
    }
    # Filtres optionnels sémantiques
    product = payload.get("product_name") or payload.get("product")
    if product: args["product_name"] = str(product)
    zone = payload.get("zone_name") or payload.get("zone")
    if zone: args["zone_name"] = str(zone)
    return "get_auctions", args


@register_action("MARKET_GET_REQUEST_DETAIL", mode="READ")
def prep_market_get_request_detail(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des offres reçues sur sa propre demande.

    Outil MCP réel : `get_auctions_bids` (pluriel) — pas `get_auction_bids`.
    Filtre serveur-side par auction_id si fourni dans status/extra (le tool
    accepte phone+status ; auction_id est appliqué côté DB par jointure).
    """
    phone = require_phone(state)
    auction_id = str(require(payload, "auction_id"))
    return "get_auctions_bids", {
        "phone": phone,
        "auction_id": auction_id,
        "status": payload.get("status") or "OPEN",
    }


@register_action("MARKET_GET_MY_PROPOSALS", mode="READ")
def prep_market_get_my_proposals(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare le suivi des propositions envoyées par le producteur.

    Outil MCP correct : `get_my_active_bids(phone)` (pas get_auctions_bids,
    qui retourne les bids reçus par l'acheteur).
    """
    phone = require_phone(state)
    return "get_my_active_bids", {"phone": phone}


@register_action("MARKET_SNAPSHOT", mode="READ")
def prep_market_snapshot(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des cours du marché local."""
    args: Dict[str, Any] = {}
    zone = payload.get("zone") or payload.get("zone_name")
    if zone: args["zone"] = str(zone)
    return "get_market_snapshot", args


@register_action("MARKET_SNAPSHOT_ZONAL", mode="READ")
def prep_market_snapshot_zonal(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des tendances locales."""
    zone = str(require(payload, "zone"))
    return "get_zone_market_overview", {"zone_id": zone}


@register_action("DASHBOARD_PRODUCER", mode="READ")
def prep_dashboard_producer(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare le chargement du tableau de bord d'exploitation.

    Outil MCP : get_producer_dashboard(producer_id).
    Le schema resolver mappe phone → producer_id via _ARG_ALIASES.
    """
    phone = require_phone(state)
    return "get_producer_dashboard", {"producer_id": phone}


@register_action("SEARCH_PRODUCTS", mode="READ")
def prep_search_products(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche par mot-clé dans le catalogue.

    Outil MCP : search_products(product_name, zone_id?, limit?).
    CRITIQUE : le paramètre s'appelle `product_name`, pas `product`.
    """
    product = str(require(payload, "product"))
    args: Dict[str, Any] = {"product": product}
    zone = payload.get("zone_name") or payload.get("zone")
    if zone:
        args["zone_id"] = str(zone)
    return "search_products", args


@register_action("SEARCH_NEARBY", mode="READ")
def prep_search_nearby(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la recherche de proximité GPS."""
    require_phone(state)
    require(payload, "latitude")
    require(payload, "longitude")
    return "get_all_zone_market_overview", {}


@register_action("VALIDATE_PRICE", mode="READ")
def prep_validate_price(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la vérification de cohérence de prix.

    Outil MCP : check_price_anomaly(product_name, proposed_price, zone_id).
    Si seul un nom de zone est fourni, on l'envoie en zone_id ; le service
    Intelligence accepte un nom (résolution interne) en mode tolérant.
    """
    product = str(require(payload, "product"))
    price = float(require(payload, "price_mentioned"))
    zone = str(require(payload, "zone"))
    return "check_price_anomaly", {
        "product_name": product,
        "proposed_price": price,
        "zone_id": zone,
    }


@register_action("SALES_PUBLISH_PRODUCT", mode="WRITE")
def prep_sales_publish_product(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    product = str(require(payload, "product"))
    price = float(require(payload, "price_mentioned"))
    qty_kg, unit = _resolve_mass_payload(payload)
    args: Dict[str, Any] = {
        "producer_id": phone,
        "name": product,
        "price": price,
        "quantity_for_sale": qty_kg,
        "unit": unit,
    }
    if payload.get("description"):
        args["description"] = str(payload["description"])
    if payload.get("category_label"):
        args["category_label"] = str(payload["category_label"])
    return "create_product", args


@register_action("SALES_RECORD_DIRECT", mode="WRITE")
def prep_sales_record_direct(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    product = str(require(payload, "product"))
    price = float(require(payload, "price_mentioned"))
    qty_kg, unit = _resolve_mass_payload(payload)
    return "record_sale", {
        "phone": phone,
        "product_name": product,
        "quantity": qty_kg,
        "total_price": price,
        "unit": unit,
    }


@register_action("SALES_PLACE_BID", mode="WRITE")
def prep_sales_place_bid(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    auction_id = str(require(payload, "auction_id"))
    price = float(require(payload, "price_mentioned"))
    args: Dict[str, Any] = {"phone": phone, "auction_id": auction_id, "offered_price": price}
    if payload.get("message"):
        args["message"] = str(payload["message"])
    return "place_bid", args


@register_action("SALES_ACCEPT_CONTRACT", mode="WRITE")
def prep_sales_accept_contract(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    transaction_id = str(require(payload, "bid_id"))
    return "commit_staged_transaction", {"transaction_id": transaction_id, "approved": True}


@register_action("SALES_UPDATE_PRODUCT", mode="WRITE")
def prep_sales_update_product(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    if not is_update_mode(state):
        raise ValueError("SALES_UPDATE_PRODUCT doit être invoqué en mode update.")

    entity = require_current_entity(state, intent="SALES_UPDATE_PRODUCT")
    product_id = coalesce_entity_value(
        payload,
        entity,
        payload_keys=("product_id",),
        entity_keys=("product_id", "id"),
    )
    if not product_id:
        raise ValueError("Impossible d'identifier le produit à modifier.")

    price_raw = coalesce_entity_value(
        payload,
        entity,
        payload_keys=("price_mentioned", "price"),
        entity_keys=("price", "price_fcfa", "unit_price"),
    )
    quantity_raw = coalesce_entity_value(
        payload,
        entity,
        payload_keys=("quantity_mentioned", "quantity"),
        entity_keys=("quantity_for_sale", "quantity", "qty"),
    )

    price_value = to_float(price_raw, field="price") if price_raw not in _EMPTY_SLOT_VALUES else None
    quantity_value = to_float(quantity_raw, field="quantity") if quantity_raw not in _EMPTY_SLOT_VALUES else None

    if price_value is None and quantity_value is None:
        raise ValueError("Aucune nouvelle valeur (prix ou quantité) n'a été fournie pour la mise à jour du produit.")

    args: Dict[str, Any] = {"phone": phone, "product_id": str(product_id)}
    if price_value is not None:
        args["price"] = price_value
    if quantity_value is not None:
        args["quantity"] = quantity_value
    return "update_product_price_and_qty", args


@register_action("SALES_LIST_ORDERS", mode="READ")
def prep_sales_list_orders(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    status_raw = payload.get("status") or payload.get("order_status")
    status = str(status_raw).upper().strip() if status_raw else None
    limit_raw = payload.get("limit") or payload.get("top")
    args: Dict[str, Any] = {"phone": phone}
    if status:
        args["status"] = status
    if limit_raw not in (None, ""):
        try:
            args["limit"] = max(1, int(limit_raw))
        except (TypeError, ValueError):
            pass
    return "get_producer_orders", args
