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

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.core.formatting import fmt_num as _fmt_num
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import AuctionGateway
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    is_success_response,
)

logger = logging.getLogger("AgriConnect.Market.ProducerFlow.Auctions")


# Mots demandant la vue « tout le marché » plutôt que « mes catégories ».
# Volontairement restreint aux termes NON AMBIGUS. "marche"/"marché" en ont été
# retirés : « ça marche », « démarche », « le marché » sont indiscernables ici,
# et « tout le marché » reste capté par "tout". Mieux vaut ne PAS élargir le
# périmètre que l'élargir à tort sur un « ça marche » d'acquiescement.
_ALL_SCOPE_TOKENS = (
    "toutes", "tout", "tous", "autres", "autre", "elargir", "élargir",
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


_ALL_SCOPE_RE = re.compile(
    r"(?<![a-zàâäéèêëïîôöùûüÿç])(" + "|".join(_ALL_SCOPE_TOKENS) + r")(?![a-zàâäéèêëïîôöùûüÿç])"
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


# Clés du tunnel de bid portées par working_memory.
# GOTCHA CRITIQUE : working_memory utilise le réducteur `merge_dict` → RETIRER
# (pop) une clé du dict retourné ne la SUPPRIME PAS de l'état (l'ancienne valeur
# survit au merge). Pour « effacer » une clé, il FAUT la remettre à None (que le
# merge écrase). Voir [[market-coach-turn-boundary-state]]. Sans ça, `bid_phase`
# et `pending_bid_price` persistaient après un bid réussi → le tour suivant
# ré-affichait le récap au lieu de parcourir, et le payload gardait `price`.
_BID_WM_KEYS = (
    "available_mapping_kind", "auction_menu", "bid_phase",
    "pending_bid_auction", "pending_bid_price",
    "pending_modify_bid", "my_bids_brief", "auction_brief",
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
    return {"price": None, "auction_id": None, "bid_id": None,
            "selection_index": None, "selected_value": None}


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
        "expected_input": "SELECTION",
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
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("auction_brief") or {}).get(str(auction_id)) or {}
    product, unit, max_price = _auction_label(brief)

    if reask:
        msg = f"💬 Indiquez un *prix* valide en FCFA (ex: 300) pour *{product}*."
    else:
        lines = [f"📦 *Appel d'offres sélectionné : {product}*"]
        qty = brief.get("quantity")
        if qty is not None:
            try:
                lines.append(f"⚖️ Quantité demandée : {_fmt_num(qty)} {unit}")
            except (TypeError, ValueError):
                pass
        if max_price is not None:
            lines.append(f"💰 Prix plafond acheteur : *{_fmt_num(max_price)} FCFA/{unit}*")
        lines.append("\n💬 *Quel prix proposez-vous ?* (par unité, en FCFA)")
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

    wm = _bid_wm(state, bid_phase="ASK_PRICE", pending_bid_auction=str(auction_id),
                 pending_bid_price=None)

    return {
        "status": "WAITING_INPUT",
        "expected_input": "PRICE",
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_BROWSE_REQUESTS",
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
    price_txt = _fmt_num(price)

    warn = ""
    if max_price is not None and price > max_price:
        warn = (
            f"\n⚠️ Votre prix dépasse le plafond acheteur ({_fmt_num(max_price)} FCFA) — "
            "l'acheteur risque de ne pas le retenir."
        )

    prefix = "🤔 " if reask else "📝 "
    msg = (
        f"{prefix}*Récapitulatif de votre proposition*\n"
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
        "current_goal": "MARKET_BROWSE_REQUESTS",
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
    """Dépose la proposition (place_bid) APRÈS confirmation, puis confirme au producteur."""
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible d'enregistrer votre proposition.")

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.place_bid(auction_id=str(auction_id), phone=phone, offered_price=price)
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
            "final_response": result.get("message") or "Votre proposition n'a pas pu être enregistrée.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    price_txt = _fmt_num(price)
    # Message du DB (distingue « transmise » d'une « mise à jour » via l'upsert).
    db_msg = result.get("message") or f"✅ Votre proposition de *{price_txt} FCFA* a été transmise à l'acheteur."
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"{db_msg}\n\n"
            "📊 Tapez *mes propositions* pour suivre son état (acceptée / en attente)."
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

async def track_my_bids(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Liste les propositions du producteur ET permet d'agir dessus :
    sélectionner une proposition *en attente* ouvre le tunnel de correction de prix
    (voir ``ask_modify_price``/``recap_modify_price``/``submit_modify_price``).
    Une proposition déjà tranchée (acceptée/refusée) reste affichée mais non actionnable.
    """
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible de charger vos propositions.")

    gw = AuctionGateway(mc_runtime)
    result = await gw.get_my_active_bids(phone)

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": result.get("message") or "Impossible de charger vos propositions en cours.",
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
        options.append(MenuOption(
            index=str(i),
            label=f"{item.get('product')} — {item.get('offered_price')} FCFA",
            value=bid_id,
        ))
        brief[bid_id] = {
            "product": item.get("product"),
            "unit": item.get("unit"),
            "quantity": item.get("quantity"),
            "auction_id": item.get("auction_id"),
            "price": item.get("offered_price"),
            "status": str(item.get("status") or "").upper(),
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
        "expected_input": "SELECTION",
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
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("my_bids_brief") or {}).get(str(bid_id)) or {}
    product = brief.get("product") or "ce produit"
    unit = brief.get("unit") or "unité"
    current_price = brief.get("price")

    if reask:
        msg = f"💬 Indiquez le *nouveau prix* en FCFA (ex: 300) pour *{product}*."
    else:
        cur_txt = _fmt_num(current_price) if current_price is not None else "?"
        msg = (
            f"✏️ *Modifier votre proposition : {product}*\n"
            f"Prix actuel : *{cur_txt} FCFA/{unit}*\n\n"
            "💬 *Quel nouveau prix proposez-vous ?*"
        )

    payload = dict(state.get("transaction_payload") or {})
    payload["bid_id"] = str(bid_id)
    payload.pop("selection_index", None)
    payload.pop("selected_value", None)
    payload.pop("price", None)

    wm = _bid_wm(state, bid_phase="ASK_PRICE_MODIFY", pending_modify_bid=str(bid_id),
                 pending_bid_price=None)

    return {
        "status": "WAITING_INPUT",
        "expected_input": "PRICE",
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
    new_price: float,
    *,
    reask: bool = False,
) -> Dict[str, Any]:
    brief = ((state.get("working_memory") or {}).get("my_bids_brief") or {}).get(str(bid_id)) or {}
    product = brief.get("product") or "ce produit"
    unit = brief.get("unit") or "unité"
    price_txt = _fmt_num(new_price)

    prefix = "🤔 " if reask else "📝 "
    msg = (
        f"{prefix}*Nouveau prix pour {product}*\n"
        f"Prix proposé : *{price_txt} FCFA/{unit}*\n\n"
        "👉 Répondez *oui* pour confirmer, envoyez un *autre montant* pour corriger, "
        "ou *non* pour annuler."
    )

    payload = dict(state.get("transaction_payload") or {})
    payload["bid_id"] = str(bid_id)
    payload["price"] = float(new_price)

    wm = _bid_wm(state, bid_phase="CONFIRM_MODIFY", pending_modify_bid=str(bid_id),
                 pending_bid_price=float(new_price))

    return {
        "status": "WAITING_INPUT",
        "expected_input": "CONFIRMATION",
        "response_strategy": "ASK_MISSING_FIELD",
        "current_goal": "MARKET_GET_MY_PROPOSALS",
        "final_response": msg,
        "transaction_payload": payload,
        "available_mapping": {},
        "working_memory": wm,
        "ag_ui_component": None,
    }


async def submit_modify_price(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    bid_id: str,
    new_price: float,
) -> Dict[str, Any]:
    phone = str(state.get("user_phone") or "")
    if not phone:
        return _error("Numéro de téléphone introuvable, impossible de modifier votre proposition.")

    gw = AuctionGateway(mc_runtime)
    try:
        result = await gw.update_bid_price(bid_id=str(bid_id), phone=phone, new_price=new_price)
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
            "final_response": result.get("message") or "Votre proposition n'a pas pu être mise à jour.",
            "working_memory": _clear_bid_wm(state),
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    price_txt = _fmt_num(new_price)
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"✅ Votre proposition a été mise à jour à *{price_txt} FCFA*.\n\n"
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
    pending_price = working.get("pending_bid_price")
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
    if phase in {"CONFIRM", "ASK_PRICE", "CONFIRM_MODIFY", "ASK_PRICE_MODIFY"} and event == "INTERRUPTION":
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
        pending_price = None
        bids_brief = working.get("my_bids_brief") or {}

    # (A) Tunnel MODIFICATION de prix en cours (priorité : plus spécifique).
    if phase == "CONFIRM_MODIFY" and pending_modify_bid and pending_price is not None:
        if event == "REJECT" or text in _NO_TOKENS:
            return _cancel_modify(state)
        new_price = _first_number(text)
        if new_price is not None:
            return await recap_modify_price(state, mc_runtime, str(pending_modify_bid), new_price)
        if event == "CONFIRM" or text in _YES_TOKENS:
            return await submit_modify_price(state, mc_runtime, str(pending_modify_bid), float(pending_price))
        return await recap_modify_price(state, mc_runtime, str(pending_modify_bid), float(pending_price), reask=True)

    if phase == "ASK_PRICE_MODIFY" and pending_modify_bid:
        if event == "REJECT" or text in _NO_TOKENS:
            return _cancel_modify(state)
        price = _price_from_answer(state)
        if price is not None:
            return await recap_modify_price(state, mc_runtime, str(pending_modify_bid), price)
        return await ask_modify_price(state, mc_runtime, str(pending_modify_bid), reask=True)

    # (B) Tunnel NOUVELLE offre en cours.
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

    if phase == "ASK_PRICE" and pending_auction:
        if event == "REJECT" or text in _NO_TOKENS:
            return _cancel_bid(state)
        price = _price_from_answer(state)
        if price is not None:
            return await recap_bid(state, mc_runtime, str(pending_auction), price)
        return await ask_bid_price(state, mc_runtime, str(pending_auction), reask=True)

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
    if not picked and (mapping_kind == "auction" or payload.get("selection_index") is not None):
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
