"""
LLM Clients — Couche d'abstraction LLM multi-provider.

Pivoter de fournisseur (Groq → Azure OpenAI → AWS Bedrock) se fait
UNIQUEMENT ici + dans settings.py / .env. Aucun agent ne connaît le provider.

Usage:
    from backend.services.llm_clients import get_chat_client, get_sdk_client

Providers supportés:
    - "groq"   : Groq Cloud (LLama 3, Mixtral)
    - "azure"  : Azure OpenAI (GPT-4o, GPT-4o-mini)
    - "bedrock": AWS Bedrock (Claude Haiku, Sonnet) — à venir
"""

import logging
from typing import Optional

from agriconnect.core.settings import settings
import os

logger = logging.getLogger(__name__)

# ── Provider résolu au démarrage ──────────────────────────────
LLM_PROVIDER: str = getattr(settings, "LLM_PROVIDER", "groq")


def get_chat_client(
    model_name: Optional[str] = None,
    temperature: Optional[float] = None,
):
    """
    Retourne un client LangChain Chat (Runnable).

    C'est LE point d'entrée pour tous les agents LangGraph.
    Le provider est déterminé par settings.LLM_PROVIDER.
    """
    _temp = temperature if temperature is not None else settings.LLM_TEMPERATURE

    if LLM_PROVIDER == "azure":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(
            azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            openai_api_version=settings.AZURE_OPENAI_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            temperature=_temp,
        )

    if LLM_PROVIDER == "bedrock":
        raise NotImplementedError(
            "Provider 'bedrock' configuré mais pas encore implémenté. "
            "Installez langchain-aws et complétez cette section."
        )

    # Default: Groq — require an API key in production/dev
    if not getattr(settings, "llm_api_key", None):
        raise RuntimeError("No LLM API key configured. Set GROQ_API_KEY or configure settings.llm_api_key.")

    from langchain_groq import ChatGroq
    return ChatGroq(
        api_key=settings.llm_api_key,
        model_name=model_name or settings.LLM_MODEL,
        temperature=_temp,
    )


def get_sdk_client():
    """
    Retourne un SDK client brut (non-LangChain).

    Utilisé par l'orchestrateur et le retriever pour les appels
    directs (HyDe, routage, etc.).
    """
    if LLM_PROVIDER == "azure":
        from openai import AzureOpenAI
        return AzureOpenAI(
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION,
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        )

    if LLM_PROVIDER == "bedrock":
        raise NotImplementedError("Provider 'bedrock' non implémenté.")

    # Default: Groq
    # Default: Groq SDK — require API key
    if not getattr(settings, "llm_api_key", None):
        raise RuntimeError("No LLM API key configured. Set GROQ_API_KEY or configure settings.llm_api_key.")

    from groq import Groq
    return Groq(api_key=settings.llm_api_key)


# ── Aliases rétro-compatibles ────────────────────────────────
get_groq_client = get_chat_client
get_groq_sdk = get_sdk_client


__all__ = [
    "get_chat_client",
    "get_sdk_client",
    "get_groq_client",
    "get_groq_sdk",
    "LLM_PROVIDER",
]
