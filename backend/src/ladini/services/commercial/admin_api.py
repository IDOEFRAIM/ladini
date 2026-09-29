"""Espace COMMERCIAL — liste des conversations, détail, relance manuelle.

Contrat (voir `ladini.services.commercial` pour l'invariant principal) :

* La LECTURE (`list_conversations`, `get_conversation_detail`) ne fait que
  des `SELECT` — `WorkspaceStore.get()` est utilisé en lecture seule pour
  exposer le goal/tunnel courant, jamais `WorkspaceStore.save()`.
* L'ENVOI (`send_follow_up`) appelle `WhatsAppChannel.send()` directement —
  le même canal qu'utilise le dispatcher outbox pour les notifications
  automatiques, mais en synchrone/immédiat. Aucun appel à
  `process_agent_task`, à un graphe LangGraph, ni à `WorkspaceStore.save()`.
  Le contenu du message n'est JAMAIS écrit dans `agri_workspaces` : il est
  uniquement journalisé dans `intelligence.audit_logs`
  (action=COMMERCIAL_OUTBOUND), qui sert aussi à reconstituer le fil des
  messages commerciaux dans le détail de conversation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.core.settings import settings
from ladini.domain.identity.models import User
from ladini.domain.intelligence.models import AuditLog, CommercialFollowup, Conversation
from ladini.services.database.common import normalize_phone
from ladini.workers.outbox.channels.whatsapp import WhatsAppChannel
from ladini.workspace.store import WorkspaceStore

FOLLOWUP_STATUSES = ("NONE", "TO_FOLLOW_UP", "FOLLOWED_UP", "RESOLVED", "NOT_INTERESTED")
_ACTIVE_STATUS_FILTERS = ("to_follow_up", "followed_up", "resolved", "long", "no_response", "all")

AUDIT_ACTION_OUTBOUND = "COMMERCIAL_OUTBOUND"
AUDIT_ACTION_STATUS_CHANGE = "COMMERCIAL_STATUS_CHANGE"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ListParams:
    status_filter: str = "all"  # to_follow_up | followed_up | resolved | long | no_response | all
    sort: str = "recent"  # recent | oldest_no_response | longest
    limit: int = 50
    offset: int = 0


def parse_list_params(query: Any) -> ListParams:
    status_filter = str(query.get("filter") or "all").strip().lower()
    if status_filter not in _ACTIVE_STATUS_FILTERS:
        raise ApiError(400, f"Filtre invalide : {status_filter}")
    sort = str(query.get("sort") or "recent").strip().lower()
    if sort not in ("recent", "oldest_no_response", "longest"):
        raise ApiError(400, f"Tri invalide : {sort}")
    raw_limit = query.get("limit")
    raw_offset = query.get("offset")
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else 50
        offset = int(raw_offset) if raw_offset not in (None, "") else 0
    except (TypeError, ValueError):
        raise ApiError(400, "limit/offset invalides.") from None
    if not (1 <= limit <= 200) or offset < 0:
        raise ApiError(400, "limit doit être entre 1 et 200, offset >= 0.")
    return ListParams(status_filter=status_filter, sort=sort, limit=limit, offset=offset)


def _uuid_or_404(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError):
        raise ApiError(404, "Utilisateur introuvable.") from None


async def list_conversations(session: AsyncSession, params: ListParams) -> dict[str, Any]:
    """Une ligne par utilisateur ayant au moins une conversation, agrégée."""

    agg = (
        select(
            Conversation.user_id.label("user_id"),
            func.count(Conversation.id).label("turns"),
            func.max(Conversation.created_at).label("last_activity"),
            func.bool_or(Conversation.needs_follow_up).label("needs_follow_up"),
        )
        .group_by(Conversation.user_id)
        .subquery()
    )
    # Dernier intent = celui de la ligne la plus récente par utilisateur.
    last_intent_sq = (
        select(Conversation.user_id, Conversation.user_intent, Conversation.created_at)
        .order_by(Conversation.user_id, Conversation.created_at.desc())
        .distinct(Conversation.user_id)
        .subquery()
    )

    stmt = (
        select(
            User.id,
            User.name,
            User.phone,
            User.role,
            agg.c.turns,
            agg.c.last_activity,
            agg.c.needs_follow_up,
            last_intent_sq.c.user_intent,
            CommercialFollowup.status,
            CommercialFollowup.assigned_commercial_id,
            CommercialFollowup.last_follow_up_at,
        )
        .join(agg, agg.c.user_id == User.id)
        .outerjoin(last_intent_sq, last_intent_sq.c.user_id == User.id)
        .outerjoin(CommercialFollowup, CommercialFollowup.user_id == User.id)
    )

    # `created_at`/`last_activity` sont des TIMESTAMP WITHOUT TIME ZONE (naïfs, UTC implicite —
    # voir orm_base.py) : comparer à un datetime aware lève TypeError côté Python et
    # DataError côté asyncpg. Même convention que services/database/*.py.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    long_threshold = settings.COMMERCIAL_LONG_CONVERSATION_TURNS
    no_response_cutoff = now - timedelta(hours=settings.COMMERCIAL_NO_RESPONSE_HOURS)

    status_filter = params.status_filter
    if status_filter == "to_follow_up":
        stmt = stmt.where(func.coalesce(CommercialFollowup.status, "NONE") == "TO_FOLLOW_UP")
    elif status_filter == "followed_up":
        stmt = stmt.where(CommercialFollowup.status == "FOLLOWED_UP")
    elif status_filter == "resolved":
        stmt = stmt.where(CommercialFollowup.status == "RESOLVED")
    elif status_filter == "long":
        stmt = stmt.where(agg.c.turns >= long_threshold)
    elif status_filter == "no_response":
        stmt = stmt.where(agg.c.last_activity <= no_response_cutoff)

    if params.sort == "recent":
        stmt = stmt.order_by(agg.c.last_activity.desc())
    elif params.sort == "oldest_no_response":
        stmt = stmt.order_by(agg.c.last_activity.asc())
    elif params.sort == "longest":
        stmt = stmt.order_by(agg.c.turns.desc())

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.limit(params.limit).offset(params.offset))).all()

    items = [
        {
            "user_id": str(r.id),
            "name": r.name,
            "phone_masked": _mask_phone(r.phone),
            "role": r.role,
            "turns": r.turns,
            "last_activity": r.last_activity.isoformat() if r.last_activity else None,
            "needs_follow_up": bool(r.needs_follow_up),
            "last_intent": r.user_intent,
            "commercial_status": r.status or "NONE",
            "assigned_commercial_id": str(r.assigned_commercial_id) if r.assigned_commercial_id else None,
            "last_follow_up_at": r.last_follow_up_at.isoformat() if r.last_follow_up_at else None,
            "is_long": r.turns >= long_threshold,
            "is_no_response": bool(r.last_activity and r.last_activity <= no_response_cutoff),
        }
        for r in rows
    ]
    return {"items": items, "total": int(total), "limit": params.limit, "offset": params.offset}


def _mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = phone.strip()
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{digits[:3]}{'*' * (len(digits) - 5)}{digits[-2:]}"


async def get_conversation_detail(session: AsyncSession, user_id_raw: str) -> dict[str, Any]:
    user_id = _uuid_or_404(user_id_raw)
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise ApiError(404, "Utilisateur introuvable.")

    turns = (
        (
            await session.execute(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    outbound = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "conversation")
                .where(AuditLog.entity_id == str(user_id))
                .where(AuditLog.action == AUDIT_ACTION_OUTBOUND)
                .order_by(AuditLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    followup = (
        await session.execute(select(CommercialFollowup).where(CommercialFollowup.user_id == user_id))
    ).scalar_one_or_none()

    timeline: list[dict[str, Any]] = []
    for t in turns:
        timeline.append({"role": "USER", "text": t.query, "at": t.created_at.isoformat()})
        if t.response:
            timeline.append({"role": "AGENT", "text": t.response, "at": t.created_at.isoformat()})
    for a in outbound:
        payload = a.new_value or {}
        timeline.append(
            {
                "role": "COMMERCIAL",
                "text": payload.get("message"),
                "at": a.created_at.isoformat(),
                "commercial_user_id": str(a.actor_id),
                "twilio_sid": payload.get("provider_ref"),
                "send_status": payload.get("status"),
            }
        )
    timeline.sort(key=lambda m: m["at"])

    # État agent — LECTURE SEULE (jamais WorkspaceStore.save() depuis ce module).
    workspace_snapshot: Optional[dict[str, Any]] = None
    if user.phone:
        workspace = await WorkspaceStore().get(normalize_phone(user.phone))
        if workspace is not None:
            workspace_snapshot = {
                "active_goal": workspace.active_goal,
                "active_form": workspace.active_form,
                "tunnel_locked": workspace.tunnel_locked,
            }

    return {
        "user_id": str(user.id),
        "name": user.name,
        "phone_masked": _mask_phone(user.phone),
        "role": user.role,
        "account_status": user.account_status,
        "commercial_status": followup.status if followup else "NONE",
        "assigned_commercial_id": str(followup.assigned_commercial_id) if followup and followup.assigned_commercial_id else None,
        "last_follow_up_at": followup.last_follow_up_at.isoformat() if followup and followup.last_follow_up_at else None,
        "workspace": workspace_snapshot,
        "timeline": timeline,
    }


async def send_follow_up(session: AsyncSession, *, actor_id: str, user_id_raw: str, message: str) -> dict[str, Any]:
    message = (message or "").strip()
    if not message:
        raise ApiError(400, "Message vide.")
    if len(message) > 2000:
        raise ApiError(400, "Message trop long (2000 caractères max).")

    user_id = _uuid_or_404(user_id_raw)
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise ApiError(404, "Utilisateur introuvable.")
    if not user.phone:
        raise ApiError(409, "Utilisateur sans numéro WhatsApp.")
    if user.account_status != "ACTIVE":
        raise ApiError(409, f"Compte non actif ({user.account_status}) — relance refusée.")

    phone = normalize_phone(user.phone)
    result = await WhatsAppChannel().send(body=message, recipient_phone=phone)

    audit = AuditLog(
        actor_id=uuid.UUID(str(actor_id)),
        action=AUDIT_ACTION_OUTBOUND,
        entity_type="conversation",
        entity_id=str(user_id),
        new_value={
            "message": message,
            "provider_ref": result.provider_ref,
            "status": "SENT" if result.ok else "FAILED",
            "error": result.error,
            "channel": "WHATSAPP",
        },
    )
    session.add(audit)

    followup = (
        await session.execute(select(CommercialFollowup).where(CommercialFollowup.user_id == user_id))
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # colonne naïve — voir list_conversations
    if followup is None:
        followup = CommercialFollowup(user_id=user_id, status="FOLLOWED_UP", last_follow_up_at=now)
        session.add(followup)
    else:
        followup.last_follow_up_at = now
        if followup.status in ("NONE", "TO_FOLLOW_UP"):
            followup.status = "FOLLOWED_UP"

    await session.commit()

    if not result.ok:
        raise ApiError(502, f"Échec de l'envoi WhatsApp : {result.error}")
    return {"sent": True, "provider_ref": result.provider_ref}


async def update_status(
    session: AsyncSession, *, actor_id: str, user_id_raw: str, status: str, assigned_commercial_id: Optional[str] = None
) -> dict[str, Any]:
    status = str(status or "").strip().upper()
    if status not in FOLLOWUP_STATUSES:
        raise ApiError(400, f"Statut invalide : {status}")

    user_id = _uuid_or_404(user_id_raw)
    user_exists = (await session.execute(select(User.id).where(User.id == user_id))).scalar_one_or_none()
    if user_exists is None:
        raise ApiError(404, "Utilisateur introuvable.")

    assigned_uuid: Optional[uuid.UUID] = None
    if assigned_commercial_id:
        assigned_uuid = _uuid_or_404(assigned_commercial_id)

    followup = (
        await session.execute(select(CommercialFollowup).where(CommercialFollowup.user_id == user_id))
    ).scalar_one_or_none()
    old_status = followup.status if followup else "NONE"

    if followup is None:
        followup = CommercialFollowup(user_id=user_id, status=status, assigned_commercial_id=assigned_uuid)
        session.add(followup)
    else:
        followup.status = status
        if assigned_commercial_id is not None:
            followup.assigned_commercial_id = assigned_uuid

    session.add(
        AuditLog(
            actor_id=uuid.UUID(str(actor_id)),
            action=AUDIT_ACTION_STATUS_CHANGE,
            entity_type="conversation",
            entity_id=str(user_id),
            old_value={"status": old_status},
            new_value={"status": status},
        )
    )
    await session.commit()
    return {"status": status}
