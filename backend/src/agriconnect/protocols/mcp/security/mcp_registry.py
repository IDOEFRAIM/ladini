from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Optional

from pydantic import BaseModel

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except Exception:  # pragma: no cover
    BaseSettings = BaseModel  # type: ignore[misc,assignment]
    SettingsConfigDict = dict  # type: ignore[misc,assignment]

from .constants import PermissionScope, RiskLevel, TOOL_RISK_MAP, TOOL_SCOPE_MAP
from .schemas import MCPServerKind, MCPToolMeta

logger = logging.getLogger("MCP.Registry")


class MCPRegistrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_", extra="ignore")

    rag_default_timeout_seconds: float = 20.0
    db_default_timeout_seconds: float = 12.0
    default_retries: int = 1
    # JSON mapping tool -> {server, scope, risk, timeout_seconds, retries, description}
    tool_overrides_json: str = "{}"


class MCPToolRegistry:
    def __init__(self, settings: Optional[MCPRegistrySettings] = None) -> None:
        self.settings = settings or MCPRegistrySettings()
        self._tools: Dict[str, MCPToolMeta] = {}

    def register(self, meta: MCPToolMeta) -> None:
        self._tools[meta.name] = meta

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def get_tool(self, tool_name: str) -> Optional[MCPToolMeta]:
        return self._tools.get(tool_name)

    def list_tools(self, server: Optional[MCPServerKind] = None) -> list[dict[str, Any]]:
        items = []
        for meta in self._tools.values():
            if server and meta.server != server:
                continue
            items.append(meta.model_dump())
        items.sort(key=lambda x: x["name"])
        return items

    def register_defaults(self) -> None:
        # Seed from existing static maps to preserve compatibility.
        rag_tools = {"search_agronomy_docs", "search_past_interactions", "rag://status"}
        for name, scope in TOOL_SCOPE_MAP.items():
            server = MCPServerKind.RAG if name in rag_tools else MCPServerKind.DB
            risk = TOOL_RISK_MAP.get(name, RiskLevel.MEDIUM)
            timeout = (
                self.settings.rag_default_timeout_seconds
                if server == MCPServerKind.RAG
                else self.settings.db_default_timeout_seconds
            )
            self.register(
                MCPToolMeta(
                    name=name,
                    server=server,
                    scope=scope,
                    risk=risk,
                    timeout_seconds=timeout,
                    retries=self.settings.default_retries,
                )
            )

    def sync_discovered_tools(self, server: MCPServerKind, discovered: Iterable[dict[str, Any]]) -> None:
        for item in discovered:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            desc = str(item.get("description") or "")
            if self.has_tool(name):
                meta = self._tools[name]
                if desc and not meta.description:
                    meta.description = desc
                continue
            # Dynamic defaults: fail-closed on unknown routing is preserved, but
            # discovered tools are auto-registered with conservative metadata.
            self.register(
                MCPToolMeta(
                    name=name,
                    server=server,
                    scope=PermissionScope.DB_READ_ONLY if server == MCPServerKind.RAG else PermissionScope.DB_DATA_WRITE,
                    risk=RiskLevel.LOW if server == MCPServerKind.RAG else RiskLevel.MEDIUM,
                    description=desc,
                    timeout_seconds=(
                        self.settings.rag_default_timeout_seconds
                        if server == MCPServerKind.RAG
                        else self.settings.db_default_timeout_seconds
                    ),
                    retries=self.settings.default_retries,
                )
            )

    def apply_overrides(self) -> None:
        try:
            raw = json.loads(self.settings.tool_overrides_json or "{}")
            if not isinstance(raw, dict):
                return
        except Exception:
            logger.warning("Invalid MCP_TOOL_OVERRIDES_JSON, ignoring")
            return

        for tool_name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            base = self._tools.get(tool_name)
            if base is None:
                try:
                    base = MCPToolMeta(
                        name=tool_name,
                        server=MCPServerKind(cfg.get("server", "db")),
                        scope=PermissionScope(cfg.get("scope", PermissionScope.DB_DATA_WRITE.value)),
                        risk=RiskLevel(cfg.get("risk", RiskLevel.MEDIUM.value)),
                        description=str(cfg.get("description", "")),
                        timeout_seconds=float(cfg.get("timeout_seconds", self.settings.db_default_timeout_seconds)),
                        retries=int(cfg.get("retries", self.settings.default_retries)),
                    )
                    self._tools[tool_name] = base
                except Exception:
                    logger.warning("Invalid override for tool '%s'", tool_name)
                continue

            for key in ("description", "timeout_seconds", "retries"):
                if key in cfg:
                    setattr(base, key, cfg[key])
            if "server" in cfg:
                base.server = MCPServerKind(cfg["server"])
            if "scope" in cfg:
                base.scope = PermissionScope(cfg["scope"])
            if "risk" in cfg:
                base.risk = RiskLevel(cfg["risk"])


def create_registry() -> MCPToolRegistry:
    reg = MCPToolRegistry()
    reg.register_defaults()
    reg.apply_overrides()
    return reg


_GLOBAL_REGISTRY: Optional[MCPToolRegistry] = None


def get_registry() -> MCPToolRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = create_registry()
    return _GLOBAL_REGISTRY
