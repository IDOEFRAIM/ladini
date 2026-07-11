from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import aiohttp

# Tentative d'import de FastMCP
try:
    from fastmcp import Client
    from fastmcp.client.elicitation import ElicitResult
except ImportError:
    Client = None
    ElicitResult = None

from agriconnect.infrastructure.mcp.security import ShieldHub

logger = logging.getLogger(__name__)

@dataclass
class MCPTransportConfig:
    """Configuration générique du transport MCP (stdio, HTTP, gRPC)."""

    kind: str = "stdio"
    stdio_script: Optional[str] = None
    stdio_cwd: Optional[str] = None
    stdio_env: Mapping[str, str] = field(default_factory=dict)
    stdio_python: Optional[str] = None
    http_base_url: Optional[str] = None
    http_headers: Mapping[str, str] = field(default_factory=dict)
    grpc_target: Optional[str] = None
    grpc_tls: bool = False
    grpc_metadata: Mapping[str, str] = field(default_factory=dict)
    grpc_list_tools_method: str = "/agriconnect.mcp.MCP/ListTools"
    grpc_call_tool_method: str = "/agriconnect.mcp.MCP/CallTool"
    stdio_log_path: Optional[str] = None

    @classmethod
    def from_settings(cls, settings: Any) -> MCPTransportConfig:
        transport = str(getattr(settings, "MCP_DB_TRANSPORT", "stdio")).lower()
        if transport == "stdio":
            script = getattr(settings, "MCP_DB_STDIO_ENTRYPOINT", "")
            if not script:
                raise ValueError("MCP_DB_STDIO_ENTRYPOINT must be configured for stdio transport")
            env = dict(getattr(settings, "MCP_DB_STDIO_ENV", {}) or {})
            if "PYTHONPATH" not in env and getattr(settings, "BASE_DIR", None):
                env["PYTHONPATH"] = str(settings.BASE_DIR)
            log_path = getattr(settings, "MCP_DB_STDIO_LOG_PATH", None)
            if not log_path:
                base_logs = getattr(settings, "LOG_DIR", None)
                if base_logs:
                    log_path = str(Path(base_logs) / "mcp-db-stdio.log")
                else:
                    default_root = getattr(settings, "BASE_DIR", None) or tempfile.gettempdir()
                    log_path = str(Path(default_root) / "logs" / "mcp-db-stdio.log")

            return cls(
                kind="stdio",
                stdio_script=script,
                stdio_cwd=getattr(settings, "MCP_DB_STDIO_CWD", None) or str(Path(script).resolve().parent),
                stdio_env=env,
                stdio_python=getattr(settings, "MCP_DB_STDIO_PYTHON", None) or sys.executable,
                stdio_log_path=log_path,
            )
        if transport == "http":
            base_url = getattr(settings, "MCP_DB_HTTP_URL", "")
            if not base_url:
                raise ValueError("MCP_DB_HTTP_URL must be configured for http transport")
            return cls(
                kind="http",
                http_base_url=base_url,
                http_headers=getattr(settings, "MCP_DB_HTTP_HEADERS", {}) or {},
            )
        if transport == "grpc":
            target = getattr(settings, "MCP_DB_GRPC_TARGET", "")
            if not target:
                raise ValueError("MCP_DB_GRPC_TARGET must be configured for grpc transport")
            return cls(
                kind="grpc",
                grpc_target=target,
                grpc_tls=bool(getattr(settings, "MCP_DB_GRPC_TLS", False)),
                grpc_metadata=getattr(settings, "MCP_DB_GRPC_METADATA", {}) or {},
                grpc_list_tools_method=getattr(settings, "MCP_DB_GRPC_LIST_TOOLS_METHOD", "/agriconnect.mcp.MCP/ListTools"),
                grpc_call_tool_method=getattr(settings, "MCP_DB_GRPC_CALL_TOOL_METHOD", "/agriconnect.mcp.MCP/CallTool"),
            )
        raise ValueError(f"Unsupported MCP transport: {transport}")


