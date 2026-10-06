"""`infrastructure/mcp/client.py` — transport MCP (stdio/HTTP/gRPC) + le client
haut niveau `AgriMCPClient` (cache d'outils, reconnexion, watchdog, parsing de
réponse tolérant).

Zéro processus réel, zéro réseau : chaque adaptateur est testé avec ses
dépendances externes (fastmcp `Client`/`PythonStdioTransport`, `aiohttp`,
`grpc`) doublées via monkeypatch.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tests.conftest import run

# =====================================================================
# MCPTransportConfig.from_settings
# =====================================================================

class TestTransportConfigFromSettings:
    def test_stdio_requires_an_entrypoint(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(MCP_DB_TRANSPORT="stdio", MCP_DB_STDIO_ENTRYPOINT="")
        with pytest.raises(ValueError, match="MCP_DB_STDIO_ENTRYPOINT"):
            MCPTransportConfig.from_settings(settings)

    def test_stdio_auto_fills_pythonpath_from_base_dir(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(
            MCP_DB_TRANSPORT="stdio",
            MCP_DB_STDIO_ENTRYPOINT="/srv/mcp/server.py",
            MCP_DB_STDIO_ENV={},
            BASE_DIR="/srv/app",
            MCP_DB_STDIO_LOG_PATH=None,
            LOG_DIR=None,
            MCP_DB_STDIO_CWD=None,
            MCP_DB_STDIO_PYTHON=None,
        )
        cfg = MCPTransportConfig.from_settings(settings)
        assert cfg.kind == "stdio"
        assert cfg.stdio_env["PYTHONPATH"] == "/srv/app"

    def test_stdio_log_path_prefers_log_dir_over_base_dir(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(
            MCP_DB_TRANSPORT="stdio",
            MCP_DB_STDIO_ENTRYPOINT="/srv/mcp/server.py",
            MCP_DB_STDIO_ENV={},
            BASE_DIR="/srv/app",
            MCP_DB_STDIO_LOG_PATH=None,
            LOG_DIR="/var/log",
            MCP_DB_STDIO_CWD=None,
            MCP_DB_STDIO_PYTHON=None,
        )
        cfg = MCPTransportConfig.from_settings(settings)
        assert "var" in cfg.stdio_log_path and "log" in cfg.stdio_log_path

    def test_http_derives_url_from_host_and_port_when_unset(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(
            MCP_DB_TRANSPORT="http", MCP_DB_HTTP_URL="",
            MCP_DB_SERVER_HOST="db-mcp", MCP_DB_SERVER_PORT=9001,
            MCP_DB_HTTP_HEADERS={},
        )
        cfg = MCPTransportConfig.from_settings(settings)
        assert cfg.http_base_url == "http://db-mcp:9001"

    def test_http_uses_explicit_url_when_set(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(
            MCP_DB_TRANSPORT="http", MCP_DB_HTTP_URL="https://mcp.internal",
            MCP_DB_HTTP_HEADERS={"X-Api-Key": "k"},
        )
        cfg = MCPTransportConfig.from_settings(settings)
        assert cfg.http_base_url == "https://mcp.internal"
        assert cfg.http_headers == {"X-Api-Key": "k"}

    def test_grpc_requires_a_target(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(MCP_DB_TRANSPORT="grpc", MCP_DB_GRPC_TARGET="")
        with pytest.raises(ValueError, match="MCP_DB_GRPC_TARGET"):
            MCPTransportConfig.from_settings(settings)

    def test_grpc_builds_config_with_defaults(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(
            MCP_DB_TRANSPORT="grpc", MCP_DB_GRPC_TARGET="mcp:50051",
            MCP_DB_GRPC_TLS=True, MCP_DB_GRPC_METADATA={"a": "b"},
        )
        cfg = MCPTransportConfig.from_settings(settings)
        assert cfg.grpc_target == "mcp:50051"
        assert cfg.grpc_tls is True
        assert cfg.grpc_metadata == {"a": "b"}

    def test_unsupported_transport_raises(self):
        from ladini.infrastructure.mcp.client import MCPTransportConfig
        settings = SimpleNamespace(MCP_DB_TRANSPORT="carrier_pigeon")
        with pytest.raises(ValueError, match="Unsupported MCP transport"):
            MCPTransportConfig.from_settings(settings)


# =====================================================================
# HttpMCPAdapter — audit MCP/AGUI 2026-08-27 : remplace l'ancien contrat
# REST maison (GET /tools, POST /call, session aiohttp) par le vrai
# transport MCP Streamable HTTP (fastmcp.Client + StreamableHttpTransport),
# le pendant HTTP exact de FastMCPProcessAdapter — mêmes fixtures/patterns
# de test (`_FakeFastMCPClient`, défini plus bas dans ce fichier).
# =====================================================================

class TestHttpMCPAdapter:
    def test_connect_raises_without_fastmcp_installed(self, monkeypatch):
        import ladini.infrastructure.mcp.client as client_mod
        from ladini.infrastructure.mcp.client import HttpMCPAdapter, MCPTransportConfig

        monkeypatch.setattr(client_mod, "Client", None)
        adapter = HttpMCPAdapter(MCPTransportConfig(kind="http", http_base_url="http://x"))
        with pytest.raises(ImportError, match="fastmcp est requis"):
            run(adapter.connect())

    def test_connect_requires_a_base_url(self, monkeypatch):
        import ladini.infrastructure.mcp.client as client_mod
        from ladini.infrastructure.mcp.client import HttpMCPAdapter, MCPTransportConfig

        monkeypatch.setattr(client_mod, "Client", _FakeFastMCPClient)
        adapter = HttpMCPAdapter(MCPTransportConfig(kind="http", http_base_url=None))
        with pytest.raises(ValueError, match="http_base_url"):
            run(adapter.connect())

    def test_connect_targets_the_mcp_endpoint(self, monkeypatch):
        """Le daemon n'expose plus qu'un unique endpoint `/mcp` (Streamable
        HTTP) — vérifie que l'URL construite pointe bien dessus, y compris
        quand `http_base_url` porte un slash final."""
        import ladini.infrastructure.mcp.client as client_mod
        from ladini.infrastructure.mcp.client import HttpMCPAdapter, MCPTransportConfig

        monkeypatch.setattr(client_mod, "Client", _FakeFastMCPClient)

        class _FakeTransport:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        import fastmcp.client.transports as transports_mod
        monkeypatch.setattr(transports_mod, "StreamableHttpTransport", _FakeTransport)

        adapter = HttpMCPAdapter(MCPTransportConfig(
            kind="http", http_base_url="http://x/", http_headers={"H": "1"},
        ))
        run(adapter.connect())
        assert adapter._client is not None
        assert adapter._client.transport.kwargs["url"] == "http://x/mcp/"
        assert adapter._client.transport.kwargs["headers"] == {"H": "1"}
        run(adapter.close())
        assert adapter._client is None

    def test_list_tools_and_call_tool_require_a_connected_client(self):
        from ladini.infrastructure.mcp.client import HttpMCPAdapter, MCPTransportConfig
        adapter = HttpMCPAdapter(MCPTransportConfig(kind="http", http_base_url="http://x"))
        with pytest.raises(RuntimeError, match="Client HTTP MCP non initialisé"):
            run(adapter.list_tools())
        with pytest.raises(RuntimeError, match="Client HTTP MCP non initialisé"):
            run(adapter.call_tool("t", {}))

    def test_list_tools_and_call_tool_delegate_once_connected(self):
        from ladini.infrastructure.mcp.client import HttpMCPAdapter, MCPTransportConfig
        adapter = HttpMCPAdapter(MCPTransportConfig(kind="http", http_base_url="http://x"))
        adapter._client = _FakeFastMCPClient(transport=None)
        assert run(adapter.list_tools()) == [{"name": "t1"}]
        assert run(adapter.call_tool("t1", {"a": 1})) == {"name": "t1", "arguments": {"a": 1}}

    def test_close_is_a_noop_without_a_connected_client(self):
        from ladini.infrastructure.mcp.client import HttpMCPAdapter, MCPTransportConfig
        adapter = HttpMCPAdapter(MCPTransportConfig(kind="http", http_base_url="http://x"))
        run(adapter.close())  # ne doit pas lever


# =====================================================================
# GrpcMCPAdapter
# =====================================================================

class _FakeUnaryUnary:
    def __init__(self, response):
        self._response = response

    async def __call__(self, payload, metadata=None):
        return self._response


class _FakeGrpcChannel:
    def __init__(self, response=None):
        self._response = response or {}
        self.closed = False

    def unary_unary(self, method, request_serializer=None, response_deserializer=None):
        return _FakeUnaryUnary(self._response)

    async def close(self):
        self.closed = True


def _install_fake_grpc(monkeypatch, channel):
    fake_grpc = SimpleNamespace(
        aio=SimpleNamespace(
            insecure_channel=lambda target: channel,
            secure_channel=lambda target, creds: channel,
        ),
        ssl_channel_credentials=lambda: "fake-creds",
    )
    monkeypatch.setitem(sys.modules, "grpc", fake_grpc)


class TestGrpcMCPAdapter:
    def test_connect_requires_a_target(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        _install_fake_grpc(monkeypatch, _FakeGrpcChannel())
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target=None))
        with pytest.raises(ValueError, match="grpc_target"):
            run(adapter.connect())

    def test_connect_uses_insecure_channel_by_default(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        channel = _FakeGrpcChannel()
        _install_fake_grpc(monkeypatch, channel)
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1", grpc_tls=False))
        run(adapter.connect())
        assert adapter._channel is channel

    def test_connect_uses_secure_channel_with_tls(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        channel = _FakeGrpcChannel()
        _install_fake_grpc(monkeypatch, channel)
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1", grpc_tls=True))
        run(adapter.connect())
        assert adapter._channel is channel

    def test_stub_without_a_channel_raises(self):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1"))
        with pytest.raises(RuntimeError, match="Canal gRPC non initialisé"):
            adapter._stub("/method")

    def test_list_tools_unwraps_tools_key(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        channel = _FakeGrpcChannel(response={"tools": [{"name": "t1"}]})
        _install_fake_grpc(monkeypatch, channel)
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1"))
        run(adapter.connect())
        assert run(adapter.list_tools()) == [{"name": "t1"}]

    def test_list_tools_returns_empty_list_on_unexpected_shape(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        channel = _FakeGrpcChannel(response={"unexpected": "shape"})
        _install_fake_grpc(monkeypatch, channel)
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1"))
        run(adapter.connect())
        assert run(adapter.list_tools()) == []

    def test_call_tool_returns_the_raw_response(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        channel = _FakeGrpcChannel(response={"result": "ok"})
        _install_fake_grpc(monkeypatch, channel)
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1"))
        run(adapter.connect())
        assert run(adapter.call_tool("t1", {"a": 1})) == {"result": "ok"}

    def test_close_closes_the_channel(self, monkeypatch):
        from ladini.infrastructure.mcp.client import GrpcMCPAdapter, MCPTransportConfig
        channel = _FakeGrpcChannel()
        _install_fake_grpc(monkeypatch, channel)
        adapter = GrpcMCPAdapter(MCPTransportConfig(kind="grpc", grpc_target="mcp:1"))
        run(adapter.connect())
        run(adapter.close())
        assert channel.closed is True
        assert adapter._channel is None


# =====================================================================
# FastMCPProcessAdapter
# =====================================================================

class _FakeFastMCPClient:
    def __init__(self, transport, **kwargs):
        self.transport = transport
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tools(self):
        return [{"name": "t1"}]

    async def call_tool(self, name, arguments):
        return {"name": name, "arguments": arguments}


class TestFastMCPProcessAdapter:
    def test_connect_raises_without_fastmcp_installed(self, monkeypatch, tmp_path):
        import ladini.infrastructure.mcp.client as client_mod
        from ladini.infrastructure.mcp.client import (
            FastMCPProcessAdapter,
            MCPTransportConfig,
        )

        monkeypatch.setattr(client_mod, "Client", None)
        adapter = FastMCPProcessAdapter(MCPTransportConfig(kind="stdio", stdio_script="anything.py"))
        with pytest.raises(ImportError, match="fastmcp est requis"):
            run(adapter.connect())

    def test_connect_raises_when_script_is_missing(self, monkeypatch, tmp_path):
        import ladini.infrastructure.mcp.client as client_mod
        from ladini.infrastructure.mcp.client import (
            FastMCPProcessAdapter,
            MCPTransportConfig,
        )

        monkeypatch.setattr(client_mod, "Client", _FakeFastMCPClient)
        missing = tmp_path / "does_not_exist.py"
        adapter = FastMCPProcessAdapter(MCPTransportConfig(kind="stdio", stdio_script=str(missing)))
        with pytest.raises(FileNotFoundError):
            run(adapter.connect())

    def test_connect_success_wires_the_client_and_transport(self, monkeypatch, tmp_path):
        import ladini.infrastructure.mcp.client as client_mod
        from ladini.infrastructure.mcp.client import (
            FastMCPProcessAdapter,
            MCPTransportConfig,
        )

        script = tmp_path / "server.py"
        script.write_text("# fake mcp server")
        log_path = tmp_path / "logs" / "mcp.log"

        monkeypatch.setattr(client_mod, "Client", _FakeFastMCPClient)

        class _FakeTransport:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        import fastmcp.client.transports as transports_mod
        monkeypatch.setattr(transports_mod, "PythonStdioTransport", _FakeTransport)

        adapter = FastMCPProcessAdapter(MCPTransportConfig(
            kind="stdio", stdio_script=str(script), stdio_log_path=str(log_path),
        ))
        run(adapter.connect())
        assert adapter._client is not None
        assert log_path.exists()
        run(adapter.close())
        assert adapter._client is None

    def test_list_tools_and_call_tool_require_a_connected_client(self):
        from ladini.infrastructure.mcp.client import (
            FastMCPProcessAdapter,
            MCPTransportConfig,
        )
        adapter = FastMCPProcessAdapter(MCPTransportConfig(kind="stdio", stdio_script="x.py"))
        with pytest.raises(RuntimeError, match="Client FastMCP non initialisé"):
            run(adapter.list_tools())
        with pytest.raises(RuntimeError, match="Client FastMCP non initialisé"):
            run(adapter.call_tool("t", {}))

    def test_list_tools_and_call_tool_delegate_once_connected(self):
        from ladini.infrastructure.mcp.client import (
            FastMCPProcessAdapter,
            MCPTransportConfig,
        )
        adapter = FastMCPProcessAdapter(MCPTransportConfig(kind="stdio", stdio_script="x.py"))
        adapter._client = _FakeFastMCPClient(transport=None)
        assert run(adapter.list_tools()) == [{"name": "t1"}]
        assert run(adapter.call_tool("t1", {"a": 1})) == {"name": "t1", "arguments": {"a": 1}}


# =====================================================================
# AgriMCPClient — orchestration haut niveau (avec un adaptateur FAKE)
# =====================================================================

class _FakeAdapter:
    def __init__(self, tools=None, call_result=None, fail_n_times=0, fail_exc=None):
        self.connected = False
        self.closed = False
        self._tools = tools if tools is not None else [{"name": "t1"}]
        self._call_result = call_result if call_result is not None else {"ok": True}
        self._fail_n_times = fail_n_times
        self._fail_exc = fail_exc or ConnectionError("transient")
        self.call_count = 0
        self.list_tools_calls = 0
        # Args réellement reçus à CHAQUE tentative — utilisé par les tests
        # d'idempotence pour vérifier ce qui traverse la boucle de retry.
        self.received_args = []

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True
        self.connected = False

    async def list_tools(self):
        self.list_tools_calls += 1
        if not self.connected:
            raise RuntimeError("not connected")
        return self._tools

    async def call_tool(self, tool_name, arguments):
        self.received_args.append(dict(arguments))
        self.call_count += 1
        if self.call_count <= self._fail_n_times:
            raise self._fail_exc
        return self._call_result


def _client_with_fake_adapter(monkeypatch, adapter):
    from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
    client = AgriMCPClient(MCPTransportConfig(kind="stdio", stdio_script="x.py"))
    monkeypatch.setattr(client, "_create_adapter", lambda: adapter)
    return client


class TestAgriMCPClientLifecycle:
    def test_connect_populates_tools_cache_and_marks_connected(self, monkeypatch):
        adapter = _FakeAdapter(tools=[{"name": "t1"}])
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        assert client._connected is True
        assert len(client._tools_cache) == 1
        run(client.close())

    def test_connect_is_idempotent(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.connect())  # 2e appel : no-op (adapter déjà défini)
        run(client.close())

    def test_context_manager_connects_and_closes(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)

        async def _use():
            async with client:
                assert client._connected is True
            return client

        run(_use())
        assert adapter.closed is True

    def test_close_cancels_the_watchdog_task(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        assert client._watchdog_task is not None
        run(client.close())
        assert client._watchdog_task is None

    def test_reconnect_closes_then_reconnects(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.reconnect())
        assert client._connected is True
        run(client.close())

    def test_create_adapter_dispatches_by_kind(self):
        from ladini.infrastructure.mcp.client import (
            AgriMCPClient,
            FastMCPProcessAdapter,
            GrpcMCPAdapter,
            HttpMCPAdapter,
            MCPTransportConfig,
        )
        assert isinstance(AgriMCPClient(MCPTransportConfig(kind="stdio"))._create_adapter(), FastMCPProcessAdapter)
        assert isinstance(AgriMCPClient(MCPTransportConfig(kind="http"))._create_adapter(), HttpMCPAdapter)
        assert isinstance(AgriMCPClient(MCPTransportConfig(kind="grpc"))._create_adapter(), GrpcMCPAdapter)

    def test_create_adapter_wires_handlers_into_the_http_adapter_too(self):
        """Parité stdio/http (audit MCP/AGUI 2026-08-27) : les deux
        transports passent désormais par `fastmcp.Client` — les handlers
        elicitation/progress/message doivent être câblés sur les deux, pas
        seulement sur le transport stdio historique."""
        from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
        client = AgriMCPClient(MCPTransportConfig(kind="http", http_base_url="http://x"))
        adapter = client._create_adapter()
        assert adapter._elicitation_handler == client._handle_elicitation
        assert adapter._progress_handler == client._handle_progress
        assert adapter._message_handler == client._handle_message

    def test_create_adapter_unknown_kind_raises(self):
        from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
        client = AgriMCPClient(MCPTransportConfig(kind="carrier_pigeon"))
        with pytest.raises(ValueError, match="Unsupported MCP transport kind"):
            client._create_adapter()

    def test_close_swallows_a_non_cancelled_watchdog_error(self, monkeypatch):
        """Régression prévenue : si la tâche watchdog se termine sur une
        exception (pas une annulation), `close()` ne doit jamais la laisser
        remonter et bloquer la fermeture de la connexion."""
        import asyncio
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)

        async def _use():
            await client.connect()

            async def _boom():
                raise RuntimeError("watchdog internal crash")

            client._watchdog_task = asyncio.create_task(_boom())
            await asyncio.sleep(0)  # laisse la tâche s'exécuter et échouer
            await client.close()  # ne doit PAS lever

        run(_use())
        assert client._watchdog_task is None

    def test_reconnect_swallows_a_close_failure_and_still_reconnects(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())

        async def _boom_close():
            raise RuntimeError("close failed")

        monkeypatch.setattr(client, "close", _boom_close)
        run(client.reconnect())
        assert client._connected is True

    def test_watchdog_reconnects_after_a_failed_heartbeat(self, monkeypatch):
        """Le watchdog interroge périodiquement l'adaptateur ; si
        `list_tools()` échoue (connexion morte côté serveur), il doit
        déclencher une reconnexion automatique — sans ça, une session MCP
        silencieusement coupée reste inutilisable jusqu'au prochain crash."""
        import asyncio

        import ladini.infrastructure.mcp.client as client_mod

        monkeypatch.setattr(client_mod.AgriMCPClient, "_WATCHDOG_INTERVAL_S", 0.01)
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)

        # Le heartbeat (list_tools) échoue systématiquement, mais seulement
        # APRÈS la connexion initiale (qui appelle aussi list_tools via
        # refresh_tools) — sinon connect() lui-même échouerait.
        calls = {"n": 0}

        async def _flaky_list_tools():
            calls["n"] += 1
            raise ConnectionError("server gone")

        reconnect_calls = {"n": 0}
        original_reconnect = client.reconnect

        async def _spy_reconnect():
            reconnect_calls["n"] += 1
            client._connected = False  # stoppe la boucle watchdog après une itération

        monkeypatch.setattr(client, "reconnect", _spy_reconnect)

        async def _use():
            await client.connect()
            adapter.list_tools = _flaky_list_tools
            for _ in range(50):
                await asyncio.sleep(0.01)
                if reconnect_calls["n"] > 0:
                    break
            client._connected = False
            if client._watchdog_task:
                client._watchdog_task.cancel()
                try:
                    await client._watchdog_task
                except asyncio.CancelledError:
                    pass

        run(_use())
        assert reconnect_calls["n"] >= 1
        assert calls["n"] >= 1


