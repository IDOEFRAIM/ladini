import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("Ladini.SecurityService")


class SecurityService:
    """Security and moderation service."""

    def __init__(self, llm_client: Any = None):
        self.llm = llm_client

    def moderate_content(
        self, query: str, context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Detect potential scams using keyword rules."""
        q_lower = query.lower()

        # Rule-based detection for financial scams
        scam_keywords = [
            "orange money",
            "moov money",
            "western union",
            "code de retrait",
            "dépôt avant",
            "frais de dossier",
            "transfert d'argent",
            "code pin",
            "mot de passe",
            "moneygram",
            "envoyer l'argent",
        ]

        for k in scam_keywords:
            if k in q_lower:
                reason = f"Mot-clé suspect détecté: '{k}'. Le transfert d'argent avant livraison est interdit."
                logger.warning(f"SCAM DETECTED: {reason}")
                return {"is_scam": True, "reason": reason, "status": "SCAM_DETECTED"}

        # If we had LLM logic here, it would be a secondary check.
        # Following user request to prioritize rules for basic scams.

        return {"is_scam": False, "status": "SAFE"}

    @staticmethod
    def build_alert_message(reason: str) -> str:
        return f"🚨 **ALERTE SÉCURITÉ**\n{reason}\nRefusez tout transfert d'argent avant livraison."
