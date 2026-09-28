"""Producer — Auctions & Bids (cycle complet côté PRODUCTEUR).

Machine à états déterministe, miroir du suivi acheteur (``order_tracking.py``) :

    parcourir  →  choisir une enchère  →  saisir un prix
        →  RÉCAP + CONFIRMATION (corrigeable)  →  déposer l'offre (place_bid)  →  suivi

Slot-filling maison : le prix est demandé, puis un récapitulatif exige une
confirmation explicite. Tant que le producteur n'a pas confirmé, il peut
renvoyer un autre montant pour corriger. Rien n'est déposé avant le « oui ».

Intégration critique avec ``nodes/memory.py`` (voir GOTCHA plus bas) : ce nœud
tourne AVANT nous (``goal_planner → memory_update → validator → context_resolver``)
et, pour un menu ``kind="auction"``, il pose ``payload["auction_id"]`` et retire
``selection_index``. On lit donc ``payload.auction_id``, jamais ``selection_index``.

On reste volontairement sur le goal ``MARKET_BROWSE_REQUESTS`` (aucun champ requis)
pendant tout le tunnel de bid : cela évite que le validator réclame lui-même le
prix (``SALES_PLACE_BID`` exige ``price``) et court-circuite notre récap.

Goals pris en charge :
  - ``MARKET_BROWSE_REQUESTS`` / ``SALES_PLACE_BID`` : découverte + dépôt d'offre.
  - ``MARKET_GET_MY_PROPOSALS``                   : suivi de l'état de mes offres.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ladini.agents.reducers import mark_deleted
from ladini.core.formatting import fmt_num as _fmt_num
from ladini.domain.bid_pricing_flow import (
    BidPriceContext,
    BidPriceParse,
    BidPriceStatus,
    find_amounts,
    parse_bid_price,
    price_per_unit_question,
    render_pricing_label,
    resolve_basis_reply,
    resolve_package_reply,
)
from ladini.domain.commercial_offer import unit_display
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingSnapshotError,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.services.domain.commercial_gate import flow_id_of
from ladini.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
    llm_deviation_reply,
)
from ladini.services.pending_photo_target import set_pending_bid_photo

logger = logging.getLogger("Ladini.Market.ProducerFlow.Auctions")


# Mots demandant la vue « tout le marché » plutôt que « mes catégories ».
# Volontairement restreint aux termes NON AMBIGUS. "marche"/"marché" en ont été
# retirés : « ça marche », « démarche », « le marché » sont indiscernables ici,
# et « tout le marché » reste capté par "tout". Mieux vaut ne PAS élargir le
# périmètre que l'élargir à tort sur un « ça marche » d'acquiescement.
_ALL_SCOPE_TOKENS = (
    "toutes",
    "tout",
    "tous",
    "autres",
    "autre",
    "elargir",
    "élargir",
)

_YES_TOKENS = frozenset(
    {
        "oui",
        "ok",
        "okay",
        "daccord",
        "d'accord",
        "cest bon",
        "c'est bon",
        "confirme",
        "confirmer",
        "je confirme",
        "valide",
        "valider",
        "go",
        "vasy",
        "vas-y",
        "parfait",
        "yes",
        "yep",
        "ouais",
    }
)
_NO_TOKENS = frozenset(
    {
        "non",
        "annuler",
        "annule",
        "annulation",
        "stop",
        "cancel",
        "quitter",
        "retour",
        "pas maintenant",
        "laisse tomber",
    }
)

# NB : le menu d'enchères utilise ``kind="auction"`` ; c'est ``nodes/memory.py``
# qui, sur la sélection, pose ``payload["auction_id"]`` (voir _AUCTION_MAPPING_KINDS).


# =====================================================================
# HELPERS
# =====================================================================


def _text_of(state: Dict[str, Any]) -> str:
    return str(state.get("normalized_text") or state.get("user_query") or "").strip()


_ALL_SCOPE_RE = re.compile(
    r"(?<![a-zàâäéèêëïîôöùûüÿç])("
    + "|".join(_ALL_SCOPE_TOKENS)
    + r")(?![a-zàâäéèêëïîôöùûüÿç])"
)


def _wants_all_scope(state: Dict[str, Any]) -> bool:
    """Le producteur demande-t-il la vue « tout le marché » plutôt que ses catégories ?

    Correspondance sur MOT ENTIER. Auparavant en sous-chaîne : « surtout »,
    « partout », « atout », « ça marche » et « démarche » déclenchaient tous à
    tort l'élargissement du périmètre (« tout » / « marche » sont contenus
    dedans). Simple préférence d'affichage, donc on garde une détection
    déterministe — mais sans les faux positifs.
    """
    return bool(_ALL_SCOPE_RE.search(_text_of(state).lower()))


def _looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{16,36}", value or ""))


def _resolve_selected_auction_id(state: Dict[str, Any]) -> Optional[str]:
    """Filet défensif : si memory_update n'a pas typé la sélection en auction_id."""
    payload = state.get("transaction_payload") or {}
    direct = payload.get("auction_id") or payload.get("selected_value")
    if direct and _looks_like_uuid(str(direct)):
        return str(direct)
    idx = payload.get("selection_index")
    if idx is None:
        idx = (state.get("extracted_entities") or {}).get("selection_index")
    if idx is None:
        raw = _text_of(state)
        if raw.isdigit():
            idx = raw
    if idx is not None:
        mapping = state.get("available_mapping") or {}
        resolved = mapping.get(str(idx))
        if resolved:
            return str(resolved)
    return str(direct) if direct else None


def _error(message: str) -> Dict[str, Any]:
    return {
        "status": "ERROR",
        "response_strategy": "ERROR",
        "final_response": message,
        "ag_ui_component": None,
    }


