"""Model Registry — §6/§33/§34 du brief.

Charge la chaîne de repli par profil DEPUIS `.env` (voir `core/settings.py`,
champs `LLM_FAST_*`/`LLM_REASONING_*`) — changer de modèle primaire est un
changement de configuration, jamais un changement de code métier
(`input_interpreter`/`routing.py`/`cart.py` ne savent pas quel modèle tourne).

Rien n'est deviné : les capacités déclarées ci-dessous sont celles vérifiées
en direct pendant l'audit (2026-09-02) — voir le rapport final pour le détail
des mesures. `bedrock_native` (boto3 Converse) est enregistré `enabled=False`
par défaut : le code existant (`core/get_llm.py::_BedrockAdapter`) ignore
silencieusement `response_format`, donc `structured_output=False` — et ce
n'est de toute façon pas le chemin actif tant que `OPENAI_BASE_URL` est
défini (`get_llm.py::get_llm` préfère la passerelle OpenAI-compatible).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ladini.graphs.agents.market_coach.llm_gateway.types import (
    LLMProfile,
    ModelCandidate,
)

logger = logging.getLogger("ladini.llm_gateway.registry")

# Timeout PAR TENTATIVE (pas le budget total) — cohérent avec les timeouts
# HTTP déjà en place (`get_llm.py::_HTTP_READ_TIMEOUT=10.0`) : chaque essai a
# une chance de finir avant que le budget global (settings.LLM_*_BUDGET_SECONDS)
# ne soit épuisé, avec de la marge pour au moins un repli.
_DEFAULT_TIMEOUT_BY_PROFILE = {
    LLMProfile.FAST: 8.0,
    LLMProfile.REASONING: 12.0,
    # Même budget que FAST — Groq llama-3.1-8b-instant (2026-09-12, Phase
    # B.1) est un modèle "instant", pas de raison de lui donner le budget
    # REASONING (12s).
    LLMProfile.INTERPRETER: 8.0,
}

# Capacités connues par provider — PAS par modèle individuel (trop de
# modèles catalogués sur la passerelle bedrock-mantle pour les auditer un
# par un ; `bedrock_gateway` est un proxy OpenAI-compatible qui a montré
# `response_format=json_object` fonctionnel sur tous les modèles testés
# cette session : qwen, deepseek, gpt-oss, glm — voir rapport final).
_CAPABILITIES_BY_PROVIDER: Dict[str, dict] = {
    "groq": {"structured_output": True},
    "bedrock_gateway": {"structured_output": True},
    # Confirmé à l'audit : `_BedrockAdapter` droppe silencieusement
    # `response_format` (pas d'équivalent natif Converse).
    "bedrock_native": {"structured_output": False},
}

_KNOWN_PROVIDERS = frozenset(_CAPABILITIES_BY_PROVIDER)


def _parse_candidate(
    raw: str, *, profile: LLMProfile, priority: int
) -> Optional[ModelCandidate]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if ":" not in raw:
        logger.warning(
            "[llm_gateway] entrée de registry ignorée (format attendu "
            "'provider:model'): %r",
            raw,
        )
        return None
    provider, _, model = raw.partition(":")
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in _KNOWN_PROVIDERS or not model:
        logger.warning(
            "[llm_gateway] provider inconnu ou modèle vide ignoré: %r "
            "(providers connus: %s)",
            raw,
            sorted(_KNOWN_PROVIDERS),
        )
        return None
    enabled = provider != "bedrock_native"  # inactif par défaut, voir docstring
    return ModelCandidate(
        provider=provider,
        model=model,
        profile=profile,
        priority=priority,
        timeout_seconds=_DEFAULT_TIMEOUT_BY_PROFILE[profile],
        enabled=enabled,
        capabilities=dict(_CAPABILITIES_BY_PROVIDER[provider]),
    )


def load_registry(settings=None) -> Dict[LLMProfile, List[ModelCandidate]]:
    """Construit le registry depuis les settings (paramétrable pour les
    tests — `settings=None` charge `ladini.core.settings.settings`)."""
    if settings is None:
        from ladini.core.settings import settings as _settings

        settings = _settings

    raw_by_profile = {
        LLMProfile.FAST: [
            getattr(settings, "LLM_FAST_PRIMARY", ""),
            getattr(settings, "LLM_FAST_FALLBACK_1", ""),
            getattr(settings, "LLM_FAST_FALLBACK_2", ""),
        ],
        LLMProfile.REASONING: [
            getattr(settings, "LLM_REASONING_PRIMARY", ""),
            getattr(settings, "LLM_REASONING_FALLBACK_1", ""),
            getattr(settings, "LLM_REASONING_FALLBACK_2", ""),
        ],
        # (2026-09-12, Phase B.1) : profil dédié à `interpreter/` — local à
        # ce chantier, NE touche PAS `LLM_FAST_*`/`LLM_REASONING_*` ni leurs
        # consommateurs existants (INPUT_NORMALIZATION/SECURITY_MODERATION/
        # STATE_CLEANER, génération de réponse).
        LLMProfile.INTERPRETER: [
            getattr(settings, "LLM_INTERPRETER_PRIMARY", ""),
            getattr(settings, "LLM_INTERPRETER_FALLBACK_1", ""),
            getattr(settings, "LLM_INTERPRETER_FALLBACK_2", ""),
        ],
    }

    registry: Dict[LLMProfile, List[ModelCandidate]] = {}
    for profile, raw_list in raw_by_profile.items():
        candidates: List[ModelCandidate] = []
        for priority, raw in enumerate(raw_list):
            candidate = _parse_candidate(raw, profile=profile, priority=priority)
            if candidate is not None:
                candidates.append(candidate)
        registry[profile] = candidates
        if not candidates:
            logger.error(
                "[llm_gateway] AUCUN candidat configuré pour le profil %s — "
                "toute requête sur ce profil échouera immédiatement.",
                profile.value,
            )
    return registry


# (2026-09-26, audit LLM_GATEWAY_EXHAUSTED, §15) : hôte de l'API publique
# OpenAI — jamais une valeur légitime pour `OPENAI_BASE_URL` quand un
# candidat `bedrock_gateway` est configuré. `get_openai_compatible_sdk()`
# (core/get_llm.py) pointe le SDK OpenAI directement sur l'API publique si
# `OPENAI_BASE_URL` est vide, ce qui n'est PAS une impossibilité détectée par
# `availability.py` (mauvaise CIBLE, pas une absence de credential) : un
# candidat `bedrock_gateway` avec un ID au format Bedrock (ex: "qwen.qwen3-
# 32b") envoyé à ce host échoue en 404 "model not found" qui RESSEMBLE en
# tout point à un vrai 404 Bedrock, sans jamais l'être.
_PUBLIC_OPENAI_HOSTS = frozenset({"api.openai.com"})


def validate_config(settings=None) -> List[str]:
    """Détecte, SANS appel réseau, les configurations manifestement
    invalides (§15) : ne fait QUE lire `settings`/le registry déjà chargé.
    Retourne une liste de messages (vide = rien à signaler) — l'appelant
    décide de logger, alerter, ou (pas encore fait ici, voir rapport final)
    lever une exception au démarrage : ce module ne connaît pas le contexte
    (tests, script ponctuel, process serveur) dans lequel il tourne."""
    if settings is None:
        from ladini.core.settings import settings as _settings

        settings = _settings

    issues: List[str] = []
    by_profile = load_registry(settings)

    openai_base_url = str(getattr(settings, "OPENAI_BASE_URL", "") or "").strip()
    from urllib.parse import urlparse

    base_host = urlparse(openai_base_url).hostname if openai_base_url else None

    for profile, candidates in by_profile.items():
        if not candidates:
            issues.append(
                f"profile={profile.value}: aucun candidat configuré "
                "(NO_CANDIDATES_CONFIGURED à l'exécution)."
            )
            continue

        seen_keys: set[str] = set()
        for candidate in candidates:
            if candidate.key in seen_keys:
                issues.append(
                    f"profile={profile.value}: candidat dupliqué {candidate.key!r} "
                    "dans la même chaîne de repli."
                )
            seen_keys.add(candidate.key)

            if candidate.provider == "bedrock_gateway" and candidate.enabled:
                if not openai_base_url:
                    issues.append(
                        f"profile={profile.value} candidate={candidate.key}: "
                        "provider=bedrock_gateway configuré mais OPENAI_BASE_URL "
                        "est vide — les requêtes cibleront silencieusement l'API "
                        "OpenAI publique au lieu de la passerelle Bedrock "
                        "(voir availability.py, mauvaise CIBLE jamais détectée "
                        "comme CREDENTIAL_MISSING)."
                    )
                elif base_host in _PUBLIC_OPENAI_HOSTS:
                    issues.append(
                        f"profile={profile.value} candidate={candidate.key}: "
                        f"OPENAI_BASE_URL pointe sur l'API OpenAI publique "
                        f"({base_host!r}) alors qu'un candidat bedrock_gateway "
                        "est configuré — cible probablement incorrecte."
                    )

    return issues


class ModelRegistry:
    """Wrapper léger — charge une fois, expose `candidates_for(profile)`.
    Pas de rechargement automatique (un changement de `.env` nécessite un
    redémarrage de process, comme le reste de `settings.py` aujourd'hui)."""

    def __init__(self, settings=None):
        self._by_profile = load_registry(settings)
        # (2026-09-26, §15) : logging seul, jamais un crash au constructeur —
        # `ModelRegistry()` est instancié dans des contextes trop variés
        # (tests, scripts, process serveur) pour qu'un `raise` ici soit sûr
        # sans une revue dédiée. Un problème listé ici reste immédiatement
        # visible dans les logs de démarrage (grep "LLM_CONFIG_INVALID").
        for issue in validate_config(settings):
            logger.error("LLM_CONFIG_INVALID | %s", issue)

    def candidates_for(self, profile: LLMProfile) -> List[ModelCandidate]:
        return [c for c in self._by_profile.get(profile, []) if c.enabled]

    def primary_model_name(self, profile: LLMProfile) -> Optional[str]:
        candidates = self.candidates_for(profile)
        return candidates[0].model if candidates else None

    def all_candidates(self) -> List[ModelCandidate]:
        out: List[ModelCandidate] = []
        for candidates in self._by_profile.values():
            out.extend(candidates)
        return out

    def find(self, provider: str, model: str) -> Optional[ModelCandidate]:
        for candidate in self.all_candidates():
            if candidate.provider == provider and candidate.model == model:
                return candidate
        return None
