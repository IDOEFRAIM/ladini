"""Protocole numérique Buyer pendant un tunnel panier (B7, 2026-10-02) : le sens d'un nombre dépend
du pending ACTIF, jamais d'un ancien menu encore présent dans l'état.

Incident prod : après le choix du producteur (« 2 » -> Gilbert-prod, « Quelle quantité ? »), l'UI
disait « Pour changer de producteur, répondez avec le numéro correspondant (1 à 7) » alors que le
nombre nu suivant doit être une QUANTITÉ — deux sens pour le même « 2 ».

Protocole final (le code appelant l'applique AVANT toute classification LLM) :

  pending = SELECT_PRODUCER (menu producteur vivant)  : « 2 »            -> producteur n°2
                                                         (route existante, inchangée)
  pending = ENTER_QUANTITY                            : « 2 », « 5 L »    -> QUANTITÉ (jamais un index)
                                                        « producteur 2 », « changer producteur 2 »,
                                                        « je veux le producteur 2 » -> producteur n°2
                                                        « changer producteur » -> menu producteurs

Priorité pendant ENTER_QUANTITY (voir `routing.input_interpreter`) : produit différent (switch) >
même produit > commande producteur explicite > quantité nue > reste (parser du tunnel / chaîne
normale). Jamais de nombre nu pour changer de producteur.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, Optional

from ladini.domain.quantity_unit import (
    is_pure_numeric_answer,
    scan_number_candidates,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_contract import (
    adapt_structured_action_to_canonical,
)

logger = logging.getLogger("Ladini.Market.NumericProtocol")

_BUYER_CART_GOALS = frozenset({"BUYER_ADD_TO_CART", "BUYER_REQUEST"})
#: pendings « valeur de slot » (la quantité en unité de base OU le nombre de paquets d'un palier résolu)
_QUANTITY_SLOT_KINDS = frozenset({InteractionKind.ENTER_QUANTITY, InteractionKind.ENTER_PACKAGE_COUNT})
#: actions structurées d'un tunnel panier qui attend une réponse précise
_TUNNEL_ACTIONS = frozenset(
    {ActionType.SELECT_PRICING_TIER, ActionType.SET_PACKAGE_COUNT, ActionType.SET_QUANTITY}
)

#: « producteur 3 », « changer producteur », « changer de producteur 3 », « je veux le producteur 3 »,
#: « choisir le vendeur 2 ». Une commande exige un VERBE de changement OU un numéro (« producteur »
#: seul est ambigu et laissé à la chaîne normale).
_PRODUCER_COMMAND = re.compile(
    r"^(?:je\s+(?:veux|voudrais)\s+)?"
    r"(?P<change>(?:changer|change|choisir|choisis|prendre|prends)\s+(?:de\s+|le\s+|un\s+autre\s+|l'autre\s+)?|(?:un\s+)?autre\s+|le\s+)?"
    r"(?:producteur|vendeur|fournisseur)s?"
    r"(?:\s*(?:n°|no\b|numero|num|#)?\s*(?P<num>\d{1,3}))?\s*$"
)


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).strip(" .!?,;:")


def _live_vendors(state: Dict[str, Any]) -> list:
    ctx = state.get("vendor_selection_context")
    if not isinstance(ctx, dict) or ctx.get("__reset__"):
        return []
    vendors = ctx.get("vendors")
    return [v for v in vendors if isinstance(v, dict)] if isinstance(vendors, list) else []


def _result(event: str, goal: str, entities: Dict[str, Any], path: str, confidence: float) -> Dict[str, Any]:
    return {
        "interpreted_event": event,
        "detected_intent": goal if event != "SELECTION" else "UNKNOWN",
        "interpreter_confidence": confidence,
        "extracted_entities": entities,
        "raw_analysis": {"path": path},
    }


def interpret_buyer_numeric_protocol(
    state: Dict[str, Any], text: str, *, llm_available: bool = True
) -> Optional[Dict[str, Any]]:
    """Résultat d'interprétation DÉTERMINISTE pour une commande producteur explicite ou un nombre nu
    pendant `ENTER_QUANTITY`, sinon `None` (le message suit alors son chemin normal)."""
    pending = get_pending_interaction(state)
    goal = str(resolve_current_goal(state) or "").upper()
    sel = build_selection_context(state)
    expected = sel.expected_action if sel is not None else None
    in_quantity_slot = pending.kind in _QUANTITY_SLOT_KINDS
    in_tier_menu = expected == ActionType.SELECT_PRICING_TIER
    # Tunnel panier Buyer qui attend une réponse précise (quantité, nombre de paquets, menu de paliers).
    if goal not in _BUYER_CART_GOALS or not (
        in_quantity_slot or expected in _TUNNEL_ACTIONS
    ):
        return None
    clean = _fold(text)
    if not clean:
        return None
    stale = bool(state.get("expected_candidates"))
    kind = pending.kind.value

    # 1. commande producteur EXPLICITE (prioritaire sur tout nombre nu — y compris pendant le menu de paliers)
    match = _PRODUCER_COMMAND.match(clean)
    vendors = _live_vendors(state)
    if match and (match.group("change") or match.group("num")) and len(vendors) > 1:
        # « le » seul n'est pas un verbe de changement : « le producteur » sans numéro = ambigu
        change_word = (match.group("change") or "").strip()
        num = match.group("num")
        if num:
            logger.info(
                "BUYER_NUMERIC_INPUT_INTERPRETED pending_kind=%s interpretation=PRODUCER_INDEX "
                "stale_candidates_present=%s explicit_command=true",
                kind, stale,
            )
            return _result("SELECTION", goal, {"selection_index": int(num)}, "deterministic_producer_command", 1.0)
        if change_word and change_word != "le":
            logger.info(
                "BUYER_NUMERIC_INPUT_INTERPRETED pending_kind=%s interpretation=CHANGE_PRODUCER_MENU "
                "stale_candidates_present=%s explicit_command=true",
                kind, stale,
            )
            return _result("ANSWER", goal, {}, "deterministic_change_producer", 0.98)

    # 2. menu de PALIERS : un nombre nu est un index de conditionnement ; hors plage = INVALIDE (jamais
    #    une quantité, un nombre de paquets ni un palier arbitraire — aucune mutation, message explicite).
    if in_tier_menu and clean.isdigit():
        n_tiers = len(sel.tier_options) if sel is not None else 0
        if n_tiers and not 1 <= int(clean) <= n_tiers:
            logger.info(
                "BUYER_NUMERIC_INPUT_INTERPRETED pending_kind=%s interpretation=INVALID_TIER_INDEX "
                "stale_candidates_present=%s explicit_command=false",
                kind, stale,
            )
            return _result("ANSWER", goal, {}, "deterministic_invalid_tier_index", 0.98)
        return None  # index valide : `fast_path_action` (déterministe, sans LLM) le résout

    # 3. nombre nu = valeur du slot ACTIF (jamais un index de menu)
    if (
        llm_available
        and in_quantity_slot
        and is_pure_numeric_answer(clean)
        and re.search(r"\d", clean)
    ):
        candidates = scan_number_candidates(clean)
        # exactement UN nombre, jamais un prix, strictement positif (« 0 », « 2 et 5 » -> parser du tunnel)
        if len(candidates) != 1 or candidates[0].near_currency or candidates[0].value <= 0:
            return None
        value, unit = candidates[0].value, candidates[0].unit
        logger.info(
            "BUYER_NUMERIC_INPUT_INTERPRETED pending_kind=%s interpretation=%s "
            "stale_candidates_present=%s explicit_command=false",
            kind, "PACKAGE_COUNT" if expected == ActionType.SET_PACKAGE_COUNT else "QUANTITY", stale,
        )
        if expected == ActionType.SET_PACKAGE_COUNT:
            # Palier RÉSOLU : le nombre est un NOMBRE DE PAQUETS, sans dimension. Une unité écrite
            # (« 30 L ») est une quantité totale -> laissée au parser/cart (classify_pack_count_unit).
            if unit:
                return None
            packages: Dict[str, Any] = adapt_structured_action_to_canonical(
                {"action": ActionType.SET_PACKAGE_COUNT, "package_count": value}, goal, "deterministic_package_count"
            )
            return packages
        # Tunnel structuré (vendeur choisi, produit à tarif unique) : MÊME contrat canonique que le
        # micro-prompt STRUCTURED_ACTION (`agent_action=SET_QUANTITY`) — le flux aval est identique.
        if expected == ActionType.SET_QUANTITY:
            adapted: Dict[str, Any] = adapt_structured_action_to_canonical(
                {"action": ActionType.SET_QUANTITY, "quantity": value, "unit": unit},
                goal,
                "deterministic_bare_quantity",
            )
            return adapted
        entities: Dict[str, Any] = {"quantity": value}
        if unit:  # unité LITTÉRALEMENT écrite — sinon l'unité de l'offre fait foi
            entities["unit"] = unit
        return _result("ANSWER", goal, entities, "deterministic_bare_quantity", 0.98)
    return None


__all__ = ["interpret_buyer_numeric_protocol"]
