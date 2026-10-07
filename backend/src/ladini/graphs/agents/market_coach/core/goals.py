"""Goal sets — SOURCE DE VÉRITÉ UNIQUE des ensembles de goals de routage.

Tous les frozensets sont DÉRIVÉS du catalogue `INTENT_CONFIG` (champs
`tunnel` / `breakout`, assignés dans `interpreter/intent.py`) — même pattern
anti-drift que `GOALS_NEEDING_FARM_ID` (`core/base.py`).

Historique : ces ensembles étaient tripliqués (`core/router.py`,
`flows/buyer/helpers.py`, `flows/buyer/order_tracking.py`) avec dérive avérée
(`MARKET_MY_REQUESTS` absent de la copie router). Toute nouvelle copie locale
est un bug — importer d'ici.

`_validate_goal_drift()` échoue fort à l'import si le catalogue et ce module
divergent (intent tunnel inconnu, rôle incohérent, tunnel vide).
"""

from __future__ import annotations

from typing import Dict, FrozenSet

from ladini.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)


def _goals_for_tunnel(name: str) -> FrozenSet[str]:
    return frozenset(
        goal for goal, cfg in INTENT_CONFIG.items() if (cfg or {}).get("tunnel") == name
    )


# ── BUYER — tunnels transactionnels ─────────────────────────────────
BUYER_CART_GOALS: FrozenSet[str] = _goals_for_tunnel("cart")
# BUYER_CART_RESET est un goal INTERNE : émis par les flows (preorder.py,
# memory.py), jamais par l'interpréteur — donc volontairement absent du
# catalogue INTENT_CONFIG. Adjonction explicite, pas une dérive.
BUYER_PREORDER_GOALS: FrozenSet[str] = _goals_for_tunnel("preorder") | {
    "BUYER_CART_RESET"
}
BUYER_NEGOTIATION_GOALS: FrozenSet[str] = _goals_for_tunnel("negotiation")
BUYER_ORDER_TRACKING_GOALS: FrozenSet[str] = _goals_for_tunnel("order_tracking")
BUYER_AUCTION_TRACKING_GOALS: FrozenSet[str] = _goals_for_tunnel("auction_tracking")
# Approvisionnement récurrent (Phase 2) — CREATE gère sa propre confirmation
# via RecurringNeedDraft, UPDATE/GET n'ont pas de confirmation générique non
# plus (résolution conversationnelle) — jamais confirmation_gate/
# mcp_tool_executor génériques pour ce tunnel.
BUYER_RECURRING_NEED_GOALS: FrozenSet[str] = _goals_for_tunnel("recurring_need")

ALL_BUYER_TUNNEL_GOALS: FrozenSet[str] = (
    BUYER_CART_GOALS
    | BUYER_PREORDER_GOALS
    | BUYER_NEGOTIATION_GOALS
    | BUYER_ORDER_TRACKING_GOALS
    | BUYER_AUCTION_TRACKING_GOALS
    | BUYER_RECURRING_NEED_GOALS
)

# ── PRODUCER — intents pris en charge par producer_auction_resolver ──
# Ils gèrent leur propre confirmation via working_memory.bid_phase et ne
# doivent JAMAIS être routés vers confirmation_gate/mcp_tool_executor.
PRODUCER_RESOLVER_GOALS: FrozenSet[str] = _goals_for_tunnel("producer_auction")

# Mise à jour catalogue/production : gèrent leur propre confirmation (voir
# _resolve_product_for_update / _resolve_cycle_for_update), jamais
# confirmation_gate/mcp_tool_executor génériques.
PRODUCER_UPDATE_GOALS: FrozenSet[str] = _goals_for_tunnel("producer_update")

# Escrow (Paydunya) : le producteur transmet le code de livraison à 4
# chiffres pour débloquer ses fonds — voir _resolve_delivery_otp, extraction
# déterministe du code, jamais de classification LLM du code lui-même.
PRODUCER_ESCROW_GOALS: FrozenSet[str] = _goals_for_tunnel("producer_escrow")

