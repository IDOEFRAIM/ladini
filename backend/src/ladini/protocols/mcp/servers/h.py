from __future__ import annotations

import datetime as _dt
import functools
import inspect
import logging
import types as _types
import uuid
from typing import Any, Callable, Dict, Union, get_args, get_origin, get_type_hints

from ladini.services.database.d import AgriDatabaseService as DatabaseService
from ladini.services.database.errors import sanitize_error_message

logger = logging.getLogger("Ladini.MCP.Introspection")


def _safe(tool_name: str):
    """Fail-safe wrapper: never crash MCP; return structured errors."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                result = await func(*args, **kwargs)
                if isinstance(result, dict) and "status" in result:
                    return result
                return {"status": "success", "data": result}
            except Exception as exc:
                # Log full details server-side, return safe message to the client.
                #
                # `f"... : {exc}"` interpolait l'exception BRUTE — c'est
                # exactement la 2e surface de fuite décrite dans
                # `services/database/errors.py` (« exceptions RE-LEVÉES par le
                # dispatcher puis interpolées par le wrapper MCP »). En pratique
                # `@transactional` convertit déjà la plupart des erreurs
                # techniques en `SafeDatabaseError`, mais toute exception levée
                # HORS de son périmètre (résolution d'outil, sérialisation,
                # shield) arrivait ici telle quelle et repartait vers l'agent —
                # donc vers l'utilisateur WhatsApp. `sanitize_error_message`
                # laisse passer le métier intact et neutralise le technique.
                logger.exception("Tool failure: %s", tool_name)
                return {
                    "status": "error",
                    "tool": tool_name,
                    # `error_type` exposait le nom de classe interne
                    # (IntegrityError, ProgrammingError...) : indice gratuit sur
                    # la stack pour un attaquant, sans valeur pour l'agent.
                    "message": sanitize_error_message(exc, context=tool_name),
                }

        return wrapper

    return decorator


def _compute_exposed_methods() -> list[str]:
    """Outils MCP = introspection ∩ ALLOW-LIST EXPLICITE.

    (2026-09-05, Phase 8 — durcissement de l'exposition MCP) : cette
    fonction exposait auparavant TOUTE méthode async publique de
    `AgriDatabaseService`. Ajouter une méthode créait donc un outil, et
    seule l'absence de scope dans `TOOL_SCOPE_MAP` l'empêchait d'être
    appelée — un modèle opt-out qui a produit quatre failles distinctes
    de la même racine (voir `infrastructure/mcp/exposure.py`).

    L'exposition est désormais **opt-in** : seuls les noms déclarés dans
    `MCP_EXPOSED_TOOLS` deviennent des outils. Une méthode publique
    ajoutée demain n'est visible nulle part tant qu'un développeur ne l'a
    pas explicitement décidée.

    L'intersection est volontaire (plutôt que d'exposer la liste telle
    quelle) : elle garantit qu'un nom mal orthographié ou une méthode
    supprimée ne produit jamais un outil fantôme — la dérive est détectée
    par `tests/architecture/test_mcp_exposure_allowlist.py`.
    """
    from ladini.infrastructure.mcp.exposure import MCP_EXPOSED_TOOLS

    methods: set[str] = set()
    for name, member in inspect.getmembers(DatabaseService):
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(member):
            methods.add(name)

    exposed = methods & MCP_EXPOSED_TOOLS
    unknown = MCP_EXPOSED_TOOLS - methods
    if unknown:
        logger.warning(
            "MCP allow-list: %d nom(s) déclaré(s) sans méthode correspondante "
            "(dérive à corriger) : %s",
            len(unknown),
            ", ".join(sorted(unknown)),
        )
    return sorted(exposed)


EXPOSED_METHODS = _compute_exposed_methods()


def _type_to_json_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional[T] / Union[T1, T2, None]
    if origin in (Union, _types.UnionType):
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1:
            base = _type_to_json_schema(non_none[0])
            return {"anyOf": [base, {"type": "null"}]}
        return {
            "anyOf": [_type_to_json_schema(a) for a in non_none] + [{"type": "null"}]
        }

    # Containers
    if origin in (list, tuple, set, frozenset):
        item_schema = _type_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin in (dict, Dict):
        value_schema = _type_to_json_schema(args[1]) if len(args) == 2 else {}
        return {"type": "object", "additionalProperties": value_schema}

    # Common scalars
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in (uuid.UUID,):
        return {"type": "string", "format": "uuid"}
    if annotation in (_dt.datetime,):
        return {"type": "string", "format": "date-time"}
    if annotation in (_dt.date,):
        return {"type": "string", "format": "date"}
    if annotation in (_dt.time,):
        return {"type": "string", "format": "time"}

    return {}


def build_mcp_infrastructure(
    db_service: DatabaseService,
    *,
    exposed_methods: list[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[dict[str, Callable[..., Any]], dict[str, str], dict[str, dict[str, Any]]]:
    """Generate MCP handlers, descriptions and JSON schemas from a service."""
    handlers: dict[str, Callable[..., Any]] = {}
    descriptions: dict[str, str] = {}
    schemas: dict[str, dict[str, Any]] = {}
    aliases = aliases or {}
    tool_names = exposed_methods or EXPOSED_METHODS

    for tool_name in tool_names:
        method_name = aliases.get(tool_name, tool_name)
        method = getattr(db_service, method_name, None)
        if method is None:
            logger.warning(
                "Declared tool '%s' but method '%s' not found.", tool_name, method_name
            )
            continue

        # Use the class method for wraps() so signature/docstring are stable.
        class_method = getattr(DatabaseService, method_name, method)

        @_safe(tool_name)
        @functools.wraps(class_method)
        async def handler(*args, __method_name: str = method_name, **kwargs):
            bound = getattr(db_service, __method_name)
            return await bound(*args, **kwargs)

        handlers[tool_name] = handler

        doc = inspect.getdoc(class_method) or tool_name
        descriptions[tool_name] = doc.split("\n")[0]

        sig = inspect.signature(class_method)
        try:
            resolved_hints = get_type_hints(class_method)
        except Exception:
            resolved_hints = {}
        properties: dict[str, Any] = {}
        required: list[str] = []
        additional_properties: bool | dict[str, Any] | None = None

        for p_name, p in sig.parameters.items():
            if p_name in {"self", "cls", "session", "args"}:
                continue
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                additional_properties = True
                continue
            if p.kind == inspect.Parameter.VAR_POSITIONAL:
                continue

            annotation = resolved_hints.get(p_name, p.annotation)
            prop_def: dict[str, Any] = {"description": p_name}
            prop_def.update(_type_to_json_schema(annotation))
            properties[p_name] = prop_def
            if p.default is inspect.Parameter.empty:
                required.append(p_name)

        schema: dict[str, Any] = {
            "type": "object",
            "description": descriptions[tool_name],
            "properties": properties,
            "required": required,
        }
        if additional_properties is not None:
            schema["additionalProperties"] = additional_properties
        schemas[tool_name] = schema

    return handlers, descriptions, schemas


# Convenience: module-level registries (kept for backward compatibility)
service_instance = DatabaseService()
TOOL_HANDLERS, TOOL_DESCRIPTIONS, TOOL_SCHEMAS = build_mcp_infrastructure(
    service_instance
)


if __name__ == "__main__":
    print("✅ MCP Infrastructure générée avec succès !")
    print("Outils exposés :", list(TOOL_HANDLERS))
