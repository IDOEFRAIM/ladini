"""Buyer negotiation gate — counter-offers, bid viewing, session lifecycle.

## Certification du prix de négociation (Phase B2c.6)

`_initiate_negotiation` et `_handle_counter_price` écrivaient `Auction.max_price_per_unit` depuis
un float brut extrait du message par l'interprète, sans base ni provenance — la même classe de bug
que celle déjà fermée pour les bids (B2b, `domain/bid_pricing_flow.py`) et pour
`PROCUREMENT_CREATE_REQUEST` (B2c.5). Sur une enchère de 10 TONNE, « 4 millions » pouvait devenir
4 000 000 FCFA/TONNE (une erreur ×10) au lieu d'un budget de 4 000 000 FCFA pour tout le lot.

Ce module réutilise le moteur `bid_pricing_flow.parse_bid_price` (même extraction montant/base/
provenance que les bids) et `domain/negotiation_offer.py` (même schéma de décision figée que
`CertifiedAwardDecision`, plus bas dans ce fichier) : AUCUNE écriture (`initiate_negotiation_session`
/ `update_negotiation_offer`) n'a lieu tant que la base du prix n'est pas certifiée, et ce qui est
confirmé à l'écran est exactement ce qui est envoyé à l'écriture."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ladini.core.idempotency import claim_once
from ladini.domain.bid_award import CertifiedAwardDecision
from ladini.domain.bid_pricing_flow import (
    BidPriceContext,
    BidPriceParse,
    BidPriceStatus,
    find_amounts,
    parse_bid_price,
    price_per_unit_question,
    resolve_basis_reply,
    resolve_package_reply,
)
from ladini.domain.commercial_pricing_snapshot import PricingSnapshotError
from ladini.domain.negotiation_offer import (
    CertifiedNegotiationOffer,
    NegotiationPriceNotCertifiable,
    build_negotiation_offer,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.buyer.award_decision import (
    confirmation_message,
    execute_award,
    lookup_award,
    requalification_message,
    unavailable_message,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    AuctionGateway,
    NegotiationGateway,
    ProductGateway,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
    llm_deviation_reply,
)

from .helpers import (
    NEGOTIATION_GOALS,
    logger,
    negotiation_action_menu,
    negotiation_choice_from_index,
    resolve_product,
    resolve_quantity,
)

# =====================================================================
# INTERNAL — build bids list UI (DRY, used in multiple branches)
# =====================================================================


def _build_bids_menu(
    bids: List[Dict[str, Any]],
    auction_id: str,
) -> Dict[str, Any]:
    """Build a selection menu from a list of bids. Returns a state patch."""
    mapping: Dict[str, str] = {}
    lines = ["📥 *Offres reçues :*"]
    options: List[Dict[str, str]] = []
    for i, b in enumerate(bids, start=1):
        bid_id = str(b.get("bid_id") or b.get("id") or "")
        producer = b.get("producer") or b.get("producer_name") or "Producteur"
        # Phase B2b : la sémantique de prix PROPRE à l'offre (base, total), jamais `offered_price` seul.
        if b.get("pricing_label"):
            price_txt = str(b["pricing_label"])
            if b.get("comparable_total") is not None:
                price_txt += f" (total {b['comparable_total']} FCFA)"
            elif b.get("requires_requalification"):
                price_txt += " ⚠️ à préciser par le producteur"
        else:
            price_txt = f"{b.get('price') or b.get('offered_price') or '?'} FCFA"
        lines.append(f"\n*{i}. {producer}* — 💰 {price_txt}")
        options.append({"index": str(i), "label": f"{producer} — {price_txt}"})
        if bid_id:
            mapping[str(i)] = bid_id
    lines.append("\n_Répondez avec le numéro pour accepter une offre._")

    bids_text = "\n".join(lines)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": bids_text,
        "transaction_payload": {"resolved_id": None, "bid_id": None},
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Offres reçues",
            options=[
                MenuOption(
                    index=o["index"], label=o["label"], value=mapping.get(o["index"])
                )
                for o in options
            ],
            kind="bid",
            metadata={"auction_id": str(auction_id)},
            preformatted_text=bids_text,
        ),
    }


async def _fetch_and_show_bids(
    mc_runtime: MarketRuntime,
    auction_id: str,
    nctx: Dict[str, Any],
    target_phase: str = "VIEWING_OFFERS",
    phone: str | None = None,
) -> Dict[str, Any]:
    """Fetch auction bids and return a menu state patch or fallback to negotiation menu."""
    # phone requis pour l'identité de contexte MCP (sinon PermissionDenied).
    buyer_phone = phone or nctx.get("buyer_phone") or nctx.get("phone")
    # Chantier résilience 2026-08 : cet appel reposait uniquement sur le
    # filet générique `_safe_node` (utils.py), qui catch bien l'exception
    # pour éviter un crash dur du tour, mais ne nettoie PAS
    # `negotiation_context` — un timeout MCP laissait l'utilisateur bloqué
    # dans une phase de négociation incohérente au tour suivant. Miroir du
    # pattern déjà utilisé côté flows/producer (try/except + reset propre).
    try:
        bids_res = await AuctionGateway(mc_runtime).get_auction_bids(
            auction_id=str(auction_id), phone=str(buyer_phone) if buyer_phone else None
        )
    except Exception as exc:
        logger.error("_fetch_and_show_bids: get_auction_bids a échoué: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible de charger les offres pour le moment. Réessayez dans un instant.",
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {"__reset__": True},
            "ag_ui_component": None,
        }
    bids = bids_res.get("bids") or []

    if str(bids_res.get("status") or "").lower() != "success" or not bids:
        msg = bids_res.get("message") or "Aucune offre reçue pour l'instant."
        neg_menu = negotiation_action_menu(str(auction_id))
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": msg,
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
            "ag_ui_component": None,
            "pending_menu": neg_menu,
        }

    result = _build_bids_menu(bids, auction_id)
    result["negotiation_context"] = {**nctx, "phase": target_phase}
    return result


# =====================================================================
# PRICE CERTIFICATION — commun à l'ouverture d'une négociation et à une
# contre-offre. Réutilise `bid_pricing_flow.parse_bid_price` (moteur B2b) :
# AUCUNE base n'est jamais devinée, AUCUNE écriture avant résolution.
# =====================================================================


def _to_qty(value: Any) -> Optional[float]:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def _price_clarification_patch(
    parsed: BidPriceParse,
    *,
    resume_phase: str,
    nctx_patch: Dict[str, Any],
) -> Dict[str, Any]:
    """WAITING_INPUT : la base (ou le contenu du conditionnement) manque — on demande, on n'écrit
    jamais rien tant qu'elle n'est pas certifiée."""
    field = "package_size" if parsed.status == BidPriceStatus.NEEDS_PACKAGE_SIZE else "price_basis"
    question = parsed.message or "Quel est votre prix (FCFA), et est-ce par unité ou pour tout le lot ?"
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name=field),
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": question,
        "negotiation_context": {**nctx_patch, "phase": resume_phase, "pending_price": parsed.to_state()},
        "ag_ui_component": None,
    }


def _certify_negotiation_price(
    parsed: BidPriceParse,
    *,
    ref: Dict[str, Any],
    quantity: Any,
    phone: str,
    auction_id: Optional[str],
) -> "tuple[Optional[CertifiedNegotiationOffer], Optional[str]]":
    """`(offer, None)` si certifiable ; sinon `(None, message)` — jamais d'exception qui remonte au
    graphe. `parsed` DOIT déjà être `RESOLVED` (base connue, provenance exécutable)."""
    try:
        snapshot = parsed.snapshot(quantity, ref["unit"])
    except PricingSnapshotError as exc:
        return None, f"Ce prix ne correspond pas à cette enchère ({exc}). Quel est votre prix, et est-ce par unité ou pour tout le lot ?"
    try:
        offer = build_negotiation_offer(
            product_id=ref["product_id"],
            product_name=ref.get("name") or "ce produit",
            buyer_phone=phone,
            pricing=snapshot,
            auction_quantity=quantity,
            auction_unit=ref["unit"],
            auction_id=auction_id,
        )
    except NegotiationPriceNotCertifiable as exc:
        return None, exc.message
    return offer, None


def _negotiation_offer_confirm_patch(
    offer: CertifiedNegotiationOffer,
    *,
    confirm_phase: str,
    nctx_patch: Dict[str, Any],
    intro: str,
) -> Dict[str, Any]:
    """WAITING_INPUT CONFIRM_ACTION : le prix est certifié, RIEN n'est encore écrit. Ce que
    l'acheteur confirme ici est exactement ce que `_handle_confirm_negotiation_offer` persistera."""
    text = f"{offer.confirmation_text(intro=intro)}\n\n👉 Répondez *oui* pour confirmer, ou *non* pour annuler."
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
        "response_strategy": "ASK_MISSING_FIELD",
        "final_response": text,
        "negotiation_context": {**nctx_patch, "phase": confirm_phase, "pending_offer": offer.to_state()},
        "ag_ui_component": None,
    }


