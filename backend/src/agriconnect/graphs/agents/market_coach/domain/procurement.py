from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .model import DomainContext, DomainResult
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId
from agriconnect.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
)


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


@dataclass(frozen=True)
class ProcurementSelectWinnerCommand:
    """Immutable command for selecting a winning bid in an auction."""

    phone: str
    auction_id: str
    bid_id: str


@dataclass(frozen=True)
class ProcurementAcceptOfferCommand:
    """Immutable command for accepting a bid (direct buy)."""

    phone: str
    bid_id: str


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

    def select_winner(self, command: ProcurementSelectWinnerCommand) -> DomainResult:
        bid_id = str(command.bid_id)
        return DomainResult(tool_id=ToolId.SELECT_WINNING_BID, tool_args={"bid_id": bid_id})

    def accept_offer(self, command: ProcurementAcceptOfferCommand) -> DomainResult:
        bid_id = str(command.bid_id)
        return DomainResult(tool_id=ToolId.ACCEPT_BID, tool_args={"bid_id": bid_id})
