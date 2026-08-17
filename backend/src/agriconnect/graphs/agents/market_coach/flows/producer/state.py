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


__all__ = ["ProducerContext"]