async def _handle_negotiation_price_clarification(
    mc_runtime: MarketRuntime,
    state: Dict[str, Any],
    nctx: Dict[str, Any],
    phone: str,
    auction_id: Optional[str],
) -> Dict[str, Any]:
    """Réponse à une question de base/conditionnement posée par `_price_clarification_patch` — pour
    l'ouverture d'une négociation (`auction_id=None`) COMME pour une contre-offre (`auction_id` fixé).
    Une réponse qui porte SON PROPRE nombre est un NOUVEAU prix, jamais fusionnée avec l'ancien."""
    ref = dict(nctx.get("pending_product") or {})
    quantity = nctx.get("pending_quantity")
    pending = BidPriceParse.from_state(nctx.get("pending_price"))
    text = str(state.get("normalized_text") or state.get("user_query") or "")
    unit = str(ref.get("unit") or nctx.get("unit") or "")

    if pending is not None and pending.status == BidPriceStatus.NEEDS_PACKAGE_SIZE:
        parsed = resolve_package_reply(pending, text, auction_unit=unit)
    elif pending is not None and pending.amount is not None:
        parsed = resolve_basis_reply(text, amount=pending.amount, auction_unit=unit, auction_quantity=quantity)
    else:
        parsed = parse_bid_price(text, auction_unit=unit, auction_quantity=quantity)

    if not parsed.is_resolved:
        resume_phase = (
            "AWAIT_NEGOTIATION_PACKAGE_SIZE"
            if parsed.status == BidPriceStatus.NEEDS_PACKAGE_SIZE
            else "AWAIT_NEGOTIATION_PRICE_BASIS"
        )
        return _price_clarification_patch(
            parsed, resume_phase=resume_phase,
            nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
        )

    offer, err = _certify_negotiation_price(parsed, ref=ref, quantity=quantity, phone=phone, auction_id=auction_id)
    if offer is None:
        return _price_clarification_patch(
            BidPriceParse(BidPriceStatus.NEEDS_BASIS, message=err),
            resume_phase="AWAIT_NEGOTIATION_PRICE_BASIS",
            nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
        )
    confirm_phase = "CONFIRM_NEGOTIATION_COUNTER" if auction_id else "CONFIRM_NEGOTIATION_INIT"
    intro = (
        "🔁 Voici votre nouvelle contre-offre :"
        if auction_id
        else f"🤝 Ouverture d'une négociation sur *{ref.get('name') or 'ce produit'}*."
    )
    return _negotiation_offer_confirm_patch(
        offer, confirm_phase=confirm_phase,
        nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
        intro=intro,
    )


