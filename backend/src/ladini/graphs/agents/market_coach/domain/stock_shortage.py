"""Décision de rupture de stock partielle (acheteur) — état explicite et déterministe.

## Incident réel (2026-09-28)

Offre « Gilbert-prod — 450000 FCFA/UNITE — Dispo: 20 ». L'acheteur demande 461000 :

    📉 Stock insuffisant : 20 disponibles sur 461000 demandés.
    🙋 Appel d'offres ? *oui* / *non*      👉 Ou répondez *20* pour prendre le stock disponible.
    « 20 »  ->  « Confirmez-vous cette opération pour 461 000 TETE de bœufs ? »
    « oui » ->  PROCUREMENT_CREATE_REQUEST  (« lançons un appel d'offres »)

Trois défauts, un seul état manquant :

1. **La quantité demandée (461000) restait autoritaire.** « 20 » n'était pas lu comme
   une décision : le tour tombait sur le récap générique de confirmation, construit depuis
   le `transaction_payload` périmé.
2. **La branche n'était pas portée par l'état.** Deux drapeaux épars de `working_memory`
   (`buyer_request_waiting_choice`, `buyer_request_available_quantity`) laissaient chaque
   tour RE-DÉDUIRE la branche : un « oui » lancé après « 20 » retombait sur l'appel d'offres.
3. **L'offre n'était pas mémorisée.** Seuls `product`/`quantity`/`unit` survivaient ;
   `vendor_selection_context` était vidé (`None`) — impossible de reprendre CETTE offre.

`StockShortageDecision` porte tout : l'offre exacte (snapshot), la quantité demandée
(HISTORIQUE — jamais relue comme quantité d'achat), la quantité disponible, et les trois
branches exclusives `TAKE_AVAILABLE` / `START_TENDER` / `CANCEL`.

Module pur : aucune dépendance au graphe, aucun appel réseau.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)

#: Discriminant de `PendingInteraction.target` pour une question de rupture de stock.
TARGET_KIND = "STOCK_SHORTAGE"

#: Clé de `working_memory` portant la décision en attente (None = aucune).
SHORTAGE_KEY = "stock_shortage"

#: Anciens drapeaux, conservés pour compatibilité de lecture (checkpoints en vol).
LEGACY_SHORTAGE_KEYS = (
    "buyer_request_waiting_choice",
    "buyer_request_catalog_checked",
    "buyer_request_last_product",
    "buyer_request_available_quantity",
    "buyer_request_available_unit",
)


class ShortageBranch(str, Enum):
    """Branches transactionnelles EXCLUSIVES d'une rupture partielle."""

    #: Achat direct de la quantité choisie (par défaut : tout le stock disponible).
    TAKE_AVAILABLE = "TAKE_AVAILABLE"
    #: Appel d'offres pour la quantité ORIGINALEMENT demandée.
    START_TENDER = "START_TENDER"
    CANCEL = "CANCEL"


_OFFER_KEYS_DROPPED = ("images",)

_BARE_NUMBER_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")


