"""Pont node -> domaine pour le vertical slice commercial de SALES_PUBLISH_PRODUCT.

`nodes/validation.py` (le `validator` existant) appelle `evaluate_sales_publish_state` : c'est
la SEULE connexion entre l'état conversationnel LangGraph et `domain/commercial_offer_flow.py`.
Ce module ne décide rien de commercial (le domaine décide) ; il lit l'état (texte du tour,
entités dites, `PendingInteraction` de la question précédente), appelle le domaine, et
journalise les événements d'observabilité.

Observabilité (mandat étape 26) : `COMMERCIAL_OFFER_PARSED`, `COMMERCIAL_OFFER_INCOMPLETE`,
`PACKAGE_REQUIRED`, `PACKAGE_RESOLVED`, `PRICE_BASIS_RESOLVED`, `PRICE_BASIS_AMBIGUOUS`,
`COMMERCIAL_OFFER_CERTIFIED`, `COMMERCIAL_OFFER_EXECUTED`. Chaque ligne porte `flow_id`
(identifiant de conversation masqué), `draft_id`, `version` — jamais le texte de l'utilisateur.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ladini.domain.bid_pricing_flow import (
    BidPriceParse,
    is_price_reply,
    resolve_basis_reply,
    resolve_package_reply,
)
from ladini.domain.commercial_offer import CommercialOffer
from ladini.domain.commercial_offer_flow import (
    FIELD_PACKAGE_SIZE,
    FIELD_PRICE_BASIS,
    CommercialQuestion,
    SalesOfferGateResult,
    evaluate_sales_offer,
)
from ladini.domain.packaging_tiers_flow import (
    ClarificationReason,
    PricingMode,
    TierPricingResult,
    evaluate_tier_pricing,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import entities_said_this_turn
from ladini.graphs.agents.market_coach.utils import slot_has_value

logger = logging.getLogger("Ladini.MarketCoach.CommercialOffer")

#: Un `pricing_tiers` fourni par l'utilisateur (« 5 L à 500 et 10 L à 900 ») ne passe PAS par
#: `evaluate_sales_publish_state` (qui rend `None`) mais par `evaluate_sales_tier_state` ci-dessous —
#: modes `PER_PACKAGE` (1 palier nommé) / `PACKAGING_TIERS` (voir `domain/packaging_tiers_flow.py`).
SALES_GOAL = "SALES_PUBLISH_PRODUCT"


def flow_id_of(state: Mapping[str, Any]) -> str:
    """Identifiant de conversation MASQUÉ pour les logs (jamais le numéro complet)."""
    raw = str(state.get("user_phone") or state.get("session_id") or "")
    return f"…{raw[-4:]}" if raw else "unknown"


def commercial_question_from_state(state: Mapping[str, Any]) -> Optional[CommercialQuestion]:
    """La question commerciale posée au tour PRÉCÉDENT, ou `None`.

    Lue AVANT que le `validator` n'écrase `pending_interaction` : c'est elle qui dit dans quel
    contexte interpréter le message courant (« 500000 » après « quel prix par tonne ? »)."""
    pending = get_pending_interaction(dict(state))
    if pending.kind != InteractionKind.ENTER_FIELD:
        return None
    return CommercialQuestion.from_target(pending.target)


def evaluate_sales_publish_state(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Optional[SalesOfferGateResult]:
    """Construit + valide l'offre depuis l'état live. `None` si ce tour n'est pas concerné
    (produit inconnu, ou paliers de prix fournis par l'utilisateur : chemin legacy)."""
    if not payload.get("product") or payload.get("pricing_tiers"):
        return None
    text = state.get("normalized_text") or state.get("user_query") or ""
    return evaluate_sales_offer(
        payload,
        said=entities_said_this_turn(dict(state)),
        text=text,
        question=commercial_question_from_state(state),
    )


def scalar_price_replaces_tiers(state: Mapping[str, Any]) -> bool:
    """Vrai si l'utilisateur vient de redonner un prix SCALAIRE sans repréciser de paliers
    (« finalement 500 par litre ») : il sort du mode paliers — les anciens paliers ne survivent pas."""
    said = entities_said_this_turn(dict(state))
    return slot_has_value(said.get("price")) and not slot_has_value(said.get("pricing_tiers"))


def evaluate_sales_tier_state(
    state: Mapping[str, Any], payload: Mapping[str, Any]
) -> Optional[TierPricingResult]:
    """Chemin PALIERS du gate (`pricing_tiers` présent). `None` si ce tour n'est pas concerné."""
    if not payload.get("product") or not payload.get("pricing_tiers"):
        return None
    return evaluate_tier_pricing(payload)


def log_pricing_mode(
    mode: PricingMode | str, state: Mapping[str, Any], *, draft_id: Optional[str] = None
) -> None:
    """`pricing_mode=…` structuré (jamais le texte ni le montant de l'utilisateur)."""
    logger.info(
        "PRICING_MODE | pricing_mode=%s | flow_id=%s | draft_id=%s | message_sid=%s",
        getattr(mode, "value", mode), flow_id_of(state), draft_id, state.get("message_sid"),
    )


def log_pricing_clarification(
    reason: ClarificationReason | str, state: Mapping[str, Any], *, pricing_mode: Optional[str] = None
) -> None:
    """`reason=…` structuré d'une clarification tarifaire (jamais le texte de l'utilisateur)."""
    logger.info(
        "PRICING_CLARIFICATION | reason=%s | pricing_mode=%s | flow_id=%s | message_sid=%s",
        getattr(reason, "value", reason), pricing_mode, flow_id_of(state), state.get("message_sid"),
    )


_QUESTION_REASON = {
    FIELD_PACKAGE_SIZE: ClarificationReason.MISSING_PACKAGE_SIZE,
    FIELD_PRICE_BASIS: ClarificationReason.AMBIGUOUS_PRICING,
}


def log_gate_events(
    result: SalesOfferGateResult,
    state: Mapping[str, Any],
    *,
    draft_id: Optional[str] = None,
    version: Optional[int] = None,
) -> None:
    offer = result.offer
    pricing = offer.pricing
    for event in result.events:
        logger.info(
            "%s | flow_id=%s | draft_id=%s | version=%s | status=%s | missing=%s | "
            "basis=%s | basis_source=%s | message_sid=%s",
            event,
            flow_id_of(state),
            draft_id,
            version,
            result.validation.status,
            ",".join(result.validation.missing_fields) or "-",
            pricing.basis.value if pricing and pricing.basis else None,
            pricing.basis_source.value if pricing else None,
            state.get("message_sid"),
        )
    if pricing is not None and pricing.basis is not None:
        log_pricing_mode(pricing.basis.value, state, draft_id=draft_id)
    question = result.question
    if question is not None and question.requested_field in _QUESTION_REASON:
        log_pricing_clarification(
            _QUESTION_REASON[question.requested_field], state,
            pricing_mode=pricing.basis.value if pricing and pricing.basis else None,
        )


def log_offer_lifecycle(
    event: str,
    state: Mapping[str, Any],
    offer: Optional[CommercialOffer],
    *,
    draft_id: Optional[str],
    version: Optional[int],
    idempotency_key: Optional[str] = None,
) -> None:
    """`COMMERCIAL_OFFER_CERTIFIED` / `COMMERCIAL_OFFER_EXECUTED`."""
    pricing = offer.pricing if offer else None
    logger.info(
        "%s | flow_id=%s | draft_id=%s | version=%s | basis=%s | idempotency_key=%s | message_sid=%s",
        event,
        flow_id_of(state),
        draft_id,
        version,
        pricing.basis.value if pricing and pricing.basis else None,
        idempotency_key,
        state.get("message_sid"),
    )


def offer_from_payload(payload: Mapping[str, Any]) -> Optional[CommercialOffer]:
    return CommercialOffer.from_dict(payload.get("commercial_offer"))


__all__: list = [
    "SALES_GOAL",
    "flow_id_of",
    "commercial_question_from_state",
    "evaluate_sales_publish_state",
    "evaluate_sales_tier_state",
    "scalar_price_replaces_tiers",
    "log_pricing_mode",
    "log_pricing_clarification",
    "log_gate_events",
    "log_offer_lifecycle",
    "offer_from_payload",
]


# ---------------------------------------------------------------------------
# Bids (Phase B2b) : voie déterministe pour la réponse à une question de PRIX
# ---------------------------------------------------------------------------

_BID_PRICE_PHASES = frozenset({"ASK_PRICE", "ASK_PRICE_MODIFY", "CONFIRM", "CONFIRM_MODIFY"})
_BID_BASIS_PHASES = frozenset({"ASK_BASIS", "ASK_BASIS_MODIFY"})
_BID_PACKAGE_PHASES = frozenset({"ASK_PACKAGE", "ASK_PACKAGE_MODIFY"})


def bid_price_reply_expected(state: Mapping[str, Any], text: str) -> bool:
    """Vrai si l'agent vient de poser une question de prix de bid ET que `text` n'est QUE la réponse.

    Le prix d'un bid ne dépend pas d'un classifieur LLM : « 450000 » après « quel prix par tonne ? », « par
    tonne » après « par tonne ou pour l'ensemble ? » ou « 25 kg » après « que contient une caisse ? » sont
    lus par `domain/bid_pricing_flow.py` dans le contexte de la question (le LLM, lui, ne fait que suggérer)."""
    wm = state.get("working_memory") or {}
    phase = str(wm.get("bid_phase") or "").upper()
    if phase in _BID_PRICE_PHASES:
        return bool(is_price_reply(text))
    pending = BidPriceParse.from_state(wm.get("pending_bid_pricing"))
    if pending is None or pending.amount is None:
        return False
    brief = ((wm.get("my_bids_brief") if phase.endswith("_MODIFY") else wm.get("auction_brief")) or {})
    ref = wm.get("pending_modify_bid") if phase.endswith("_MODIFY") else wm.get("pending_bid_auction")
    info = brief.get(str(ref)) or {}
    unit, qty = info.get("unit"), info.get("quantity")
    if not unit or qty is None:
        return False
    if phase in _BID_BASIS_PHASES:
        return bool(resolve_basis_reply(text, amount=pending.amount, auction_unit=str(unit), auction_quantity=qty).is_resolved)
    if phase in _BID_PACKAGE_PHASES:
        return bool(resolve_package_reply(pending, text, auction_unit=str(unit)).is_resolved)
    return False