async def _handle_confirm_negotiation_offer(
    mc_runtime: MarketRuntime,
    state: Dict[str, Any],
    nctx: Dict[str, Any],
    phone: str,
    auction_id: Optional[str],
) -> Dict[str, Any]:
    """CONFIRM_NEGOTIATION_INIT (`auction_id=None`) / CONFIRM_NEGOTIATION_COUNTER (`auction_id`
    fixé) : exécute l'écriture (ouverture ou mise à jour du plafond) sur les TERMES CERTIFIÉS figés
    par `pending_offer`, jamais sur `transaction_payload`/l'état mutable relu après coup (Golden F)."""
    event = str(state.get("interpreted_event") or "").upper().strip()
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()
    offer = CertifiedNegotiationOffer.from_state(nctx.get("pending_offer"))
    ref = dict(nctx.get("pending_product") or {})
    quantity = nctx.get("pending_quantity")

    def _cancelled() -> Dict[str, Any]:
        if not auction_id:
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": "D'accord, aucun prix n'a été retenu.",
                "current_goal": None,
                "transaction_payload": {"__reset__": True},
                "negotiation_context": {"__reset__": True},
                "ag_ui_component": None,
            }
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": "D'accord, contre-offre annulée.",
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
            "ag_ui_component": None,
            "pending_menu": negotiation_action_menu(str(auction_id)),
        }

    if offer is None or event == "REJECT" or text in _AWARD_NO:
        return _cancelled()

    if not (event == "CONFIRM" or text in _AWARD_YES):
        # Une correction de prix pendant la confirmation -> une NOUVELLE certification, jamais un
        # remplacement en place du montant déjà figé (Golden E : "finalement 4 millions pour tout").
        if find_amounts(text):
            unit = str(ref.get("unit") or nctx.get("unit") or "")
            parsed = parse_bid_price(text, auction_unit=unit, auction_quantity=quantity)
            if parsed.is_resolved:
                new_offer, err = _certify_negotiation_price(
                    parsed, ref=ref, quantity=quantity, phone=phone, auction_id=auction_id
                )
                if new_offer is not None:
                    confirm_phase = "CONFIRM_NEGOTIATION_COUNTER" if auction_id else "CONFIRM_NEGOTIATION_INIT"
                    intro = (
                        "🔁 Voici votre nouvelle contre-offre :"
                        if auction_id
                        else f"🤝 Ouverture d'une négociation sur *{ref.get('name') or 'ce produit'}*."
                    )
                    return _negotiation_offer_confirm_patch(
                        new_offer, confirm_phase=confirm_phase,
                        nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
                        intro=intro,
                    )
            return _price_clarification_patch(
                parsed, resume_phase="AWAIT_NEGOTIATION_PRICE_BASIS",
                nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
            )
        note = await llm_deviation_reply(
            mc_runtime, text, "confirmer ce prix (oui/non)",
        )
        prompt = "Répondez *oui* pour confirmer, ou *non* pour annuler."
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": f"{note}\n\n{prompt}" if note else prompt,
            "negotiation_context": nctx,
            "ag_ui_component": None,
        }

    # CONFIRM — écriture sur les termes CERTIFIÉS ci-dessus, jamais recalculés depuis le texte brut.
    if not claim_once(offer.idempotency_key):
        if not auction_id:
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": "Cette négociation est déjà en cours d'ouverture.",
                "negotiation_context": {"__reset__": True},
                "ag_ui_component": None,
            }
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": "Ce prix est déjà en cours d'enregistrement.",
            "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
            "ag_ui_component": None,
            "pending_menu": negotiation_action_menu(str(auction_id)),
        }

    if auction_id is None:
        try:
            res = await NegotiationGateway(mc_runtime).initiate_session(
                buyer_phone=phone,
                product_id=offer.product_id,
                offered_price=float(offer.ceiling_per_unit),
                quantity=float(offer.auction_quantity),
            )
        except Exception as exc:
            logger.error("_handle_confirm_negotiation_offer: initiate_session a échoué: %s", exc)
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": "Impossible d'ouvrir la négociation pour le moment. Réessayez dans un instant.",
                "negotiation_context": {"__reset__": True},
                "ag_ui_component": None,
            }
        if str(res.get("status") or "").upper() not in {"PENDING", "SUCCESS"}:
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": res.get("message") or "Impossible d'ouvrir la négociation.",
                "fallback_recommendations": res.get("fallback") or [],
                "negotiation_context": {"__reset__": True},
                "ag_ui_component": None,
            }
        neg_init_menu = MenuRequest(
            title="Négociation en cours",
            options=negotiation_action_menu(str(res.get("negotiation_id") or "")).options,
            kind="negotiation_action",
            metadata={"session_id": res.get("negotiation_id")},
        )
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": res.get("message"),
            "negotiation_context": {
                "session_id": res.get("negotiation_id"),
                "auction_id": res.get("auction_id"),
                "product_id": res.get("product_id"),
                "producer_id": res.get("producer_id"),
                "buyer_offer": res.get("buyer_offer"),
                "seller_minimum": res.get("seller_minimum"),
                "status": "PENDING",
                "phase": "NEGOTIATION_MENU",
                "last_message": res.get("message"),
                # Persistés pour la PROCHAINE contre-offre (unité/quantité de l'enchère, snapshot
                # de prix déjà certifié) — avant ce correctif, `_initiate_negotiation` ne les
                # écrivait jamais dans `negotiation_context`, ce qui rendait toute contre-offre
                # suivante à nouveau ambiguë (aucun contexte de base à réutiliser).
                "quantity": float(offer.auction_quantity),
                "unit": offer.auction_unit,
                "product_name": offer.product_name,
                "pricing": offer.pricing.to_dict(),
            },
            "ag_ui_component": None,
            "pending_menu": neg_init_menu,
        }

    # Contre-offre sur une négociation déjà ouverte.
    try:
        upd = await NegotiationGateway(mc_runtime).update_offer(
            buyer_phone=phone,
            negotiation_id=str(auction_id),
            new_price=float(offer.ceiling_per_unit),
        )
    except Exception as exc:
        logger.error("_handle_confirm_negotiation_offer: update_offer a échoué: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible d'enregistrer votre contre-offre pour le moment. Réessayez dans un instant.",
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {"__reset__": True},
            "ag_ui_component": None,
        }
    if not is_success_response(upd):
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": upd.get("message") or "La contre-offre n'a pas pu être enregistrée.",
            "negotiation_context": {**nctx, "phase": "NEGOTIATION_MENU"},
            "ag_ui_component": None,
            "pending_menu": negotiation_action_menu(str(auction_id)),
        }
    msg = upd.get("message") or offer.confirmation_text(intro="🔁 Offre mise à jour.")
    neg_menu = negotiation_action_menu(str(auction_id))
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": msg,
        "transaction_payload": {"resolved_id": None},
        "negotiation_context": {
            **nctx,
            "phase": "NEGOTIATION_MENU",
            "buyer_offer": upd.get("new_price") or nctx.get("buyer_offer"),
            "pricing": offer.pricing.to_dict(),
        },
        "ag_ui_component": None,
        "pending_menu": neg_menu,
    }


