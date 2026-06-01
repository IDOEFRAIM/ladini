from __future__ import annotations

import uuid as _uuid_mod
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict, Optional
from datetime import datetime,time
import logging
import uuid
from sqlalchemy import select, update, and_,func,desc
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert


from agriconnect.domain.models import (
    AIRatingReasoning,
    AgentAction,
    Anomaly,
    AuditLog,
    Conversation,
    MarketMatch,
    TerritoryEvent,
    TrustScore,
    User,
    UserContextState,
    ZoneMetric,
    _uuid4,
)
from .common import clamp_limit, clean_text


def _uuid() -> str:
    return _uuid4()

logger = logging.getLogger("agriconnect.services.database")

SYSTEM_ACTOR_ID = "00000000-0000-0000-0000-000000000000"


def _coerce_uuid(value: Any, *, fallback: str | None = None) -> str:
    try:
        return str(_uuid_mod.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        if fallback is not None:
            return fallback
        raise ValueError(f"Invalid UUID value: {value!r}")


class IntelligenceMixin:
    async def get_user_context(self, session: AsyncSession, user_id: str) -> Dict[str, Any]:
        user_id = clean_text(user_id, "user_id", required=True)
        stmt = select(UserContextState).where(UserContextState.user_id == user_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return {
                "user_id": user_id,
                "last_intent": None,
                "pending_intent": None,
                "draft_data": {},
            }
        return row.to_dict()

    async def upsert_user_context(self, session: AsyncSession, payload: dict) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        user_id = payload.get("user_id")
        user_id = clean_text(user_id, "user_id", required=True)
        last_intent = payload.get("last_intent")
        pending_intent = payload.get("pending_intent")
        draft_data = payload.get("draft_data")
        stmt = select(UserContextState).where(UserContextState.user_id == user_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

        if not row:
            row = UserContextState(
                id=_uuid(),
                user_id=user_id,
                last_intent=last_intent,
                pending_intent=pending_intent,
                draft_data=draft_data or {},
            )
            session.add(row)
        else:
            if last_intent is not None:
                row.last_intent = last_intent
            if pending_intent is not None:
                row.pending_intent = pending_intent
            if draft_data is not None:
                row.draft_data = draft_data

        await session.flush()
        return row.to_dict()

    async def create_market_match(self, session: AsyncSession, payload: dict) -> Dict[str, Any]:
        product_id = payload.get("product_id")
        buyer_id = payload.get("buyer_id")
        score = payload.get("score", 0.0)
        status = payload.get("status", "SUGGESTED")
        meta = payload.get("meta")
        match = MarketMatch(
            id=_uuid(),
            product_id=product_id,
            buyer_id=buyer_id,
            score=max(0.0, min(1.0, float(score))),
            status=status,
            meta=meta or {},
        )
        session.add(match)
        await session.flush()
        return match.to_dict()

    async def list_market_matches(self, session: AsyncSession, filters: dict | None = None) -> List[Dict[str, Any]]:
        filters = filters or {}
        buyer_id = filters.get("buyer_id")
        status = filters.get("status")
        limit = clamp_limit(filters.get("limit", 20))
        stmt = select(MarketMatch)
        if buyer_id:
            stmt = stmt.where(MarketMatch.buyer_id == buyer_id)
        if status:
            stmt = stmt.where(MarketMatch.status == status)
        stmt = stmt.order_by(desc(MarketMatch.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [m.to_dict() for m in result.scalars()]

    async def record_zone_metric(self, session: AsyncSession, zone_id: str, metric_name: str, value: float) -> Dict[str, Any]:
        metric = ZoneMetric(id=_uuid(), zone_id=zone_id, metric_name=metric_name, value=value)
        session.add(metric)
        await session.flush()
        return metric.to_dict()

    async def log_conversation(self, session: AsyncSession, user_id: str, query: str, response: str, agent_type: str = None, crop: str = None, zone_id: str = None, mode: str = "text", audio_url: str = None, execution_path: list = None, confidence_score: float = None, tokens_used: int = 0, response_time_ms: int = None) -> Dict[str, Any]:
        conv_id = _uuid()
        # conversations.user_id is UUID-typed + FK to users.id.
        # If caller passes a non-UUID (e.g. "anonymous"), write a placeholder UUID
        # and insert a minimal users row so FK constraints are satisfied.
        user_id_for_db = user_id
        try:
            user_id_for_db = _coerce_uuid(user_id)
        except Exception:
            user_id_for_db = str(_uuid_mod.uuid4())
            exists_stmt = select(User.id).where(User.id == user_id_for_db)
            exists_res = await session.execute(exists_stmt)
            if exists_res.first() is None:
                placeholder = User(id=user_id_for_db, name=("Anonymous" if user_id == "anonymous" else "User"))
                session.add(placeholder)
                await session.flush()

        conv = Conversation(id=conv_id, user_id=user_id_for_db, query=query, response=response, agent_type=agent_type, crop=crop, zone_id=zone_id, mode=mode, audio_url=audio_url, execution_path=execution_path, confidence_score=confidence_score, total_tokens_used=tokens_used, response_time_ms=response_time_ms)
        session.add(conv)
        await session.flush()
        return {"conversation_id": conv_id, "user_id": user_id_for_db}

    async def create_agent_action(self, session: AsyncSession, agent_name: str, action_type: str, payload: dict, user_id: str = None, priority: str = "MEDIUM", ai_reasoning: str = None, order_id: str = None) -> Dict[str, Any]:
        action = AgentAction(id=_uuid(), agent_name=agent_name, action_type=action_type, payload=payload, user_id=user_id, priority=priority, ai_reasoning=ai_reasoning, order_id=order_id)
        session.add(action)
        await session.flush()
        return action.to_dict()

    async def get_pending_actions(self, session: AsyncSession, agent_name: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        limit = clamp_limit(limit)
        stmt = select(AgentAction).where(AgentAction.status == "PENDING")
        if agent_name:
            stmt = stmt.where(AgentAction.agent_name == agent_name)
        stmt = stmt.order_by(desc(AgentAction.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [a.to_dict() for a in result.scalars()]

    async def update_action_status(self, session: AsyncSession, action_id: str, new_status: str, admin_notes: str = None, validated_by_id: str = None) -> Optional[Dict[str, Any]]:
        stmt = select(AgentAction).where(AgentAction.id == action_id)
        result = await session.execute(stmt)
        action = result.scalar_one_or_none()
        if not action:
            return None
        action.status = new_status
        if admin_notes:
            action.admin_notes = admin_notes
        if validated_by_id:
            action.validated_by_id = validated_by_id
        await session.flush()
        return action.to_dict()

    async def log_audit(self, session: AsyncSession, actor_id: str, action: str, entity_type: str, entity_id: str, old_value: dict = None, new_value: dict = None, ip_address: str = None) -> Dict[str, Any]:
        log_id = _uuid()
        actor_id = _coerce_uuid(actor_id, fallback=SYSTEM_ACTOR_ID)
        log = AuditLog(id=log_id, actor_id=actor_id, action=action, entity_type=entity_type, entity_id=entity_id, old_value=old_value, new_value=new_value, ip_address=ip_address)
        session.add(log)
        await session.flush()
        return {"audit_log_id": log_id, "action": action, "entity_type": entity_type, "entity_id": entity_id}


    async def initialize_trust_score(
        self, 
        session: AsyncSession, 
        user_id: uuid.UUID
    ) -> Dict[str, Any]:
        """
        Initialise le trust score avec la clause ON CONFLICT pour éviter les erreurs de log.
        """
        try:
            # Préparation de l'insertion
            insert_stmt = insert(TrustScore).values(
                id=uuid.uuid4(),
                user_id=user_id,
                global_score=0.5,
                reliability_index=0.5,
                quality_index=0.5,
                compliance_index=0.5,
                resilience_bonus=0.0
            )

            # Clause ON CONFLICT : Si le user_id existe déjà, on ne fait rien (DO NOTHING)
            # Mais on demande de retourner l'ID existant
            upsert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=['user_id']
            ).returning(TrustScore.id)

            result = await session.execute(upsert_stmt)
            row = result.fetchone()

            if row:
                await session.flush()
                return {"status": "success", "message": "Initialisé", "id": str(row[0])}
            else:
                # Si row est None, c'est que le conflit a eu lieu et DO NOTHING a agi
                # On récupère l'existant
                stmt = select(TrustScore.id).where(TrustScore.user_id == user_id)
                res = await session.execute(stmt)
                existing_id = res.scalar()
                return {"status": "success", "message": "Existant", "id": str(existing_id)}

        except Exception as e:
            logger.error(f"Erreur fatale TrustScore: {e}")
            return {"status": "error", "message": str(e)}

    async def get_user_trust_score(
        self, 
        session: AsyncSession, 
        user_id: str
    ) -> Dict[str, Any]:
        """
        Récupère le score de confiance global et les indices détaillés.
        """
        try:
            # 1. Conversion sécurisée de l'ID
            u_id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id

            # 2. Requête sur la table trust_scores (schéma intelligence)
            stmt = (
                select(TrustScore)
                .where(TrustScore.user_id == u_id)
            )
            
            result = await session.execute(stmt)
            score_record = result.scalar_one_or_none()

            # 3. Gestion du cas "Inexistant"
            if not score_record:
                return {
                    "status": "not_found",
                    "global_score": 0.0,
                    "details": "Aucun score initialisé pour cet utilisateur."
                }

            # 4. Retour formaté des indicateurs
            return {
                "status": "success",
                "data": {
                    "global_score": score_record.global_score,
                    "indices": {
                        "reliability": score_record.reliability_index,
                        "quality": score_record.quality_index,
                        "compliance": score_record.compliance_index
                    },
                    "bonus": score_record.resilience_bonus,
                    "updated_at": score_record.updated_at.isoformat() if score_record.updated_at else None
                }
            }

        except Exception as e:
            logger.error(f"Erreur lors de la récupération du trust score pour {user_id}: {e}")
            return {"status": "error", "message": str(e)}




    async def update_trust_score(
        self, 
        session: AsyncSession, 
        user_id: str, 
        agent_name: str, 
        justification: str, 
        reliability_delta: float = 0, 
        quality_delta: float = 0, 
        compliance_delta: float = 0, 
        resilience_delta: float = 0
    ) -> Dict[str, Any]:
        """
        Met à jour les indices de confiance et recalcule le score global.
        Suit la structure : reliability, quality, compliance, resilience.
        """
        try:
            u_id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id

            # 1. Récupération du score actuel
            stmt = select(TrustScore).where(TrustScore.user_id == u_id)
            result = await session.execute(stmt)
            ts = result.scalar_one_or_none()

            if not ts:
                return {"status": "error", "message": "Trust score introuvable pour cet utilisateur."}

            # 2. Calcul des nouveaux indices avec bridage [0.0, 1.0]
            # La résilience peut parfois dépasser 1.0 selon ton business model, 
            # mais ici on reste sur une base normalisée.
            new_reliability = max(0.0, min(1.0, float(ts.reliability_index or 0.5) + reliability_delta))
            new_quality = max(0.0, min(1.0, float(ts.quality_index or 0.5) + quality_delta))
            new_compliance = max(0.0, min(1.0, float(ts.compliance_index or 0.5) + compliance_delta))
            new_resilience = max(0.0, min(1.0, float(ts.resilience_bonus or 0.0) + resilience_delta))

            # 3. Calcul du global_score (Moyenne pondérée ou simple)
            # On fait une moyenne simple des 3 piliers + bonus de résilience
            new_global = (new_reliability + new_quality + new_compliance) / 3
            new_global = max(0.0, min(1.0, new_global + (new_resilience * 0.1))) # Le bonus pèse 10%

            # 4. Exécution de l'UPDATE
            update_stmt = (
                update(TrustScore)
                .where(TrustScore.user_id == u_id)
                .values(
                    reliability_index=new_reliability,
                    quality_index=new_quality,
                    compliance_index=new_compliance,
                    resilience_bonus=new_resilience,
                    global_score=new_global,
                    updated_at=func.now()
                )
            )

            await session.execute(update_stmt)
            
            # 5. Log de l'audit (Optionnel : tu peux insérer dans une table TrustScoreHistory ici)
            logger.info(f"TrustUpdate | Agent: {agent_name} | User: {user_id} | Raison: {justification}")

            await session.commit()

            return {
                "status": "success",
                "new_global_score": round(new_global, 2),
                "details": {
                    "reliability": round(new_reliability, 2),
                    "quality": round(new_quality, 2),
                    "compliance": round(new_compliance, 2),
                    "resilience": round(new_resilience, 2)
                },
                "agent": agent_name
            }

        except Exception as e:
            await session.rollback()
            logger.error(f"Erreur update_trust_score : {e}")
            return {"status": "error", "message": str(e)}

    async def report_anomaly(self, session: AsyncSession, zone_id: str, level: str, title: str, message: str = None, source: str = None, details: dict = None) -> Dict[str, Any]:
        anomaly = Anomaly(id=_uuid(), zone_id=zone_id, level=level, title=title, message=message, source=source, details=details)
        session.add(anomaly)
        await session.flush()
        return anomaly.to_dict()

    async def get_active_anomalies(self, session: AsyncSession, zone_id: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        limit = clamp_limit(limit)
        stmt = select(Anomaly).where(Anomaly.is_resolved.is_(False))
        if zone_id:
            stmt = stmt.where(Anomaly.zone_id == zone_id)
        stmt = stmt.order_by(desc(Anomaly.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [a.to_dict() for a in result.scalars()]

    async def emit_territory_event(self, session: AsyncSession, zone_id: str, event_type: str, payload: dict = None, meta: dict = None) -> Dict[str, Any]:
        event = TerritoryEvent(id=_uuid(), zone_id=zone_id, event_type=event_type, payload=payload, meta=meta)
        session.add(event)
        await session.flush()
        return {"event_id": str(event.id), "zone_id": zone_id, "event_type": event_type}
