"""Smoke test des providers LLM (Groq / bedrock_gateway / bedrock_native) —
EN DEHORS du moteur conversationnel, pour reproduire/valider un incident
LLM_GATEWAY_EXHAUSTED indépendamment de tout tour de conversation.

À LANCER SUR LE SERVEUR DE PROD (ou tout environnement qui a les vraies
variables — OPENAI_BASE_URL/OPENAI_API_KEY/GROQ_API_KEY/AWS_*) avec le MÊME
python/venv que l'application (mêmes dépendances : openai, groq, boto3,
pydantic-settings) :

    cd backend && python ../scripts/smoke_test_llm_providers.py
    # ou, si l'app tourne via poetry :
    cd backend && poetry run python ../scripts/smoke_test_llm_providers.py

Ce script :
  1. Affiche `validate_config()` (llm_gateway/registry.py) — détecte SANS
     appel réseau les configs manifestement invalides (ex: bedrock_gateway
     configuré sans OPENAI_BASE_URL, ou pointant sur l'API OpenAI publique).
  2. Affiche le HOST (jamais la clé) vers lequel OPENAI_BASE_URL pointe.
  3. Pour CHAQUE candidat (provider:model) configuré sur les 3 profils
     (FAST/REASONING/INTERPRETER), tente un appel minimal ("Réponds
     seulement OK") via les MÊMES adapters que la prod
     (`core/get_llm.py::_GroqAdapter`/`_BedrockAdapter`), en bypassant le
     Gateway (pas de disjoncteur/budget/retry — un candidat, un essai, un
     verdict) — exactement l'esprit du mandat §5/§20 ("tester Bedrock
     indépendamment du moteur").
  4. N'affiche JAMAIS de secret (clé API, token) — seulement provider,
     modèle, host, statut, et le message d'erreur du SDK (déjà sans
     credential par construction : openai/groq/botocore n'y mettent jamais
     la clé elle-même).

Sortie attendue : une ligne par candidat, OK ou ÉCHEC avec la classe
d'erreur/le message. Aucune écriture, aucune modification — lecture seule.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

# Rend `ladini` importable qu'on lance ce script depuis la racine du repo,
# depuis backend/, ou ailleurs — sans dépendre d'un PYTHONPATH déjà posé.
_BACKEND_SRC = Path(__file__).resolve().parent.parent / "backend" / "src"
if _BACKEND_SRC.is_dir() and str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))


def main() -> int:
    from ladini.core.settings import settings
    from ladini.graphs.agents.market_coach.llm_gateway.registry import (
        ModelRegistry,
        validate_config,
    )
    from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile

    print("=" * 70)
    print("1. validate_config() — détection statique, sans réseau")
    print("=" * 70)
    issues = validate_config(settings)
    if not issues:
        print("  Aucun problème de configuration détecté.")
    else:
        for issue in issues:
            print(f"  ⚠ {issue}")

    print()
    print("=" * 70)
    print("2. Cible de OPENAI_BASE_URL (jamais la clé)")
    print("=" * 70)
    base_url = str(getattr(settings, "OPENAI_BASE_URL", "") or "")
    if not base_url:
        print("  OPENAI_BASE_URL est VIDE — le client OpenAI-compatible visera")
        print("  l'API OpenAI publique par défaut (jamais Bedrock).")
    else:
        parsed = urlparse(base_url)
        print(f"  scheme={parsed.scheme!r} host={parsed.hostname!r} port={parsed.port!r}")
        if parsed.hostname == "api.openai.com":
            print("  ⚠ Ceci est l'API OpenAI PUBLIQUE, pas une passerelle Bedrock.")

    print()
    print("=" * 70)
    print("3. Appel minimal par candidat configuré (bypass du Gateway)")
    print("=" * 70)
    registry = ModelRegistry(settings)
    any_attempted = False
    for profile in (LLMProfile.FAST, LLMProfile.REASONING, LLMProfile.INTERPRETER):
        for candidate in registry.candidates_for(profile):
            any_attempted = True
            _try_candidate(profile, candidate)

    if not any_attempted:
        print("  Aucun candidat activé trouvé dans la configuration actuelle.")
    return 0


def _client_for(provider: str):
    from ladini.core.get_llm import (
        _BedrockAdapter,
        _GroqAdapter,
        get_bedrock_client,
        get_groq_sdk,
        get_openai_compatible_sdk,
    )

    if provider == "groq":
        return _GroqAdapter(get_groq_sdk(), provider="groq")
    if provider == "bedrock_gateway":
        return _GroqAdapter(get_openai_compatible_sdk(), provider="bedrock_gateway")
    if provider == "bedrock_native":
        return _BedrockAdapter(get_bedrock_client())
    raise ValueError(f"provider inconnu: {provider!r}")


def _try_candidate(profile, candidate) -> None:
    label = f"profile={profile.value:<11} candidate={candidate.key}"
    try:
        client = _client_for(candidate.provider)
    except Exception as exc:
        print(f"  ✗ {label} | construction du client échouée: {type(exc).__name__}: {exc}")
        return

    try:
        resp = client.chat.completions.create(
            model=candidate.model,
            messages=[{"role": "user", "content": "Réponds seulement OK"}],
            temperature=0.0,
            max_tokens=20,
        )
        content = None
        try:
            content = resp.choices[0].message.content
        except Exception:
            pass
        print(f"  ✓ {label} | 200 OK | réponse={content!r}")
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        print(
            f"  ✗ {label} | {type(exc).__name__}"
            f"{f' (status_code={status_code})' if status_code is not None else ''}: {exc}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
