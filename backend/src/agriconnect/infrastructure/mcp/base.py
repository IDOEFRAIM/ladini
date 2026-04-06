from __future__ import annotations

import asyncio
import inspect
import json
import logging
import signal
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from fastmcp import FastMCP
from pydantic import BaseModel, ValidationError, create_model

from agriconnect.infrastructure.mcp.security import PermissionDenied, get_execution_policy

logger = logging.getLogger("MCP.Core.Base")


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    handler: Callable[..., Awaitable[str]]
    description: str = ""
    timeout_seconds: float = 10.0
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None


class MCPProvider(Protocol):
    name: str

    def get_tools(self) -> List[MCPToolSpec]:
        ...

    async def ping(self) -> Dict[str, Any]:
        ...


class MCPServerApp:
    def __init__(
        self,
        server_name: str,
        providers: List[MCPProvider],
        startup_callbacks: Optional[List[Callable[[], Any]]] = None,
        shutdown_callbacks: Optional[List[Callable[[], Any]]] = None,
    ) -> None:
        self.server_name = server_name
        self.providers = providers
        self._startup_callbacks = startup_callbacks or []
        self._shutdown_callbacks = shutdown_callbacks or []
        self._exec_policy = get_execution_policy()
        self.mcp = FastMCP(server_name)
        self._register_tools()
        self._register_health_resource()
        self._install_signal_handlers()

    def _register_tools(self) -> None:
        for provider in self.providers:
            for spec in provider.get_tools():
                wrapped = self._wrap_tool(provider.name, spec)
                self.mcp.tool(wrapped)
                logger.info("Registered MCP tool '%s' from provider '%s'", spec.name, provider.name)

    def _wrap_tool(self, provider_name: str, spec: MCPToolSpec) -> Callable[..., Awaitable[dict[str, Any]]]:
        sig = inspect.signature(spec.handler)
        input_model = spec.input_model or self._build_input_model(spec.name, sig)

        for param in sig.parameters.values():
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                logger.warning(
                    "Tool '%s' from provider '%s' has unsupported signature shape for strict wrapper; using raw handler",
                    spec.name,
                    provider_name,
                )
                return spec.handler

        async def _wrapped_impl(call_kwargs: dict[str, Any]) -> dict[str, Any]:
            clean_kwargs = dict(call_kwargs)

            user_id = str(
                clean_kwargs.get("user_id")
                or clean_kwargs.get("buyer_phone")
                or clean_kwargs.get("phone")
                or "anonymous"
            )
            request_id = str(clean_kwargs.get("request_id") or uuid.uuid4())

            validate_input = {k: v for k, v in clean_kwargs.items() if k not in {"ctx", "request_id"}}
            try:
                validated = input_model.model_validate(validate_input).model_dump()
            except ValidationError as exc:
                raise PermissionDenied(spec.name, f"invalid_input:{exc.errors()}") from exc

            if "ctx" in clean_kwargs:
                validated["ctx"] = clean_kwargs["ctx"]
            if "request_id" in sig.parameters:
                validated["request_id"] = request_id

            envelope = await self._exec_policy.execute(
                spec.name,
                spec.handler,
                validated,
                user_id=user_id,
                request_id=request_id,
                timeout_seconds=spec.timeout_seconds,
            )

            if spec.output_model is not None:
                try:
                    validated_out = spec.output_model.model_validate(envelope.get("data") or {})
                    envelope["data"] = validated_out.model_dump()
                except ValidationError as exc:
                    raise PermissionDenied(spec.name, f"invalid_output:{exc.errors()}") from exc

            return envelope

        defaults_ns: dict[str, Any] = {}
        param_defs: list[str] = []
        map_entries: list[str] = []
        has_kw_only = False

        for pname, param in sig.parameters.items():
            if param.kind == inspect.Parameter.KEYWORD_ONLY and not has_kw_only:
                param_defs.append("*")
                has_kw_only = True

            if param.default is inspect._empty:
                param_defs.append(pname)
            else:
                default_name = f"_d_{pname}"
                defaults_ns[default_name] = param.default
                param_defs.append(f"{pname}={default_name}")

            map_entries.append(f"'{pname}': {pname}")

        fn_src = (
            f"async def _generated({', '.join(param_defs)}):\n"
            f"    return await _wrapped_impl({{{', '.join(map_entries)}}})"
        )
        namespace: dict[str, Any] = {"_wrapped_impl": _wrapped_impl}
        namespace.update(defaults_ns)
        exec(fn_src, namespace)
        generated = namespace["_generated"]

        generated.__name__ = spec.name
        generated.__doc__ = spec.description or getattr(spec.handler, "__doc__", "")
        generated.__module__ = spec.handler.__module__
        return generated

    @staticmethod
    def _build_input_model(tool_name: str, sig: inspect.Signature) -> type[BaseModel]:
        fields: dict[str, tuple[Any, Any]] = {}
        for pname, param in sig.parameters.items():
            if pname in {"self", "ctx", "request_id"}:
                continue
            annotation = param.annotation if param.annotation is not inspect._empty else Any
            default = param.default if param.default is not inspect._empty else ...
            fields[pname] = (annotation, default)
        model_name = f"{tool_name.title().replace('_', '')}Input"
        return create_model(model_name, **fields)

    def _register_health_resource(self) -> None:
        @self.mcp.resource("mcp://health")
        async def mcp_health() -> str:
            providers_health: Dict[str, Any] = {}
            for provider in self.providers:
                try:
                    providers_health[provider.name] = await provider.ping()
                except Exception as exc:
                    providers_health[provider.name] = {"status": "down", "error": str(exc)}
            return json.dumps(
                {
                    "status": "ok",
                    "server": self.server_name,
                    "providers": providers_health,
                },
                ensure_ascii=False,
            )

    def _install_signal_handlers(self) -> None:
        def _shutdown_handler(signum: int, _frame: Any) -> None:
            logger.warning("Signal %s received, terminating MCP server '%s'", signum, self.server_name)
            self._run_shutdown_callbacks()
            raise KeyboardInterrupt

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _shutdown_handler)
            except Exception:
                logger.debug("Cannot register handler for %s on this platform", sig_name)

    def _run_startup_callbacks(self) -> None:
        for cb in self._startup_callbacks:
            self._run_callback(cb, stage="startup")

    def _run_shutdown_callbacks(self) -> None:
        for cb in self._shutdown_callbacks:
            self._run_callback(cb, stage="shutdown")

    @staticmethod
    def _run_callback(cb: Callable[[], Any], stage: str) -> None:
        try:
            result = cb()
            if inspect.isawaitable(result):
                asyncio.run(result)
        except Exception:
            logger.exception("MCP %s callback failed", stage)

    def run(self, transport: str = "stdio", host: Optional[str] = None, port: Optional[int] = None) -> None:
        logger.info(
            "Starting MCP server '%s' with transport=%s host=%s port=%s",
            self.server_name,
            transport,
            host,
            port,
        )
        kwargs: Dict[str, Any] = {"transport": transport}
        if host:
            kwargs["host"] = host
        if port:
            kwargs["port"] = int(port)
        self._run_startup_callbacks()
        try:
            self.mcp.run(**kwargs)
        finally:
            self._run_shutdown_callbacks()


class BaseServer(MCPServerApp):
    """Lightweight base class that MCP servers in protocols/mcp/servers/ can inherit.

    Provides a convenient constructor and keeps the `run` behavior from
    `MCPServerApp`. Servers can override or extend startup/shutdown hooks.
    """

    def __init__(
        self,
        server_name: str,
        providers: Optional[List[MCPProvider]] = None,
        startup_callbacks: Optional[List[Callable[[], Any]]] = None,
        shutdown_callbacks: Optional[List[Callable[[], Any]]] = None,
    ) -> None:
        super().__init__(server_name, providers or [], startup_callbacks=startup_callbacks, shutdown_callbacks=shutdown_callbacks)