# =====================================================================
# PHASE: AWAIT_COUNTER_PRICE — buyer submits a new price
# =====================================================================


async def _handle_counter_price(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Process the counter-offer price submission — certifie la base du prix (comme à l'ouverture)
    avant toute confirmation ; `update_offer` n'est appelé que depuis
    `_handle_confirm_negotiation_offer`, jamais ici."""
    state = state or {}
    text = str(state.get("normalized_text") or state.get("user_query") or "")
    if not find_amounts(text):
        base_question = "Quel est votre nouveau prix (FCFA) ?"
        # Chantier résilience 2026-08 (volet acheteur) : cette branche n'est
        # JAMAIS l'affichage initial de la question (celui-ci part de
        # `_handle_negotiation_menu::NEGOTIATION_COUNTER`, qui ne passe pas
        # par ici) — elle traite TOUJOURS une vraie réponse de l'utilisateur
        # qui n'a pas pu être lue comme un prix (aucun montant dans le texte).
        # Accuse d'abord réception via le LLM au lieu de rejouer la même question mot pour mot.
        note = None
        if text.strip():
            note = await llm_deviation_reply(
                mc_runtime, text, "répondre au nouveau prix de votre contre-offre (en FCFA)",
            )
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="price"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": f"{note}\n\n{base_question}" if note else base_question,
            "ag_ui_component": None,
        }

    unit = str(nctx.get("unit") or "")
    quantity = nctx.get("quantity")
    # Cette branche est TOUJOURS la réponse à la question posée par `NEGOTIATION_COUNTER`
    # (« Quel prix proposez-vous *par {unit}* ? ») : un montant nu ici est QUESTION_CONTEXT_EXPLICIT
    # par l'unité de l'enchère (Golden D), jamais ambigu — un montant EXPLICITEMENT marqué autrement
    # dans le texte (« ... pour tout », « ... la caisse ») garde priorité (voir `parse_bid_price`).
    context = BidPriceContext.per_auction_unit(unit) if unit else None
    parsed = parse_bid_price(text, auction_unit=unit, auction_quantity=quantity, context=context)
    ref = {"unit": unit, "name": nctx.get("product_name"), "product_id": nctx.get("product_id")}

    if not parsed.is_resolved:
        resume_phase = (
            "AWAIT_NEGOTIATION_PACKAGE_SIZE"
            if parsed.status == BidPriceStatus.NEEDS_PACKAGE_SIZE
            else "AWAIT_NEGOTIATION_PRICE_BASIS"
        )
        return _price_clarification_patch(
            parsed, resume_phase=resume_phase,
            nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
        )

    offer, err = _certify_negotiation_price(parsed, ref=ref, quantity=quantity, phone=phone, auction_id=str(auction_id))
    if offer is None:
        return _price_clarification_patch(
            BidPriceParse(BidPriceStatus.NEEDS_BASIS, message=err),
            resume_phase="AWAIT_NEGOTIATION_PRICE_BASIS",
            nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
        )
    return _negotiation_offer_confirm_patch(
        offer, confirm_phase="CONFIRM_NEGOTIATION_COUNTER",
        nctx_patch={**nctx, "pending_product": ref, "pending_quantity": quantity},
        intro="🔁 Voici votre nouvelle contre-offre :",
    )


# =====================================================================
# PHASE: VIEWING_OFFERS — buyer selects a bid to accept
# =====================================================================


async def _handle_viewing_offers(
    mc_runtime: MarketRuntime,
    payload: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
    phone: str,
) -> Dict[str, Any]:
    """Process bid selection or re-display bids.

    (2026-09-28, audit fiabilité agent — gap réel confirmé) : `phone` est
    désormais un paramètre EXPLICITE — `negotiation_gate` le calcule déjà
    depuis `state["user_phone"]` (la SEULE source fiable de l'identité de
    l'appelant réel) pour les 2 AUTRES branches de phase
    (`_handle_counter_price`/`_handle_negotiation_menu`), mais cette
    branche-ci recalculait sa propre variable (`_buyer_phone = nctx.get(
    "buyer_phone") or nctx.get("phone")`) — et `_initiate_negotiation`
    n'écrit JAMAIS ces deux clés dans `negotiation_context` (vérifié :
    aucun site d'écriture), donc `_buyer_phone` valait TOUJOURS `None` en
    pratique. Conséquence directe : `AuctionMixin.select_winning_bid`
    (services/database/auction.py) ne filtre PAS par propriétaire de
    l'enchère (contrairement à son voisin `cancel_auction`, MÊME fichier) —
    son SEUL rôle de sécurité pour ce chemin est le `phone` qu'on lui
    passe, exploité UNIQUEMENT si on lui en donne un vrai. `bid_id` reste
    par ailleurs toujours sourcé depuis `get_auction_bids` scopé à CETTE
    session de négociation (jamais un texte libre arbitraire), donc ce
    correctif est une défense en profondeur — pas la fermeture d'un
    exploit conversationnel démontré — mais la seule protection dont
    dispose cette écriture irréversible (création de commande) ne doit
    jamais dépendre d'un paramètre systématiquement `None`."""
    bid_id = payload.get("bid_id")
    if bid_id:
        # Phase B2b : choisir une ligne ne désigne PAS le gagnant. On construit la décision d'attribution
        # certifiée (prix + BASE + quantité + total), on l'affiche et on attend la confirmation — jamais
        # d'attribution sur un numéro de ligne, jamais sur un bid dont la base est inconnue.
        _buyer_phone = phone or nctx.get("buyer_phone") or nctx.get("phone")
        lookup = await lookup_award(mc_runtime, str(auction_id), str(bid_id), str(_buyer_phone or ""))
        if lookup.found and lookup.requires_requalification:
            return {
                "status": "COMPLETED",
                "response_strategy": "SUCCESS",
                "final_response": requalification_message(lookup),
                "transaction_payload": {"resolved_id": None, "bid_id": None},
                "negotiation_context": {"__reset__": True},
                "ag_ui_component": None,
            }
        if not lookup.selectable:
            return {
                "status": "COMPLETED",
                "response_strategy": "ERROR",
                "final_response": unavailable_message(),
                "transaction_payload": {"resolved_id": None, "bid_id": None},
                "negotiation_context": {"__reset__": True},
                "ag_ui_component": None,
            }
        assert lookup.decision is not None
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": confirmation_message(lookup),
            "transaction_payload": {"resolved_id": None, "bid_id": None},
            "negotiation_context": {
                **nctx,
                "phase": "CONFIRM_AWARD",
                "pending_award": lookup.decision.to_state(),
            },
            "ag_ui_component": None,
        }

    # No bid_id yet — fetch and show bids
    return await _fetch_and_show_bids(mc_runtime, auction_id, nctx, "VIEWING_OFFERS")


# =====================================================================
# PHASE: CONFIRM_AWARD — l'acheteur confirme une décision d'attribution FIGÉE (Phase B2b)
# =====================================================================

_AWARD_YES = frozenset({"oui", "ok", "okay", "daccord", "d'accord", "confirme", "confirmer", "je confirme",
                        "valide", "valider", "yes", "go", "vasy", "parfait", "c'est bon", "cest bon"})
_AWARD_NO = frozenset({"non", "annuler", "annule", "stop", "cancel", "quitter", "retour", "pas maintenant"})


async def _handle_confirm_award(
    mc_runtime: MarketRuntime,
    state: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
    phone: str,
) -> Dict[str, Any]:
    """Exécute la décision CONFIRMÉE (`nctx.pending_award`), après revalidation contre l'état réel. Termes
    changés -> nouvelle confirmation ; jamais d'attribution sur un objet mutable relu après coup."""
    event = str(state.get("interpreted_event") or "").upper().strip()
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()
    frozen = CertifiedAwardDecision.from_state(nctx.get("pending_award"))

    def _done(message: str, strategy: str = "SUCCESS") -> Dict[str, Any]:
        return {
            "status": "COMPLETED",
            "response_strategy": strategy,
            "final_response": message,
            "current_goal": None,
            "transaction_payload": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "ag_ui_component": None,
        }

    if frozen is None or event == "REJECT" or text in _AWARD_NO:
        return _done("D'accord, aucune proposition n'a été retenue. Tapez *mes appels d'offres* pour les revoir.")
    if not (event == "CONFIRM" or text in _AWARD_YES):
        note = await llm_deviation_reply(
            mc_runtime, text, "confirmer le gagnant retenu pour cet appel d'offres (oui/non)",
        )
        prompt = "Répondez *oui* pour confirmer le gagnant, ou *non* pour annuler."
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": f"{note}\n\n{prompt}" if note else prompt,
            "negotiation_context": nctx,
            "ag_ui_component": None,
        }

    lookup = await lookup_award(mc_runtime, str(auction_id), frozen.bid_id, phone)
    if lookup.found and lookup.requires_requalification:
        return _done(requalification_message(lookup))
    if not lookup.selectable:
        return _done(unavailable_message(), "ERROR")
    assert lookup.decision is not None
    if lookup.decision.fingerprint != frozen.fingerprint:
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "⚠️ Les termes de cette offre ont *changé* entre-temps.\n\n" f"{confirmation_message(lookup)}"
            ),
            "negotiation_context": {**nctx, "phase": "CONFIRM_AWARD", "pending_award": lookup.decision.to_state()},
            "ag_ui_component": None,
        }

    # Point GPS de livraison — best-effort (voir `[[gps-delivery-burkina-faso-2026-08]]` pour cet écart connu).
    delivery_lat: Optional[float] = None
    delivery_lon: Optional[float] = None
    if phone:
        try:
            from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
                _get_stored_location,
            )

            delivery_lat, delivery_lon = await _get_stored_location(mc_runtime, phone)
        except Exception:
            logger.warning("_handle_confirm_award: échec de lecture du point GPS par défaut")
    try:
        win = await execute_award(mc_runtime, frozen, phone=phone, delivery_lat=delivery_lat, delivery_lon=delivery_lon)
    except Exception as exc:
        logger.error("_handle_confirm_award: select_winning_bid a échoué: %s", exc)
        business = str(getattr(exc, "error_code", "") or "").upper() == "BUSINESS_ERROR"
        return _done(
            str(exc) if business else "Impossible de valider cette offre pour le moment. Réessayez dans un instant.",
            "ERROR",
        )
    if str(win.get("status") or "").lower() != "success":
        return _done(win.get("message") or "Impossible de valider cette offre.", "ERROR")
    logger.info("BID_AWARD_EXECUTED | auction=%s | bid=%s | idempotency_key=%s", auction_id, frozen.bid_id, frozen.idempotency_key)
    return _done(win.get("summary_buyer") or "✅ Offre acceptée.")


