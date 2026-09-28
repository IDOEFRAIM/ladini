"""Attribution d'un appel d'offres côté ACHETEUR : confirmer un objet FIGÉ, pas un numéro de ligne (Phase B2b).

Un acheteur ne retient jamais un gagnant sans confirmer PRIX + BASE DU PRIX + QUANTITÉ + TOTAL. Ce module
est l'unique passage de « l'acheteur a choisi une offre » à « la commande est créée » pour les deux tunnels
(`order_tracking.py::confirm_winner_selection/finalize_winner` et `negotiation.py::VIEWING_OFFERS`) :

1. `lookup_award`  — lit l'offre (état RÉEL), reconstruit la `CertifiedAwardDecision` côté serveur ;
2. la confirmation affiche `decision.confirmation_text` et GÈLE la décision (`working_memory.pending_award`) ;
3. `execute_award` — appelle `select_winning_bid` avec les termes CONFIRMÉS (`expected_award`) : le serveur
   les revalide sous verrou. Ce qui est exécuté est ce qui a été confirmé, jamais l'état mutable relu après coup.

Une offre sans base de prix certifiée (bid antérieur) n'est JAMAIS attribuée ni interprétée : on l'explique à
l'acheteur, le producteur doit la requalifier. Aucune base n'est déduite.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ladini.core.formatting import fmt_num
from ladini.domain.bid_award import CertifiedAwardDecision
from ladini.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway
from ladini.graphs.agents.market_coach.utils import MarketRuntime

logger = logging.getLogger("Ladini.Market.BuyerFlow.AwardDecision")


@dataclass(frozen=True)
class AwardLookup:
    found: bool
    producer: str = "ce producteur"
    product: str = "votre produit"
    bid_status: Optional[str] = None
    price: Optional[float] = None  # montant brut, affichage de secours uniquement
    pricing_label: Optional[str] = None
    requires_requalification: bool = False
    decision: Optional[CertifiedAwardDecision] = None

    @property
    def selectable(self) -> bool:
        return self.found and self.decision is not None and self.bid_status in (None, "PENDING")


async def lookup_award(
    mc_runtime: MarketRuntime, auction_id: Optional[str], bid_id: str, phone: str
) -> AwardLookup:
    """Projection de l'offre depuis l'état RÉEL (jamais un texte mémorisé) + sa décision certifiée."""
    if not auction_id:
        return AwardLookup(found=False)
    try:
        detail = await AuctionGateway(mc_runtime).get_auction_bids(auction_id=str(auction_id), phone=phone)
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning("lookup_award: refetch failed: %s", exc)
        return AwardLookup(found=False)
    product = (detail.get("auction") or {}).get("product") or "votre produit"
    for b in detail.get("bids") or []:
        if str(b.get("bid_id") or b.get("id")) != str(bid_id):
            continue
        decision = CertifiedAwardDecision.from_state(b.get("award_decision"))
        price = b.get("price") if b.get("price") is not None else b.get("offered_price")
        return AwardLookup(
            found=True,
            producer=b.get("producer") or b.get("producer_name") or "ce producteur",
            product=product,
            bid_status=str(b.get("status") or "").upper() or None,
            price=float(price) if price is not None else None,
            pricing_label=b.get("pricing_label"),
            requires_requalification=bool(b.get("requires_requalification")),
            decision=decision,
        )
    return AwardLookup(found=False, product=product)


def confirmation_message(lookup: AwardLookup) -> str:
    assert lookup.decision is not None
    return (
        f"{lookup.decision.confirmation_text(lookup.product)}\n"
        "👉 Répondez *oui* pour confirmer, ou *non* pour annuler."
    )


def requalification_message(lookup: AwardLookup) -> str:
    """Bid sans base de prix : on n'attribue pas, on n'invente pas."""
    amount = f"{fmt_num(lookup.price)} FCFA" if lookup.price is not None else "un montant"
    return (
        f"⚠️ L'offre de *{lookup.producer}* ({amount}) n'indique pas si son prix est *par unité* "
        "(par tonne, par kg…), *par conditionnement* ou *pour l'ensemble* du lot.\n\n"
        "Je ne peux pas la retenir sans cette précision : le producteur doit la corriger dans *mes propositions*. "
        "Vous pouvez en attendant retenir une autre offre."
    )


def unavailable_message() -> str:
    return (
        "⚠️ Cette proposition n'est plus disponible (retirée ou déjà traitée entre-temps). "
        "Tapez *mes appels d'offres* pour revoir les offres actuelles."
    )


async def execute_award(
    mc_runtime: MarketRuntime,
    decision: CertifiedAwardDecision,
    *,
    phone: str,
    delivery_lat: Optional[float] = None,
    delivery_lon: Optional[float] = None,
) -> Dict[str, Any]:
    """Exécute l'attribution SUR LES TERMES CONFIRMÉS. Clé d'idempotence = celle de la décision : un double
    « oui » (ou une redélivrance du même tour) ne crée qu'UNE commande ; des termes changés = une autre clé,
    donc jamais un rejeu silencieux sur d'anciens termes."""
    logger.info(
        "BID_AWARD_EXECUTING | auction=%s | bid=%s | fingerprint=%s | idempotency_key=%s",
        decision.auction_id, decision.bid_id, decision.fingerprint[:12], decision.idempotency_key,
    )
    return await AuctionGateway(mc_runtime).select_winning_bid(
        bid_id=decision.bid_id,
        phone=phone,
        delivery_lat=delivery_lat,
        delivery_lon=delivery_lon,
        idempotency_key=decision.idempotency_key,
        expected_award={"fingerprint": decision.fingerprint},
    )


__all__ = [
    "AwardLookup",
    "lookup_award",
    "confirmation_message",
    "requalification_message",
    "unavailable_message",
    "execute_award",
]
