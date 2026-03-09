"""
Identity-Context MCP Server (FastMCP).
Domain: user profiles, farm parcels, and interaction history.

Tools:
  - get_profile(user_id)                       → ProfilePayload
  - patch_profile(user_id, patch, agent_id)    → ProfilePayload  (atomic SQL merge)
  - get_parcels(user_id)                       → ParcelsPayload
  - get_interaction_history(user_id, limit)    → HistoryPayload
  - get_profile_schema()                       → JSON Schema dict
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context

logger = logging.getLogger("MCP.IdentityServer")

# ────────────────────── FastMCP instance ──────────────────────────────────

mcp = FastMCP("AgriConnect Identity MCP Server")

# ────────────────────── Lazy singleton ────────────────────────────────────

_session_factory = None


def _require_session():
    if _session_factory is None:
        raise RuntimeError("IdentityServer requires a session_factory")
    return _session_factory()


# ────────────────────── Pydantic models ───────────────────────────────────

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


# ── Allowed profile keys (schema exposed to agents) ──────────────────────

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


# ────────────────────── Tools ─────────────────────────────────────────────

@mcp.tool()
async def get_profile(user_id: str, ctx: Context = None) -> str:
    """Récupère le profil ferme complet d'un utilisateur.

    Args:
        user_id: UUID de l'utilisateur
        ctx: MCP context

    Returns:
        JSON ProfilePayload
    """
    if ctx:
        await ctx.info(f"get_profile user_id={user_id}")

    if not _session_factory:
        return ProfilePayload(user_id=user_id).model_dump_json(indent=2)

    try:
        from agriconnect.services.memory.user_profile import UserFarmProfileModel
    except Exception as exc:
        logger.error("UserFarmProfileModel import failed: %s", exc)
        return ProfilePayload(user_id=user_id).model_dump_json(indent=2)

    session = _require_session()
    try:
        p = session.query(UserFarmProfileModel).filter_by(user_id=user_id).first()
        if not p:
            payload = ProfilePayload(user_id=user_id, exists=False)
        else:
            payload = ProfilePayload(
                user_id=user_id,
                profile_data=p.profile_data or {},
                version=getattr(p, "version", 0) or 0,
                updated_at=p.updated_at.isoformat() if getattr(p, "updated_at", None) else None,
                exists=True,
            )
        return payload.model_dump_json(indent=2)
    finally:
        session.close()


@mcp.tool()
async def patch_profile(
    user_id: str,
    patch: str,
    agent_id: str = "",
    ctx: Context = None,
) -> str:
    """Mise à jour atomique du profil via SQL JSON merge.

    Valeurs null ou ``{"__delete": true}`` suppriment la clé.
    L'agent_id est enregistré dans audit_logs.

    Args:
        user_id: UUID de l'utilisateur
        patch: JSON string du subset conforme à get_profile_schema
        agent_id: ID de l'agent qui effectue la mise à jour
        ctx: MCP context

    Returns:
        JSON ProfilePayload mis à jour
    """
    if ctx:
        await ctx.info(f"patch_profile user_id={user_id}")

    if not _session_factory:
        raise RuntimeError("No session_factory available")

    patch_dict: Dict[str, Any] = json.loads(patch) if isinstance(patch, str) else patch

    try:
        from agriconnect.services.memory.user_profile import UserFarmProfileModel
        from sqlalchemy import text as sa_text
    except Exception as exc:
        raise RuntimeError(f"Import error: {exc}") from exc

    table = UserFarmProfileModel.__table__.name
    set_obj = {k: v for k, v in patch_dict.items() if not (v is None or (isinstance(v, dict) and v.get("__delete")))}
    del_keys = [k for k, v in patch_dict.items() if v is None or (isinstance(v, dict) and v.get("__delete"))]

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

    session = _require_session()
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

        try:
            session.execute(
                sa_text("INSERT INTO audit_logs (user_id, agent_id, patch, created_at) VALUES (:u, :a, :p::jsonb, now())"),
                {"u": user_id, "a": agent_id, "p": json.dumps(patch_dict, ensure_ascii=False)},
            )
        except Exception:
            logger.debug("audit_logs insert skipped")

        session.commit()
        m = dict(row._mapping)
        payload = ProfilePayload(
            user_id=user_id,
            profile_data=m.get("profile_data", {}),
            version=m.get("version", 1),
            updated_at=str(m.get("updated_at", "")),
            exists=True,
        )
        return payload.model_dump_json(indent=2)
    except Exception:
        session.rollback()
        logger.exception("patch_profile failed for %s", user_id)
        raise
    finally:
        session.close()


@mcp.tool()
async def get_parcels(user_id: str, ctx: Context = None) -> str:
    """Liste les parcelles enregistrées pour un utilisateur.

    Args:
        user_id: UUID de l'utilisateur
        ctx: MCP context

    Returns:
        JSON ParcelsPayload
    """
    if ctx:
        await ctx.info(f"get_parcels user_id={user_id}")

    if not _session_factory:
        return ParcelsPayload(user_id=user_id).model_dump_json(indent=2)

    try:
        from sqlalchemy import text as sa_text
        session = _require_session()
        try:
            rows = session.execute(
                sa_text("SELECT id, name, area_ha, crop, zone, soil_type FROM parcels WHERE user_id = :uid"),
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
            payload = ParcelsPayload(user_id=user_id, parcels=parcels)
            return payload.model_dump_json(indent=2)
        finally:
            session.close()
    except Exception as exc:
        logger.warning("get_parcels failed: %s", exc)
        return ParcelsPayload(user_id=user_id).model_dump_json(indent=2)


@mcp.tool()
async def get_interaction_history(
    user_id: str,
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Historique des interactions récentes de l'utilisateur.

    Args:
        user_id: UUID de l'utilisateur
        limit: Nombre max d'interactions
        ctx: MCP context

    Returns:
        JSON HistoryPayload
    """
    if ctx:
        await ctx.info(f"get_interaction_history user_id={user_id} limit={limit}")

    if not _session_factory:
        return HistoryPayload(user_id=user_id).model_dump_json(indent=2)

    try:
        from sqlalchemy import text as sa_text
        session = _require_session()
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
            payload = HistoryPayload(user_id=user_id, interactions=interactions)
            return payload.model_dump_json(indent=2)
        finally:
            session.close()
    except Exception as exc:
        logger.warning("get_interaction_history failed: %s", exc)
        return HistoryPayload(user_id=user_id).model_dump_json(indent=2)