# =====================================================================
# PHASE: NEGOTIATION_MENU — action selection
# =====================================================================


async def _handle_negotiation_menu(
    mc_runtime: MarketRuntime,
    phone: str,
    payload: Dict[str, Any],
    state: Dict[str, Any],
    nctx: Dict[str, Any],
    auction_id: str,
) -> Dict[str, Any]:
    """Process action selection from the negotiation menu."""
    action = payload.get("resolved_id")
    if not action:
        selection_idx = payload.get("selection_index")
        mapped = negotiation_choice_from_index(selection_idx)
        if mapped:
            action = mapped
            payload["resolved_id"] = action
        if selection_idx not in (None, ""):
            payload.pop("selection_index", None)

    action = str(action).upper().strip() if action else ""

    if not action:
        msg = (
            nctx.get("last_message")
            or state.get("final_response")
            or "Négociation en cours."
        )
        # Chantier résilience 2026-08 (volet acheteur) : cette branche traite
        # toujours une vraie réponse à un menu déjà affiché (jamais l'entrée
        # fraîche) — accuse d'abord réception via le LLM au lieu de rejouer
        # le même texte tel quel.
        user_text = str(state.get("normalized_text") or state.get("user_query") or "")
        note = None
        if user_text.strip():
            note = await llm_deviation_reply(
                mc_runtime, user_text, "choisir une action dans le menu de négociation ci-dessous",
            )
        neg_menu = negotiation_action_menu(str(auction_id))
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.SELECTION_MENU),
            "response_strategy": "SELECTION_MENU",
            "final_response": f"{note}\n\n{msg}" if note else msg,
            "ag_ui_component": None,
            "pending_menu": neg_menu,
        }

    if action == "NEGOTIATION_VIEW_OFFERS":
        return await _fetch_and_show_bids(
            mc_runtime, auction_id, nctx, "VIEWING_OFFERS", phone=phone
        )

    if action == "NEGOTIATION_COUNTER":
        # La question FIXE la base (« par TONNE ») : un montant nu en réponse est alors
        # QUESTION_CONTEXT_EXPLICIT, jamais ambigu — voir `_handle_counter_price` (Golden D).
        unit = str(nctx.get("unit") or "")
        question = price_per_unit_question(unit) if unit else "Quel est votre nouveau prix (FCFA) ?"
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="price"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": question,
            "transaction_payload": {"resolved_id": None},
            "negotiation_context": {**nctx, "phase": "AWAIT_COUNTER_PRICE"},
            "ag_ui_component": None,
        }

    # NEGOTIATION_ABORT or unknown → close
    try:
        close_res = await NegotiationGateway(mc_runtime).close_session(
            buyer_phone=phone,
            negotiation_id=str(auction_id),
            reason="buyer_abandoned",
        )
    except Exception as exc:
        logger.error("_handle_negotiation_menu: close_session a échoué: %s", exc)
        # Même en échec technique, on ferme le tunnel côté état — retenter
        # côté serveur laisserait l'utilisateur bloqué sans porte de sortie.
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": "Négociation annulée.",
            "current_goal": None,
            "transaction_payload": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "ag_ui_component": None,
        }
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": close_res.get("message") or "Négociation annulée.",
        "current_goal": None,
        "transaction_payload": {"__reset__": True},
        "negotiation_context": {"__reset__": True},
        "ag_ui_component": None,
    }


