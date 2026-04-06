"""
agents/base.py — BaseAgent production-ready
============================================
Patterns utilisés :
  - Strategy  : dispatch() route vers _handle_<intent>() → élimine les if/elif à rallonge
  - Observer  : capabilities() permet au routeur de s'auto-configurer (zéro modif du cœur)
  - State     : require_human / clarify_intent / handoff apposent des flags sur l'état
                que l'orchestrateur DOIT intercepter avant toute suite d'exécution
"""
from __future__ import annotations
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Agent.Base")


class BaseAgent:
    """
    Classe de base pour tous les agents AgriConnect.

    Chaque agent hérite de cette classe et peut :
      - Demander une confirmation humaine  (require_human)
      - Demander une clarification          (clarify_intent)
      - Passer le relais à un autre agent  (handoff)
      - Dispatcher par intent              (dispatch / Strategy pattern)
      - Déclarer ses capacités             (capabilities / Observer pattern)
    """

    # Surcharger dans chaque sous-classe pour le registre dynamique
    _capabilities: List[str] = []

    # ────────────────────────────────────────────────────────────────
    # HITL — Human-in-the-Loop
    # Pourquoi : toute action irréversible (écriture DB, transaction
    # financière) doit obtenir un accord explicite avant exécution.
    # ────────────────────────────────────────────────────────────────

    def require_human(
        self,
        state: Dict[str, Any],
        reason: str,
        prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Marque l'état comme nécessitant une validation humaine.
        L'orchestrateur DOIT intercepter `requires_human=True` et stopper
        l'exécution jusqu'à réception d'une confirmation.
        """
        state = dict(state)
        state["requires_human"] = True
        state["human_reason"] = reason
        state["final_response"] = prompt or f"⚠️ Confirmation requise : {reason}"
        state["status"] = "PENDING_HUMAN"
        logger.info("[HITL][%s] Human confirmation needed: %s", self.__class__.__name__, reason)
        return state

    # ────────────────────────────────────────────────────────────────
    # CLARIFICATION
    # Pourquoi : mieux vaut demander que supposer, surtout pour
    # des requêtes ambiguës ou multi-intentionnelles.
    # ────────────────────────────────────────────────────────────────

    def clarify_intent(
        self,
        state: Dict[str, Any],
        question: str,
    ) -> Dict[str, Any]:
        """
        Demande une précision à l'utilisateur. Arrête le pipeline
        et retourne la question comme `final_response`.
        """
        state = dict(state)
        state["clarification_needed"] = question
        state["requires_human"] = True
        state["final_response"] = question
        state["status"] = "AWAITING_CLARIFICATION"
        logger.info("[CLARIFY][%s] → %s", self.__class__.__name__, question)
        return state

    # ────────────────────────────────────────────────────────────────
    # HAND-OFF inter-agents
    # Pourquoi : coopération fluide sans modifier le cœur.
    # L'orchestrateur lit `handoff_to` après chaque appel d'expert
    # et re-route vers l'agent désigné sans nouvelle analyse LLM.
    # ────────────────────────────────────────────────────────────────

    def handoff(
        self,
        state: Dict[str, Any],
        target_agent: str,
        context: Optional[Dict[str, Any]] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Signale un transfert de responsabilité vers `target_agent`.
        L'orchestrateur DOIT détecter `handoff_to` et appeler le bon expert.
        """
        state = dict(state)
        state["handoff_to"] = target_agent
        state["handoff_reason"] = reason
        if context:
            state["handoff_context"] = context
        logger.info(
            "[HANDOFF][%s] → %s  reason=%r",
            self.__class__.__name__, target_agent, reason,
        )
        return state

    # ────────────────────────────────────────────────────────────────
    # STRATEGY — Dispatch par intent
    # Pourquoi : remplace les if/elif à rallonge. Ajouter un intent
    # = ajouter un handler _handle_<intent>, sans toucher dispatch().
    # ────────────────────────────────────────────────────────────────

    def dispatch(self, intent: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recherche et appelle `_handle_<intent.lower()>`.
        Si aucun handler n'existe, appelle `_handle_unknown`.
        """
        handler: Callable = getattr(
            self,
            f"_handle_{intent.lower()}",
            self._handle_unknown,
        )
        return handler(state)

    def _handle_unknown(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return self.clarify_intent(
            state,
            "Je n'ai pas compris votre demande. Pouvez-vous préciser ce que vous souhaitez faire ?",
        )

    # ────────────────────────────────────────────────────────────────
    # OBSERVER — Registre dynamique
    # Pourquoi : le routeur peut découvrir les capacités de l'agent
    # sans modifier le code du « Cerveau Central ».
    # ────────────────────────────────────────────────────────────────

    @classmethod
    def capabilities(cls) -> List[str]:
        """Retourne la liste des intents/actions supportés par l'agent."""
        return list(cls._capabilities)
