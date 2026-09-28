from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ladini.domain.commercial_offer import (
    CommercialQuantity,
    convert_commercial_quantity_to_base_unit,
)
from ladini.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
    require,
    require_phone,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolId

from .model import DomainContext, DomainResult

_EMPTY_SLOT_VALUES = (None, "", [], {})


def _pick_first_value(*candidates: Any) -> Any:
    for candidate in candidates:
        if candidate not in _EMPTY_SLOT_VALUES:
            return candidate
    return None


def _resolve_mass_payload(payload: Mapping[str, Any]) -> Tuple[float, str, float]:
    """Resolve quantity + unit for publish / record intents from raw payload.

    Returns `(quantity_in_base_unit, base_unit, price_rescale_factor)`.
    `price_rescale_factor` is what a PER-BASE-UNIT price must be DIVIDED by
    to stay correctly paired with the returned quantity/unit (2026-09-28,
    mandat "Commercial Quantity & Pricing Domain Hardening", bug P0 confirmé
    par audit : ce facteur était calculé pour la quantité mais jamais
    répercuté sur le prix — "200 tonnes à 500 000 F la tonne" devenait
    500 000 F/KG, un facteur 1000x, sans aucune erreur). 1.0 when no
    conversion happened (already KG, or a package/unrecognized unit that
    `normalize_quantity_to_kg` now deliberately leaves untouched — see its
    docstring)."""

    quantity_raw = _pick_first_value(
        payload.get("original_quantity"),
        payload.get("quantity_display"),
        payload.get("quantity"),
    )
    if quantity_raw is None:
        quantity_raw = require(payload, "quantity")

    unit_raw = _pick_first_value(
        payload.get("original_unit"),
        payload.get("unit_display"),
        payload.get("unit"),
        payload.get("unit"),
    )

    try:
        qty_value = float(str(quantity_raw).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Quantité invalide: {quantity_raw!r}") from exc

    qty_kg, canonical_unit = normalize_quantity_to_kg(qty_value, unit_raw)
    # Même règle déterministe que `normalize_quantity_to_kg` (MASS/VOLUME
    # uniquement) pour retrouver le facteur EXACT qu'elle a appliqué —
    # `convert_commercial_quantity_to_base_unit` renvoie `None` (donc
    # facteur neutre 1.0 ici) exactement quand `normalize_quantity_to_kg`
    # a choisi de ne rien convertir (package/unité inconnue).
    converted = convert_commercial_quantity_to_base_unit(
        CommercialQuantity(qty_value, str(unit_raw or "KG")), canonical_unit
    )
    factor = converted[1] if converted is not None else 1.0
    return qty_kg, canonical_unit, factor


@dataclass(frozen=True)
class SalesUpdateProductCommand:
    """Strongly-typed command for SALES_UPDATE_PRODUCT domain logic."""

    producer_id: str
    product_id: str
    price: Optional[float] = None
    quantity: Optional[float] = None
    name: Optional[str] = None
    unit: Optional[str] = None
    pricing_tiers: Optional[List[Dict[str, Any]]] = None


@dataclass(frozen=True)
class SalesUpdateProductionCommand:
    """Typed command for SALES_UPDATE_PRODUCTION (mise à jour d'un lot futur / MarketOffer)."""

    producer_id: str  # en pratique le téléphone (convention DB : arg `phone`)
    cycle_id: str
    price: Optional[float] = None
    quantity: Optional[float] = None
    product_label: Optional[str] = None
    unit: Optional[str] = None
    estimated_available_at: Optional[str] = None
    production_type: Optional[str] = None


@dataclass(frozen=True)
class SalesPublishProductCommand:
    """Strongly-typed command for SALES_PUBLISH_PRODUCT domain logic."""

    producer_id: str
    product: str
    quantity: float
    unit: Optional[str]
    price: float
    description: Optional[str] = None
    category_label: Optional[str] = None
    # Voir sales_dto.py::SalesPublishProductPayload.pricing_tiers — jamais
    # passé par `_resolve_mass_payload` (conversion kg), contrairement à
    # `quantity`/`unit` ci-dessus : ces unités restent LITTÉRALES.
    pricing_tiers: Optional[List[Dict[str, Any]]] = None
    # Phase B2a — offre commerciale certifiée (sérialisée), reconstruite et re-validée par `create_product`.
    commercial_offer: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SalesRecordDirectCommand:
    """Strongly-typed command for SALES_RECORD_DIRECT domain logic."""

    producer_id: str
    product: str
    quantity: float
    unit: Optional[str]
    price: float


@dataclass(frozen=True)
class MarketGetRequestsCommand:
    """Typed command for MARKET_BROWSE_REQUESTS/MARKET_MY_REQUESTS read intent."""

    phone: str
    status: str = "OPEN"
    view_mode: str = "MARKETPLACE"
    product_name: Optional[str] = None
    zone_name: Optional[str] = None


@dataclass(frozen=True)
class SalesListOrdersCommand:
    """Typed command for SALES_LIST_ORDERS read intent."""

    phone: str
    status: Optional[str] = None
    limit: Optional[int] = None


@dataclass
class SalesService:
    """Business logic for Sales & Marketplace intents.

    Handlers call into this service and translate the returned
    ``DomainResult`` into ToolProvider calls.
    """

    context: DomainContext

    def get_catalog(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        require_phone(state)
        farm_id = (
            payload.get("farm_id")
            or (state.get("transaction_payload") or {}).get("farm_id")
            or (state.get("stable_entities") or {}).get("farm_id")
        )
        if not farm_id:
            raise ValueError(
                "L'identifiant de la ferme est requis pour consulter le catalogue."
            )

        args: Dict[str, Any] = {"farm_id": str(farm_id)}
        return DomainResult(tool_id=ToolId.GET_STOCKS, tool_args=args)

    def get_market_requests(self, command: MarketGetRequestsCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "phone": command.phone,
            "status": command.status,
            "view_mode": command.view_mode,
        }
        if command.product_name:
            args["product_name"] = command.product_name
        if command.zone_name:
            args["zone_name"] = command.zone_name
        return DomainResult(tool_id=ToolId.GET_AUCTIONS, tool_args=args)

    def get_my_proposals(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        return DomainResult(
            tool_id=ToolId.GET_MY_ACTIVE_BIDS, tool_args={"phone": phone}
        )

    def market_snapshot(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        args: Dict[str, Any] = {}
        zone = payload.get("zone") or payload.get("zone_name")
        if zone:
            args["zone_query"] = str(zone)
        # Filtre produit : "quel est le prix du riz ?" ne doit renvoyer QUE
        # le riz, pas tout le catalogue — voir get_market_snapshot pour la
        # validation catalogue + prix standard qui en découlent. Voir
        # [[precommande-architecture-consolidation-2026-08]].
        product = payload.get("product")
        if product:
            args["product_query"] = str(product)
        return DomainResult(tool_id=ToolId.GET_MARKET_SNAPSHOT, tool_args=args)

    def validate_price(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        product = str(require(payload, "product"))
        price = float(require(payload, "price"))
        zone = str(require(payload, "zone"))
        args = {
            "product_name": product,
            "proposed_price": price,
            "zone_id": zone,
        }
        return DomainResult(tool_id=ToolId.CHECK_PRICE_ANOMALY, tool_args=args)

    def publish_product(self, command: SalesPublishProductCommand) -> DomainResult:
        qty_kg, unit, price_rescale_factor = _resolve_mass_payload(
            {
                "quantity": command.quantity,
                "unit": command.unit,
            }
        )
        # (2026-09-28, hardening P0) : `Product.price` est documenté comme
        # "par `Product.unit`" (voir confirmation_summary.py et le schéma
        # DB) — si la quantité a été convertie (ex: TONNE -> KG, facteur
        # 1000), le prix DOIT être re-basé dans la MÊME proportion pour
        # rester correct ("500 000 F/TONNE" -> "500 F/KG"), jamais laissé
        # tel quel comme avant ce correctif (bug confirmé : le prix restait
        # à 500 000, silencieusement réétiqueté F/KG).
        rebased_price = command.price / price_rescale_factor
        args: Dict[str, Any] = {
            "producer_id": command.producer_id,
            "name": command.product,
            "price": rebased_price,
            "quantity_for_sale": qty_kg,
            "unit": unit,
        }
        if command.description:
            args["description"] = command.description
        if command.category_label:
            args["category_label"] = command.category_label
        if command.pricing_tiers:
            args["pricing_tiers"] = command.pricing_tiers
        if command.commercial_offer:
            args["commercial_offer"] = command.commercial_offer
        return DomainResult(tool_id=ToolId.CREATE_PRODUCT, tool_args=args)

    def record_direct_sale(self, command: SalesRecordDirectCommand) -> DomainResult:
        # `command.price` est un TOTAL de lot ici (voir `ToolId.RECORD_SALE` /
        # `services/database/marketplace.py::record_sale`, qui dérive
        # lui-même un prix unitaire via `total_price / quantity`) — jamais
        # rebasé par unité comme dans `publish_product` : un total de lot ne
        # change pas de valeur selon l'unité dans laquelle la quantité est
        # exprimée.
        qty_kg, unit, _price_rescale_factor = _resolve_mass_payload(
            {
                "quantity": command.quantity,
                "unit": command.unit,
            }
        )
        args = {
            "phone": command.producer_id,
            "product_name": command.product,
            "quantity": qty_kg,
            "total_price": command.price,
            "unit": unit,
        }
        return DomainResult(tool_id=ToolId.RECORD_SALE, tool_args=args)

    def place_bid(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        phone = require_phone(state)
        auction_id = str(require(payload, "auction_id"))
        price = float(require(payload, "price"))
        args: Dict[str, Any] = {
            "phone": phone,
            "auction_id": auction_id,
            "offered_price": price,
        }
        if payload.get("message"):
            args["message"] = str(payload["message"])
        return DomainResult(tool_id=ToolId.PLACE_BID, tool_args=args)

    def update_product(self, command: SalesUpdateProductCommand) -> DomainResult:
        """Domain logic for updating a published product from a typed command."""

        if (
            command.price is None
            and command.quantity is None
            and command.name is None
            and command.unit is None
            and command.pricing_tiers is None
        ):
            raise ValueError(
                "Indiquez au moins un champ à modifier (prix, quantité, nom, unité ou tarifs)."
            )

        args: Dict[str, Any] = {
            "phone": command.producer_id,
            "product_id": command.product_id,
        }
        if command.price is not None:
            args["price"] = command.price
        if command.quantity is not None:
            args["quantity"] = command.quantity
        if command.name is not None:
            args["name"] = command.name
        if command.unit is not None:
            args["unit"] = command.unit
        if command.pricing_tiers is not None:
            args["pricing_tiers"] = command.pricing_tiers

        return DomainResult(tool_id=ToolId.UPDATE_PRODUCT_PRICE_AND_QTY, tool_args=args)

    def update_production(self, command: SalesUpdateProductionCommand) -> DomainResult:
        """Domain logic : mise à jour partielle d'un lot futur (MarketOffer)."""
        optional = {
            "price": command.price,
            "quantity": command.quantity,
            "product_label": command.product_label,
            "unit": command.unit,
            "estimated_available_at": command.estimated_available_at,
            "production_type": command.production_type,
        }
        provided = {k: v for k, v in optional.items() if v is not None}
        if not provided:
            raise ValueError(
                "Indiquez au moins un champ à modifier (prix, quantité, nom, unité, date ou type)."
            )
        args: Dict[str, Any] = {
            "phone": command.producer_id,
            "cycle_id": command.cycle_id,
            **provided,
        }
        return DomainResult(tool_id=ToolId.UPDATE_PRODUCTION_FIELDS, tool_args=args)

    def list_orders(self, command: SalesListOrdersCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": command.phone}
        if command.status:
            args["status"] = command.status
        if command.limit is not None:
            args["limit"] = command.limit
        return DomainResult(tool_id=ToolId.GET_PRODUCER_ORDERS, tool_args=args)