# Clés du tunnel de bid portées par working_memory.
# GOTCHA CRITIQUE : working_memory utilise le réducteur `merge_dict` → RETIRER
# (pop) une clé du dict retourné ne la SUPPRIME PAS de l'état (l'ancienne valeur
# survit au merge). Pour « effacer » une clé, il FAUT la remettre à None (que le
# merge écrase). Voir [[market-coach-turn-boundary-state]]. Sans ça, `bid_phase`
# et `pending_bid_price` persistaient après un bid réussi → le tour suivant
# ré-affichait le récap au lieu de parcourir, et le payload gardait `price`.
_BID_WM_KEYS = (
    "available_mapping_kind",
    "auction_menu",
    "bid_phase",
    "pending_bid_auction",
    "pending_bid_price",
    "pending_bid_pricing",
    "pending_modify_bid",
    "my_bids_brief",
    "auction_brief",
)


def _bid_wm(state: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    """working_memory pour une étape du tunnel de bid, avec surcharges explicites.

    Neutralise les marqueurs de menu volatils (available_mapping_kind/auction_menu)
    en les remettant à None (merge-safe) puis applique les clés fournies
    (``bid_phase``, ``pending_bid_auction``, ``pending_bid_price``, ...).
    """
    wm = dict(state.get("working_memory") or {})
    wm["available_mapping_kind"] = None
    wm["auction_menu"] = None
    wm.update(overrides)
    return wm


def _clear_bid_wm(state: Dict[str, Any]) -> Dict[str, Any]:
    """Efface TOUT l'état du tunnel de bid (fin/annulation) — via None-overwrite."""
    wm = dict(state.get("working_memory") or {})
    for k in _BID_WM_KEYS:
        wm[k] = None
    return wm


def _clear_bid_payload() -> Dict[str, Any]:
    """Purge les artefacts de bid du transaction_payload (merge_dict → None-overwrite).

    Empêche un `price`/`auction_id`/`bid_id` fantôme d'un tunnel précédent de
    faire croire au pipeline natif (validator/confirmation_gate/executor) qu'une
    transaction est complète — cause du récap générique parasite + « erreur
    technique » observés.
    """
    return {
        "price": None,
        "auction_id": None,
        "bid_id": None,
        "selection_index": None,
        "selected_value": None,
    }


def _auction_label(brief: Dict[str, Any]) -> tuple[str, str, Optional[float]]:
    """(produit, unité, prix_plafond) depuis le résumé d'enchère."""
    product = brief.get("product") or "l'enchère sélectionnée"
    unit = brief.get("unit") or "unité"
    max_price = brief.get("max_price")
    try:
        max_price = float(max_price) if max_price is not None else None
    except (TypeError, ValueError):
        max_price = None
    return product, unit, max_price



# =====================================================================
# PRIX D'UN BID — jamais un nombre nu : montant + BASE + provenance (Phase B2b)
# =====================================================================
# Le prix d'un bid n'est plus JAMAIS « le premier nombre du texte » (`_first_number`/`_price_from_answer`
# ont été supprimés) : il est certifié par `domain/bid_pricing_flow.py`, qui exige une base explicite
# (« 450000 la tonne », « 4,5 millions pour tout ») ou fixée par la QUESTION posée.


def _auction_terms(brief: Dict[str, Any]) -> tuple[Optional[str], Optional[float]]:
    unit = brief.get("unit")
    qty = brief.get("quantity")
    try:
        qty_f = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty_f = None
    return (str(unit).upper() if unit else None), qty_f


def _llm_hints(state: Dict[str, Any]) -> Dict[str, Any]:
    """Suggestions du LLM sur le prix — JAMAIS autoritaires (elles ne sont que journalisées)."""
    ent = state.get("extracted_entities") or {}
    return {k: ent.get(k) for k in ("price_basis", "price_unit", "package_type") if ent.get(k)}


def _log_parse(event_ctx: str, ident: str, parse: BidPriceParse, state: Optional[Dict[str, Any]] = None) -> None:
    flow_id = flow_id_of(state or {})
    logger.info(
        "BID_PRICING_PARSED | flow=%s | flow_id=%s | ref=%s | status=%s | basis=%s | source=%s",
        event_ctx, flow_id, ident, parse.status.value, parse.basis.value if parse.basis else None, parse.source.value,
    )
    if parse.status == BidPriceStatus.RESOLVED:
        logger.info("BID_PRICE_BASIS_RESOLVED | flow=%s | ref=%s | basis=%s | source=%s",
                    event_ctx, ident, parse.basis.value if parse.basis else None, parse.source.value)
    elif parse.status in {BidPriceStatus.NEEDS_BASIS, BidPriceStatus.NEEDS_PACKAGE_SIZE}:
        logger.info("BID_PRICE_BASIS_AMBIGUOUS | flow=%s | ref=%s | llm_hint=%s",
                    event_ctx, ident, parse.llm_hint_basis)


def _totals(parse: BidPriceParse, qty: Optional[float], unit: Optional[str]):
    """(snapshot, total) du prix résolu contre l'enchère, ou (None, None) si non certifiable."""
    if not parse.is_resolved or qty is None or unit is None:
        return None, None
    try:
        snap = parse.snapshot(qty, unit)
        return snap, snap.total_for(qty, unit)
    except PricingSnapshotError as exc:
        logger.warning("BID_PRICING_REJECTED | %s", exc)
        return None, None


def _pricing_lines(parse: BidPriceParse, qty: Optional[float], unit: Optional[str], max_price: Optional[float]) -> tuple[str, str]:
    """(bloc récap, avertissement plafond). Le plafond est par unité de l'enchère : comparé au prix NORMALISÉ."""
    snap, total = _totals(parse, qty, unit)
    if snap is None or total is None:
        return "", ""
    qty_txt = f"{_fmt_num(qty)} {unit_display(unit, qty)}" if qty is not None and unit else ""
    lines = (
        f"Votre prix : *{render_pricing_label(snap)}*\n"
        f"Quantité demandée : {qty_txt}\n"
        f"Total : *{_fmt_num(float(total))} FCFA*"
    )
    warn = ""
    if max_price is not None and qty:
        per_unit = float(total) / float(qty)
        if per_unit > max_price:
            warn = (
                f"\n⚠️ Ce prix ({_fmt_num(per_unit)} FCFA/{unit}) dépasse le plafond acheteur "
                f"({_fmt_num(max_price)} FCFA/{unit}) — l'acheteur risque de ne pas le retenir."
            )
    return lines, warn


# =====================================================================
# 1. DISCOVERY — lister les enchères (par catégorie)
# =====================================================================


async def browse_auctions(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error(
            "Numéro de téléphone introuvable, impossible de charger le marché."
        )

    scope = "ALL" if _wants_all_scope(state) else "MATCHABLE"
    payload = state.get("transaction_payload") or {}
    product = payload.get("product")

    gw = AuctionGateway(mc_runtime)
    result = await gw.get_producer_auctions(phone, scope=scope, product_name=product)

    if not is_success_response(result) or int(result.get("count") or 0) == 0:
        msg = result.get("message") or "Aucun appel d'offres ouvert pour le moment."
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": msg,
            "available_mapping": {},
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": _clear_bid_payload(),
            "ag_ui_component": None,
        }

    mapping = result.get("mapping") or {}
    menu = result.get("formatted_menu") or "Appels d'offres disponibles."
    data = result.get("data") or []

    options = [
        MenuOption(
            index=str(i),
            label=f"{d.get('product')} — {d.get('max_price')} FCFA",
            value=mapping.get(str(i)),
        )
        for i, d in enumerate(data, start=1)
    ]

    # Résumé par auction_id : consulté aux tours suivants SANS rappeler
    # get_auction_bids (interdit au rôle producteur). Survit au cleanup tant que
    # le canal de sélection est maintenu (clé non-volatile).
    brief = {
        str(d.get("auction_id")): {
            "product": d.get("product"),
            "unit": d.get("unit"),
            "max_price": d.get("max_price"),
            "quantity": d.get("quantity"),
        }
        for d in data
        if d.get("auction_id")
    }

    # On repart d'un tunnel de bid propre à chaque nouveau listing.
    wm = _clear_bid_wm(state)
    wm["available_mapping_kind"] = "auction"
    wm["auction_scope"] = result.get("scope") or scope
    wm["auction_brief"] = brief

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "available_mapping": mapping,
        "expected_candidates": [str(d.get("product") or "") for d in data],
        "working_memory": wm,
        "transaction_payload": _clear_bid_payload(),
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Appels d'offres",
            options=options,
            kind="auction",
            preformatted_text=menu,
        ),
    }


