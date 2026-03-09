"""
Intelligence Tool — Outils IA & Audit pour les agents AgriConnect.

Fournit aux agents les capacités d'intelligence :
  • TrustScore : score de confiance pondéré + raisonnement IA
  • Anomalies : détection & signalement
  • Audit : journalisation des actions (conformité)
  • Conversations : persistance des échanges
  • Agent Actions : HITL (Human-in-the-Loop)
  • Territory Events : événements de zone
  • Télémétrie : suivi des performances agents
"""

import logging
from typing import Any, Dict, List, Optional

from agriconnect.services.database.database_service import AgriDatabaseService

logger = logging.getLogger("Tool.Intelligence")


class IntelligenceTool:
    """
    Outil centralisé pour toutes les opérations du schéma `intelligence`.
    Utilisé par TOUS les agents (pas seulement le marketplace).
    """

    def __init__(self, db_service: AgriDatabaseService = None):
        self.db = db_service or AgriDatabaseService()

    # ══════════════════════════════════════════════════════════════
    # TRUST SCORE — Réputation & Fiabilité
    # ══════════════════════════════════════════════════════════════

    async def get_trust_score(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Récupère le score de confiance d'un utilisateur."""
        return await self.db.get_trust_score(user_id)

    async def update_trust_score(
        self,
        user_id: str,
        agent_name: str,
        justification: str,
        data_points: dict,
        reliability_delta: float = 0,
        quality_delta: float = 0,
        compliance_delta: float = 0,
        resilience_delta: float = 0,
    ) -> Dict[str, Any]:
        """
        Met à jour le score de confiance.

        Chaque agent peut ajuster les indices :
        - reliability_index : tient ses engagements (livraisons, délais)
        - quality_index : qualité produits/stock
        - compliance_index : respect des règles (prix officiel, bonnes pratiques)
        - resilience_bonus : diversification, adaptation climat

        Le global_score est recalculé automatiquement (pondéré 35/30/25/10).
        """
        return await self.db.update_trust_score(
            user_id=user_id, agent_name=agent_name,
            justification=justification, data_points=data_points,
            reliability_delta=reliability_delta,
            quality_delta=quality_delta,
            compliance_delta=compliance_delta,
            resilience_delta=resilience_delta,
        )

    async def reward_delivery(self, user_id: str, order_id: str) -> Dict[str, Any]:
        """Récompense une livraison réussie (+reliability)."""
        return await self.update_trust_score(
            user_id=user_id, agent_name="marketplace",
            justification=f"Livraison réussie (commande {order_id})",
            data_points={"order_id": order_id, "event": "delivery_success"},
            reliability_delta=0.05,
        )

    async def penalize_no_show(self, user_id: str, order_id: str) -> Dict[str, Any]:
        """Pénalise un producteur qui ne livre pas (-reliability)."""
        return await self.update_trust_score(
            user_id=user_id, agent_name="marketplace",
            justification=f"Non-livraison (commande {order_id})",
            data_points={"order_id": order_id, "event": "no_show"},
            reliability_delta=-0.1,
        )

    async def reward_good_quality(self, user_id: str, details: str) -> Dict[str, Any]:
        """Récompense la bonne qualité produit (+quality)."""
        return await self.update_trust_score(
            user_id=user_id, agent_name="marketplace",
            justification=f"Qualité validée: {details}",
            data_points={"event": "quality_validated", "details": details},
            quality_delta=0.05,
        )

    async def reward_resilience(self, user_id: str, practice: str) -> Dict[str, Any]:
        """Récompense une bonne pratique de résilience (+bonus)."""
        return await self.update_trust_score(
            user_id=user_id, agent_name="sentinelle",
            justification=f"Pratique résiliente: {practice}",
            data_points={"event": "resilience_practice", "practice": practice},
            resilience_delta=0.03,
        )

    # ══════════════════════════════════════════════════════════════
    # ANOMALIES — Détection & Signalement
    # ══════════════════════════════════════════════════════════════

    async def report_anomaly(
        self, zone_id: str, level: str, title: str,
        message: str = None, source: str = None,
        details: dict = None,
    ) -> Dict[str, Any]:
        """
        Signale une anomalie.

        Niveaux : INFO, WARNING, CRITICAL
        Sources : "agent_sentinelle", "agent_marketplace", "agent_doctor", etc.
        """
        return await self.db.report_anomaly(
            zone_id=zone_id, level=level, title=title,
            message=message, source=source, details=details,
        )

    async def report_price_anomaly(
        self, zone_id: str, product_name: str,
        proposed_price: float, reference_price: float,
        producer_phone: str = None,
    ) -> Dict[str, Any]:
        """Signale un prix suspect automatiquement."""
        ratio = proposed_price / reference_price if reference_price > 0 else 0
        return await self.report_anomaly(
            zone_id=zone_id,
            level="WARNING" if ratio < 5 else "CRITICAL",
            title=f"Prix suspect: {product_name} à {proposed_price} FCFA",
            message=f"Prix proposé {ratio:.1f}x le prix ref. ({reference_price} FCFA).",
            source="agent_marketplace",
            details={
                "product": product_name,
                "proposed_price": proposed_price,
                "reference_price": reference_price,
                "ratio": ratio,
                "producer_phone": producer_phone,
            },
        )

    async def report_weather_anomaly(
        self, zone_id: str, anomaly_type: str,
        details: dict,
    ) -> Dict[str, Any]:
        """Signale une anomalie météo (sécheresse, inondation, etc.)."""
        return await self.report_anomaly(
            zone_id=zone_id,
            level="CRITICAL" if anomaly_type in ("flood", "drought_severe") else "WARNING",
            title=f"Alerte climatique: {anomaly_type}",
            source="agent_sentinelle",
            details=details,
        )

    async def get_active_anomalies(
        self, zone_id: str = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        return await self.db.get_active_anomalies(zone_id, limit)

    # ══════════════════════════════════════════════════════════════
    # AUDIT — Journal des actions
    # ══════════════════════════════════════════════════════════════

    async def log_audit(
        self, actor_id: str, action: str,
        entity_type: str, entity_id: str,
        old_value: dict = None, new_value: dict = None,
    ) -> str:
        """Enregistre une action dans le journal d'audit. Retourne l'ID."""
        return await self.db.log_audit(
            actor_id=actor_id, action=action,
            entity_type=entity_type, entity_id=entity_id,
            old_value=old_value, new_value=new_value,
        )

    async def audit_stock_change(
        self, actor_id: str, stock_id: str,
        old_qty: float, new_qty: float, reason: str,
    ) -> str:
        """Audit spécialisé pour changements de stock."""
        return await self.log_audit(
            actor_id=actor_id, action="STOCK_UPDATE",
            entity_type="stock", entity_id=stock_id,
            old_value={"quantity": old_qty},
            new_value={"quantity": new_qty, "reason": reason},
        )

    async def audit_order(
        self, actor_id: str, order_id: str,
        action: str, details: dict = None,
    ) -> str:
        """Audit spécialisé pour les commandes."""
        return await self.log_audit(
            actor_id=actor_id, action=f"ORDER_{action}",
            entity_type="order", entity_id=order_id,
            new_value=details,
        )

    # ══════════════════════════════════════════════════════════════
    # CONVERSATIONS — Persistance des échanges
    # ══════════════════════════════════════════════════════════════

    async def log_conversation(
        self, user_id: str, query: str, response: str,
        agent_type: str = None, crop: str = None,
        zone_id: str = None, mode: str = "text",
        execution_path: list = None, confidence_score: float = None,
        tokens_used: int = 0, response_time_ms: int = None,
    ) -> str:
        """Enregistre un échange conversation agent↔utilisateur."""
        return await self.db.log_conversation(
            user_id=user_id, query=query, response=response,
            agent_type=agent_type, crop=crop, zone_id=zone_id,
            mode=mode, execution_path=execution_path,
            confidence_score=confidence_score,
            tokens_used=tokens_used,
            response_time_ms=response_time_ms,
        )

    # ══════════════════════════════════════════════════════════════
    # AGENT ACTIONS — HITL (Human-in-the-Loop)
    # ══════════════════════════════════════════════════════════════

    async def create_agent_action(
        self, agent_name: str, action_type: str, payload: dict,
        user_id: str = None, priority: str = "MEDIUM",
        ai_reasoning: str = None, order_id: str = None,
    ) -> Dict[str, Any]:
        """Crée une action agent en attente de validation humaine."""
        return await self.db.create_agent_action(
            agent_name=agent_name, action_type=action_type,
            payload=payload, user_id=user_id,
            priority=priority, ai_reasoning=ai_reasoning,
            order_id=order_id,
        )

    async def get_pending_actions(
        self, agent_name: str = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Liste les actions en attente de validation."""
        return await self.db.get_pending_actions(agent_name, limit)

    async def validate_action(
        self, action_id: str, admin_notes: str = None,
        validated_by_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Valide (approuve) une action agent."""
        return await self.db.update_action_status(
            action_id, "APPROVED",
            admin_notes=admin_notes,
            validated_by_id=validated_by_id,
        )

    async def reject_action(
        self, action_id: str, admin_notes: str = None,
        validated_by_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Rejette une action agent."""
        return await self.db.update_action_status(
            action_id, "REJECTED",
            admin_notes=admin_notes,
            validated_by_id=validated_by_id,
        )

    # ══════════════════════════════════════════════════════════════
    # TERRITORY EVENTS
    # ══════════════════════════════════════════════════════════════

    async def emit_territory_event(
        self, zone_id: str, event_type: str,
        payload: dict = None, meta: dict = None,
    ) -> str:
        """Émet un événement territorial (alerte zone, mouvement marché, etc.)."""
        return await self.db.emit_territory_event(
            zone_id=zone_id, event_type=event_type,
            payload=payload, meta=meta,
        )

    async def emit_market_movement(
        self, zone_id: str, product_name: str,
        direction: str, details: dict = None,
    ) -> str:
        """Émet un événement de mouvement marché."""
        return await self.emit_territory_event(
            zone_id=zone_id,
            event_type=f"MARKET_{direction.upper()}",
            payload={"product": product_name, **(details or {})},
        )

    # ══════════════════════════════════════════════════════════════
    # MÉTRIQUES DE ZONE
    # ══════════════════════════════════════════════════════════════

    async def record_zone_metric(
        self, zone_id: str, metric_name: str, value: float,
    ) -> Dict[str, Any]:
        """Enregistre une métrique pour une zone (température, prix moyen, etc.)."""
        return await self.db.record_zone_metric(zone_id, metric_name, value)
