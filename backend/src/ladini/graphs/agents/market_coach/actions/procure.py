"""Action handlers for the Procurement domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ladini.graphs.agents.market_coach.actions.common import (
    require,
    require_phone,
)
from ladini.graphs.agents.market_coach.actions.procure_dto import (
    ProcurementCreateRequestPayload,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolResolver
from ladini.graphs.agents.market_coach.domain import DomainContext
from ladini.graphs.agents.market_coach.domain.procurement import (
    ProcurementCreateRequestCommand,
    ProcurementService,
)
from ladini.graphs.agents.market_coach.registry import register_action

# (2026-09-14, Deep Intent Architecture Cleanup) : PROCUREMENT_SELECT_
# WINNER/PROCUREMENT_ACCEPT_OFFER SUPPRIMÉS d'INTENT_CONFIG — ces handlers
# neutralisés (F4, 2026-09-04) ne servaient déjà plus qu'à transformer une
# tentative d'atteindre ce chemin en erreur technique propre plutôt qu'un
# contournement silencieux. Avec l'intent lui-même retiré du catalogue
# classifiable, NEW_TASK ne peut structurellement plus produire ces valeurs
# — le filet de sécurité applicatif devient inutile, supprimé avec lui
# (spec §33 : "un tool sans consumer -> dead tool"). Le SEUL chemin
# conversationnel réel pour désigner un gagnant reste
# BUYER_CHECK_AUCTION_STATUS -> confirm_winner_selection ->
# finalize_winner (flows/buyer/order_tracking.py), inchangé.
#
# `ProcurementSelectWinnerPayload`/`ProcurementAcceptOfferPayload`/
# `ProcurementSelectWinnerCommand`/`ProcurementAcceptOfferCommand`/
# `ProcurementService.select_winner`/`.accept_offer` (domain/procurement.py,
# actions/procure_dto.py) étaient déjà du code mort avant ce chantier —
# volontairement non supprimés ici pour limiter le rayon d'action de cette
# passe (voir le rapport final).


@register_action("PROCUREMENT_CREATE_REQUEST", mode="WRITE")
def prep_procurement_create_request(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    # Validation légère spécifique au handler (ex: présence de champs clés)
    require_phone(state)
    require(payload, "product")
    require(payload, "quantity")
    require(payload, "price")

    context = DomainContext.from_state(state)
    dto = ProcurementCreateRequestPayload.from_payload(payload)
    command = ProcurementCreateRequestCommand(
        phone=context.phone,
        product=dto.product,
        quantity=dto.quantity,
        unit=dto.unit,
        max_price=dto.price,
        deadline=dto.deadline,
        delivery_location=dto.delivery_location,
        delivery_deadline=dto.delivery_deadline,
        incoterm=dto.incoterm,
        auto_extend=bool(dto.auto_extend) if dto.auto_extend is not None else True,
        zone_name=dto.zone_name,
    )

    service = ProcurementService(context=context)
    result = service.create_request(command)

    tool_name = ToolResolver.resolve_name(result.tool_id or "create_auction")
    return tool_name, dict(result.tool_args)


