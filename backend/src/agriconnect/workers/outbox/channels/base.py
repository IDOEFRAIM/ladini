"""Interface commune des canaux de notification (découplage total du métier)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_ref: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def success(cls, provider_ref: Optional[str] = None) -> "SendResult":
        return cls(ok=True, provider_ref=provider_ref)

    @classmethod
    def failure(cls, error: str) -> "SendResult":
        return cls(ok=False, error=error)


@runtime_checkable
class NotificationChannel(Protocol):
    """Contrat qu'implémente chaque canal (WhatsApp, Email, Push, In-App)."""

    name: str

    def is_configured(self) -> bool:
        """Vrai si le canal a les credentials/config nécessaires pour envoyer."""
        ...

    async def send(
        self,
        *,
        body: str,
        recipient_phone: Optional[str] = None,
        recipient_user_id: Optional[Any] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Envoie le message. Ne lève pas : encapsule l'échec dans ``SendResult``."""
        ...