def build_shortage_state(
    *,
    offer: Dict[str, Any],
    requested_quantity: float,
    available_quantity: float,
    unit: str,
    menu_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Snapshot durable de la décision. `offer` = ligne EXACTE du menu (jamais
    re-résolue depuis le seul producteur)."""
    slim_offer = {k: v for k, v in dict(offer).items() if k not in _OFFER_KEYS_DROPPED}
    return {
        "status": "ACTIVE",
        "menu_id": menu_id,
        "product_id": str(slim_offer.get("product_id") or slim_offer.get("id") or ""),
        "producer_id": str(slim_offer.get("producer_id") or ""),
        "product_name": slim_offer.get("name"),
        "vendor_name": slim_offer.get("vendor_name"),
        "unit_price": slim_offer.get("price"),
        "unit": unit,
        # HISTORIQUE : ce que l'acheteur avait demandé. Ne pilote JAMAIS l'achat direct.
        "original_requested_quantity": float(requested_quantity),
        "available_quantity": float(available_quantity),
        "offer": slim_offer,
    }


def active_shortage(state_or_wm: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """La décision en attente, si elle existe. Accepte l'état complet ou directement
    `working_memory`."""
    wm = state_or_wm.get("working_memory") if "working_memory" in state_or_wm else state_or_wm
    if not isinstance(wm, dict):
        return None
    value = wm.get(SHORTAGE_KEY)
    if isinstance(value, dict) and value.get("status") == "ACTIVE" and value.get("offer"):
        return value
    return None


def pending_target(shortage: Dict[str, Any]) -> Dict[str, Any]:
    """`PendingInteraction.target` liant la question posée à CETTE décision/offre."""
    return {
        "kind": TARGET_KIND,
        "product_id": shortage.get("product_id"),
        "available_quantity": shortage.get("available_quantity"),
    }


def shortage_awaiting_reply(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """La décision de rupture à laquelle l'acheteur est EN TRAIN de répondre — ou None.

    Exige que l'interaction en attente soit la question de rupture elle-même (kind
    CONFIRM_ACTION + `target.kind == STOCK_SHORTAGE` + même offre), pas seulement qu'un
    dict de décision traîne dans `working_memory` : une confirmation sans rapport
    (précommande, appel d'offres) ne peut jamais capter un nombre nu comme
    `TAKE_AVAILABLE`."""
    shortage = active_shortage(state)
    if shortage is None:
        return None
    pending = get_pending_interaction(state)
    if pending.kind != InteractionKind.CONFIRM_ACTION:
        return None
    target = pending.target or {}
    if target.get("kind") != TARGET_KIND:
        return None
    if str(target.get("product_id") or "") != str(shortage.get("product_id") or ""):
        return None
    return shortage


def clear_shortage_patch() -> Dict[str, Any]:
    """Patch `working_memory` (canal `merge_dict` : seule une valeur `None` efface)."""
    patch: Dict[str, Any] = {SHORTAGE_KEY: None}
    for key in LEGACY_SHORTAGE_KEYS:
        patch[key] = None
    return patch


def parse_bare_quantity(text: str) -> Optional[float]:
    """`« 20 »` / `« 2,5 »` -> 20.0 / 2.5 ; tout autre message -> None.

    Volontairement strict (même discipline que `fast_path_action`) : un message qui
    contient autre chose qu'un nombre (« non je prends 20 ») est laissé à
    l'interpréteur, jamais deviné ici."""
    match = _BARE_NUMBER_RE.match(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_quantity_reply(shortage: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Réponse numérique nue à la question « répondez N pour prendre le stock ».

    GÉNÉRIQUE — aucune valeur en dur : pour `available=7`, « 7 » prend tout le stock ;
    « 2.5 » (kg) idem si c'est le disponible. Un nombre différent du disponible reste
    une quantité d'achat DIRECT choisie par l'acheteur (ex. « 15 » sur 20 disponibles) ;
    l'appelant revalide le stock. Retourne None si `text` n'est pas un nombre nu."""
    quantity = parse_bare_quantity(text)
    if quantity is None:
        return None
    available = float(shortage.get("available_quantity") or 0.0)
    takes_all = abs(quantity - available) < 1e-9
    return {
        "branch": ShortageBranch.TAKE_AVAILABLE,
        "purchase_quantity": quantity,
        "takes_all_available": takes_all,
    }


__all__ = [
    "SHORTAGE_KEY",
    "TARGET_KIND",
    "LEGACY_SHORTAGE_KEYS",
    "ShortageBranch",
    "build_shortage_state",
    "active_shortage",
    "pending_target",
    "shortage_awaiting_reply",
    "clear_shortage_patch",
    "parse_bare_quantity",
    "resolve_quantity_reply",
]