class MCPTransportAdapter(ABC):
    """Interface minimale pour piloter un transport MCP."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def list_tools(self) -> List[Any]: ...

    @abstractmethod
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any: ...

class FastMCPProcessAdapter(MCPTransportAdapter):
    def __init__(
        self,
        config: MCPTransportConfig,
        elicitation_handler: Optional[Callable[..., Awaitable[Any]]] = None,
        progress_handler: Optional[Callable[..., Awaitable[Any]]] = None,
        message_handler: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self.config = config
        self._client: Optional[Client] = None
        self._exit_stack = AsyncExitStack()
        self._elicitation_handler = elicitation_handler
        self._progress_handler = progress_handler
        self._message_handler = message_handler
        self._log_handle: Optional[Any] = None

    async def connect(self) -> None:
        if not Client:
            raise ImportError("fastmcp est requis pour le transport stdio")
        from fastmcp.client.transports.stdio import PythonStdioTransport

        script_path = Path(str(self.config.stdio_script)).resolve()
        if not script_path.exists():
            raise FileNotFoundError(f"MCP server script introuvable: {script_path}")

        env = dict(os.environ)
        env.update(self.config.stdio_env or {})

        log_file_path = self.config.stdio_log_path or str(Path(tempfile.gettempdir()) / "mcp-db-stdio.log")
        log_file = Path(log_file_path).expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(log_file, "ab", buffering=0)

        transport = PythonStdioTransport(
            script_path=str(script_path),
            env=env,
            cwd=self.config.stdio_cwd or str(script_path.parent),
            python_cmd=self.config.stdio_python or sys.executable,
            log_file=self._log_handle,
        )

        self._client = Client(
            transport,
            elicitation_handler=self._elicitation_handler,
            progress_handler=self._progress_handler,
            message_handler=self._message_handler,
        )
        await self._exit_stack.enter_async_context(self._client)

    async def close(self) -> None:
        await self._exit_stack.aclose()
        self._client = None
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                logger.debug("Failed to close MCP stdio log handle", exc_info=True)
            finally:
                self._log_handle = None

    async def list_tools(self) -> List[Any]:
        if not self._client:
            raise RuntimeError("Client FastMCP non initialisé")
        return await self._client.list_tools()

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self._client:
            raise RuntimeError("Client FastMCP non initialisé")
        return await self._client.call_tool(tool_name, arguments)


class HttpMCPAdapter(MCPTransportAdapter):
    def __init__(self, config: MCPTransportConfig) -> None:
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        if not self.config.http_base_url:
            raise ValueError("http_base_url manquant pour le transport HTTP")
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)




    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _headers(self) -> Dict[str, str]:
        base = {"Content-Type": "application/json"}
        base.update(self.config.http_headers or {})
        return base

    def _endpoint(self, suffix: str) -> str:
        assert self.config.http_base_url
        return f"{self.config.http_base_url.rstrip('/')}/{suffix.lstrip('/')}"

    async def list_tools(self) -> List[Any]:
        if not self._session:
            raise RuntimeError("Session HTTP non initialisée")
        async with self._session.get(self._endpoint("tools"), headers=self._headers()) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self._session:
            raise RuntimeError("Session HTTP non initialisée")
        payload = {"name": tool_name, "arguments": arguments}
        async with self._session.post(
            self._endpoint("call"),
            headers=self._headers(),
            json=payload,
        ) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return await resp.json()
            return await resp.text()


class GrpcMCPAdapter(MCPTransportAdapter):
    """Adaptateur gRPC générique basé sur des payloads JSON."""

    def __init__(self, config: MCPTransportConfig) -> None:
        self.config = config
        self._channel = None
        self._grpc = None

    async def connect(self) -> None:
        import importlib

        try:
            self._grpc = importlib.import_module("grpc")
        except ImportError as exc:
            raise ImportError("grpcio est requis pour le transport gRPC") from exc

        if not self.config.grpc_target:
            raise ValueError("grpc_target manquant pour le transport gRPC")

        if self.config.grpc_tls:
            credentials = self._grpc.ssl_channel_credentials()
            self._channel = self._grpc.aio.secure_channel(self.config.grpc_target, credentials)
        else:
            self._channel = self._grpc.aio.insecure_channel(self.config.grpc_target)

    async def close(self) -> None:
        if self._channel:
            await self._channel.close()
            self._channel = None

    def _stub(self, method: str):
        if not self._channel or not self._grpc:
            raise RuntimeError("Canal gRPC non initialisé")
        return self._channel.unary_unary(
            method,
            request_serializer=lambda obj: json.dumps(obj).encode("utf-8"),
            response_deserializer=lambda data: json.loads(data.decode("utf-8")),
        )

    async def list_tools(self) -> List[Any]:
        call = self._stub(self.config.grpc_list_tools_method)
        response = await call({}, metadata=list((self.config.grpc_metadata or {}).items()))
        tools = response.get("tools", response)
        if isinstance(tools, list):
            return tools
        return []

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        call = self._stub(self.config.grpc_call_tool_method)
        payload = {"name": tool_name, "arguments": arguments}
        return await call(payload, metadata=list((self.config.grpc_metadata or {}).items()))

class AgriMCPClient:
    """Client MCP supportant plusieurs transports (stdio, HTTP, gRPC)."""

    _MAX_RECONNECT_ATTEMPTS = 2
    _RECONNECT_DELAY_S = 1.0
    _WATCHDOG_INTERVAL_S = 30.0

    def __init__(self, transport_config: MCPTransportConfig):
        self.transport = transport_config
        self._adapter: Optional[MCPTransportAdapter] = None
        self._tools_cache: List[Dict[str, Any]] = []
        self._raw_tools_cache: List[Any] = []
        self._connected = False
        self._last_connect_ts = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._request_lock = asyncio.Lock()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        if self._adapter is not None:
            return
        self._adapter = self._create_adapter()
        await self._adapter.connect()
        self._connected = True
        self._last_connect_ts = time.monotonic()
        await self.refresh_tools()
        self._ensure_watchdog()

    async def close(self):
        self._connected = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("MCP watchdog close error")
            finally:
                self._watchdog_task = None
        if self._adapter:
            try:
                await self._adapter.close()
            finally:
                self._adapter = None

    async def reconnect(self):
        """Ferme et ré-ouvre la connexion MCP (keep-alive / recovery)."""
        logger.warning("MCP reconnect triggered")
        try:
            await self.close()
        except Exception:
            pass
        await self.connect()

    def _ensure_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            return

        async def _watchdog() -> None:
            try:
                while self._connected:
                    await asyncio.sleep(self._WATCHDOG_INTERVAL_S)
                    if not self._adapter:
                        continue
                    try:
                        await self._with_lock(self._adapter.list_tools)
                        logger.debug("MCP watchdog heartbeat OK")
                    except Exception as exc:
                        logger.warning("MCP watchdog detected failure: %s", exc)
                        try:
                            await self.reconnect()
                        except Exception as re_exc:
                            logger.exception("MCP watchdog reconnect failed: %s", re_exc)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("MCP watchdog crashed")

        self._watchdog_task = asyncio.create_task(_watchdog())

    async def _with_lock(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs):
        async with self._request_lock:
            return await fn(*args, **kwargs)

    def _create_adapter(self) -> MCPTransportAdapter:
        kind = str(self.transport.kind).lower()
        if kind == "stdio":
            return FastMCPProcessAdapter(
                self.transport,
                elicitation_handler=self._handle_elicitation,
                progress_handler=self._handle_progress,
                message_handler=self._handle_message,
            )
        if kind == "http":
            return HttpMCPAdapter(self.transport)
        if kind == "grpc":
            return GrpcMCPAdapter(self.transport)
        raise ValueError(f"Unsupported MCP transport kind: {self.transport.kind}")

    @staticmethod
    def _normalize_tool_descriptor(tool: Any) -> Dict[str, Any]:
        def _extract_schema(candidate: Any) -> Dict[str, Any]:
            if isinstance(candidate, dict):
                return candidate
            return {"type": "object", "properties": {}}

        name = getattr(tool, "name", None)
        description = getattr(tool, "description", "")
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        if isinstance(tool, dict):
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
            name = name or tool.get("name") or (fn or {}).get("name")
            description = description or tool.get("description") or (fn or {}).get("description", "")
            schema = schema or tool.get("inputSchema") or tool.get("input_schema") or (fn or {}).get("parameters")
        if not schema:
            schema = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": name or "tool",
                "description": description or "",
                "parameters": _extract_schema(schema),
            },
        }

    async def refresh_tools(self) -> List[Dict[str, Any]]:
        if not self._adapter:
            raise RuntimeError("Client non connecté.")

        tools_response = await self._adapter.list_tools()
        self._raw_tools_cache = list(tools_response or [])
        self._tools_cache = [self._normalize_tool_descriptor(tool) for tool in self._raw_tools_cache]
        return self._tools_cache

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Retourne les outils au format OpenAI/LangChain (avec cache)."""
        if not self._tools_cache and self._adapter:
            await self.refresh_tools()
        return self._tools_cache

    def get_tools_for_langchain(self) -> List[Dict[str, Any]]:
        """Retourne les outils au format OpenAI/LangChain."""
        return self._tools_cache

    @staticmethod
    def _sanitize_arguments(arguments: Any) -> Dict[str, Any]:
        """Postel's Law gate: supprime les None et convertit les types invalides."""
        if not isinstance(arguments, dict):
            return {}
        cleaned: Dict[str, Any] = {}
        for k, v in arguments.items():
            if v is None:
                continue
            cleaned[k] = v
        return cleaned

    async def call_tool(self, tool_name: str, arguments) -> Any:
        if not self._adapter or not self._connected:
            raise RuntimeError("Client non connecté.")

        safe_args = self._sanitize_arguments(arguments)

        logger.info(
            "MCP_CALL_AUDIT | tool=%s | args=%s",
            tool_name,
            json.dumps(safe_args, default=str, ensure_ascii=False),
        )

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._MAX_RECONNECT_ATTEMPTS + 1):
            try:
                result = await self._with_lock(self._adapter.call_tool, tool_name, safe_args)
                break
            except (ConnectionError, OSError, RuntimeError, aiohttp.ClientError) as exc:
                last_exc = exc
                logger.warning(
                    "MCP call_tool attempt %d/%d failed (%s): %s",
                    attempt, self._MAX_RECONNECT_ATTEMPTS, tool_name, exc,
                )
                if attempt < self._MAX_RECONNECT_ATTEMPTS:
                    try:
                        await self.reconnect()
                    except Exception as re_exc:
                        logger.error("MCP reconnect failed: %s", re_exc)
                else:
                    raise last_exc  # type: ignore[misc]
            except Exception:
                raise
        else:
            raise RuntimeError(f"MCP call_tool {tool_name} failed after {self._MAX_RECONNECT_ATTEMPTS} attempts")

        if hasattr(result, "content") and isinstance(result.content, list):
            raw_output = "\n".join([c.text for c in result.content if hasattr(c, "text")])
            try:
                return json.loads(raw_output)
            except (json.JSONDecodeError, TypeError):
                return raw_output

        return result

    async def _handle_elicitation(self, message, response_type, params, context):
        logger.warning(f"Server requested elicitation: {message}")
        if ElicitResult:
            return ElicitResult(action="decline")
        return None

    async def _handle_progress(self, progress: float, total: Optional[float], message: Optional[str]) -> None:
        log_msg = f"MCP Progress: {progress}/{total if total else '?'} - {message}"
        logger.debug(log_msg)

    async def _handle_message(self, message):
        if hasattr(message, "method") and message.method == "notifications/tools/list_changed":
            await self.refresh_tools()

