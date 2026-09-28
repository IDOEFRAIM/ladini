from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ladini.domain.commercial_offer import (
    CommercialQuantity,
    convert_commercial_quantity_to_base_unit,
)
from ladini.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolId

from .model import DomainContext, DomainResult


@dataclass(frozen=True)
class ProcurementCreateRequestCommand:
    """Immutable command for creating a procurement request (auction)."""

    phone: str
    product: str
    quantity: float
    unit: Optional[str]
    max_price: float
    deadline: Optional[object] = None  # domain parses str|datetime|None into datetime
    delivery_location: Optional[str] = None
    delivery_deadline: Optional[object] = None  # parsed in domain
    incoterm: Optional[str] = "DDP"
    auto_extend: bool = True
    zone_name: Optional[str] = None


@dataclass
class ProcurementService:
    """Business logic for procurement-related intents.

    Handlers create DTOs then Commands and call these methods.
    """

    context: DomainContext

    def create_request(self, command: ProcurementCreateRequestCommand) -> DomainResult:
        phone = command.phone
        product = command.product
        qty_raw = float(command.quantity)
        price = float(command.max_price)
        qty_kg, unit = normalize_quantity_to_kg(qty_raw, command.unit)
        # (2026-09-28, hardening P0 — même bug que `sales.py::publish_product`,
        # confirmé par audit) : `max_price` est un prix plafond PAR unité (le
        # buyer dit "500 000 F la tonne" pour 200 tonnes) — si la quantité est
        # convertie (TONNE -> KG), le plafond doit être re-basé dans la même
        # proportion, jamais laissé inchangé pendant que `unit` change.
        converted = convert_commercial_quantity_to_base_unit(
            CommercialQuantity(qty_raw, str(command.unit or "KG")), unit
        )
        price_rescale_factor = converted[1] if converted is not None else 1.0
        price = price / price_rescale_factor

        # 1. Gestion de la date limite
        deadline = command.deadline
        if isinstance(deadline, str):
            try:
                deadline = datetime.fromisoformat(deadline)
            except ValueError:
                deadline = None
        if not isinstance(deadline, datetime) or deadline <= datetime.now():
            deadline = datetime.now() + timedelta(days=30)

        # 2. Extraction des nouveaux champs obligatoires (Logistique)
        delivery_location = command.delivery_location or "Non spécifié"

        delivery_deadline = command.delivery_deadline
        if isinstance(delivery_deadline, str):
            try:
                delivery_deadline = datetime.fromisoformat(delivery_deadline)
            except Exception:  # noqa: BLE001
                delivery_deadline = None
        if not isinstance(delivery_deadline, datetime):
            # Par défaut : 3 jours après la deadline
            delivery_deadline = deadline + timedelta(days=3)

        args: Dict[str, Any] = {
            "phone": phone,
            "product_query": product,
            "qty": qty_kg,
            "unit": unit,
            "max_price": price,
            "deadline": deadline,
            "delivery_location": delivery_location,
            "delivery_deadline": delivery_deadline,
            "incoterm": command.incoterm or "DDP",
            "auto_extend": bool(command.auto_extend),
        }

        zone = command.zone_name
        if zone:
            args["zone_query"] = str(zone)

        return DomainResult(tool_id=ToolId.CREATE_AUCTION, tool_args=args)

