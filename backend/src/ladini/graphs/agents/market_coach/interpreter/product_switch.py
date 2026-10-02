"""Détection DÉTERMINISTE d'un changement de produit Buyer pendant un tunnel quantité (B4).

Incident prod (2026-10-02) : « je veux acheter du lait » pendant `ENTER_QUANTITY(poulets)` partait
dans le micro-parser STRUCTURED_ACTION, dont le petit schéma (SELECT_PRODUCER/SELECT_PRICING_TIER/
SET_PACKAGE_COUNT/SET_QUANTITY) ne sait pas encoder une nouvelle demande : sortie invalide ->
UNKNOWN -> `recover_active_tunnel` -> retry++ -> « Max retries reached ».

Principe : un parser spécialisé de tunnel n'a pas le pouvoir de transformer une nouvelle intention
Buyer EXPLICITE en UNKNOWN. Ce module ne CLASSIFIE aucune intention (le classifieur NEW_TASK reste
le moteur) : il dit seulement « ce message n'est pas une réponse au slot courant », signal assez
fort pour contourner le micro-parser du tunnel (`interpreter/routing.py`). Signal fort = verbe
d'achat explicite (`_looks_like_buyer_product_request`, source existante) + produit nommé par un
déterminant (« du/des/de/d' X ») ou directement après un verbe d'achat, DIFFÉRENT du produit suivi.
Jamais sur un nombre/une unité seuls, ni sans produit courant comparable.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ladini.domain.quantity_unit import normalize_unit
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.buyer.helpers import (
    _PRODUCT_HINT_PATTERN,
    _PRODUCT_STOP_WORDS,
)
from ladini.graphs.agents.market_coach.interpreter.active_slot_contract import (
    _product_key,
)
from ladini.graphs.agents.market_coach.interpreter.goal_planner import (
    _BUYER_FILLER_WORDS,
    _looks_like_buyer_product_request,
)

#: Pendings pour lesquels « un autre produit » est incompatible avec la réponse attendue.
_SWITCHABLE_PENDING = frozenset({InteractionKind.ENTER_QUANTITY, InteractionKind.ENTER_PACKAGE_COUNT})
_BUYER_CART_GOALS = frozenset({"BUYER_ADD_TO_CART", "BUYER_REQUEST"})

#: Verbes d'achat DIRECTS (le produit suit immédiatement) — complète les amorces « je veux… » de
#: `_looks_like_buyer_product_request` pour « achète-moi du lait » / « je veux prendre du lait ».
_DIRECT_BUY_VERB = re.compile(r"\b(?:achet\w*|command\w*|prendre|prends)\b(?:-moi)?\s+")
_ADVERBS = frozenset({"plutot", "finalement", "aussi", "encore", "moi", "svp", "stp", "alors", "donc"})
_QTY_EXPR = re.compile(r"\d+(?:[.,]\d+)?\s*[a-z]*\b")


@dataclass(frozen=True)
class ProductSwitch:
    new_product: str
    reason: str


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def _is_noise(token: str) -> bool:
    t = token.strip(" .,;!?'-")
    if not t or t in _PRODUCT_STOP_WORDS or t in _BUYER_FILLER_WORDS or t in _ADVERBS:
        return True
    if normalize_unit(t) is not None:  # kg, l, sacs, unités…
        return True
    from ladini.domain.quantity_unit import text_states_a_quantity

    return bool(text_states_a_quantity(t, 2.0))  # chiffre / numéral (« deux », « douzaine »)


def extract_requested_product(text: str) -> Optional[str]:
    """Produit nommé par un message d'achat explicite, ou `None` (jamais deviné)."""
    clean = _QTY_EXPR.sub(" ", _fold(text))
    candidate: Optional[str] = None
    hint = _PRODUCT_HINT_PATTERN.search(clean)
    if hint:
        candidate = hint.group(1)
    else:
        direct = _DIRECT_BUY_VERB.search(clean)
        if direct:
            candidate = clean[direct.end():]
    if not candidate:
        return None
    words = [w for w in re.findall(r"[a-z'\-]+", candidate)]
    while words and _is_noise(words[-1]):
        words.pop()
    while words and _is_noise(words[0]):
        words.pop(0)
    if not words or len(words) > 2:
        return None
    return " ".join(words)


def _same_product(a: str, b: str) -> bool:
    ka, kb = _product_key(a), _product_key(b)
    return bool(ka and kb) and (ka in kb or kb in ka)