# NOTE: `UnifiedMCPClient` is defined once in `security.py` (canonical location,
# closest to ShieldHub). Import it from there:
#     from agriconnect.infrastructure.mcp.security import UnifiedMCPClient


async def main():
    if len(sys.argv) < 2:
        logger.info("Usage: python client.py <path_to_server.py>")
        sys.exit(1)

    server_script = sys.argv[1]
    logging.basicConfig(level=logging.INFO)

    transport = MCPTransportConfig(
        kind="stdio",
        stdio_script=server_script,
        stdio_cwd=str(Path(server_script).resolve().parent),
        stdio_env={"PYTHONPATH": str(Path(server_script).resolve().parent.parent)},
    )

    try:
        async with AgriMCPClient(transport) as client:
            logger.info("\n--- Liste des outils disponibles ---")
            tools = await client.refresh_tools()

            logger.info("\n--- Test d'appel d'outil ---")
            norm = await client.call_tool("get_producer_dashboard", {"producer_id": "3cadb350-59e5-4ad8-ae95-5ff7cc1350bd"})
            logger.info(f"Normalisation (5 tonnes) : {norm}")

    except Exception as e:
        logger.info(f"Erreur : {e}")



if __name__ == "__main__":
    import asyncio

    # Lancement de la boucle d'événements
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nInterruption par l'utilisateur (Ctrl+C).")
    except Exception as e:
        logger.info(f"Erreur fatale : {e}")
        sys.exit(1)