# =====================================================================
# NEGOTIATION INITIATION — open a new negotiation session
# =====================================================================


async def _initiate_negotiation(
    mc_runtime: MarketRuntime,
    phone: str,
    product_name: str,
    quantity: Any,
    payload: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Initiate a new negotiation session for a product.

    CERTIFIE la base du prix (par unité / pour tout le lot / conditionnement) AVANT toute écriture
    — jamais d'`Auction` créée depuis un float ambigu (voir en-tête du module)."""
    # Resolve product reference
    ref = await _resolve_product_ref(mc_runtime, phone, product_name)
    if ref is None:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": f"Aucun produit « {product_name} » n'est disponible pour négocier.",
            "ag_ui_component": None,
        }

    qty = _to_qty(quantity) or ref.get("available_quantity") or 1.0
    text = str(state.get("normalized_text") or state.get("user_query") or "")
    parsed = parse_bid_price(text, auction_unit=ref["unit"], auction_quantity=qty)

    if not parsed.is_resolved:
        resume_phase = (
            "AWAIT_NEGOTIATION_PACKAGE_SIZE"
            if parsed.status == BidPriceStatus.NEEDS_PACKAGE_SIZE
            else "AWAIT_NEGOTIATION_PRICE_BASIS"
        )
        return _price_clarification_patch(
            parsed, resume_phase=resume_phase,
            nctx_patch={"pending_product": ref, "pending_quantity": qty},
        )

    offer, err = _certify_negotiation_price(parsed, ref=ref, quantity=qty, phone=phone, auction_id=None)
    if offer is None:
        return _price_clarification_patch(
            BidPriceParse(BidPriceStatus.NEEDS_BASIS, message=err),
            resume_phase="AWAIT_NEGOTIATION_PRICE_BASIS",
            nctx_patch={"pending_product": ref, "pending_quantity": qty},
        )
    return _negotiation_offer_confirm_patch(
        offer, confirm_phase="CONFIRM_NEGOTIATION_INIT",
        nctx_patch={"pending_product": ref, "pending_quantity": qty},
        intro=f"🤝 Ouverture d'une négociation sur *{ref['name']}*.",
    )