class TestAgriMCPClientToolsCache:
    def test_list_tools_refreshes_when_cache_is_empty(self, monkeypatch):
        adapter = _FakeAdapter(tools=[{"name": "t1"}])
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        client._tools_cache = []
        result = run(client.list_tools())
        assert len(result) == 1

    def test_get_tools_for_langchain_reads_the_cache_without_refresh(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        cached = client.get_tools_for_langchain()
        assert cached == client._tools_cache

    def test_refresh_tools_without_a_connected_adapter_raises(self):
        from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
        client = AgriMCPClient(MCPTransportConfig(kind="stdio"))
        with pytest.raises(RuntimeError, match="Client non connecté"):
            run(client.refresh_tools())

    @pytest.mark.parametrize("tool,expected_name", [
        (SimpleNamespace(name="t1", description="d1", input_schema={"type": "object"}), "t1"),
        ({"name": "t2", "description": "d2", "inputSchema": {"type": "object"}}, "t2"),
        ({"function": {"name": "t3", "description": "d3", "parameters": {"type": "object"}}}, "t3"),
        ({}, "tool"),
    ])
    def test_normalize_tool_descriptor_shapes(self, tool, expected_name):
        from ladini.infrastructure.mcp.client import AgriMCPClient
        normalized = AgriMCPClient._normalize_tool_descriptor(tool)
        assert normalized["type"] == "function"
        assert normalized["function"]["name"] == expected_name
        assert isinstance(normalized["function"]["parameters"], dict)


class TestAgriMCPClientSanitizeArguments:
    def test_drops_none_values(self):
        from ladini.infrastructure.mcp.client import AgriMCPClient
        assert AgriMCPClient._sanitize_arguments({"a": 1, "b": None}) == {"a": 1}

    def test_non_dict_input_becomes_empty_dict(self):
        from ladini.infrastructure.mcp.client import AgriMCPClient
        assert AgriMCPClient._sanitize_arguments("not-a-dict") == {}
        assert AgriMCPClient._sanitize_arguments(None) == {}


class TestAgriMCPClientCallTool:
    def test_raises_without_a_connected_adapter(self):
        from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
        client = AgriMCPClient(MCPTransportConfig(kind="stdio"))
        with pytest.raises(RuntimeError, match="Client non connecté"):
            run(client.call_tool("t", {}))

    def test_success_returns_raw_dict_result(self, monkeypatch):
        adapter = _FakeAdapter(call_result={"order_id": "o1"})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        result = run(client.call_tool("create_order", {"a": None, "b": 1}))
        assert result == {"order_id": "o1"}

    def test_retries_once_on_transient_error_then_succeeds(self, monkeypatch):
        adapter = _FakeAdapter(fail_n_times=1, call_result={"ok": True})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        result = run(client.call_tool("t", {}))
        assert result == {"ok": True}
        assert adapter.call_count == 2

    def test_raises_after_exhausting_retries(self, monkeypatch):
        adapter = _FakeAdapter(fail_n_times=99, fail_exc=ConnectionError("still down"))
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        with pytest.raises(ConnectionError, match="still down"):
            run(client.call_tool("t", {}))

    def test_non_retryable_exception_propagates_immediately(self, monkeypatch):
        adapter = _FakeAdapter(fail_n_times=99, fail_exc=ValueError("business logic error"))
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        with pytest.raises(ValueError, match="business logic error"):
            run(client.call_tool("t", {}))
        assert adapter.call_count == 1, "une erreur non-réseau ne doit PAS être retentée"

    def test_content_list_result_is_parsed_as_json(self, monkeypatch):
        class _Chunk:
            text = '{"result": "ok"}'

        result_obj = SimpleNamespace(content=[_Chunk()])
        adapter = _FakeAdapter(call_result=result_obj)
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        assert run(client.call_tool("t", {})) == {"result": "ok"}

    def test_content_list_falls_back_to_python_literal_eval(self, monkeypatch):
        """Régression documentée dans le fichier source : certains tools DB
        sérialisent via `str(dict)` (guillemets simples) plutôt que JSON —
        `ast.literal_eval` doit récupérer la structure SANS corrompre les
        apostrophes internes du texte français."""
        class _Chunk:
            text = "{'message': \"l'exploitation n'a pas trouvé d'offres\"}"

        result_obj = SimpleNamespace(content=[_Chunk()])
        adapter = _FakeAdapter(call_result=result_obj)
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        result = run(client.call_tool("t", {}))
        assert result == {"message": "l'exploitation n'a pas trouvé d'offres"}

    def test_content_list_falls_back_to_raw_text_when_totally_unparseable(self, monkeypatch):
        class _Chunk:
            text = "not json, not a python literal {{{"

        result_obj = SimpleNamespace(content=[_Chunk()])
        adapter = _FakeAdapter(call_result=result_obj)
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        result = run(client.call_tool("t", {}))
        assert "not json" in result

    def test_content_list_scalar_literal_is_not_treated_as_structured_data(self, monkeypatch):
        """`ast.literal_eval("'just text'")` réussit mais renvoie un `str`,
        pas un dict/list — ne doit pas être renvoyé comme si c'était une
        structure ; le texte brut original doit revenir tel quel."""
        class _Chunk:
            text = "'just text'"

        result_obj = SimpleNamespace(content=[_Chunk()])
        adapter = _FakeAdapter(call_result=result_obj)
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        result = run(client.call_tool("t", {}))
        assert result == "'just text'"

    def test_content_list_bare_json_number_is_parsed_as_a_number(self, monkeypatch):
        """`"42"` est du JSON valide (un scalaire) : `json.loads` réussit et
        renvoie l'entier — ce cas ne passe même pas par le repli literal_eval."""
        class _Chunk:
            text = "42"

        result_obj = SimpleNamespace(content=[_Chunk()])
        adapter = _FakeAdapter(call_result=result_obj)
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        assert run(client.call_tool("t", {})) == 42


# =====================================================================
# AgriMCPClient.call_tool — idempotence sur retry (audit MCP/AGUI 2026-08-27)
#
# Sans clé stable, un retry après coupure réseau intermédiaire (la
# connexion tombe APRÈS que l'écriture a réellement atteint la DB, mais
# AVANT que la réponse ne revienne côté client) rejoue create_order/
# initiate_escrow_payment comme un appel entièrement nouveau — indistinguable
# d'une seconde commande légitime pour tout mécanisme de déduplication
# côté serveur qui s'appuierait sur cette clé.
# =====================================================================

class TestAgriMCPClientIdempotencyKey:
    _UUID4_RE = (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )

    def test_a_uuid4_is_generated_when_no_key_is_provided(self, monkeypatch):
        import re

        adapter = _FakeAdapter(call_result={"ok": True})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.call_tool("create_order", {"a": 1}))

        assert len(adapter.received_args) == 1
        key = adapter.received_args[0]["_idempotency_key"]
        assert re.match(self._UUID4_RE, key), f"pas un UUIDv4: {key!r}"

    def test_two_separate_calls_get_two_different_generated_keys(self, monkeypatch):
        adapter = _FakeAdapter(call_result={"ok": True})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.call_tool("create_order", {"a": 1}))
        run(client.call_tool("create_order", {"a": 2}))

        key1 = adapter.received_args[0]["_idempotency_key"]
        key2 = adapter.received_args[1]["_idempotency_key"]
        assert key1 != key2, "deux appels logiques distincts doivent avoir des clés distinctes"

    def test_the_same_generated_key_survives_every_retry_attempt(self, monkeypatch):
        """2e tentative (après une 1re coupure réseau) : même clé."""
        adapter = _FakeAdapter(fail_n_times=1, call_result={"ok": True})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.call_tool("initiate_escrow_payment", {"order_id": "o1"}))

        assert adapter.call_count == 2
        key1 = adapter.received_args[0]["_idempotency_key"]
        key2 = adapter.received_args[1]["_idempotency_key"]
        assert key1 == key2

    def test_the_same_generated_key_survives_all_retries_up_to_exhaustion(self, monkeypatch):
        """3e tentative (2 échecs consécutifs, dernière tentative levée) :
        les 2 essais effectivement tentés portent la même clé — même quand
        l'appel finit par échouer pour de bon."""
        adapter = _FakeAdapter(fail_n_times=99, fail_exc=ConnectionError("still down"))
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        with pytest.raises(ConnectionError):
            run(client.call_tool("create_order", {"a": 1}))

        assert client._MAX_RECONNECT_ATTEMPTS >= 2
        assert len(adapter.received_args) == client._MAX_RECONNECT_ATTEMPTS
        keys = {a["_idempotency_key"] for a in adapter.received_args}
        assert len(keys) == 1, f"une clé différente par tentative: {keys}"

    def test_an_explicit_key_is_kept_verbatim_across_every_attempt(self, monkeypatch):
        adapter = _FakeAdapter(fail_n_times=1, call_result={"ok": True})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.call_tool(
            "create_order", {"a": 1}, idempotency_key="custom-key-123",
        ))

        assert adapter.call_count == 2
        assert adapter.received_args[0]["_idempotency_key"] == "custom-key-123"
        assert adapter.received_args[1]["_idempotency_key"] == "custom-key-123"

    def test_the_key_does_not_overwrite_a_business_argument_of_the_same_shape(self, monkeypatch):
        """Non-régression : `_idempotency_key` est bien un champ dédié — les
        arguments métier fournis par l'appelant restent intacts à côté."""
        adapter = _FakeAdapter(call_result={"ok": True})
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        run(client.call_tool("create_order", {"product": "tomates", "qty": 50}))

        sent = adapter.received_args[0]
        assert sent["product"] == "tomates"
        assert sent["qty"] == 50
        assert "_idempotency_key" in sent


