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

On reste volontairement sur le goal ``MARKET_GET_REQUESTS`` (aucun champ requis)
pendant tout le tunnel de bid : cela évite que le validator réclame lui-même le
prix (``SALES_PLACE_BID`` exige ``price``) et court-circuite notre récap.

Goals pris en charge :
  - ``MARKET_GET_REQUESTS`` / ``SALES_PLACE_BID`` : découverte + dépôt d'offre.
  - ``MARKET_GET_MY_PROPOSALS``                   : suivi de l'état de mes offres.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)

logger = logging.getLogger("AgriConnect.Market.ProducerFlow.Auctions")


# Mots-clés qui demandent la vue « tout le marché » plutôt que « mes catégories ».
_ALL_SCOPE_TOKENS = (
    "toutes", "tout", "tous", "marche", "marché", "autres", "autre", "elargir", "élargir",
)

_YES_TOKENS = frozenset({
    "oui", "ok", "okay", "daccord", "d'accord", "cest bon", "c'est bon", "confirme",
    "confirmer", "je confirme", "valide", "valider", "go", "vasy", "vas-y", "parfait",
    "yes", "yep", "ouais",
})
_NO_TOKENS = frozenset({
    "non", "annuler", "annule", "annulation", "stop", "cancel", "quitter", "retour",
    "pas maintenant", "laisse tomber",
})

_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")

# NB : le menu d'enchères utilise ``kind="auction"`` ; c'est ``nodes/memory.py``
# qui, sur la sélection, pose ``payload["auction_id"]`` (voir _AUCTION_MAPPING_KINDS).


# =====================================================================
# HELPERS
# =====================================================================

def _text_of(state: Dict[str, Any]) -> str:
    return str(state.get("normalized_text") or state.get("user_query") or "").strip()


def _wants_all_scope(state: Dict[str, Any]) -> bool:
    low = _text_of(state).lower()
    return any(tok in low for tok in _ALL_SCOPE_TOKENS)


def _first_number(text: str) -> Optional[float]:
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _price_from_answer(state: Dict[str, Any]) -> Optional[float]:
    """Prix saisi en réponse à la question du prix (phase ASK_PRICE UNIQUEMENT).

    Sources : payload.price / entities.price (posés par l'interpréteur pour un
    slot PRICE), sinon le premier nombre du texte. Ne JAMAIS appeler hors phase
    ASK_PRICE : sur un tour de sélection, « 1 » est un index, pas un prix.
    """
    payload = state.get("transaction_payload") or {}
    for c in (payload.get("price"), payload.get("offered_price"),
              (state.get("extracted_entities") or {}).get("price")):
        if c in (None, "", [], {}):
            continue
        try:
            val = float(c)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return _first_number(_text_of(state))


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


