"""Types purs du LLM Gateway — aucune I/O ici (Redis, HTTP, etc. vivent dans
les autres modules du package). Garde ce fichier trivialement testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LLMProfile(str, Enum):
    """Capacité demandée par un node métier — JAMAIS un nom de modèle.

    FAST : normalisation, classification simple, modération rapide — latence
    prioritaire sur la profondeur de raisonnement.
    REASONING : interprétation d'intent, extraction complexe, génération de
    réponse — tout goal métier qui a besoin de comprendre, pas juste classer.
    """

    FAST = "FAST"
    REASONING = "REASONING"


class ErrorClass(str, Enum):
    """Classification d'une exception d'appel LLM — voir error_classification.py.

    TRANSIENT : compte pour le disjoncteur (timeout, réseau, 429/5xx).
    CONFIG : le candidat lui-même est mal configuré (401/403, modèle
        inexistant/non supporté par la passerelle) — désactivé, PAS retenté,
        ne pollue pas le compteur d'échecs "le modèle est temporairement
        dégradé" du disjoncteur.
    APPLICATION : bug de notre côté (JSON invalide, exception Python) — ne
        doit JAMAIS faire croire que le provider est en panne.
    """

    TRANSIENT = "TRANSIENT"
    CONFIG = "CONFIG"
    APPLICATION = "APPLICATION"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class ModelCandidate:
    """Un couple (provider, modèle) éligible pour un profil, avec sa priorité
    dans la chaîne de repli (0 = primaire) et ses capacités réellement
    vérifiées (jamais supposées — voir le tableau du plan/rapport final)."""

    provider: str  # "groq" | "bedrock_gateway" | "bedrock_native"
    model: str
    profile: LLMProfile
    priority: int
    timeout_seconds: float
    enabled: bool = True
    capabilities: dict = field(default_factory=lambda: {"structured_output": True})

    @property
    def key(self) -> str:
        """Clé stable utilisée pour les clés Redis (health, probe lock, incident)."""
        return f"{self.provider}:{self.model}"

    def supports(self, *, structured_output_required: bool) -> bool:
        if structured_output_required and not self.capabilities.get(
            "structured_output", True
        ):
            return False
        return True


@dataclass
class HealthRecord:
    """Miroir de la structure §12 du brief — sérialisé en JSON dans Redis.

    Les latences (`recent_latencies_ms`) alimentent p50/p95 calculés à la
    lecture (`health_registry.py::percentiles`) — pas de librairie stats,
    juste un tri sur une liste bornée (dernières 100 mesures)."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_requests: int = 0
    total_failures: int = 0
    timeout_count: int = 0
    config_error: bool = False
    config_error_message: Optional[str] = None
    last_failure_at: Optional[float] = None  # epoch seconds
    last_success_at: Optional[float] = None
    cooldown_until: Optional[float] = None  # epoch seconds
    recent_latencies_ms: list = field(default_factory=list)

    def cooldown_elapsed(self, now: float) -> bool:
        """True si le cooldown est écoulé — OU s'il n'a jamais été fixé.

        Incident réel (2026-09-09) : un enregistrement Redis avec
        `state=OPEN`/`config_error=True` mais `cooldown_until=None` (créé
        avant l'introduction de ce champ, ou par un futur bug qui oublierait
        de le poser) restait bloqué en SKIP éternellement — l'ancien test
        `bool(cooldown_until and now >= cooldown_until)` traite `None` comme
        "jamais écoulé", donc plus AUCUN probe n'était jamais retenté (candidat
        Bedrock resté hors service des jours après que la vraie cause — une
        clé API expirée — a été corrigée). Un `cooldown_until` absent est une
        absence d'information, jamais la preuve d'une impossibilité
        permanente : on le traite comme déjà écoulé (éligible à un probe
        immédiat), jamais comme un verrou infini."""
        return self.cooldown_until is None or now >= self.cooldown_until

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HealthRecord":
        d = dict(d)
        state = d.pop("state", CircuitState.CLOSED.value)
        rec = cls(**d)
        rec.state = CircuitState(state)
        return rec