async def _resolve_product_ref(
    mc_runtime: MarketRuntime,
    phone: str,
    product_name: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a product name to a catalog reference."""
    if not product_name:
        return None
    res = await ProductGateway(mc_runtime).search_products(
        product=str(product_name), phone=str(phone)
    )
    if not is_success_response(res):
        return None
    results = res.get("results") or (res.get("data") or {}).get("results") or []
    if not results:
        return None
    best = results[0]
    return {
        "product_id": str(best.get("id")),
        "crop_cycle_id": best.get("crop_cycle_id"),
        "name": str(best.get("name") or product_name),
        "price": float(best.get("price") or 0.0),
        "unit": str(best.get("unit") or "KG").upper(),
        "vendor": best.get("vendor"),
        "vendor_name": best.get("vendor_name")
        or best.get("producer_name")
        or best.get("vendor"),
        "producer_id": best.get("producer_id") or best.get("vendor_id"),
        # Repli de quantité si l'acheteur n'en a donné aucune (mirroir du défaut déjà appliqué
        # côté serveur, `services/database/buyer.py::initiate_negotiation_session`) — nécessaire
        # ICI pour certifier le prix (TOTAL_LOT, divisibilité d'un conditionnement) avant d'appeler
        # le serveur, jamais après.
        "available_quantity": float(best.get("available_quantity") or 0.0) or None,
    }


# =====================================================================
# MAIN ENTRY POINT — negotiation_gate node
# =====================================================================


async def negotiation_gate(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Negotiation gate — deterministic phase-based routing."""
    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    phone = str(state.get("user_phone") or "")
    stable_entities = dict(state.get("stable_entities") or {})

    nctx: Dict[str, Any] = dict(state.get("negotiation_context") or {})
    nphase = str(nctx.get("phase") or "NEGOTIATION_MENU").upper().strip()
    auction_id = nctx.get("auction_id") or nctx.get("session_id")

    product_name = resolve_product(payload, stable_entities, state)
    quantity = resolve_quantity(payload, stable_entities)

    if goal not in NEGOTIATION_GOALS:
        return {"status": "PLANNING", "ag_ui_component": None}

    # Route by negotiation phase — certification du prix (ouverture ET contre-offre) : ces 3 phases
    # existent AVANT toute écriture (`initiate_negotiation_session`/`update_negotiation_offer`),
    # qu'une enchère existe déjà (`auction_id` fixé, contre-offre) ou non (ouverture).
    if nphase in ("AWAIT_NEGOTIATION_PRICE_BASIS", "AWAIT_NEGOTIATION_PACKAGE_SIZE"):
        return await _handle_negotiation_price_clarification(mc_runtime, state, nctx, phone, auction_id)

    if nphase in ("CONFIRM_NEGOTIATION_INIT", "CONFIRM_NEGOTIATION_COUNTER"):
        return await _handle_confirm_negotiation_offer(mc_runtime, state, nctx, phone, auction_id)

    if auction_id and nphase == "AWAIT_COUNTER_PRICE":
        return await _handle_counter_price(mc_runtime, phone, payload, nctx, auction_id, state)

    if auction_id and nphase == "CONFIRM_AWARD":
        return await _handle_confirm_award(mc_runtime, state, nctx, auction_id, phone)

    if auction_id and nphase == "VIEWING_OFFERS":
        return await _handle_viewing_offers(mc_runtime, payload, nctx, auction_id, phone)

    if auction_id and nphase == "NEGOTIATION_MENU":
        return await _handle_negotiation_menu(
            mc_runtime, phone, payload, state, nctx, auction_id
        )

    # No active session — initiate new negotiation. `_initiate_negotiation` fait sa propre
    # extraction robuste du prix (base + provenance) depuis le TEXTE : on ne bloque plus ici sur
    # `payload.get("price")` (le slot brut de l'interprète, exactement ce qui causait le bug —
    # un « 4 millions » sans base finissait tel quel dans `Auction.max_price_per_unit`).
    if not product_name:
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": "Quel produit souhaitez-vous négocier et à quel prix ?",
            "ag_ui_component": None,
        }

    return await _initiate_negotiation(
        mc_runtime, phone, str(product_name), quantity, payload, state
    )


__all__ = ["negotiation_gate"]