def _bid_wm(state: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    """working_memory de base pour le tunnel de bid, avec surcharges explicites.

    Nettoie les marqueurs de menu volatils et applique les clés fournies
    (``bid_phase``, ``pending_bid_auction``, ``pending_bid_price``, ...).
    """
    wm = dict(state.get("working_memory") or {})
    wm.pop("available_mapping_kind", None)
    wm.pop("auction_menu", None)
    wm.update(overrides)
    return wm


def _clear_bid_wm(state: Dict[str, Any]) -> Dict[str, Any]:
    wm = dict(state.get("working_memory") or {})
    for k in ("available_mapping_kind", "auction_menu", "bid_phase",
              "pending_bid_auction", "pending_bid_price"):
        wm.pop(k, None)
    return wm


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
# 1. DISCOVERY — lister les enchères (par catégorie)
# =====================================================================

async def browse_auctions(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible de charger le marché.")

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
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": menu,
        "available_mapping": mapping,
        "expected_candidates": [str(d.get("product") or "") for d in data],
        "working_memory": wm,
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
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("auction_brief") or {}).get(str(auction_id)) or {}
    product, unit, max_price = _auction_label(brief)

    if reask:
        msg = f"💬 Indiquez un *prix* valide en FCFA (ex: 300) pour *{product}*."
    else:
        lines = [f"📦 *Enchère sélectionnée : {product}*"]
        qty = brief.get("quantity")
        if qty is not None:
            try:
                lines.append(f"⚖️ Quantité demandée : {float(qty):g} {unit}")
            except (TypeError, ValueError):
                pass
        if max_price is not None:
            lines.append(f"💰 Prix plafond acheteur : *{max_price:g} FCFA/{unit}*")
        lines.append("\n💬 *Quel prix proposez-vous ?* (par unité, en FCFA)")
        msg = "\n".join(lines)

    payload = dict(state.get("transaction_payload") or {})
    payload["auction_id"] = str(auction_id)
    payload.pop("selection_index", None)
    payload.pop("selected_value", None)
    payload.pop("price", None)

    wm = _bid_wm(state, bid_phase="ASK_PRICE", pending_bid_auction=str(auction_id),
                 pending_bid_price=None)

    return {
        "status": "WAITING_INPUT",
        "expected_input": "PRICE",
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_GET_REQUESTS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


# =====================================================================
# 3. RECAP + CONFIRMATION — prix saisi, corrigeable avant dépôt
# =====================================================================

async def recap_bid(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    price: float,
    *,
    reask: bool = False,
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("auction_brief") or {}).get(str(auction_id)) or {}
    product, unit, max_price = _auction_label(brief)
    price_txt = f"{float(price):g}"

    warn = ""
    if max_price is not None and price > max_price:
        warn = (
            f"\n⚠️ Votre prix dépasse le plafond acheteur ({max_price:g} FCFA) — "
            "l'acheteur risque de ne pas le retenir."
        )

    prefix = "🤔 " if reask else "📝 "
    msg = (
        f"{prefix}*Récapitulatif de votre offre*\n"
        f"Produit : *{product}*\n"
        f"Votre prix : *{price_txt} FCFA/{unit}*{warn}\n\n"
        "👉 Répondez *oui* pour confirmer, envoyez un *autre montant* pour corriger, "
        "ou *non* pour annuler."
    )

    payload = dict(state.get("transaction_payload") or {})
    payload["auction_id"] = str(auction_id)
    payload["price"] = float(price)

    wm = _bid_wm(state, bid_phase="CONFIRM", pending_bid_auction=str(auction_id),
                 pending_bid_price=float(price))

    return {
        "status": "WAITING_INPUT",
        "expected_input": "CONFIRMATION",
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_GET_REQUESTS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def submit_bid(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    auction_id: str,
    price: float,
) -> Dict[str, Any]:
    """Dépose l'offre (place_bid) APRÈS confirmation, puis confirme au producteur."""
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible d'enregistrer votre offre.")

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.place_bid(auction_id=str(auction_id), phone=phone, offered_price=price)
    except Exception as exc:
        logger.exception("submit_bid: place_bid failed: %s", exc)
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": "Impossible d'enregistrer votre offre pour le moment. Réessayez dans un instant.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "ERROR",
            "final_response": result.get("message") or "Votre offre n'a pas pu être enregistrée.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    price_txt = f"{float(price):g}"
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"✅ Votre offre de *{price_txt} FCFA* a été transmise à l'acheteur.\n\n"
            "📊 Tapez *mes offres* pour suivre son état (acceptée / en attente)."
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
            "❌ Offre annulée. Rien n'a été envoyé à l'acheteur.\n\n"
            "🛒 Tapez *voir les enchères* pour recommencer."
        ),
        "working_memory": _clear_bid_wm(state),
        "transaction_payload": {"__reset__": True},
        "available_mapping": {},
        "ag_ui_component": None,
    }


# =====================================================================
# 4. TRACK — suivre l'état de mes offres
# =====================================================================

async def track_my_bids(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible de charger vos offres.")

    gw = AuctionGateway(mc_runtime)
    result = await gw.get_my_active_bids(phone)

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": result.get("message") or "Impossible de charger vos offres en cours.",
            "ag_ui_component": None,
        }

    data = result.get("data") or []
    if not data:
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "Vous n'avez encore fait aucune proposition.\n\n"
                "🛒 Tapez *voir les enchères* pour trouver des acheteurs."
            ),
            "ag_ui_component": None,
        }

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": result.get("formatted_menu") or "Vos offres.",
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
    pending_price = working.get("pending_bid_price")

    # (A) Suivi des offres émises.
    if goal == "MARKET_GET_MY_PROPOSALS":
        return await track_my_bids(state, mc_runtime)

    # (B) Phase CONFIRMATION : oui → dépôt ; un autre montant → corriger ; non → annuler.
    if phase == "CONFIRM" and pending_auction and pending_price is not None:
        if event == "REJECT" or text in _NO_TOKENS:
            return _cancel_bid(state)
        new_price = _first_number(text)
        if new_price is not None:
            return await recap_bid(state, mc_runtime, str(pending_auction), new_price)
        if event == "CONFIRM" or text in _YES_TOKENS:
            return await submit_bid(state, mc_runtime, str(pending_auction), float(pending_price))
        # Réponse ambiguë : on ré-affiche le récap.
        return await recap_bid(state, mc_runtime, str(pending_auction), float(pending_price), reask=True)

    # (C) Phase SAISIE DU PRIX : on attend un montant → récap.
    if phase == "ASK_PRICE" and pending_auction:
        if event == "REJECT" or text in _NO_TOKENS:
            return _cancel_bid(state)
        price = _price_from_answer(state)
        if price is not None:
            return await recap_bid(state, mc_runtime, str(pending_auction), price)
        return await ask_bid_price(state, mc_runtime, str(pending_auction), reask=True)

    # (D) Enchère fraîchement choisie (memory_update a posé payload.auction_id).
    picked = payload.get("auction_id") if mapping_kind == "auction" else None
    if not picked and (mapping_kind == "auction" or payload.get("selection_index") is not None):
        picked = _resolve_selected_auction_id(state)
    if picked:
        return await ask_bid_price(state, mc_runtime, str(picked))

    # (E) Entrée par défaut : afficher les enchères.
    return await browse_auctions(state, mc_runtime)


__all__ = [
    "producer_auction_resolver",
    "browse_auctions",
    "ask_bid_price",
    "recap_bid",
    "submit_bid",
    "track_my_bids",
]