@mcp.tool()
async def get_profile_schema(ctx: Context = None) -> str:
    """Retourne le schéma JSON autorisé pour patch_profile (validation côté agent).

    Returns:
        JSON schema dict
    """
    if ctx:
        await ctx.info("Returning profile schema")
    return json.dumps(PROFILE_SCHEMA, ensure_ascii=False, indent=2)


# ────────────────────── Resources ─────────────────────────────────────────

@mcp.resource("identity://schema")
async def profile_schema_resource() -> str:
    """Profile JSON schema as an MCP resource."""
    return json.dumps(PROFILE_SCHEMA, ensure_ascii=False, indent=2)


# ────────────────────── Backward-compatible class wrapper ─────────────────

class IdentityMCPServer:
    """Compat wrapper for in-process callers."""

    name = "identity"

    def __init__(self, session_factory=None):
        global _session_factory
        if session_factory is not None:
            _session_factory = session_factory

    @staticmethod
    def list_tools():
        return [
            {"name": "get_profile", "description": "Récupère le profil ferme complet"},
            {"name": "patch_profile", "description": "Mise à jour atomique du profil"},
            {"name": "get_parcels", "description": "Liste les parcelles"},
            {"name": "get_interaction_history", "description": "Historique interactions"},
            {"name": "get_profile_schema", "description": "Schéma JSON autorisé"},
        ]

    @staticmethod
    async def _dispatch(name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "get_profile": get_profile,
            "patch_profile": patch_profile,
            "get_parcels": get_parcels,
            "get_interaction_history": get_interaction_history,
            "get_profile_schema": get_profile_schema,
        }
        fn = handlers.get(name)
        if not fn:
            raise ValueError(f"Unknown identity tool: {name}")
        return await fn(**args)

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        raw = loop.run_until_complete(self._dispatch(name, arguments))
        return {"ok": True, "data": json.loads(raw) if isinstance(raw, str) else raw}


# ────────────────────── Entry point ───────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting AgriConnect Identity MCP Server")
    mcp.run()