# =====================================================================
# 2. ASK PRICE — enchère choisie, demander le prix (slot-filling)
# =====================================================================


async def ask_bid_price(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    *,
    reask: bool = False,
    deviation_note: Optional[str] = None,
    invalid_note: Optional[str] = None,
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("auction_brief") or {}).get(
        str(auction_id)
    ) or {}
    product, unit, max_price = _auction_label(brief)
    a_unit, _a_qty = _auction_terms(brief)

    # La question FIXE la base du prix (« par tonne ») : un montant nu en réponse est « par tonne »
    # (QUESTION_CONTEXT_EXPLICIT) ; « X pour tout » reste possible pour un lot entier.
    question = price_per_unit_question(a_unit) if a_unit else "Quel prix proposez-vous ? (en FCFA, avec l'unité ou « pour tout »)"
    if reask:
        msg = f"💬 {question}"
        if invalid_note:
            msg = f"⚠️ {invalid_note}\n\n{msg}"
        if deviation_note:
            msg = f"{deviation_note}\n\n{msg}"
    else:
        lines = [f"📦 *Appel d'offres sélectionné : {product}*"]
        qty = brief.get("quantity")
        if qty is not None:
            try:
                lines.append(f"⚖️ Quantité demandée : {_fmt_num(qty)} {unit_display(unit, float(qty))}")
            except (TypeError, ValueError):
                pass
        if max_price is not None:
            lines.append(
                f"💰 Prix plafond acheteur : *{_fmt_num(max_price)} FCFA par {unit_display(unit)}*"
            )
        lines.append(f"\n💬 {question}")
        msg = "\n".join(lines)

    payload = dict(state.get("transaction_payload") or {})
    payload["auction_id"] = str(auction_id)
    # None-overwrite OBLIGATOIRE : `transaction_payload` est réduit par
    # `merge_dict` — un pop sur le patch retourné ne supprime RIEN. Avec le pop,
    # l'ANCIEN `price` survivait alors qu'on s'apprête justement à redemander le
    # prix de l'enchère (expected_input=PRICE ci-dessous) : le pipeline pouvait
    # considérer le prix déjà rempli et sauter la question.
    payload["selection_index"] = None
    payload["selected_value"] = None
    payload["price"] = None

    wm = _bid_wm(
        state,
        bid_phase="ASK_PRICE",
        pending_bid_auction=str(auction_id),
        pending_bid_price=None,
        pending_bid_pricing=None,
    )

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="price"),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_BROWSE_REQUESTS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def ask_bid_clarification(
    state: Dict[str, Any],
    auction_id: Optional[str],
    parse: BidPriceParse,
    *,
    modify_bid: Optional[str] = None,
) -> Dict[str, Any]:
    """Le prix n'a PAS de base fiable (ou son conditionnement est inconnu) : on DEMANDE, on n'écrit rien.
    Le montant est conservé dans `pending_bid_pricing` pour que la réponse (« par tonne ») le complète."""
    phase = ("ASK_PACKAGE" if parse.status == BidPriceStatus.NEEDS_PACKAGE_SIZE else "ASK_BASIS") + (
        "_MODIFY" if modify_bid else ""
    )
    field = "package_size" if parse.status == BidPriceStatus.NEEDS_PACKAGE_SIZE else "price_basis"
    overrides: Dict[str, Any] = {"bid_phase": phase, "pending_bid_pricing": parse.to_state(), "pending_bid_price": None}
    if modify_bid:
        overrides["pending_modify_bid"] = str(modify_bid)
    else:
        overrides["pending_bid_auction"] = str(auction_id)
    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name=field),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_GET_MY_PROPOSALS" if modify_bid else "MARKET_BROWSE_REQUESTS",
        "final_response": f"💬 {parse.message}",
        "transaction_payload": dict(state.get("transaction_payload") or {}),
        "available_mapping": {},
        "working_memory": _bid_wm(state, **overrides),
        "ag_ui_component": None,
    }


