"""Action handlers for the Procurement domain."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.actions.common import (
    require,
    require_phone,
)
from agriconnect.graphs.agents.market_coach.actions.procure_dto import (
    ProcurementCreateRequestPayload,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolResolver
from agriconnect.graphs.agents.market_coach.domain import DomainContext
from agriconnect.graphs.agents.market_coach.domain.procurement import (
    ProcurementCreateRequestCommand,
    ProcurementService,
)
from agriconnect.graphs.agents.market_coach.registry import register_action

# (2026-09-04, F4 — audit anti-bypass winner-selection) : `ProcurementSelectWinnerPayload`/
# `ProcurementAcceptOfferPayload`/`ProcurementSelectWinnerCommand`/
# `ProcurementAcceptOfferCommand`/`ProcurementService.select_winner`/
# `.accept_offer` (domain/procurement.py, actions/procure_dto.py) restent
# définis mais ne sont plus appelés nulle part (recherche exhaustive) —
# code mort, volontairement NON supprimé ici (pas de cascade de suppression
# hors du périmètre de ce chantier, voir le rapport final).


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


_WINNER_SELECTION_BYPASS_MESSAGE = (
    "Ce chemin de sélection de gagnant est désactivé : il contournait le "
    "tunnel sécurisé (revalidation du prix, étape GPS, gardes de statut — "
    "voir docs/AUCTION_BID_TRANSACTIONAL_AUDIT_2026-09-04.md et "
    "AUCTION_BID_WINNER_ORDER_LIFECYCLE_2026-09-04.md). Le SEUL chemin "
    "conversationnel réel pour désigner un gagnant est "
    "BUYER_CHECK_AUCTION_STATUS -> confirm_winner_selection -> "
    "finalize_winner (flows/buyer/order_tracking.py), jamais celui-ci."
)


@register_action("PROCUREMENT_SELECT_WINNER", mode="WRITE")
def prep_procurement_select_winner(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """DÉSACTIVÉ (2026-09-04, F4 — audit anti-bypass winner-selection).

    AVANT ce correctif, ce handler résolvait vers le tool_name RÉEL
    `select_winning_bid` (la même fonction DB sécurisée que le tunnel
    `auction_tracking`) via l'exécuteur GÉNÉRIQUE — un second chemin
    conversationnel VIVANT vers la même décision métier, qui ne passait par
    AUCUNE des protections du tunnel sécurisé (revalidation de prix,
    étape GPS obligatoire, garde `auction.status`/`bid.status`). La
    fonction DB elle-même reste inchangée et intacte (mandat §19 : "ne
    duplique pas select_winning_bid") — seule cette ENTRÉE est neutralisée.
    `RuntimeError` (pas `ValueError`) : évite délibérément le mécanisme de
    "self-heal" de l'exécuteur (qui tenterait de "réparer" un champ
    manquant) — ceci n'est PAS un champ manquant, c'est un chemin
    intentionnellement bloqué, l'exécuteur le traite alors via son
    catch-all générique (message technique neutre, jamais un contournement
    silencieux).
    """
    raise RuntimeError(_WINNER_SELECTION_BYPASS_MESSAGE)


@register_action("PROCUREMENT_ACCEPT_OFFER", mode="WRITE")
def prep_procurement_accept_offer(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """DÉSACTIVÉ (2026-09-04, F4) — résolvait vers `accept_bid`, un tool_name
    qui ne correspond à AUCUNE méthode DB réelle (confirmé par l'audit
    fonctionnel global) : ce chemin échouait déjà systématiquement, mais
    avec une erreur technique brute au lieu d'un rejet propre. Même
    justification que `prep_procurement_select_winner` ci-dessus."""
    raise RuntimeError(_WINNER_SELECTION_BYPASS_MESSAGE)
