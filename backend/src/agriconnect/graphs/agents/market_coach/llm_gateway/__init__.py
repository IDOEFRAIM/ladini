"""LLM Gateway — routage par profil, santé partagée (Redis), disjoncteur,
repli inter-modèle/inter-provider et observabilité (Langfuse/Prometheus).

Introduit le 2026-09-02 après deux incidents réels coup sur coup :
  1. `LLM_PROVIDER` corrompu dans `.env` → crash total de l'interpréteur.
  2. `LLM_MODEL_REASONING` mesuré à 12.6s de latence réelle contre un budget
     HTTP de 10s → timeout systématique, repli `UNKNOWN`.

Point d'entrée public : `get_llm_gateway().complete(profile=..., messages=...)`.
Les nodes métier ne doivent plus jamais appeler `get_llm()`/choisir un nom de
modèle eux-mêmes — voir `graphs/agents/market_coach/utils.py::MarketRuntime.
llm_gateway` pour le point d'injection unique.

Ce package NE RÉÉCRIT PAS `core/get_llm.py` : les adapters `_GroqAdapter`/
`_BedrockAdapter` (normalisation SDK, strip `<think>`, repli same-provider sur
429/throttling, télémétrie point-unique) restent la couche d'exécution réelle.
Ce package décide QUEL (provider, modèle) essayer, dans quel ordre, avec quel
budget — la couche au-dessus, pas un remplacement.
"""

from agriconnect.graphs.agents.market_coach.llm_gateway.gateway import (
    LegacyOverrideGateway,
    LLMGateway,
    LLMGatewayExhausted,
    get_llm_gateway,
    resolve_gateway,
    resolve_profile,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    CircuitState,
    ErrorClass,
    LLMProfile,
    ModelCandidate,
)

__all__ = [
    "LLMGateway",
    "LLMGatewayExhausted",
    "LegacyOverrideGateway",
    "get_llm_gateway",
    "resolve_gateway",
    "resolve_profile",
    "LLMProfile",
    "ModelCandidate",
    "ErrorClass",
    "CircuitState",
]
