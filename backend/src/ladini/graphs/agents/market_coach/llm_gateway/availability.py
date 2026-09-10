"""Provider availability pre-check — incident 2026-09-05, §6/§7 du brief.

FALLBACK REGISTRY ≠ PROVIDER RUNTIME CONFIGURATION : un candidat déclaré dans
la chaîne de repli (`registry.py`, ex: `groq:llama-3.3-70b-versatile`) peut
être structurellement impossible à utiliser dans l'environnement courant
(identifiant manquant). Le DÉCOUVRIR en le tentant gaspille une tentative et,
pire, peut se classer `ErrorClass.APPLICATION` (§15, jamais `CONFIG`) si le
message d'exception ne matche aucun fragment connu de
`error_classification.py` — ce qui l'empêche d'ouvrir le disjoncteur et le
fait retenter à CHAQUE appel, indéfiniment (c'est exactement ce qui s'est
produit : `get_groq_sdk()` refusait de construire un client tant que
`settings.LLM_PROVIDER != "groq"`, une condition sans rapport avec la
disponibilité réelle de l'identifiant Groq — voir le commentaire d'incident
dans `core/get_llm.py::get_groq_sdk`).

Modèle retenu (§6, Modèle A — déjà implicite dans `registry.py`) : chaque
candidat de la chaîne de repli utilise SES PROPRES identifiants,
indépendamment de `settings.LLM_PROVIDER` (qui ne gouverne QUE le client
LEGACY unique construit par `core/get_llm.py::get_llm()` — jamais ce
Gateway). C'est déjà le contrat de `.env` : chaque entrée
(`LLM_FAST_PRIMARY=bedrock_gateway:...`) déclare explicitement son provider ;
rien ne doit le reconditionner ensuite par `LLM_PROVIDER`.

Aucun appel réseau ici — uniquement une lecture de configuration. Ne
remplace PAS le Circuit Breaker (santé mesurée en marche) : ce module répond
à une question différente et antérieure — "cette tentative a-t-elle ne
serait-ce qu'un sens ?".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ladini.graphs.agents.market_coach.llm_gateway.types import ModelCandidate


@dataclass(frozen=True)
class AvailabilityCheck:
    """`usable=False` => `reason` explique pourquoi (§7) : seule catégorie
    produite ici aujourd'hui — `CREDENTIAL_MISSING` (identifiant absent pour
    ce provider). Un provider non reconnu par ce module n'en fait PAS
    partie : voir la note fail-open ci-dessous."""

    usable: bool
    reason: Optional[str] = None


def is_candidate_usable(candidate: ModelCandidate, settings: Any) -> AvailabilityCheck:
    """Décide, SANS appel réseau, si `candidate` peut être tenté dans CET
    environnement. Un candidat structurellement mal configuré n'a jamais été
    "en panne" (notion de santé/disjoncteur) — il est IMPOSSIBLE à essayer,
    catégorie distincte et antérieure."""
    if bool(getattr(settings, "MOCK_EXTERNAL_APIS", False)):
        # Sandbox (2026-08-27) : chaque provider retombe déjà sur un client
        # factice sans jamais toucher au réseau ni exiger de vraie clé — voir
        # `core/get_llm.py::_MockGroqClient`. Aucune raison de bloquer ici ce
        # que `core/get_llm.py` autorise explicitement plus bas.
        return AvailabilityCheck(usable=True)

    if candidate.provider == "groq":
        if not str(getattr(settings, "llm_api_key", "") or ""):
            return AvailabilityCheck(usable=False, reason="CREDENTIAL_MISSING")
        return AvailabilityCheck(usable=True)

    if candidate.provider == "bedrock_gateway":
        # `get_openai_compatible_sdk()` exige `OPENAI_API_KEY` (fail-fast
        # sinon) — voir `core/get_llm.py`. `OPENAI_BASE_URL` n'est volontai-
        # rement PAS vérifié ici : son absence fait pointer le client vers
        # l'API OpenAI publique plutôt que la passerelle Bedrock — une
        # mauvaise CIBLE, pas une IMPOSSIBILITÉ (le candidat reste tentable,
        # l'échec réel — 401/404 — sera classé CONFIG normalement).
        if not str(getattr(settings, "OPENAI_API_KEY", "") or ""):
            return AvailabilityCheck(usable=False, reason="CREDENTIAL_MISSING")
        return AvailabilityCheck(usable=True)

    if candidate.provider == "bedrock_native":
        # boto3 a sa propre chaîne de résolution de credentials IMPLICITE
        # (rôle IAM, ~/.aws/credentials, variables d'env standard AWS) — ni
        # `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` explicites ni aucune
        # autre variable ne permettent de conclure "impossible" sans un appel
        # réseau réel. Limitation assumée (§26, documentée plutôt que
        # devinée) : ce candidat est toujours considéré usable ici ; sa vraie
        # disponibilité n'est prouvée qu'à l'appel (et gérée ensuite par le
        # Circuit Breaker comme tout autre échec).
        return AvailabilityCheck(usable=True)

    # Provider que ce module ne sait pas spécifiquement vérifier — jamais
    # rencontré en production (`registry.py::_KNOWN_PROVIDERS` filtre déjà
    # les providers non reconnus à la construction du registry) mais
    # légitimement rencontré dans les tests (doubles génériques
    # "provider_a"/"provider_b"). Choix délibéré : FAIL-OPEN, pas
    # fail-closed — "je ne sais pas vérifier" n'est pas une preuve
    # d'impossibilité (même philosophie prudente que
    # `error_classification.py::classify_llm_error`, défaut APPLICATION
    # plutôt que CONFIG tant que ce n'est pas prouvé). La vraie
    # disponibilité, si ce candidat est réellement mal configuré, sera de
    # toute façon découverte à l'appel et gérée par le disjoncteur.
    return AvailabilityCheck(usable=True)


__all__ = ["AvailabilityCheck", "is_candidate_usable"]
