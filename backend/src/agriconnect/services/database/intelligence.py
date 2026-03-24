from typing import Any, Dict, List, Optional
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from .common import _uuid, AgentAction, Conversation, AuditLog, TrustScore, AIRatingReasoning, Anomaly, TerritoryEvent, ZoneMetric, UserContextState, MarketMatch


class IntelligenceMixin:
    async def get_user_context(self, session: AsyncSession, user_id: str) -> Dict[str, Any]:
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
        user_id = payload.get("user_id")
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
        limit = filters.get("limit", 20)
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

    async def log_conversation(self, session: AsyncSession, user_id: str, query: str, response: str, agent_type: str = None, crop: str = None, zone_id: str = None, mode: str = "text", audio_url: str = None, execution_path: list = None, confidence_score: float = None, tokens_used: int = 0, response_time_ms: int = None) -> str:
        conv_id = _uuid()
        # Ensure we write a valid UUID into UUID-typed columns. If the
        # provided user_id is not a UUID, generate a placeholder UUID and
        # create a lightweight placeholder user record so FK constraints
        # are satisfied.
        user_id_for_db = user_id
        try:
            uuid.UUID(str(user_id))
        except Exception:
            user_id_for_db = str(uuid.uuid4())
            try:
                from agriconnect.services.models_v3 import User

                placeholder = User(id=user_id_for_db, name=("Anonymous" if user_id == "anonymous" else "User"))
                session.add(placeholder)
                await session.flush()
            except Exception:
                # If placeholder creation fails, continue and hope FK checks are relaxed.
                pass

        conv = Conversation(id=conv_id, user_id=user_id_for_db, query=query, response=response, agent_type=agent_type, crop=crop, zone_id=zone_id, mode=mode, audio_url=audio_url, execution_path=execution_path, confidence_score=confidence_score, total_tokens_used=tokens_used, response_time_ms=response_time_ms)
        session.add(conv)
        await session.flush()
        return conv_id

    async def create_agent_action(self, session: AsyncSession, agent_name: str, action_type: str, payload: dict, user_id: str = None, priority: str = "MEDIUM", ai_reasoning: str = None, order_id: str = None) -> Dict[str, Any]:
        action = AgentAction(id=_uuid(), agent_name=agent_name, action_type=action_type, payload=payload, user_id=user_id, priority=priority, ai_reasoning=ai_reasoning, order_id=order_id)
        session.add(action)
        await session.flush()
        return action.to_dict()

    async def get_pending_actions(self, session: AsyncSession, agent_name: str = None, limit: int = 20) -> List[Dict[str, Any]]:
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

    async def log_audit(self, session: AsyncSession, actor_id: str, action: str, entity_type: str, entity_id: str, old_value: dict = None, new_value: dict = None, ip_address: str = None) -> str:
        log_id = _uuid()
        log = AuditLog(id=log_id, actor_id=actor_id, action=action, entity_type=entity_type, entity_id=entity_id, old_value=old_value, new_value=new_value, ip_address=ip_address)
        session.add(log)
        await session.flush()
        return log_id

    async def get_trust_score(self, session: AsyncSession, user_id: str) -> Optional[Dict[str, Any]]:
        stmt = select(TrustScore).where(TrustScore.user_id == user_id)
        result = await session.execute(stmt)
        ts = result.scalar_one_or_none()
        return ts.to_dict() if ts else None

    async def update_trust_score(self, session: AsyncSession, user_id: str, agent_name: str, justification: str, data_points: dict, reliability_delta: float = 0, quality_delta: float = 0, compliance_delta: float = 0, resilience_delta: float = 0) -> Dict[str, Any]:
        stmt = select(TrustScore).where(TrustScore.user_id == user_id)
        result = await session.execute(stmt)
        ts = result.scalar_one_or_none()

        if not ts:
            ts = TrustScore(id=_uuid(), user_id=user_id, reliability_index=max(0, min(1, 0.5 + reliability_delta)), quality_index=max(0, min(1, 0.5 + quality_delta)), compliance_index=max(0, min(1, 0.5 + compliance_delta)), resilience_bonus=max(0, min(1, resilience_delta)))
            session.add(ts)
        else:
            ts.reliability_index = max(0, min(1, ts.reliability_index + reliability_delta))
            ts.quality_index = max(0, min(1, ts.quality_index + quality_delta))
            ts.compliance_index = max(0, min(1, ts.compliance_index + compliance_delta))
            ts.resilience_bonus = max(0, min(1, ts.resilience_bonus + resilience_delta))

        ts.global_score = round(0.35 * ts.reliability_index + 0.30 * ts.quality_index + 0.25 * ts.compliance_index + 0.10 * ts.resilience_bonus, 3)

        reasoning = AIRatingReasoning(id=_uuid(), trust_score_id=ts.id, agent_name=agent_name, justification=justification, data_points=data_points)
        session.add(reasoning)
        await session.flush()
        return ts.to_dict()

    async def report_anomaly(self, session: AsyncSession, zone_id: str, level: str, title: str, message: str = None, source: str = None, details: dict = None) -> Dict[str, Any]:
        anomaly = Anomaly(id=_uuid(), zone_id=zone_id, level=level, title=title, message=message, source=source, details=details)
        session.add(anomaly)
        await session.flush()
        return anomaly.to_dict()

    async def get_active_anomalies(self, session: AsyncSession, zone_id: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        stmt = select(Anomaly).where(Anomaly.is_resolved.is_(False))
        if zone_id:
            stmt = stmt.where(Anomaly.zone_id == zone_id)
        stmt = stmt.order_by(desc(Anomaly.created_at)).limit(limit)
        result = await session.execute(stmt)
        return [a.to_dict() for a in result.scalars()]

    async def emit_territory_event(self, session: AsyncSession, zone_id: str, event_type: str, payload: dict = None, meta: dict = None) -> str:
        event = TerritoryEvent(id=_uuid(), zone_id=zone_id, event_type=event_type, payload=payload, meta=meta)
        session.add(event)
        await session.flush()
        return event.id