# =====================================================================
# 3. RECAP + CONFIRMATION — prix saisi, corrigeable avant dépôt
# =====================================================================


async def recap_bid(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    parse: BidPriceParse,
    *,
    reask: bool = False,
    deviation_note: Optional[str] = None,
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("auction_brief") or {}).get(
        str(auction_id)
    ) or {}
    product, unit, max_price = _auction_label(brief)
    a_unit, a_qty = _auction_terms(brief)
    block, warn = _pricing_lines(parse, a_qty, a_unit, max_price)
    if not block:  # non certifiable contre CETTE enchère : jamais de récap trompeur
        return await ask_bid_price(
            state, mc_runtime, auction_id, reask=True,
            invalid_note="Ce prix ne peut pas être appliqué à cet appel d'offres.",
        )
    logger.info("BID_PRICING_CERTIFIED | flow_id=%s | auction=%s | basis=%s | source=%s", flow_id_of(state), auction_id,
                parse.basis.value if parse.basis else None, parse.source.value)

    prefix = "🤔 " if reask else "📝 "
    msg = (
        f"{prefix}*Récapitulatif de votre proposition*\n"
        f"Produit : *{product}*\n"
        f"{block}{warn}\n\n"
        "👉 Répondez *oui* pour confirmer, envoyez un *autre montant* (avec sa base : « 450 000 la tonne », "
        "« 4 500 000 pour tout ») pour corriger, ou *non* pour annuler."
    )
    if deviation_note:
        msg = f"{deviation_note}\n\n{msg}"

    payload = dict(state.get("transaction_payload") or {})
    payload["auction_id"] = str(auction_id)
    payload["price"] = float(parse.amount) if parse.amount is not None else None

    wm = _bid_wm(
        state,
        bid_phase="CONFIRM",
        pending_bid_auction=str(auction_id),
        pending_bid_price=float(parse.amount) if parse.amount is not None else None,
        pending_bid_pricing=parse.to_state(),
    )

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_BROWSE_REQUESTS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def _route_parse(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    parse: BidPriceParse,
    *,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Oriente un prix analysé : récap si certifiable, sinon LA question précise (jamais d'écriture)."""
    _log_parse("new", str(auction_id), parse, state)
    if parse.is_resolved:
        return await recap_bid(state, mc_runtime, auction_id, parse, deviation_note=note)
    if parse.status in {BidPriceStatus.NEEDS_BASIS, BidPriceStatus.NEEDS_PACKAGE_SIZE}:
        return await ask_bid_clarification(state, auction_id, parse)
    if parse.status == BidPriceStatus.INVALID:
        return await ask_bid_price(
            state, mc_runtime, auction_id, reask=True, deviation_note=note, invalid_note=parse.message
        )
    return await ask_bid_price(state, mc_runtime, auction_id, reask=True, deviation_note=note)


async def submit_bid(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    parse: BidPriceParse,
) -> Dict[str, Any]:
    """Dépose la proposition (place_bid) APRÈS confirmation, puis confirme au producteur.

    Le contrat de prix COMPLET est transmis (montant + base + unité/conditionnement) : `place_bid` refuse un
    nouveau bid sans base."""
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error(
            "Numéro de téléphone introuvable, impossible d'enregistrer votre proposition."
        )
    if not parse.is_resolved or parse.amount is None or parse.basis is None:
        return await ask_bid_price(state, mc_runtime, auction_id, reask=True)
    price = float(parse.amount)

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.place_bid(
            auction_id=str(auction_id), phone=phone, offered_price=price,
            price_basis=parse.basis.value, price_unit=parse.price_unit,
            package_type=parse.package_type,
            package_content_amount=float(parse.package_content_amount) if parse.package_content_amount is not None else None,
            package_content_unit=parse.package_content_unit,
        )
    except Exception as exc:
        logger.exception("submit_bid: place_bid failed: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible d'enregistrer votre proposition pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": result.get("message")
            or "Votre proposition n'a pas pu être enregistrée.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    price_txt = _fmt_num(price)
    # Message du DB (distingue « transmise » d'une « mise à jour » via l'upsert).
    db_msg = (
        result.get("message")
        or f"✅ Votre proposition de *{price_txt} FCFA* a été transmise à l'acheteur."
    )

    # Photo du lot proposé — même hint que le chemin générique
    # (nodes/rendering/success.py), posé ICI aussi : ce flow construit son
    # propre `final_response` directement (bypass du rendu générique, voir
    # son commentaire "les nœuds negotiation posent final_response
    # directement"), donc le hook générique ne s'exécute jamais pour ce
    # chemin — constaté en usage réel (le hint n'apparaissait pas après une
    # offre placée via ce tunnel de confirmation producteur).
    photo_hint = ""
    bid_id = result.get("bid_id")
    if bid_id and phone:
        set_pending_bid_photo(phone, str(bid_id))
        photo_hint = (
            "\n\n📸 Envoyez une photo de ce lot pour rassurer l'acheteur — "
            "elle sera automatiquement liée à cette offre."
        )

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"{db_msg}\n\n"
            "📊 Tapez *mes propositions* pour suivre son état (acceptée / en attente)."
            f"{photo_hint}"
        ),
        "working_memory": _clear_bid_wm(state),
        "transaction_payload": {"__reset__": True},
        "ag_ui_component": None,
    }


def _cancel_bid(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            "❌ Proposition annulée. Rien n'a été envoyé à l'acheteur.\n\n"
            "🛒 Tapez *voir les appels d'offres* pour recommencer."
        ),
        "working_memory": _clear_bid_wm(state),
        "transaction_payload": {"__reset__": True},
        "available_mapping": {},
        "ag_ui_component": None,
    }


