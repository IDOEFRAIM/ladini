"""
LLM Client — Point d'entrée unique pour tous les clients LLM AgriConnect.

Toute initialisation LLM passe par ici. Plus jamais d'import direct
de rag.components ou services.llm_clients dans le code métier.

Usage:
    from agriconnect.core.llm import get_llm, get_groq_sdk
"""

from agriconnect.core.get_llm import get_groq_sdk, get_llm

__all__ = ["get_llm", "get_groq_sdk"]
