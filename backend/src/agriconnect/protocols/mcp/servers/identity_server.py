"""Identity-Context MCP Micro-Server (async).

Domain: user profiles, farm parcels and interaction history.
This server is *data-only*.

Tools exposed:
  - get_profile(user_id)                       → ProfilePayload
  - patch_profile(user_id, patch, agent_id)    → ProfilePayload  (atomic SQL merge)
  - get_parcels(user_id)                       → ParcelsPayload
  - get_interaction_history(user_id, limit)    → HistoryPayload
  - get_profile_schema()                        → JSON Schema dict
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import AsyncMCPServer

logger = logging.getLogger("MCP.IdentityServer")


# ────────────────────── Pydantic response models ──────────────────────────

class ProfilePayload(BaseModel):
    user_id: str
    profile_data: Dict[str, Any] = Field(default_factory=dict)
    version: int = 0
    updated_at: Optional[str] = None
    exists: bool = False


class Parcel(BaseModel):
    parcel_id: str
    name: Optional[str] = None
    area_ha: Optional[float] = None
    crop: Optional[str] = None
    zone: Optional[str] = None
    soil_type: Optional[str] = None


class ParcelsPayload(BaseModel):
    user_id: str
    parcels: List[Parcel] = Field(default_factory=list)


class InteractionRecord(BaseModel):
    timestamp: str
    agent: str
    intent: str
    summary: str


class HistoryPayload(BaseModel):
    user_id: str
    interactions: List[InteractionRecord] = Field(default_factory=list)


# ── Allowed profile keys (schema exposed to agents) ───────────────────────
PROFILE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Fiche ferme JSONB. Seules ces clés sont autorisées.",
    "properties": {
        "surface": {"type": "number", "description": "Surface totale exploitée (hectares)"},
        "culture_actuelle": {"type": "string", "description": "Culture en cours (ex: Maïs, Coton)"},
        "cultures_historiques": {"type": "array", "items": {"type": "string"}},
        "region": {"type": "string"},
        "village": {"type": "string"},
        "dernier_orage": {"type": "string", "format": "date-time"},
        "dernier_evenement_maladie": {"type": "string"},
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nom": {"type": "string"},
                    "telephone": {"type": "string"},
                    "role": {"type": "string"},
                },
            },
        },
        "niveau": {"type": "string", "enum": ["debutant", "intermediaire", "expert"]},
        "langue": {"type": "string", "description": "fr | moore | dioula"},
    },
    "additionalProperties": False,
}


# ────────────────────── Server ────────────────────────────────────────────

class IdentityMCPServer(AsyncMCPServer):
    """Async MCP server for user identity and farm context."""

    name = "identity"

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory
        super().__init__()

    def _register_tools(self) -> None:
        self.register(
            name="get_profile",
            description="Récupère le profil ferme complet d'un utilisateur",
            input_schema={
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
            handler=self._get_profile,
        )
        self.register(
            name="patch_profile",
            description=(
                "Mise à jour atomique du profil via SQL JSON merge. "
                "Valeurs null ou {'__delete': true} suppriment la clé. "
                "L'agent_id est enregistré dans audit_logs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "patch": {"type": "object", "description": "Subset conforme à get_profile_schema"},
                    "agent_id": {"type": "string"},
                },
                "required": ["user_id", "patch"],
            },
            handler=self._patch_profile,
        )
        self.register(
            name="get_parcels",
            description="Liste les parcelles enregistrées pour un utilisateur",
            input_schema={
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
            handler=self._get_parcels,
        )
        self.register(
            name="get_interaction_history",
            description="Historique des interactions récentes de l'utilisateur",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["user_id"],
            },
            handler=self._get_interaction_history,
        )
        self.register(
            name="get_profile_schema",
            description="Retourne le schéma JSON autorisé pour patch_profile (validation côté agent)",
            input_schema={"type": "object", "properties": {}},
            handler=self._get_profile_schema,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _require_session(self):
        if self._session_factory is None:
            raise RuntimeError("IdentityMCPServer requires a session_factory")
        return self._session_factory()

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _get_profile(self, args: Dict[str, Any]) -> ProfilePayload:
        user_id = args["user_id"]
        if not self._session_factory:
            return ProfilePayload(user_id=user_id)
        try:
            from agriconnect.services.memory.user_profile import UserFarmProfileModel
        except Exception as exc:
            logger.error("UserFarmProfileModel import failed: %s", exc)
            return ProfilePayload(user_id=user_id)

        session = self._require_session()
        try:
            p = session.query(UserFarmProfileModel).filter_by(user_id=user_id).first()
            if not p:
                return ProfilePayload(user_id=user_id, exists=False)
            return ProfilePayload(
                user_id=user_id,
                profile_data=p.profile_data or {},
                version=getattr(p, "version", 0) or 0,
                updated_at=p.updated_at.isoformat() if getattr(p, "updated_at", None) else None,
                exists=True,
            )
        finally:
            session.close()

    async def _patch_profile(self, args: Dict[str, Any]) -> ProfilePayload:
        user_id = args["user_id"]
        patch: Dict[str, Any] = args.get("patch") or {}
        agent_id: Optional[str] = args.get("agent_id")

        if not self._session_factory:
            raise RuntimeError("No session_factory available")

        try:
            from agriconnect.services.memory.user_profile import UserFarmProfileModel
            from sqlalchemy import text as sa_text
        except Exception as exc:
            raise RuntimeError(f"Import error: {exc}") from exc

        table = UserFarmProfileModel.__table__.name

        # Separate deletions from updates
        set_obj = {k: v for k, v in patch.items() if not (v is None or (isinstance(v, dict) and v.get("__delete")))}
        del_keys = [k for k, v in patch.items() if v is None or (isinstance(v, dict) and v.get("__delete"))]

        params: Dict[str, Any] = {
            "user_id": user_id,
            "set_json": json.dumps(set_obj, ensure_ascii=False),
        }
        del_expr = "profile_data"
        for i, k in enumerate(del_keys):
            pname = f"del_{i}"
            del_expr = f"({del_expr} - :{pname})"
            params[pname] = k

        upd = sa_text(f"""
            UPDATE {table}
            SET profile_data = ({del_expr}) || :set_json::jsonb,
                version = COALESCE(version, 0) + 1,
                updated_at = now()
            WHERE user_id = :user_id
            RETURNING profile_data, version, updated_at
        """)

        session = self._require_session()
        try:
            res = session.execute(upd, params)
            row = res.fetchone()
            if not row:
                ins = sa_text(f"""
                    INSERT INTO {table} (user_id, profile_data, version, updated_at)
                    VALUES (:user_id, :set_json::jsonb, 1, now())
                    RETURNING profile_data, version, updated_at
                """)
                row = session.execute(ins, params).fetchone()

            # Best-effort audit
            try:
                session.execute(
                    sa_text("INSERT INTO audit_logs (user_id, agent_id, patch, created_at) VALUES (:u, :a, :p::jsonb, now())"),
                    {"u": user_id, "a": agent_id, "p": json.dumps(patch, ensure_ascii=False)},
                )
            except Exception:
                logger.debug("audit_logs insert skipped")

            session.commit()
            m = dict(row._mapping)
            return ProfilePayload(
                user_id=user_id,
                profile_data=m.get("profile_data", {}),
                version=m.get("version", 1),
                updated_at=str(m.get("updated_at", "")),
                exists=True,
            )
        except Exception:
            session.rollback()
            logger.exception("patch_profile failed for %s", user_id)
            raise
        finally:
            session.close()

    async def _get_parcels(self, args: Dict[str, Any]) -> ParcelsPayload:
        user_id = args["user_id"]
        if not self._session_factory:
            return ParcelsPayload(user_id=user_id)
        try:
            from sqlalchemy import text as sa_text

            session = self._require_session()
            try:
                rows = session.execute(
                    sa_text("""
                        SELECT id, name, area_ha, crop, zone, soil_type
                        FROM parcels WHERE user_id = :uid
                    """),
                    {"uid": user_id},
                )
                parcels = [
                    Parcel(
                        parcel_id=str(r._mapping.get("id", "")),
                        name=r._mapping.get("name"),
                        area_ha=r._mapping.get("area_ha"),
                        crop=r._mapping.get("crop"),
                        zone=r._mapping.get("zone"),
                        soil_type=r._mapping.get("soil_type"),
                    )
                    for r in rows
                ]
                return ParcelsPayload(user_id=user_id, parcels=parcels)
            finally:
                session.close()
        except Exception as exc:
            logger.warning("get_parcels failed: %s", exc)
            return ParcelsPayload(user_id=user_id)

    async def _get_interaction_history(self, args: Dict[str, Any]) -> HistoryPayload:
        user_id = args["user_id"]
        limit = int(args.get("limit", 10))
        if not self._session_factory:
            return HistoryPayload(user_id=user_id)
        try:
            from sqlalchemy import text as sa_text

            session = self._require_session()
            try:
                rows = session.execute(
                    sa_text("""
                        SELECT created_at, agent, intent, summary
                        FROM interaction_history
                        WHERE user_id = :uid
                        ORDER BY created_at DESC
                        LIMIT :lim
                    """),
                    {"uid": user_id, "lim": limit},
                )
                interactions = [
                    InteractionRecord(
                        timestamp=str(r._mapping.get("created_at", "")),
                        agent=r._mapping.get("agent", ""),
                        intent=r._mapping.get("intent", ""),
                        summary=r._mapping.get("summary", ""),
                    )
                    for r in rows
                ]
                return HistoryPayload(user_id=user_id, interactions=interactions)
            finally:
                session.close()
        except Exception as exc:
            logger.warning("get_interaction_history failed: %s", exc)
            return HistoryPayload(user_id=user_id)

    async def _get_profile_schema(self, args: Dict[str, Any] = None) -> Dict[str, Any]:
        return PROFILE_SCHEMA


if __name__ == "__main__":
    import asyncio, json

    server = IdentityMCPServer()
    print("Tools:", [t["name"] for t in server.list_tools()])
    result = asyncio.run(server.call_tool("get_profile_schema", {}))
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