# =====================================================================
# 4. TRACK — suivre l'état de mes offres
# =====================================================================


async def track_my_bids(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Liste les propositions du producteur ET permet d'agir dessus :
    sélectionner une proposition *en attente* ouvre le tunnel de correction de prix
    (voir ``ask_modify_price``/``recap_modify_price``/``submit_modify_price``).
    Une proposition déjà tranchée (acceptée/refusée) reste affichée mais non actionnable.
    """
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error(
            "Numéro de téléphone introuvable, impossible de charger vos propositions."
        )

    gw = AuctionGateway(mc_runtime)
    result = await gw.get_my_active_bids(phone)

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": result.get("message")
            or "Impossible de charger vos propositions en cours.",
            "working_memory": _clear_bid_wm(state),
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not data:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "Vous n'avez encore fait aucune proposition.\n\n"
                "🛒 Tapez *voir les appels d'offres* pour trouver des acheteurs."
            ),
            "working_memory": _clear_bid_wm(state),
            "ag_ui_component": None,
        }

    mapping: Dict[str, str] = {}
    options: List[MenuOption] = []
    brief: Dict[str, Any] = {}
    for i, item in enumerate(data, start=1):
        bid_id = str(item.get("bid_id") or "")
        if not bid_id:
            continue
        mapping[str(i)] = bid_id
        options.append(
            MenuOption(
                index=str(i),
                label=f"{item.get('product')} — {item.get('pricing_label') or str(item.get('offered_price')) + ' FCFA'}",
                value=bid_id,
            )
        )
        brief[bid_id] = {
            "product": item.get("product"),
            "unit": item.get("unit"),
            "quantity": item.get("quantity"),
            "auction_id": item.get("auction_id"),
            "price": item.get("offered_price"),
            "status": str(item.get("status") or "").upper(),
            # Phase B2b : SA sémantique de prix (base connue ou non) pour corriger sans la deviner.
            "pricing_label": item.get("pricing_label"),
            "pricing": item.get("pricing"),
        }

    menu_text = (
        (result.get("formatted_menu") or "Vos propositions.")
        + "\n\n_Répondez avec le numéro d'une proposition *en attente* pour en modifier le prix, "
        "ou tapez *voir les appels d'offres* pour en proposer une nouvelle._"
    )

    wm = _clear_bid_wm(state)
    wm["available_mapping_kind"] = "bid"
    wm["my_bids_brief"] = brief

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.SELECTION_MENU),
        "response_strategy": "SELECTION_MENU",
        "current_goal": "MARKET_GET_MY_PROPOSALS",
        "final_response": menu_text,
        "available_mapping": mapping,
        "working_memory": wm,
        "transaction_payload": _clear_bid_payload(),
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Vos propositions",
            options=options,
            kind="bid",
            preformatted_text=menu_text,
        ),
    }


# =====================================================================
# 4b. MODIFY — corriger le prix d'une offre PENDING existante
# =====================================================================


async def ask_modify_price(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    bid_id: str,
    *,
    reask: bool = False,
    deviation_note: Optional[str] = None,
    invalid_note: Optional[str] = None,
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("my_bids_brief") or {}).get(
        str(bid_id)
    ) or {}
    product = brief.get("product") or "ce produit"
    current_label = brief.get("pricing_label") or (
        f"{_fmt_num(brief.get('price'))} FCFA" if brief.get("price") is not None else "?"
    )
    certified = CommercialPricingSnapshot.from_dict(brief.get("pricing")) is not None
    if certified:
        how = (
            "Envoyez un montant (même base) ou précisez une autre base : « 430 000 la tonne », "
            "« 4 000 000 pour tout »."
        )
    else:
        how = (
            "Cette offre n'a pas de base de prix enregistrée. Indiquez votre prix AVEC sa base : "
            "« 450 000 la tonne » ou « 4 500 000 pour tout »."
        )

    if reask:
        msg = f"💬 Indiquez le *nouveau prix* pour *{product}*. {how}"
        if invalid_note:
            msg = f"⚠️ {invalid_note}\n\n{msg}"
        if deviation_note:
            msg = f"{deviation_note}\n\n{msg}"
    else:
        msg = (
            f"✏️ *Modifier votre proposition : {product}*\n"
            f"Prix actuel : *{current_label}*\n\n"
            f"💬 *Quel nouveau prix proposez-vous ?* {how}"
        )

    payload = dict(state.get("transaction_payload") or {})
    payload["bid_id"] = str(bid_id)
    # Le prix actuel doit vraiment disparaître pour être redemandé.
    mark_deleted(payload, "selection_index", "selected_value", "price")

    wm = _bid_wm(
        state,
        bid_phase="ASK_PRICE_MODIFY",
        pending_modify_bid=str(bid_id),
        pending_bid_price=None,
        pending_bid_pricing=None,
    )

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="price"),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_GET_MY_PROPOSALS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def recap_modify_price(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    bid_id: str,
    parse: BidPriceParse,
    *,
    reask: bool = False,
    deviation_note: Optional[str] = None,
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("my_bids_brief") or {}).get(
        str(bid_id)
    ) or {}
    product = brief.get("product") or "ce produit"
    a_unit, a_qty = _auction_terms(brief)
    block, _warn = _pricing_lines(parse, a_qty, a_unit, None)
    if not block:
        return await ask_modify_price(
            state, mc_runtime, bid_id, reask=True,
            invalid_note="Ce prix ne peut pas être appliqué à cet appel d'offres.",
        )
    logger.info("BID_PRICING_CERTIFIED | flow_id=%s | bid=%s | basis=%s | source=%s", flow_id_of(state), bid_id,
                parse.basis.value if parse.basis else None, parse.source.value)

    prefix = "🤔 " if reask else "📝 "
    msg = (
        f"{prefix}*Nouveau prix pour {product}*\n"
        f"{block}\n\n"
        "👉 Répondez *oui* pour confirmer, envoyez un *autre montant* pour corriger, "
        "ou *non* pour annuler."
    )
    if deviation_note:
        msg = f"{deviation_note}\n\n{msg}"

    payload = dict(state.get("transaction_payload") or {})
    payload["bid_id"] = str(bid_id)
    payload["price"] = float(parse.amount) if parse.amount is not None else None

    wm = _bid_wm(
        state,
        bid_phase="CONFIRM_MODIFY",
        pending_modify_bid=str(bid_id),
        pending_bid_price=float(parse.amount) if parse.amount is not None else None,
        pending_bid_pricing=parse.to_state(),
    )

    return {
        "status": "WAITING_INPUT",
        **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_GET_MY_PROPOSALS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def _route_modify_parse(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    bid_id: str,
    parse: BidPriceParse,
    *,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    _log_parse("modify", str(bid_id), parse, state)
    if parse.is_resolved:
        return await recap_modify_price(state, mc_runtime, bid_id, parse, deviation_note=note)
    if parse.status in {BidPriceStatus.NEEDS_BASIS, BidPriceStatus.NEEDS_PACKAGE_SIZE}:
        return await ask_bid_clarification(state, None, parse, modify_bid=bid_id)
    if parse.status == BidPriceStatus.INVALID:
        return await ask_modify_price(
            state, mc_runtime, bid_id, reask=True, deviation_note=note, invalid_note=parse.message
        )
    return await ask_modify_price(state, mc_runtime, bid_id, reask=True, deviation_note=note)


async def submit_modify_price(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    bid_id: str,
    parse: BidPriceParse,
) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error(
            "Numéro de téléphone introuvable, impossible de modifier votre proposition."
        )
    if not parse.is_resolved or parse.amount is None or parse.basis is None:
        return await ask_modify_price(state, mc_runtime, bid_id, reask=True)
    new_price = float(parse.amount)

    gw = AuctionGateway(mc_runtime)
    try:
        # la BASE est toujours transmise : « finalement 4 millions pour tout » change PER_BASE_UNIT -> TOTAL_LOT
        # et requalifie un bid ancien ; le serveur recalcule le snapshot.
        result = await gw.update_bid_price(
            bid_id=str(bid_id), phone=phone, new_price=new_price,
            price_basis=parse.basis.value, price_unit=parse.price_unit,
            package_type=parse.package_type,
            package_content_amount=float(parse.package_content_amount) if parse.package_content_amount is not None else None,
            package_content_unit=parse.package_content_unit,
        )
    except Exception as exc:
        logger.exception("submit_modify_price: update_bid_price failed: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible de mettre à jour votre proposition pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": result.get("message")
            or "Votre proposition n'a pas pu être mise à jour.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    snap_label = ""
    try:
        _brief = ((state.get("working_memory") or {}).get("my_bids_brief") or {}).get(str(bid_id)) or {}
        _u, _q = _auction_terms(_brief)
        _snap, _ = _totals(parse, _q, _u)
        snap_label = render_pricing_label(_snap) if _snap is not None else f"{_fmt_num(new_price)} FCFA"
    except Exception:  # affichage seulement
        snap_label = f"{_fmt_num(new_price)} FCFA"
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"✅ Votre proposition a été mise à jour : *{snap_label}*.\n\n"
            "📊 Tapez *mes propositions* pour suivre son état."
        ),
        "working_memory": _clear_bid_wm(state),
        "transaction_payload": {"__reset__": True},
        "ag_ui_component": None,
    }


def _cancel_modify(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": "❌ Modification annulée, votre proposition reste inchangée.\n\n📊 Tapez *mes propositions* pour revoir la liste.",
        "working_memory": _clear_bid_wm(state),
        "transaction_payload": {"__reset__": True},
        "available_mapping": {},
        "ag_ui_component": None,
    }


# =====================================================================
# ORCHESTRATOR
# =====================================================================


async def producer_auction_resolver(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Aiguille le cycle enchères producteur (slot-filling + confirmation)."""
    goal = str(state.get("current_goal") or "").upper().strip()
    working = state.get("working_memory") or {}
    mapping_kind = str(working.get("available_mapping_kind") or "").lower().strip()
    payload = state.get("transaction_payload") or {}
    text = _text_of(state).lower()
    event = str(state.get("interpreted_event") or "").upper().strip()

    phase = str(working.get("bid_phase") or "").upper().strip()
    pending_auction = working.get("pending_bid_auction")
    pending_modify_bid = working.get("pending_modify_bid")
    bids_brief = working.get("my_bids_brief") or {}

    # (0) INTERRUPTION LÉGITIME DU TUNNEL DE BID.
    # Ce resolver a sa PROPRE mini machine à états (bid_phase), indépendante de
    # confirmation_gate/TunnelManager (voir docstring du module — c'est le but
    # du "confirmation maison"). Problème : par le temps qu'un message arrive
    # ici, TunnelManager.evaluate() + goal_planner ont DÉJÀ décidé si ce message
    # a le droit de casser une confirmation (confiance suffisante, ou intent
    # critique) et ont posé `interpreted_event = "INTERRUPTION"` en conséquence.
    # Sans ce garde, les branches (A)/(B) ci-dessous ne savent reconnaître que
    # REJECT / un prix / CONFIRM — tout le reste (y compris cette interruption
    # DÉJÀ AUTORISÉE) retombait dans la branche "réponse ambiguë" et ré-affichait
    # indéfiniment le récap périmé, quel que soit ce que l'utilisateur demandait
    # réellement (ex: "je veux voir les enchères" pendant qu'un bid Tomate/150
    # FCFA attendait confirmation). Purge le tunnel de bid et laisse tomber vers
    # le routage normal (C/D/E/F), qui gère déjà correctement MARKET_GET_MY_PROPOSALS
    # / une sélection fraîche / le browse par défaut.
    if (
        phase in {
            "CONFIRM", "ASK_PRICE", "ASK_BASIS", "ASK_PACKAGE",
            "CONFIRM_MODIFY", "ASK_PRICE_MODIFY", "ASK_BASIS_MODIFY", "ASK_PACKAGE_MODIFY",
        }
        and event == "INTERRUPTION"
    ):
        logger.info(
            "[ProducerAuctionResolver] Interruption autorisée casse le tunnel de bid "
            "(phase=%s) — purge et redirection vers le routage normal.",
            phase,
        )
        state = {
            **state,
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {**payload, **_clear_bid_payload()},
        }
        working = state["working_memory"]
        payload = state["transaction_payload"]
        mapping_kind = str(working.get("available_mapping_kind") or "").lower().strip()
        phase = ""
        pending_auction = None
        pending_modify_bid = None
        bids_brief = working.get("my_bids_brief") or {}

    pending_pricing = BidPriceParse.from_state(working.get("pending_bid_pricing"))
    auction_brief = (working.get("auction_brief") or {}).get(str(pending_auction)) or {}
    modify_brief = bids_brief.get(str(pending_modify_bid)) or {}

    def _is_reject() -> bool:
        return event == "REJECT" or text in _NO_TOKENS

    def _is_confirm() -> bool:
        return event == "CONFIRM" or text in _YES_TOKENS

    def _has_amount() -> bool:
        return bool(find_amounts(text)) and text not in _YES_TOKENS and text not in _NO_TOKENS

    def _keep_context(parse: Optional[BidPriceParse], brief: Dict[str, Any]) -> BidPriceContext:
        """Un montant nu GARDE la base affichée dans le récap / celle de l'offre certifiée existante ; sans base
        connue, il est ambigu (donc demandé)."""
        a_unit, a_qty = _auction_terms(brief)
        try:
            if parse is not None and parse.is_resolved and a_unit and a_qty:
                return BidPriceContext.keep(parse.snapshot(a_qty, a_unit))
        except PricingSnapshotError:
            pass
        existing = CommercialPricingSnapshot.from_dict(brief.get("pricing"))
        return BidPriceContext.keep(existing) if existing is not None else BidPriceContext()

    # (A) Tunnel MODIFICATION de prix en cours (priorité : plus spécifique).
    if phase in {"CONFIRM_MODIFY", "ASK_BASIS_MODIFY", "ASK_PACKAGE_MODIFY", "ASK_PRICE_MODIFY"} and pending_modify_bid:
        m_unit, m_qty = _auction_terms(modify_brief)
        bid_ref = str(pending_modify_bid)
        if _is_reject():
            return _cancel_modify(state)
        if m_unit is None or m_qty is None:
            return _error("Impossible de vérifier l'appel d'offres de cette proposition. Réessayez avec *mes propositions*.")

        if phase == "CONFIRM_MODIFY" and pending_pricing is not None:
            if _has_amount():
                ctx = _keep_context(pending_pricing, modify_brief)
                new = parse_bid_price(text, auction_unit=m_unit, auction_quantity=m_qty, context=ctx, llm_hints=_llm_hints(state))
                return await _route_modify_parse(state, mc_runtime, bid_ref, new)
            if _is_confirm():
                return await submit_modify_price(state, mc_runtime, bid_ref, pending_pricing)
            # Chantier résilience 2026-08 : accuse d'abord réception via le LLM
            # au lieu de rejouer le même récap tel quel.
            note = await llm_deviation_reply(
                mc_runtime, _text_of(state),
                "un récapitulatif de nouveau prix à confirmer (oui/non/autre montant)",
            )
            return await recap_modify_price(state, mc_runtime, bid_ref, pending_pricing, reask=True, deviation_note=note)

        if phase == "ASK_BASIS_MODIFY" and pending_pricing is not None and pending_pricing.amount is not None:
            reply = resolve_basis_reply(text, amount=pending_pricing.amount, auction_unit=m_unit, auction_quantity=m_qty)
            return await _route_modify_parse(state, mc_runtime, bid_ref, reply)

        if phase == "ASK_PACKAGE_MODIFY" and pending_pricing is not None:
            reply = resolve_package_reply(pending_pricing, text, auction_unit=m_unit)
            return await _route_modify_parse(state, mc_runtime, bid_ref, reply)

        if phase == "ASK_PRICE_MODIFY":
            ctx = _keep_context(None, modify_brief)
            parsed = parse_bid_price(text, auction_unit=m_unit, auction_quantity=m_qty, context=ctx, llm_hints=_llm_hints(state))
            note = None
            if parsed.status == BidPriceStatus.NO_PRICE and event in {"UNKNOWN", "OUT_OF_SCOPE"}:
                note = await llm_deviation_reply(mc_runtime, _text_of(state), "répondre au nouveau prix demandé (en FCFA)")
            return await _route_modify_parse(state, mc_runtime, bid_ref, parsed, note=note)

    # (B) Tunnel NOUVELLE offre en cours.
    if phase in {"CONFIRM", "ASK_BASIS", "ASK_PACKAGE", "ASK_PRICE"} and pending_auction:
        a_unit, a_qty = _auction_terms(auction_brief)
        auction_ref = str(pending_auction)
        if _is_reject():
            return _cancel_bid(state)
        if a_unit is None or a_qty is None:
            return _error("Impossible de vérifier cet appel d'offres. Tapez *voir les appels d'offres* pour recommencer.")

        if phase == "CONFIRM" and pending_pricing is not None:
            if _has_amount():
                ctx = _keep_context(pending_pricing, auction_brief)
                new = parse_bid_price(text, auction_unit=a_unit, auction_quantity=a_qty, context=ctx, llm_hints=_llm_hints(state))
                return await _route_parse(state, mc_runtime, auction_ref, new)
            if _is_confirm():
                return await submit_bid(state, mc_runtime, auction_ref, pending_pricing)
            # Réponse ambiguë : accuse d'abord réception via le LLM (chantier
            # résilience 2026-08) avant de rejouer le récap.
            note = await llm_deviation_reply(
                mc_runtime, _text_of(state),
                "un récapitulatif de proposition à confirmer (oui/non/autre montant)",
            )
            return await recap_bid(state, mc_runtime, auction_ref, pending_pricing, reask=True, deviation_note=note)

        if phase == "ASK_BASIS" and pending_pricing is not None and pending_pricing.amount is not None:
            reply = resolve_basis_reply(text, amount=pending_pricing.amount, auction_unit=a_unit, auction_quantity=a_qty)
            return await _route_parse(state, mc_runtime, auction_ref, reply)

        if phase == "ASK_PACKAGE" and pending_pricing is not None:
            reply = resolve_package_reply(pending_pricing, text, auction_unit=a_unit)
            return await _route_parse(state, mc_runtime, auction_ref, reply)

        if phase == "ASK_PRICE":
            # La QUESTION posée (« quel prix par tonne ? ») fixe la base d'un montant nu ; « X pour tout » et
            # « X la tonne » restent EXPLICITES. Le texte décide, jamais un indice du LLM.
            parsed = parse_bid_price(
                text, auction_unit=a_unit, auction_quantity=a_qty,
                context=BidPriceContext.per_auction_unit(a_unit), llm_hints=_llm_hints(state),
            )
            note = None
            if parsed.status == BidPriceStatus.NO_PRICE and event in {"UNKNOWN", "OUT_OF_SCOPE"}:
                note = await llm_deviation_reply(mc_runtime, _text_of(state), "répondre au prix proposé (en FCFA)")
            return await _route_parse(state, mc_runtime, auction_ref, parsed, note=note)

    # (C) Sélection d'une offre depuis « mes offres » (kind="bid" → memory.py
    #     pose payload.bid_id). Doit être vérifié AVANT le court-circuit goal
    #     MARKET_GET_MY_PROPOSALS ci-dessous, sinon on ré-affiche juste la liste
    #     au lieu d'agir sur la sélection.
    if bids_brief and mapping_kind == "bid":
        picked_bid = payload.get("bid_id")
        if picked_bid and str(picked_bid) in bids_brief:
            info = bids_brief[str(picked_bid)]
            if str(info.get("status") or "").upper() != "PENDING":
                return {
                    "status": "COMPLETED",
                    "response_strategy": "SUCCESS",
                    "final_response": (
                        "Cette proposition a déjà été traitée, elle n'est plus modifiable.\n\n"
                        "📊 Tapez *mes propositions* pour revoir la liste."
                    ),
                    "working_memory": _clear_bid_wm(state),
                    "ag_ui_component": None,
                }
            return await ask_modify_price(state, mc_runtime, str(picked_bid))

    # (D) Suivi des offres émises — entrée fraîche (pas de sélection en cours).
    if goal == "MARKET_GET_MY_PROPOSALS":
        return await track_my_bids(state, mc_runtime)

    # (E) Enchère fraîchement choisie (memory_update a posé payload.auction_id).
    picked = payload.get("auction_id") if mapping_kind == "auction" else None
    if not picked and (
        mapping_kind == "auction" or payload.get("selection_index") is not None
    ):
        picked = _resolve_selected_auction_id(state)
    if picked:
        return await ask_bid_price(state, mc_runtime, str(picked))

    # (F) Entrée par défaut : afficher les enchères.
    return await browse_auctions(state, mc_runtime)


__all__ = [
    "producer_auction_resolver",
    "browse_auctions",
    "ask_bid_price",
    "recap_bid",
    "submit_bid",
    "track_my_bids",
    "ask_modify_price",
    "recap_modify_price",
    "submit_modify_price",
]
