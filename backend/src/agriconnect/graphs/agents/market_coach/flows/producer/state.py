"""ProducerContext — Sous-structure typée propre au flow Producteur.

Contient les champs strictement liés à l'exploitation agricole (cache des
fermes du producteur, etc.). Hérité par `MarketAgentState` via composition de
`TypedDict` : les clés restent à plat dans l'état runtime, aucune réécriture
des accès `state.get(...)` n'est nécessaire.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing_extensions import Annotated, TypedDict

from agriconnect.agents.reducers import load_snapshot, replace_value


class ProducerContext(TypedDict, total=False):
    """Champs spécifiques au domaine Producteur."""

    # Cache des fermes/exploitations de l'utilisateur producteur ; alimenté par
    # input_normalizer au boot d'une session et consommé par farm_logic /
    # validation pour résoudre `farm_id` sans aller-retour MCP supplémentaire.
    user_farms_cache: Annotated[Optional[List[Dict[str, Any]]], replace_value]

    original_entity: Annotated[Optional[Dict[str, Any]], load_snapshot]
    current_entity: Annotated[Optional[Dict[str, Any]], load_snapshot]

    # (2026-09-08, P1-4 audit architectural) : les 3 champs suivants étaient
    # écrits par `flows/producer/farm_logic.py::ensure_farm_node` mais
    # n'étaient déclarés dans AUCUN des 3 registres — LangGraph les
    # supprimait silencieusement à la traversée du graphe COMPILÉ, y compris
    # DANS LE MÊME TOUR (le filtrage de schéma s'applique à chaque superstep,
    # pas seulement à la sauvegarde du checkpoint). Conséquence : le garde
    # anti-double-tentative de création de ferme était mort (toujours faux
    # au tour suivant), et les deux messages utilisateur qui en dépendent
    # (`rendering/success.py`) ne pouvaient jamais s'afficher.
    farm_creation_attempted: Annotated[Optional[bool], replace_value]
    auto_farm_notice: Annotated[Optional[str], replace_value]
    error_creating_farm: Annotated[Optional[bool], replace_value]


__all__ = ["ProducerContext"]
