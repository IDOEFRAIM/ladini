"""Mixin MCP : attribution d'une demande acheteur à une campagne de disponibilités.

Un seul outil, en écriture idempotente : `record_campaign_interest`. Il ne crée JAMAIS de commande ni de réservation :
il rattache le message à la dernière campagne reçue (fenêtre de réponse), enregistre l'intérêt (une fois par message
entrant et produit), compare l'offre présentée à l'offre vivante et met en file la notification producteur.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class CampaignMixin:
    async def record_campaign_interest(
        self,
        phone: str = "",
        product_name: str = "",
        quantity: Optional[float] = None,
        unit: Optional[str] = None,
        message_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        from ladini.services.availability_campaigns import interest_service

        session = self.session  # type: ignore[attr-defined]
        if session is None:
            return {"status": "error", "message": "Session indisponible."}
        result: Dict[str, Any] = await interest_service.record_interest(
            session, phone, product_label=product_name, quantity=quantity, unit=unit, message_ref=message_ref
        )
        return result
