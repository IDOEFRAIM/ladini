from __future__ import annotations
import time
import logging
from datetime import datetime
from typing import Optional
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)

logger = logging.getLogger(__name__)

class CircuitBreaker:
    """
    Sentinelle de survie : empêche de pilonner une source qui échoue.
    Gère les états : CLOSED (OK), OPEN (KO), HALF-OPEN (Test).
    """
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure: Optional[datetime] = None

    @property
    def is_open(self) -> bool:
        """Si le circuit est ouvert, on bloque les requêtes."""
        if self.failure_count < self.failure_threshold:
            return False
        
        # Vérification du délai de récupération
        if self.last_failure:
            elapsed = (datetime.now() - self.last_failure).total_seconds()
            if elapsed > self.recovery_timeout:
                # On passe en "Half-Open" (on laisse passer une chance)
                return False
        return True

    def record_success(self) -> None:
        """Reset total en cas de succès."""
        self.failure_count = 0
        self.last_failure = None

    def record_failure(self) -> None:
        """Incrémente l'échec et marque le timestamp."""
        self.failure_count += 1
        self.last_failure = datetime.now()
        logger.warning(f"CircuitBreaker failure: {self.failure_count}/{self.failure_threshold}")

    def can_execute(self) -> bool:
        """Compatibility helper expected by orchestrator."""
        return not self.is_open

class RateLimiter:
    """
    Contrôle de flux strict pour simuler un comportement humain (Throttling).
    """
    def __init__(self, rate_limit: int = 2, burst_size: Optional[int] = None):
        self.min_interval = 1.0 / max(1, rate_limit)
        self.last_call = 0.0
        self.burst_size = burst_size

    def acquire(self) -> None:
        """Bloque l'exécution pour respecter le délai minimal."""
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            # Ajout d'un léger jitter (aléatoire) pour le stealth
            time.sleep(sleep_time)
        self.last_call = time.time()

# --- DÉCORATEUR DE RETRY INDUSTRIEL ---
# On utilise tenacity qui est plus robuste que les fonctions maison
def retry_strategy(max_retries: int = 3, backoff_factor: float = 1.0):
    return retry(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=backoff_factor, min=1, max=10),
        retry=retry_if_exception_type((requests.RequestException, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.INFO),
        reraise=True
    )


# Backward-compatible export for orchestrator decorator usage
retry_with_backoff = retry_strategy