"""Lecture CONVERSATIONNELLE de l'état de sélection de palier.

(2026-09-05, Phase 9 — extraction du domaine partagé) : cette fonction
vivait dans `pricing_tiers.py`, qui a été déplacé vers
`agriconnect.domain` pour que la couche transactionnelle puisse
l'importer sans dépendre du paquet conversationnel.

Elle ne pouvait pas suivre : elle lit `pending_interaction` et les
contextes de sélection de l'état LangGraph — c'est de l'ORCHESTRATION,
pas une règle de tarification. Le domaine partagé doit rester importable
par `services/` sans traîner l'état conversationnel avec lui.

Les règles pures (validation des paliers, résolution, calcul de ligne,
débit de stock) restent dans `agriconnect.domain.pricing_tiers` et sont
utilisées des deux côtés.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def pending_pack_count_tier(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Le palier (dict brut) dont on attend le NOMBRE DE PAQUETS, sinon None.

    C'est le discriminant d'état `WAITING_FOR_PACKAGE_COUNT` : un palier a été
    résolu (`resolved_tier_id`) et la quantité reste à donner. Distinct de
    `WAITING_FOR_TIER_SELECTION` (menu affiché, aucun palier résolu), pour que
    le même texte "2" ne soit jamais interprété par le même chemin dans les
    deux états — voir `interpreter/routing.py::_interpret_fast_path`.

    (2026-09-02, "no legacy shim") : le garde n'est plus la comparaison de
    chaîne `expected_input=="QUANTITY"` — cette chaîne était partagée avec le
    cas complètement différent d'un simple champ `quantity` en attente
    (`ENTER_FIELD`), et ne se distinguait jusqu'ici que par accident grâce au
    garde `resolved_tier_id` plus bas. `pending_interaction.kind` discrimine
    maintenant les deux cas explicitement, sans ambiguïté possible.
    """
    from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
        InteractionKind,
        get_pending_interaction,
    )

    pending_kind = get_pending_interaction(state).kind
    if pending_kind not in (
        InteractionKind.ENTER_PACKAGE_COUNT,
        InteractionKind.ENTER_QUANTITY,
    ):
        return None

    def _live(ctx: Any) -> Optional[Dict[str, Any]]:
        if isinstance(ctx, dict) and ctx and not ctx.get("__reset__"):
            return ctx
        return None

    tier_ctx = _live(state.get("tier_selection_context"))
    vendor_ctx = _live(state.get("vendor_selection_context"))

    tier_id = None
    for ctx in (tier_ctx, vendor_ctx):
        if ctx and ctx.get("resolved_tier_id"):
            tier_id = ctx.get("resolved_tier_id")
            break
    if not tier_id:
        return None

    pools: List[Any] = []
    if tier_ctx:
        pools.append(tier_ctx.get("tiers"))
    if vendor_ctx:
        chosen = vendor_ctx.get("chosen_vendor")
        if isinstance(chosen, dict):
            pools.append(chosen.get("pricing_tiers"))
    for pool in pools:
        if not isinstance(pool, list):
            continue
        for raw in pool:
            if isinstance(raw, dict) and str(raw.get("tier_id")) == str(tier_id):
                return raw
    return None


__all__ = ["pending_pack_count_tier"]