# ── NAVIGATION / BREAKOUT ────────────────────────────────────────────
# Intents de navigation autorisés à interrompre un tunnel actif.
# Consommé par TunnelManager (breakout) et goal_planner/routing (navigation).
NAVIGATION_BREAKOUT_GOALS: FrozenSet[str] = frozenset(
    goal for goal, cfg in INTENT_CONFIG.items() if (cfg or {}).get("breakout")
)

_KNOWN_TUNNELS = frozenset(
    {
        "cart",
        "preorder",
        "negotiation",
        "order_tracking",
        "auction_tracking",
        "producer_auction",
        "producer_update",
        "producer_escrow",
        "recurring_need",
    }
)

# Intents handled_by_flow sans tunnel de routage post-validator (résolus
# ailleurs dans le graphe) — liste fermée, toute nouveauté doit être choisie.
_TUNNELLESS_FLOW_INTENTS = frozenset({"BUYER_REQUEST"})


def _validate_goal_drift() -> None:
    problems = []

    for goal, cfg in INTENT_CONFIG.items():
        tunnel = (cfg or {}).get("tunnel")
        if tunnel is not None and tunnel not in _KNOWN_TUNNELS:
            problems.append(f"{goal}: tunnel inconnu {tunnel!r}")
        if (
            (cfg or {}).get("handled_by_flow")
            and tunnel is None
            and goal not in _TUNNELLESS_FLOW_INTENTS
        ):
            problems.append(
                f"{goal}: handled_by_flow sans tunnel — assigner un tunnel "
                "dans intent.py (_TUNNEL_ASSIGNMENTS) ou l'ajouter à "
                "_TUNNELLESS_FLOW_INTENTS"
            )

    for name in _KNOWN_TUNNELS:
        if not _goals_for_tunnel(name):
            problems.append(f"tunnel {name!r}: aucun intent assigné")

    buyer_tunnel_goals = ALL_BUYER_TUNNEL_GOALS - {"BUYER_CART_RESET"}
    for goal in buyer_tunnel_goals:
        if INTENT_ROLE.get(goal) not in {"BUYER", "BOTH"}:
            problems.append(f"{goal}: tunnel buyer mais rôle {INTENT_ROLE.get(goal)!r}")
    for goal in PRODUCER_RESOLVER_GOALS:
        if INTENT_ROLE.get(goal) not in {"PRODUCER", "BOTH"}:
            problems.append(
                f"{goal}: tunnel producer mais rôle {INTENT_ROLE.get(goal)!r}"
            )
    for goal in PRODUCER_UPDATE_GOALS:
        if INTENT_ROLE.get(goal) not in {"PRODUCER", "BOTH"}:
            problems.append(
                f"{goal}: tunnel producer_update mais rôle {INTENT_ROLE.get(goal)!r}"
            )
    for goal in PRODUCER_ESCROW_GOALS:
        if INTENT_ROLE.get(goal) not in {"PRODUCER", "BOTH"}:
            problems.append(
                f"{goal}: tunnel producer_escrow mais rôle {INTENT_ROLE.get(goal)!r}"
            )

    if problems:
        raise RuntimeError(
            "Dérive goals/INTENT_CONFIG détectée:\n  - " + "\n  - ".join(problems)
        )


_validate_goal_drift()


# =====================================================================
# REFINEMENT — relation goal générique ↔ goal spécialisé (2026-09-09,
# audit Bloc 2, Blocker A). Relocalisé depuis `nodes/memory.py` (son seul
# appelant) : `core/goals.py` est déjà la source unique des RELATIONS entre
# goals (tunnels, breakout) — cette relation de refinement en est une de
# plus, et vivre ici lui garantit de ne JAMAIS être recopiée dans un second
# nœud (`goal_planner` n'a AUCUNE logique de refinement équivalente —
# vérifié par audit — donc il n'y avait pas de divergence réelle à
# corriger, seulement une dispersion à prévenir).
# =====================================================================

