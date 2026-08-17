"""UI Engine — Point unique de conversion MenuRequest → composant AG-UI.

Ce nœud LangGraph lit ``state["pending_menu"]`` (un ``MenuRequest``)
et produit les champs d'état standardisés :
  - ``ag_ui_component``    : dict AG-UI ``ListMenu``
  - ``available_mapping``  : dict index → valeur métier
  - ``expected_candidates``: list[str] des labels d'options
  - ``working_memory``     : fragment ``{available_mapping_kind: ...}``

Si ``pending_menu`` est ``None`` ou absent, le nœud est un **pass-through
silencieux** (retourne un dict vide).

Règle :
  Les flows métier NE DOIVENT PAS construire ``ag_ui_component``
  manuellement. Ils retournent un ``MenuRequest`` que le DomainRouter
  stocke dans ``state["pending_menu"]``. Ce nœud le consomme.

  Pendant la période de migration, les flows qui retournent encore
  un ``ag_ui_component`` directement continuent de fonctionner —
  ce nœud ne les écrasera pas (il ne s'active QUE si ``pending_menu``
  est un ``MenuRequest``).

Dépendances :
  - ``flows.common.menu_contracts`` (feuille pure, zéro cycle)
  - ``core.state`` (types uniquement, pas de logique)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)

logger = logging.getLogger("AgriConnect.Market.UIEngine")


# =====================================================================
# PUBLIC NODE
# =====================================================================


async def ui_engine(
    state: Dict[str, Any], _mc_runtime: Any | None = None, **_kwargs: Any
) -> Dict[str, Any]:
    """Nœud LangGraph : transforme un ``MenuRequest`` en composant AG-UI.

    Paramètres ignorés via ``**_kwargs`` pour compatibilité avec
    ``_safe_node`` qui injecte ``mc_runtime``.
    """
    menu = state.get("pending_menu")

    # Pas de menu en attente → pass-through silencieux
    if not isinstance(menu, MenuRequest):
        return {}

    logger.info(
        "ui_engine: rendering menu kind=%s options=%d title=%s",
        menu.kind,
        len(menu.options),
        menu.title[:40],
    )

    # Construire le composant AG-UI
    ag_ui = _build_ag_ui_component(menu)
    mapping = menu.to_mapping()
    candidates = menu.to_candidates()
    wm_patch = menu.to_working_memory_patch()

    session_id = str(state.get("session_id") or state.get("user_phone") or "")
    snapshot = menu_snapshot_store.save(
        session_id,
        mapping,
        kind=menu.kind,
        metadata=menu.metadata,
    )
    wm_patch["menu_snapshot_id"] = snapshot.menu_id
    ag_ui.setdefault("kwargs", {}).setdefault("metadata", {})["menu_snapshot_id"] = (
        snapshot.menu_id
    )

    result: Dict[str, Any] = {
        "ag_ui_component": ag_ui,
        "available_mapping": mapping,
        "expected_candidates": candidates,
        "expected_input": "SELECTION",
        "working_memory": {
            **(state.get("working_memory") or {}),
            **wm_patch,
        },
        "menu_snapshot_id": snapshot.menu_id,
        # Consommer le menu pour qu'il ne soit pas re-traité
        "pending_menu": None,
    }

    base_text = (state.get("final_response") or "").strip()
    if menu.preformatted_text:
        instructions = menu.preformatted_text.strip()
        if instructions:
            if base_text:
                normalized_base = " ".join(base_text.split())
                normalized_instructions = " ".join(instructions.split())
                if normalized_base == normalized_instructions:
                    result["final_response"] = base_text
                else:
                    result["final_response"] = f"{base_text}\n\n{instructions}".strip()
            else:
                result["final_response"] = instructions

    # Conserver le texte existant s'il n'y a pas d'instructions
    if base_text and "final_response" not in result:
        result["final_response"] = base_text

    return result


# =====================================================================
# PRIVATE — construction du dict AG-UI
# =====================================================================


def _build_ag_ui_component(menu: MenuRequest) -> Dict[str, Any]:
    """Construit le dictionnaire ``ag_ui_component`` normalisé."""
    options_list = [{"index": opt.index, "label": opt.label} for opt in menu.options]
    metadata = dict(menu.metadata)
    metadata.setdefault("kind", menu.kind)

    return {
        "lc_type": "constructor",
        "id": ["ag_ui", "ListMenu"],
        "kwargs": {
            "title": menu.title,
            "options": options_list,
            "metadata": metadata,
        },
    }


__all__ = ["ui_engine"]
