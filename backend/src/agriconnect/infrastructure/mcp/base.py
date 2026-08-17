"""MCP infrastructure — base classes for MCP server apps and tool wrapping.

Role in the stack:
  * `infrastructure/mcp/`     — engine (this file, runtime, client, context, security)
  * `protocols/mcp/servers/`  — stdio entry points + AgriDBService introspection
  * `market_coach/services/mcp/gateway.py` — high-level agent adapter

`MCPServerApp` is the generic wrapper around FastMCP. `AgriDBMCPServer`
(runtime.py) is the in-process backend used by the stdio entry point.
"""

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

from agriconnect.infrastructure.mcp.context import get_mcp_context
from agriconnect.infrastructure.mcp.security import (
    get_execution_policy,
)
from agriconnect.infrastructure.mcp.utils import run_coro_blocking

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

    def get_tools(self) -> List[MCPToolSpec]: ...

    async def ping(self) -> Dict[str, Any]: ...


class ValidationFailure(Exception):
    pass


class MissingContextError(Exception):
    pass


class ToolOutputValidationError(Exception):
    pass


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
                logger.info(
                    "Registered MCP tool '%s' from provider '%s'",
                    spec.name,
                    provider.name,
                )

    def _wrap_tool(
        self, provider_name: str, spec: MCPToolSpec
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        sig = inspect.signature(spec.handler)
        input_model = spec.input_model or self._build_input_model(spec.name, sig)

        for param in sig.parameters.values():
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                logger.warning(
                    "Tool '%s' from provider '%s' has unsupported signature shape for strict wrapper; using raw handler",
                    spec.name,
                    provider_name,
                )
                return spec.handler

        async def _wrapped_impl(call_kwargs: dict[str, Any]) -> dict[str, Any]:
            clean_kwargs = dict(call_kwargs)
            context_identity = get_mcp_context()
            if context_identity is None:
                raise MissingContextError(
                    f"Context identity missing for tool '{spec.name}' execution"
                )
            request_id = str(clean_kwargs.get("request_id") or uuid.uuid4())

            validate_input = {
                k: v for k, v in clean_kwargs.items() if k not in {"ctx", "request_id"}
            }
            try:
                validated = input_model.model_validate(validate_input).model_dump()
            except ValidationError as exc:
                error_text = _format_validation_error(exc)
                logger.warning(
                    "Validation failed for tool %s: %s", spec.name, error_text
                )
                raise ValidationFailure(error_text) from exc

            if "ctx" in clean_kwargs:
                validated["ctx"] = clean_kwargs["ctx"]
            if "request_id" in sig.parameters:
                validated["request_id"] = request_id

            user_id = context_identity.user_id

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
                    validated_out = spec.output_model.model_validate(
                        envelope.get("data") or {}
                    )
                    envelope["data"] = validated_out.model_dump()
                except ValidationError as exc:
                    raise ToolOutputValidationError(
                        f"{spec.name}: invalid_output {exc.errors()}"
                    ) from exc

            return envelope

        async def generated(*args: Any, **kwargs: Any) -> dict[str, Any]:
            bound = sig.bind_partial(*args, **kwargs)
            return await _wrapped_impl(bound.arguments)

        generated.__name__ = spec.name
        generated.__doc__ = spec.description or getattr(spec.handler, "__doc__", "")
        generated.__module__ = spec.handler.__module__
        generated.__signature__ = sig  # type: ignore[attr-defined]
        return generated

    @staticmethod
    def _build_input_model(tool_name: str, sig: inspect.Signature) -> type[BaseModel]:
        fields: dict[str, tuple[Any, Any]] = {}
        for pname, param in sig.parameters.items():
            if pname in {"self", "ctx", "request_id"}:
                continue
            annotation = (
                param.annotation if param.annotation is not inspect._empty else Any
            )
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
                    providers_health[provider.name] = await asyncio.wait_for(
                        provider.ping(), timeout=5.0
                    )
                except Exception as exc:
                    providers_health[provider.name] = {
                        "status": "down",
                        "error": str(exc),
                    }
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
            logger.warning(
                "Signal %s received, terminating MCP server '%s'",
                signum,
                self.server_name,
            )
            try:
                run_coro_blocking(self._run_shutdown_callbacks(), timeout=10)
            except Exception:
                logger.exception("Shutdown callbacks failed during signal handling")
            raise KeyboardInterrupt

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _shutdown_handler)
            except Exception:
                logger.debug(
                    "Cannot register handler for %s on this platform", sig_name
                )

    async def _run_startup_callbacks(self) -> None:
        await self._run_callbacks(self._startup_callbacks, stage="startup")

    async def _run_shutdown_callbacks(self) -> None:
        await self._run_callbacks(self._shutdown_callbacks, stage="shutdown")

    async def _run_callbacks(
        self, callbacks: List[Callable[[], Any]], stage: str
    ) -> None:
        if not callbacks:
            return
        tasks = [self._invoke_callback(cb, stage) for cb in callbacks]
        await asyncio.gather(*tasks, return_exceptions=False)

    async def _invoke_callback(self, cb: Callable[[], Any], stage: str) -> None:
        try:
            result = cb()
            if inspect.isawaitable(result):
                await result
            elif inspect.iscoroutinefunction(cb):
                await cb()
            else:
                return
        except Exception:
            logger.exception("MCP %s callback failed", stage)

    def run(
        self,
        transport: str = "stdio",
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
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
        run_coro_blocking(self._run_startup_callbacks(), timeout=15)
        try:
            self.mcp.run(**kwargs)
        finally:
            run_coro_blocking(self._run_shutdown_callbacks(), timeout=15)


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(loc) for loc in err.get('loc', []))}:{err.get('msg')}"
        for err in exc.errors()
    )


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
        super().__init__(
            server_name,
            providers or [],
            startup_callbacks=startup_callbacks,
            shutdown_callbacks=shutdown_callbacks,
        )