BUYER_REQUEST_SPECIALIZATIONS: FrozenSet[str] = frozenset(
    {
        "BUYER_ADD_TO_CART",
        "BUYER_VIEW_CART",
        "BUYER_EDIT_CART",
        "BUYER_PREORDER_INIT",
        "BUYER_PREORDER_CONFIRM",
        "BUYER_NEGOTIATE_PRICE",
        "BUYER_CHECK_ORDER_STATUS",
        "BUYER_LIST_ORDERS",
        "BUYER_CANCEL_ORDER",
        "BUYER_LIST_AUCTIONS",
        "BUYER_CHECK_AUCTION_STATUS",
        "BUYER_CART_RESET",
    }
)


# =====================================================================
# DRAFTS CANONIQUES VERSIONNÉS (mandat 2026-09-30, "interruption d'une
# confirmation active par une nouvelle intention") : goals qui possèdent leur
# propre cycle de confirmation versionné (`domain/sales_publish_draft.py`,
# `domain/procurement_draft.py`) plutôt que le mécanisme générique
# `confirmation_summary`/`transaction_payload`. Déclaré ici, PAS dérivé de
# `INTENT_CONFIG` (aucun champ du catalogue ne porte cette information) —
# source unique quand même : c'était auparavant SEULEMENT
# `nodes/confirmation_gate.py::_DRAFT_BASED_CONFIRMATION_GOALS`, que
# `nodes/cognitive.py` a désormais besoin de lire aussi (détection d'une
# nouvelle intention pendant WAITING_CONFIRMATION) — une 2e copie locale y
# aurait dérivé exactement comme l'historique documenté en tête de fichier.
DRAFT_BASED_CONFIRMATION_GOALS: FrozenSet[str] = frozenset(
    {"PROCUREMENT_CREATE_REQUEST", "SALES_PUBLISH_PRODUCT"}
)

#: Clé d'état où vit le draft versionné de chaque goal ci-dessus, et nom du
#: champ produit qu'il porte — permet à `cognitive_guard` de comparer le
#: produit du draft ACTIF au produit de la nouvelle intention SANS connaître
#: la forme exacte de chaque draft (juste où lire un `product`).
DRAFT_BASED_CONFIRMATION_STATE_KEY: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": "sales_publish_draft",
    "PROCUREMENT_CREATE_REQUEST": "procurement_draft",
}


def is_goal_refinement(previous: str, incoming: str) -> bool:
    """``BUYER_REQUEST`` → ``BUYER_ADD_TO_CART`` (et symétriquement) est une
    spécialisation, pas un vrai changement de goal.

    ``context_resolver`` (flows buyer/producer, hors périmètre) fait le pont
    entre l'intention générique ``BUYER_REQUEST`` classée par
    l'interpréteur et le goal transactionnel précis (panier, précommande,
    négociation) qu'un tour ultérieur résout. Cette transition ne doit
    JAMAIS déclencher une purge du payload déjà collecté — seul
    ``nodes/memory.py`` en a besoin (déclenchée sur un changement de
    ``payload["intent"]``, pas sur un changement de ``current_goal`` — voir
    sa docstring d'appel), mais la relation elle-même est une propriété du
    CATALOGUE de goals, pas de ce nœud."""
    if previous == "BUYER_REQUEST" and incoming in BUYER_REQUEST_SPECIALIZATIONS:
        return True
    if incoming == "BUYER_REQUEST" and previous in BUYER_REQUEST_SPECIALIZATIONS:
        return True
    return False


__all__ = [
    "BUYER_CART_GOALS",
    "BUYER_PREORDER_GOALS",
    "BUYER_NEGOTIATION_GOALS",
    "BUYER_ORDER_TRACKING_GOALS",
    "BUYER_AUCTION_TRACKING_GOALS",
    "BUYER_RECURRING_NEED_GOALS",
    "ALL_BUYER_TUNNEL_GOALS",
    "PRODUCER_RESOLVER_GOALS",
    "PRODUCER_UPDATE_GOALS",
    "PRODUCER_ESCROW_GOALS",
    "NAVIGATION_BREAKOUT_GOALS",
    "BUYER_REQUEST_SPECIALIZATIONS",
    "DRAFT_BASED_CONFIRMATION_GOALS",
    "DRAFT_BASED_CONFIRMATION_STATE_KEY",
    "is_goal_refinement",
]