class TestAgriMCPClientHandlers:
    def test_handle_elicitation_declines_by_default(self):
        from ladini.infrastructure.mcp.client import (
            AgriMCPClient,
            ElicitResult,
            MCPTransportConfig,
        )
        client = AgriMCPClient(MCPTransportConfig(kind="stdio"))
        result = run(client._handle_elicitation("msg", None, None, None))
        if ElicitResult:
            assert result.action == "decline"
        else:
            assert result is None

    def test_handle_progress_does_not_raise(self):
        from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
        client = AgriMCPClient(MCPTransportConfig(kind="stdio"))
        run(client._handle_progress(1.0, 10.0, "working"))
        run(client._handle_progress(1.0, None, None))

    def test_handle_message_triggers_refresh_on_list_changed(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        adapter.list_tools_calls = 0
        message = SimpleNamespace(method="notifications/tools/list_changed")
        run(client._handle_message(message))
        assert adapter.list_tools_calls == 1

    def test_handle_message_ignores_unrelated_messages(self, monkeypatch):
        adapter = _FakeAdapter()
        client = _client_with_fake_adapter(monkeypatch, adapter)
        run(client.connect())
        adapter.list_tools_calls = 0
        message = SimpleNamespace(method="notifications/something_else")
        run(client._handle_message(message))
        assert adapter.list_tools_calls == 0