def entities_name_different_product(state: Dict[str, Any]) -> bool:
    """Les entités extraites CE tour nomment-elles un produit DIFFÉRENT de celui du tunnel ?

    Signal structurel (pas lexical) : une vraie réponse de slot ne change jamais de produit —
    même invariant que `active_slot_contract.buyer_slot_answer_conflict` (B1), appliqué ici au
    relabellage NEW_TASK -> ANSWER de `cognitive_guard` (« je veux acheter 5 L de lait » ne doit
    jamais devenir « 5 » pour les poulets)."""
    entities = state.get("extracted_entities")
    payload = state.get("transaction_payload")
    said = str(entities.get("product") or "").strip() if isinstance(entities, dict) else ""
    current = str(payload.get("product") or "").strip() if isinstance(payload, dict) else ""
    return bool(said and current and not _same_product(said, current))


#: Déictiques/ordinaux d'un menu (« celui du premier », « le dernier producteur ») : jamais un
#: produit — ne servent qu'à ne PAS confondre une référence à une option avec un changement de produit.
_MENU_DEICTIC = frozenset(
    "premier premiere deuxieme troisieme quatrieme dernier derniere second seconde celui celle "
    "ceux celles producteur producteurs vendeur vendeurs conditionnement paquet option choix".split()
)


def _option_haystack(state: Dict[str, Any]) -> str:
    """Texte replié de TOUT ce qu'un menu du tunnel affiche (producteurs, offres, paliers) : un
    nom cité dedans (« je veux celui de Gilbert ») désigne une OPTION, pas un nouveau produit."""
    parts: list[str] = [str(c) for c in (state.get("expected_candidates") or [])]
    for key in ("vendor_selection_context", "tier_selection_context"):
        ctx = state.get(key)
        if not isinstance(ctx, dict) or ctx.get("__reset__"):
            continue
        for v in ctx.get("vendors") or []:
            if isinstance(v, dict):
                parts += [str(v.get("vendor_name") or ""), str(v.get("name") or "")]
        for t in ctx.get("tiers") or []:
            if isinstance(t, dict):
                parts += [str(t.get("packaging") or ""), str(t.get("unit") or "")]
    return _fold(" ".join(parts))


def _in_cart_tunnel(state: Dict[str, Any]) -> bool:
    """Un tunnel panier Buyer attend une réponse précise : quantité/paquets, OU un menu
    producteur/palier (`SelectionContext.expected_action`, même signal que la route
    STRUCTURED_ACTION — jamais `SELECTION_MENU` générique, qui couvre aussi commandes/enchères)."""
    from ladini.graphs.agents.market_coach.domain.selection_actions import (
        build_selection_context,
    )

    if get_pending_interaction(state).kind in _SWITCHABLE_PENDING:
        return True
    ctx = build_selection_context(state)
    return ctx is not None and ctx.expected_action is not None


def detect_buyer_product_switch(state: Dict[str, Any], text: str) -> Optional[ProductSwitch]:
    """Le message est-il une NOUVELLE demande d'achat sur un produit différent, pendant un
    tunnel quantité Buyer ? `None` dans tous les autres cas (réponse de slot, même produit,
    annulation, message incompris…)."""
    from ladini.graphs.agents.market_coach.core.state import resolve_current_goal

    goal = str(resolve_current_goal(state) or "").upper()
    if goal not in _BUYER_CART_GOALS or not _in_cart_tunnel(state):
        return None
    payload = state.get("transaction_payload")
    current = str(payload.get("product") or "").strip() if isinstance(payload, dict) else ""
    if not current:
        vctx = state.get("vendor_selection_context")
        current = str(vctx.get("product") or "").strip() if isinstance(vctx, dict) else ""
    if not current:
        return None
    clean = str(text or "").strip().lower()
    if not clean:
        return None
    explicit_buy = _looks_like_buyer_product_request(clean) or bool(_DIRECT_BUY_VERB.search(_fold(clean)))
    if not explicit_buy:
        return None
    requested = extract_requested_product(clean)
    if not requested or _same_product(requested, current):
        return None
    # Menu producteur/palier : une référence à une option (nom d'offre affiché, ordinal) n'est
    # PAS un changement de produit.
    if any(w in _MENU_DEICTIC for w in requested.split()):
        return None
    if _fold(requested) in _option_haystack(state):
        return None
    return ProductSwitch(new_product=requested, reason="explicit_buy_different_product")


__all__ = [
    "ProductSwitch",
    "detect_buyer_product_switch",
    "entities_name_different_product",
    "extract_requested_product",
]
