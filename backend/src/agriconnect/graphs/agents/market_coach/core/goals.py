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

from typing import FrozenSet

from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)


def _goals_for_tunnel(name: str) -> FrozenSet[str]:
    return frozenset(
        goal for goal, cfg in INTENT_CONFIG.items()
        if (cfg or {}).get("tunnel") == name
    )


# ── BUYER — tunnels transactionnels ─────────────────────────────────
BUYER_CART_GOALS: FrozenSet[str] = _goals_for_tunnel("cart")
# BUYER_CART_RESET est un goal INTERNE : émis par les flows (preorder.py,
# memory.py), jamais par l'interpréteur — donc volontairement absent du
# catalogue INTENT_CONFIG. Adjonction explicite, pas une dérive.
BUYER_PREORDER_GOALS: FrozenSet[str] = _goals_for_tunnel("preorder") | {"BUYER_CART_RESET"}
BUYER_NEGOTIATION_GOALS: FrozenSet[str] = _goals_for_tunnel("negotiation")
BUYER_ORDER_TRACKING_GOALS: FrozenSet[str] = _goals_for_tunnel("order_tracking")
BUYER_AUCTION_TRACKING_GOALS: FrozenSet[str] = _goals_for_tunnel("auction_tracking")

ALL_BUYER_TUNNEL_GOALS: FrozenSet[str] = (
    BUYER_CART_GOALS
    | BUYER_PREORDER_GOALS
    | BUYER_NEGOTIATION_GOALS
    | BUYER_ORDER_TRACKING_GOALS
    | BUYER_AUCTION_TRACKING_GOALS
)

# ── PRODUCER — intents pris en charge par producer_auction_resolver ──
# Ils gèrent leur propre confirmation via working_memory.bid_phase et ne
# doivent JAMAIS être routés vers confirmation_gate/mcp_tool_executor.
PRODUCER_RESOLVER_GOALS: FrozenSet[str] = _goals_for_tunnel("producer_auction")

# ── NAVIGATION / BREAKOUT ────────────────────────────────────────────
# Intents de navigation autorisés à interrompre un tunnel actif.
# Consommé par TunnelManager (breakout) et goal_planner/routing (navigation).
NAVIGATION_BREAKOUT_GOALS: FrozenSet[str] = frozenset(
    goal for goal, cfg in INTENT_CONFIG.items()
    if (cfg or {}).get("breakout")
)

_KNOWN_TUNNELS = frozenset({
    "cart", "preorder", "negotiation", "order_tracking",
    "auction_tracking", "producer_auction",
})

# Intents handled_by_flow sans tunnel de routage post-validator (résolus
# ailleurs dans le graphe) — liste fermée, toute nouveauté doit être choisie.
_TUNNELLESS_FLOW_INTENTS = frozenset({"BUYER_REQUEST"})


def _validate_goal_drift() -> None:
    problems = []

    for goal, cfg in INTENT_CONFIG.items():
        tunnel = (cfg or {}).get("tunnel")
        if tunnel is not None and tunnel not in _KNOWN_TUNNELS:
            problems.append(f"{goal}: tunnel inconnu {tunnel!r}")
        if (cfg or {}).get("handled_by_flow") and tunnel is None \
                and goal not in _TUNNELLESS_FLOW_INTENTS:
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
            problems.append(f"{goal}: tunnel producer mais rôle {INTENT_ROLE.get(goal)!r}")

    if problems:
        raise RuntimeError(
            "Dérive goals/INTENT_CONFIG détectée:\n  - " + "\n  - ".join(problems)
        )


_validate_goal_drift()


__all__ = [
    "BUYER_CART_GOALS",
    "BUYER_PREORDER_GOALS",
    "BUYER_NEGOTIATION_GOALS",
    "BUYER_ORDER_TRACKING_GOALS",
    "BUYER_AUCTION_TRACKING_GOALS",
    "ALL_BUYER_TUNNEL_GOALS",
    "PRODUCER_RESOLVER_GOALS",
    "NAVIGATION_BREAKOUT_GOALS",
]
