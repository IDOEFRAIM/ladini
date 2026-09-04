from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import httpx

# Tentative d'import de FastMCP
try:
    from fastmcp import Client
    from fastmcp.client.elicitation import ElicitResult
except ImportError:
    Client = None
    ElicitResult = None


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
                raise ValueError(
                    "MCP_DB_STDIO_ENTRYPOINT must be configured for stdio transport"
                )
            env = dict(getattr(settings, "MCP_DB_STDIO_ENV", {}) or {})
            if "PYTHONPATH" not in env and getattr(settings, "BASE_DIR", None):
                env["PYTHONPATH"] = str(settings.BASE_DIR)
            log_path = getattr(settings, "MCP_DB_STDIO_LOG_PATH", None)
            if not log_path:
                base_logs = getattr(settings, "LOG_DIR", None)
                if base_logs:
                    log_path = str(Path(base_logs) / "mcp-db-stdio.log")
                else:
                    default_root = (
                        getattr(settings, "BASE_DIR", None) or tempfile.gettempdir()
                    )
                    log_path = str(Path(default_root) / "logs" / "mcp-db-stdio.log")

            return cls(
                kind="stdio",
                stdio_script=script,
                stdio_cwd=getattr(settings, "MCP_DB_STDIO_CWD", None)
                or str(Path(script).resolve().parent),
                stdio_env=env,
                stdio_python=getattr(settings, "MCP_DB_STDIO_PYTHON", None)
                or sys.executable,
                stdio_log_path=log_path,
            )
        if transport == "http":
            base_url = getattr(settings, "MCP_DB_HTTP_URL", "")
            if not base_url:
                # Fallback : dérive l'URL du daemon HTTP MCP depuis
                # MCP_DB_SERVER_HOST/MCP_DB_SERVER_PORT (les mêmes valeurs que
                # protocols/mcp/servers/http_server.py utilise pour son bind
                # par défaut) — évite de dupliquer la config host/port.
                host = getattr(settings, "MCP_DB_SERVER_HOST", "") or "localhost"
                port = getattr(settings, "MCP_DB_SERVER_PORT", None) or 8003
                base_url = f"http://{host}:{port}"
            if not base_url:
                raise ValueError(
                    "MCP_DB_HTTP_URL must be configured for http transport"
                )
            headers = dict(getattr(settings, "MCP_DB_HTTP_HEADERS", {}) or {})
            # Secret partagé avec le daemon (protocols/mcp/servers/http_server.py).
            # Injecté ici plutôt que dans MCP_DB_HTTP_HEADERS pour que la même
            # variable d'environnement configure les DEUX côtés d'un coup — un
            # secret posé côté daemon seul rendrait tous les appels 401.
            # Un en-tête explicitement fourni dans MCP_DB_HTTP_HEADERS reste
            # prioritaire (échappatoire pour un proxy/gateway qui l'injecte).
            token = str(getattr(settings, "MCP_HTTP_AUTH_TOKEN", "") or "").strip()
            if token and not any(k.lower() == "authorization" for k in headers):
                headers["Authorization"] = f"Bearer {token}"
            return cls(
                kind="http",
                http_base_url=base_url,
                http_headers=headers,
            )
        if transport == "grpc":
            target = getattr(settings, "MCP_DB_GRPC_TARGET", "")
            if not target:
                raise ValueError(
                    "MCP_DB_GRPC_TARGET must be configured for grpc transport"
                )
            return cls(
                kind="grpc",
                grpc_target=target,
                grpc_tls=bool(getattr(settings, "MCP_DB_GRPC_TLS", False)),
                grpc_metadata=getattr(settings, "MCP_DB_GRPC_METADATA", {}) or {},
                grpc_list_tools_method=getattr(
                    settings,
                    "MCP_DB_GRPC_LIST_TOOLS_METHOD",
                    "/agriconnect.mcp.MCP/ListTools",
                ),
                grpc_call_tool_method=getattr(
                    settings,
                    "MCP_DB_GRPC_CALL_TOOL_METHOD",
                    "/agriconnect.mcp.MCP/CallTool",
                ),
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
        from fastmcp.client.transports import PythonStdioTransport

        script_path = Path(str(self.config.stdio_script)).resolve()
        if not script_path.exists():
            raise FileNotFoundError(f"MCP server script introuvable: {script_path}")

        env = dict(os.environ)
        env.update(self.config.stdio_env or {})

        log_file_path = self.config.stdio_log_path or str(
            Path(tempfile.gettempdir()) / "mcp-db-stdio.log"
        )
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
    """Transport HTTP conforme au spec MCP (Streamable HTTP, JSON-RPC 2.0).

    Audit MCP/AGUI 2026-08-27 : remplace l'ancien contrat REST maison
    (``GET /tools``, ``POST /call``) que ``protocols/mcp/servers/http_server.py``
    n'expose plus — ce daemon route désormais tout le trafic MCP via un
    ``StreamableHTTPSessionManager`` officiel sur l'unique endpoint ``/mcp``.
    Même construction que ``FastMCPProcessAdapter`` (``fastmcp.Client`` +
    handlers elicitation/progress/message), transport différent uniquement.
    """

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

    async def connect(self) -> None:
        if not Client:
            raise ImportError("fastmcp est requis pour le transport http")
        from fastmcp.client.transports import StreamableHttpTransport

        if not self.config.http_base_url:
            raise ValueError("http_base_url manquant pour le transport HTTP")

        transport = StreamableHttpTransport(
            # Trailing slash évite un aller-retour 307 Temporary Redirect sur
            # CHAQUE appel MCP (FastMCP monte ses routes en "/mcp/", voir les
            # logs "POST /mcp -> 307 -> POST /mcp/ -> 200" — cosmétique mais
            # double la latence réseau de chaque appel pour rien).
            url=f"{self.config.http_base_url.rstrip('/')}/mcp/",
            headers=dict(self.config.http_headers or {}),
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

    async def list_tools(self) -> List[Any]:
        if not self._client:
            raise RuntimeError("Client HTTP MCP non initialisé")
        return await self._client.list_tools()

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self._client:
            raise RuntimeError("Client HTTP MCP non initialisé")
        return await self._client.call_tool(tool_name, arguments)


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
            self._channel = self._grpc.aio.secure_channel(
                self.config.grpc_target, credentials
            )
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
        response = await call(
            {}, metadata=list((self.config.grpc_metadata or {}).items())
        )
        tools = response.get("tools", response)
        if isinstance(tools, list):
            return tools
        return []

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        call = self._stub(self.config.grpc_call_tool_method)
        payload = {"name": tool_name, "arguments": arguments}
        return await call(
            payload, metadata=list((self.config.grpc_metadata or {}).items())
        )


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
                            logger.exception(
                                "MCP watchdog reconnect failed: %s", re_exc
                            )
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
            return HttpMCPAdapter(
                self.transport,
                elicitation_handler=self._handle_elicitation,
                progress_handler=self._handle_progress,
                message_handler=self._handle_message,
            )
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
        schema = getattr(tool, "input_schema", None) or getattr(
            tool, "inputSchema", None
        )
        if isinstance(tool, dict):
            fn = (
                tool.get("function") if isinstance(tool.get("function"), dict) else None
            )
            name = name or tool.get("name") or (fn or {}).get("name")
            description = (
                description
                or tool.get("description")
                or (fn or {}).get("description", "")
            )
            schema = (
                schema
                or tool.get("inputSchema")
                or tool.get("input_schema")
                or (fn or {}).get("parameters")
            )
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
        self._tools_cache = [
            self._normalize_tool_descriptor(tool) for tool in self._raw_tools_cache
        ]
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

    async def call_tool(
        self,
        tool_name: str,
        arguments,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        """Exécute un outil, en retentant jusqu'à `_MAX_RECONNECT_ATTEMPTS`
        fois après une coupure réseau intermédiaire (`reconnect()` entre deux
        tentatives).

        `idempotency_key` (audit MCP/AGUI 2026-08-27) : identifie la tentative
        logique — PAS chaque essai HTTP/stdio individuel. Une clé unique est
        générée si l'appelant n'en fournit pas, puis réutilisée TELLE QUELLE
        à chaque itération de la boucle ci-dessous. Sans ça, un retry après
        coupure réseau (la connexion tombe APRÈS que l'écriture a réellement
        atteint la DB, mais AVANT que la réponse ne revienne) rejouerait un
        `create_order`/`initiate_escrow_payment` comme un appel entièrement
        nouveau, indistinguable d'une seconde commande légitime.

        Note de portée : ceci propage la clé jusqu'au backend (`_idempotency_key`
        dans les arguments) — la déduplication CÔTÉ SERVEUR (contrainte
        unique, table de déduplication...) n'est pas dans le périmètre de ce
        chantier et reste à implémenter tool par tool pour les actions
        sensibles. `AgriDBMCPServer.call_tool` (runtime.py) retire déjà la
        clé avant dispatch, exactement comme `_caller_phone`, pour ne pas
        faire planter un outil dont la signature ne l'accepte pas.
        """
        if not self._adapter or not self._connected:
            raise RuntimeError("Client non connecté.")

        key = idempotency_key or str(uuid.uuid4())
        safe_args = {**self._sanitize_arguments(arguments), "_idempotency_key": key}

        logger.info(
            "MCP_CALL_AUDIT | tool=%s | idempotency_key=%s | args=%s",
            tool_name,
            key,
            json.dumps(safe_args, default=str, ensure_ascii=False),
        )

        for attempt in range(1, self._MAX_RECONNECT_ATTEMPTS + 1):
            try:
                # `safe_args` (donc la même `_idempotency_key`) est réutilisé
                # tel quel à chaque tentative — c'est ce qui rend le retry
                # idempotent-compatible plutôt qu'un nouvel appel logique.
                result = await self._with_lock(
                    self._adapter.call_tool, tool_name, safe_args
                )
                break
            except (ConnectionError, OSError, RuntimeError, httpx.HTTPError) as exc:
                logger.warning(
                    "MCP call_tool attempt %d/%d failed (%s): %s",
                    attempt,
                    self._MAX_RECONNECT_ATTEMPTS,
                    tool_name,
                    exc,
                )
                if attempt < self._MAX_RECONNECT_ATTEMPTS:
                    try:
                        await self.reconnect()
                    except Exception as re_exc:
                        logger.error("MCP reconnect failed: %s", re_exc)
                else:
                    raise
            except Exception:
                raise
        else:
            raise RuntimeError(
                f"MCP call_tool {tool_name} failed after {self._MAX_RECONNECT_ATTEMPTS} attempts"
            )

        if hasattr(result, "content") and isinstance(result.content, list):
            raw_output = "\n".join(
                [c.text for c in result.content if hasattr(c, "text")]
            )
            try:
                return json.loads(raw_output)
            except (json.JSONDecodeError, TypeError) as exc:
                # Filet de sécurité AVANT de renvoyer le texte brut : certains
                # tools DB sérialisent leur réponse via `str(dict)`/f-string
                # plutôt que `json.dumps` (littéral Python — guillemets
                # simples, None/True/False) plutôt que du JSON strict. Sans ce
                # filet, le texte brut atterrissait dans `ensure_dict()`
                # (utils.py) dont l'heuristique de repli fait un naïf
                # `.replace("'", '"')` — DESTRUCTEUR sur tout texte français
                # contenant une apostrophe interne ("d'offres", "l'exploitation",
                # "n'ai pas trouvé"...), qui corrompt la structure JSON et fait
                # disparaître silencieusement `message`/`data` de la réponse.
                # `ast.literal_eval` gère les littéraux Python sans jamais
                # toucher au contenu des chaînes.
                try:
                    parsed = ast.literal_eval(raw_output)
                    if isinstance(parsed, (dict, list)):
                        return parsed
                except (ValueError, SyntaxError, TypeError):
                    pass
                logger.warning(
                    "MCP_NON_JSON_RESPONSE | tool=%s | error=%s | raw_output[:300]=%s",
                    tool_name,
                    exc,
                    raw_output[:300],
                )
                return raw_output

        return result

    async def _handle_elicitation(self, message, response_type, params, context):
        logger.warning(f"Server requested elicitation: {message}")
        if ElicitResult:
            return ElicitResult(action="decline")
        return None

    async def _handle_progress(
        self, progress: float, total: Optional[float], message: Optional[str]
    ) -> None:
        log_msg = f"MCP Progress: {progress}/{total if total else '?'} - {message}"
        logger.debug(log_msg)

    async def _handle_message(self, message):
        if (
            hasattr(message, "method")
            and message.method == "notifications/tools/list_changed"
        ):
            await self.refresh_tools()


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
            await client.refresh_tools()

            logger.info("\n--- Test d'appel d'outil ---")
            norm = await client.call_tool(
                "get_producer_dashboard",
                {"producer_id": "3cadb350-59e5-4ad8-ae95-5ff7cc1350bd"},
            )
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
