import logging
from typing import Any, Dict, Optional
from agriconnect.services.db_handler import AgriDatabase

logger = logging.getLogger(__name__)

class AgriPersister:
    """
    Gestionnaire de Persistance Métier.
    Décharge l'orchestrateur de la logique d'enregistrement des actions proactives.
    """

    def __init__(self, db: AgriDatabase, memory: Optional[Any] = None):
        self.db = db
        self.memory = memory

    def save_all(self, state: Dict[str, Any]) -> None:
        """Point d'entrée unique pour sauvegarder tout ce qui concerne une interaction."""
        user_id = state.get("user_id", "anonymous")
        zone_id = state.get("zone_id")
        
        # 1. Sauvegarde de la conversation de base
        self._persist_conversation(state, user_id)

        # 2. Sauvegarde des actions spécifiques aux experts
        expert_responses = state.get("expert_responses", [])
        for resp in expert_responses:
            expert = resp.get("expert", "")
            try:
                self._dispatch_expert_action(expert, state, user_id, zone_id)
            except Exception as e:
                logger.warning(f"❌ Erreur persistance pour l'expert {expert}: {e}")

        # 3. Mise à jour de la mémoire épisodique
        self._record_memory(state, user_id, zone_id)

    def _persist_conversation(self, state: Dict[str, Any], user_id: str) -> None:
        """Enregistre le log de chat standard."""
        try:
            assistant_message = state.get("final_response") or "Pas de réponse générée."
            # Normalize assistant_message: could be str or dict
            if isinstance(assistant_message, dict):
                # prefer explicit 'response' key
                assistant_message = assistant_message.get("response") or assistant_message.get("text") or str(assistant_message)

            self.db.log_conversation(
                user_id=user_id,
                user_message=state.get("requete_utilisateur", "") or "",
                assistant_message=assistant_message,
                audio_url=state.get("audio_url"),
            )
        except Exception as e:
            logger.error(f"💾 Erreur log_conversation: {e}")

    def _dispatch_expert_action(self, expert: str, state: Dict[str, Any], user_id: str, zone_id: str) -> None:
        """Route l'enregistrement selon l'expert concerné."""
        if expert in ("market", "market_coach", "marketplace"):
            self._handle_market(expert, state, user_id, zone_id)
        elif expert == "formation":
            self._handle_formation(state, user_id)
        elif expert == "sentinelle":
            self._handle_sentinelle(state, user_id, zone_id)

    def _handle_market(self, expert: str, state: Dict[str, Any], user_id: str, zone_id: str) -> None:
        """Logique pour le marché (surplus et audit)."""
        market_data = state.get("market_data", {})
        surplus = market_data.get("surplus_detected")
        
        if surplus and isinstance(surplus, dict):
            self.db.save_surplus_offer(
                product_name=surplus.get("product", "Inconnu"),
                quantity_kg=surplus.get("quantity_kg", 0),
                price_kg=surplus.get("price_kg"),
                zone_id=zone_id,
                user_id=user_id,
                channel="agent",
            )

        resp_text = self._get_expert_text(state, expert)
        if resp_text:
            self.db.log_audit_action(
                agent_name="MarketCoach" if expert in ("market", "market_coach") else "MarketplaceAgent",
                action_type="MARKET_ADVICE",
                user_id=user_id,
                protocol="INTERNAL",
                resource="market_db",
                payload={"query": state.get("requete_utilisateur"), "advice": resp_text[:500]},
                confidence=0.9,
            )

    def _handle_formation(self, state: Dict[str, Any], user_id: str) -> None:
        """Logique pour la formation (audit RAG)."""
        resp_text = self._get_expert_text(state, "formation")
        if resp_text:
            self.db.log_audit_action(
                agent_name="FormationCoach",
                action_type="ADVICE_GIVEN",
                user_id=user_id,
                protocol="MCP_RAG",
                resource="vector_store",
                payload={"query": state.get("requete_utilisateur"), "advice": resp_text[:500]},
                confidence=0.95,
            )

    def _handle_sentinelle(self, state: Dict[str, Any], user_id: str, zone_id: str) -> None:
        """Logique pour la sentinelle (alertes critiques)."""
        hazards = state.get("meteo_data", {}).get("hazards", [])
        for h in hazards:
            if h.get("severity") in ("HAUT", "CRITIQUE"):
                self.db.create_alert(
                    alert_type=h.get("type", "WEATHER"),
                    severity=h.get("severity"),
                    message=h.get("description", "Alerte météo"),
                    zone_id=zone_id or "unknown",
                )

    def _record_memory(self, state: Dict[str, Any], user_id: str, zone_id: str) -> None:
        """Met à jour la mémoire épisodique si disponible."""
        if self.memory:
            try:
                # On identifie le leader pour la mémoire
                lead_expert = next((r.get("expert") for r in state.get("expert_responses", []) if r.get("is_lead")), "general")
                
                self.memory.record_interaction(
                    user_id=user_id,
                    query=state.get("requete_utilisateur", ""),
                    response=state.get("final_response", ""),
                    agent_type=lead_expert,
                    crop=state.get("crop"),
                    zone=zone_id,
                    intent=state.get("needs", {}).get("intent"),
                )
            except Exception as e:
                logger.warning(f"🧠 Erreur record_memory: {e}")

    def _get_expert_text(self, state: Dict[str, Any], expert_name: str) -> str:
        """Utilitaire pour extraire la réponse d'un expert spécifique dans le state."""
        for r in state.get("expert_responses", []):
            if r.get("expert") != expert_name:
                continue
            val = r.get("response")
            # Normalize types
            if isinstance(val, dict):
                return val.get("response") or val.get("text") or str(val)
            return str(val or "")
        return ""