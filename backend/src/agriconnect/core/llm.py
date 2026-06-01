"""
LLM Client — Point d'entrée unique pour tous les clients LLM AgriConnect.

Toute initialisation LLM passe par ici. Plus jamais d'import direct
de rag.components ou services.llm_clients dans le code métier.

Usage:
    from agriconnect.core.llm import get_llm, get_groq_sdk
"""

from agriconnect.core.get_llm import get_llm

__all__ = ["get_llm", "get_groq_sdk"]


def get_groq_sdk():
    """Retourne le SDK client brut (provider-agnostic).

    Délègue à ``rag.components.get_groq_sdk`` pour l'instanciation,
    mais expose un chemin d'import canonique unique.
    """
    from agriconnect.rag.components import get_groq_sdk as _get_groq_sdk

    return _get_groq_sdk()
