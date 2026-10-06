"""Durcissement de la frontière MCP (audit sécurité 2026-08).

Chaque test encode une faille RÉELLE trouvée à l'audit de
`infrastructure/mcp` + `protocols/mcp` + `services/database` :

  1. La porte de sécurité pré-exécution (`_run_preflight`) testait un
     attribut inexistant (`allowed` au lieu de `passed`) — son verdict était
     calculé puis silencieusement jeté : AUCUNE injection n'a jamais été
     bloquée depuis la création du garde-fou.
  2. Les arguments d'outils étaient journalisés BRUTS — donc le code OTP à 4
     chiffres qui débloque les fonds escrow, et les numéros de téléphone.
  3. Le daemon MCP HTTP exposait `POST /call` (exécution de n'importe quel
     outil, identité dérivée du payload) sans AUCUNE authentification.
  4. Les exceptions techniques repartaient brutes vers l'agent (wrapper
     `h.py`) et vers le client HTTP — contournant la barrière anti-fuite de
     `services/database/errors.py`.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import run

# =====================================================================
# 1. PORTE PRÉ-EXÉCUTION — le verdict doit être APPLIQUÉ
# =====================================================================

class TestPreflightGateIsEnforced:
    def test_preflight_result_exposes_passed_not_allowed(self):
        """Contrat verrouillé : c'est la divergence de nom entre `passed` et
        `allowed` qui a rendu la porte inopérante. Si `PreflightResult`
        gagnait un jour un attribut `allowed`, ce test rappellerait qu'un
        appelant historique s'y est déjà fié à tort."""
        from ladini.infrastructure.mcp.security import PreflightResult
        pf = PreflightResult(False, "raison")
        assert hasattr(pf, "passed")
        assert not hasattr(pf, "allowed")

    def test_sql_injection_in_arguments_is_blocked_before_execution(self):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.infrastructure.mcp.security import HostBlockedError

        srv = AgriDBMCPServer()
        with pytest.raises(HostBlockedError):
            run(srv.call_tool("create_order", {"phone": "+2260", "note": "DROP TABLE users"}))

    def test_raw_sql_payload_is_blocked_before_execution(self):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.infrastructure.mcp.security import HostBlockedError

        srv = AgriDBMCPServer()
        with pytest.raises(HostBlockedError):
            run(srv.call_tool("get_orders", {"phone": "+2260", "q": "SELECT * FROM users WHERE 1=1"}))

    def test_legitimate_business_arguments_are_not_blocked(self):
        """Anti-faux-positif : ré-activer une porte restée morte depuis sa
        création ne doit pas se payer par le blocage d'appels normaux. Le
        préflight ne doit pas lever `HostBlockedError` ici (un échec DB en
        aval est attendu et sans rapport)."""
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.infrastructure.mcp.security import HostBlockedError

        srv = AgriDBMCPServer()
        try:
            run(srv.call_tool("get_user_by_phone", {"phone": "+22668815299"}))
        except HostBlockedError as exc:
            pytest.fail(f"faux positif du préflight sur un appel légitime : {exc}")
        except Exception:
            pass  # échec DB/infra : hors sujet pour ce test


# =====================================================================
# 1bis. OUTIL MCP "PHOTO PRODUIT" — déclaration + résolution
# =====================================================================

class TestProductPhotoTool:
    """Feature "photo produit par WhatsApp" — le nouvel outil `add_product_photo`
    doit être exposé, avoir un scope EXPLICITE (pas deviné) et se résoudre/
    s'exécuter sans jamais être bloqué par la porte de sécurité."""

    def test_add_product_photo_is_exposed_as_an_mcp_tool(self):
        from ladini.protocols.mcp.servers.h import TOOL_HANDLERS
        assert "add_product_photo" in TOOL_HANDLERS

    def test_add_product_photo_has_an_explicit_write_scope(self):
        from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP, PermissionScope
        assert TOOL_SCOPE_MAP.get("add_product_photo") == PermissionScope.DB_DATA_WRITE

    def test_add_product_photo_resolves_and_dispatches_without_being_blocked(self):
        from ladini.infrastructure.mcp.runtime import AgriDBMCPServer
        from ladini.infrastructure.mcp.security import (
            HostBlockedError,
            PermissionDenied,
        )

        srv = AgriDBMCPServer()
        try:
            run(srv.call_tool("add_product_photo", {
                "phone": "+22668815299",
                "product_id": "00000000-0000-0000-0000-000000000000",
                "image_url": "https://example.supabase.co/storage/v1/object/public/product-photos/x.jpg",
            }))
        except (HostBlockedError, PermissionDenied) as exc:
            pytest.fail(f"le nouvel outil add_product_photo est bloqué par la porte de sécurité : {exc}")
        except Exception:
            pass  # échec DB/infra (pas de produit réel) : hors sujet pour ce test


# =====================================================================
# 2. MASQUAGE DES SECRETS DANS LES LOGS
# =====================================================================

class TestLogMasking:
    def test_otp_code_is_fully_redacted(self):
        from ladini.infrastructure.mcp.utils import mask_log_args
        assert mask_log_args({"otp_code": "1234"})["otp_code"] == "***"

    @pytest.mark.parametrize("key", [
        "phone", "user_phone", "producer_phone", "buyer_phone", "_caller_phone",
    ])
    def test_phone_variants_are_partially_masked(self, key):
        from ladini.infrastructure.mcp.utils import mask_log_args
        masked = mask_log_args({key: "+22668815299"})[key]
        assert masked == "***5299"
        assert "2266881" not in masked

    def test_business_fields_are_left_readable_for_debugging(self):
        from ladini.infrastructure.mcp.utils import mask_log_args
        masked = mask_log_args({"product": "carottes", "quantity": 500, "price": 300})
        assert masked == {"product": "carottes", "quantity": 500, "price": 300}

    def test_nested_envelopes_are_masked_too(self):
        """Les outils reçoivent souvent une enveloppe `data={...}` — le
        masquage doit descendre dedans, sinon le secret fuit d'un cran."""
        from ladini.infrastructure.mcp.utils import mask_log_args
        masked = mask_log_args({"data": {"otp_code": "9999", "phone": "+22670000001"}})
        assert masked["data"]["otp_code"] == "***"
        assert masked["data"]["phone"] == "***0001"

    def test_masking_never_mutates_the_caller_payload(self):
        """Le masquage sert au LOG : muter l'original casserait l'appel réel."""
        from ladini.infrastructure.mcp.utils import mask_log_args
        original = {"otp_code": "1234", "phone": "+22668815299"}
        mask_log_args(original)
        assert original == {"otp_code": "1234", "phone": "+22668815299"}

    def test_depth_limit_stops_pathological_nesting(self):
        from ladini.infrastructure.mcp.utils import mask_log_args
        deep: dict = {"a": {"b": {"c": {"d": {"e": "trop profond"}}}}}
        assert mask_log_args(deep)  # borné, ne boucle pas

    def test_cyclic_structure_does_not_hang_the_logger(self):
        from ladini.infrastructure.mcp.utils import mask_log_args
        cyclic: dict = {"phone": "+22668815299"}
        cyclic["self"] = cyclic
        assert mask_log_args(cyclic)["phone"] == "***5299"


# =====================================================================
# 3. DAEMON MCP HTTP — authentification + validation d'entrée
#
# Audit MCP/AGUI 2026-08-27 : le contrat REST maison (GET /tools,
# POST /call) a été remplacé par le transport MCP Streamable HTTP officiel
# (JSON-RPC 2.0) sur un unique endpoint /mcp — voir http_server.py.
# =====================================================================

_ACCEPT_HEADERS = {"Accept": "application/json, text/event-stream"}


def _jsonrpc_call(client, method, headers, *, params=None, id_=1):
    """Requête JSON-RPC minimale sur /mcp (Accept requis par le spec
    Streamable HTTP — sans lui le serveur répond 406, indépendamment de
    l'authentification)."""
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": method, "id": id_, "params": params or {}},
        headers={**headers, **_ACCEPT_HEADERS},
    )


@pytest.fixture(scope="module")
def http_app_client():
    """Un SEUL client pour tout ce fichier.

    `session_manager` (http_server.py) est un singleton module-level dont
    `.run()` — appelé par le `lifespan` FastAPI à l'entrée du
    `TestClient` — ne peut être invoqué qu'UNE SEULE fois par processus
    (contrat du SDK MCP : `StreamableHTTPSessionManager.run() can only be
    called once per instance`). Ré-ouvrir un `TestClient(hs.app)` à chaque
    test lèverait `RuntimeError` dès le deuxième. Un seul client, réutilisé
    par tous les tests, reflète d'ailleurs la réalité de production : ce
    daemon démarre une fois et sert tous les tours ensuite.
    """
    from fastapi.testclient import TestClient

    import ladini.protocols.mcp.servers.http_server as hs

    with TestClient(hs.app) as client:
        yield client


@pytest.fixture()
def http_client(http_app_client, monkeypatch):
    """Client + en-tête d'authentification — le jeton est ré-évalué à
    CHAQUE requête par `require_auth` (jamais mis en cache au démarrage),
    donc le faire varier par test via `monkeypatch` reste sûr même avec un
    client/app partagé pour tout le fichier."""
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "MCP_HTTP_AUTH_TOKEN", "secret-de-test", raising=False)
    return http_app_client, {"Authorization": "Bearer secret-de-test"}


class TestHttpDaemonAuth:
    def test_mcp_without_a_token_is_rejected(self, http_client):
        client, _ = http_client
        r = _jsonrpc_call(client, "tools/list", {})
        assert r.status_code == 401

    def test_mcp_with_a_wrong_token_is_rejected(self, http_client):
        client, _ = http_client
        r = _jsonrpc_call(client, "tools/list", {"Authorization": "Bearer faux"})
        assert r.status_code == 401

    def test_x_mcp_token_header_is_accepted_as_an_alternative(self, http_client):
        client, _ = http_client
        r = _jsonrpc_call(client, "tools/list", {"X-MCP-Token": "secret-de-test"})
        assert r.status_code == 200

    def test_health_endpoint_stays_open_for_supervision(self, http_client):
        """systemd/load balancer doivent pouvoir sonder sans secret — /health
        ne révèle aucune donnée métier."""
        client, _ = http_client
        assert client.get("/health").status_code == 200

    def test_no_token_configured_keeps_existing_deployments_working(self, http_app_client, monkeypatch):
        """Rétro-compatibilité assumée : sans secret configuré le daemon
        répond toujours (un CRITICAL a déjà été journalisé au démarrage du
        process — `require_auth` lit `MCP_HTTP_AUTH_TOKEN` à CHAQUE requête,
        pas seulement au lifespan, donc ce test n'a pas besoin de relancer
        le daemon pour le vérifier)."""
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "MCP_HTTP_AUTH_TOKEN", "", raising=False)
        assert _jsonrpc_call(http_app_client, "tools/list", {}).status_code == 200


class TestHttpDaemonInputValidation:
    def test_malformed_json_yields_400_not_500(self, http_client):
        client, headers = http_client
        r = client.post(
            "/mcp", content=b"{pas du json",
            headers={**headers, **_ACCEPT_HEADERS, "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_missing_jsonrpc_method_is_rejected(self, http_client):
        """Le SDK MCP applique lui-même la validation du schéma JSON-RPC —
        une requête sans `method` reste refusée proprement (400), pas une
        500 opaque."""
        client, headers = http_client
        r = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1},
            headers={**headers, **_ACCEPT_HEADERS},
        )
        assert r.status_code == 400

    def test_missing_accept_header_is_rejected_per_spec(self, http_client):
        """Streamable HTTP exige `Accept: application/json, text/event-stream`
        — un client qui ne le déclare pas est rejeté (406), avant même
        d'atteindre la logique métier."""
        client, headers = http_client
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}},
            headers=headers,
        )
        assert r.status_code == 406

    def test_oversized_body_is_rejected(self, http_client):
        client, headers = http_client
        payload = (
            b'{"jsonrpc":"2.0","method":"tools/call","id":1,"params":{"name":"x",'
            b'"arguments":{"p":"' + b"a" * 300_000 + b'"}}}'
        )
        r = client.post(
            "/mcp", content=payload,
            headers={**headers, **_ACCEPT_HEADERS, "Content-Type": "application/json"},
        )
        assert r.status_code == 413


# =====================================================================
# 4. BARRIÈRE ANTI-FUITE — aucune trace technique vers l'extérieur
# =====================================================================

class TestTechnicalErrorsNeverLeak:
    def test_http_call_failure_returns_a_sanitized_message(self, http_client, monkeypatch):
        import ladini.protocols.mcp.servers.http_server as hs

        async def _boom(name, arguments=None, **kw):
            raise RuntimeError('relation "orders" does not exist LINE 1: SELECT * FROM orders')

        monkeypatch.setattr(hs.mcp_entry_point.backend, "call_tool", _boom)
        client, headers = http_client
        r = _jsonrpc_call(client, "tools/call", headers, params={"name": "get_orders", "arguments": {}})

        body = _sse_json_result(r)
        payload = json.loads(body["content"][0]["text"])
        assert payload["ok"] is False
        assert "relation" not in payload["error"]
        assert "SELECT" not in payload["error"]

    def test_business_errors_still_reach_the_caller_intact(self, http_client, monkeypatch):
        """La sanitisation ne doit pas rendre l'agent aveugle : un échec
        métier explicite doit traverser mot pour mot."""
        import ladini.protocols.mcp.servers.http_server as hs
        from ladini.services.database.errors import BusinessRuleException

        async def _business(name, arguments=None, **kw):
            raise BusinessRuleException("Stock insuffisant : il reste 12 KG")

        monkeypatch.setattr(hs.mcp_entry_point.backend, "call_tool", _business)
        client, headers = http_client
        # Le SDK MCP valide les arguments contre le JSON Schema déclaré
        # AVANT d'atteindre call_tool — `add_stock` exige farm_id/item_name/
        # quantity, sans quoi la requête est rejetée en amont du mock.
        # `producer_phone` est requis depuis l'audit sécurité agent 2026-09-10
        # (contrôle de propriété sur `farm_id` — voir
        # `BaseMixin._assert_farm_owned_by`). Le schéma JSON exposé par MCP est
        # dérivé de la signature : l'omettre fait rejeter l'appel EN AMONT du
        # handler, donc avant le mock que ce test veut exercer.
        args = {
            "farm_id": "f1",
            "item_name": "tomates",
            "quantity": 10,
            "producer_phone": "+22670000001",
        }
        r = _jsonrpc_call(client, "tools/call", headers, params={"name": "add_stock", "arguments": args})

        body = _sse_json_result(r)
        payload = json.loads(body["content"][0]["text"])
        assert "Stock insuffisant" in payload["error"]


def _sse_json_result(response):
    """Extrait le corps JSON-RPC d'une réponse Streamable HTTP — le SDK MCP
    répond en flux `text/event-stream` (une ligne `data: {...}`) même pour
    un résultat unique et immédiat."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])["result"]
    raise AssertionError(f"aucune ligne 'data:' dans la réponse SSE: {response.text!r}")

    def test_tool_wrapper_sanitizes_technical_failures(self):
        """`h.py::_safe` interpolait l'exception brute — 2e surface de fuite
        décrite dans `services/database/errors.py`."""
        from ladini.protocols.mcp.servers.h import _safe

        @_safe("some_tool")
        async def _boom():
            raise RuntimeError('duplicate key value violates unique constraint "bids_pkey"')

        result = run(_boom())
        assert result["status"] == "error"
        assert "constraint" not in result["message"]
        assert "bids_pkey" not in result["message"]

    def test_tool_wrapper_no_longer_exposes_internal_exception_class_names(self):
        from ladini.protocols.mcp.servers.h import _safe

        @_safe("some_tool")
        async def _boom():
            raise RuntimeError("boom")

        assert "error_type" not in run(_boom())

    def test_tool_wrapper_keeps_business_messages_intact(self):
        from ladini.protocols.mcp.servers.h import _safe
        from ladini.services.database.errors import BusinessRuleException

        @_safe("some_tool")
        async def _business():
            raise BusinessRuleException("Quantité négative interdite")

        assert "Quantité négative interdite" in run(_business())["message"]


# =====================================================================
# 5. RÉSILIENCE — aucun appel externe sans timeout
# =====================================================================

class TestExternalCallsAreBounded:
    """Le SDK Twilio construit par défaut `TwilioHttpClient(timeout=None)` :
    AUCUN timeout. Une connexion suspendue immobilisait indéfiniment le
    thread appelant — un worker Celery pour le sender principal, un thread du
    pool + le dispatcher pour le cron outbox."""

    def test_twilio_sdk_default_really_has_no_timeout(self):
        """Justifie le correctif : si Twilio changeait ce défaut un jour, ce
        test le signalerait au lieu de laisser croire que le nôtre est inutile."""
        from twilio.http.http_client import TwilioHttpClient
        assert TwilioHttpClient().timeout is None

    def test_main_sender_passes_an_explicit_timeout(self, monkeypatch):
        import ladini.services.twilio_sender as ts

        seen = {}

        class _FakeClient:
            def __init__(self, sid, token, http_client=None):
                seen["timeout"] = getattr(http_client, "timeout", None)
                self.messages = type(
                    "M", (), {"create": staticmethod(lambda **kw: type("R", (), {"sid": "SM1"})())},
                )()

        monkeypatch.setattr(ts, "Client", _FakeClient)
        ts.send_whatsapp_message("+22668815299", "bonjour")
        assert seen["timeout"] == ts._TIMEOUT_S
        assert seen["timeout"] is not None

    def test_outbox_channel_passes_an_explicit_timeout(self, monkeypatch):
        import twilio.rest

        from ladini.workers.outbox.channels.whatsapp import (
            _TWILIO_TIMEOUT_S,
            WhatsAppChannel,
        )

        seen = {}

        class _FakeClient:
            def __init__(self, sid, token, http_client=None):
                seen["timeout"] = getattr(http_client, "timeout", None)
                self.messages = type(
                    "M", (), {"create": staticmethod(lambda **kw: type("R", (), {"sid": "SM1"})())},
                )()

        monkeypatch.setattr(twilio.rest, "Client", _FakeClient)
        WhatsAppChannel()._send_sync_twilio("+22668815299", "bonjour")
        assert seen["timeout"] == _TWILIO_TIMEOUT_S

    def test_sender_masks_the_recipient_number_in_logs(self, monkeypatch, caplog):
        import logging

        import ladini.services.twilio_sender as ts

        class _FakeClient:
            def __init__(self, sid, token, http_client=None):
                self.messages = type(
                    "M", (), {"create": staticmethod(lambda **kw: type("R", (), {"sid": "SM1"})())},
                )()

        monkeypatch.setattr(ts, "Client", _FakeClient)
        with caplog.at_level(logging.INFO, logger="Ladini.TwilioSender"):
            ts.send_whatsapp_message("+22668815299", "bonjour")
        assert "+22668815299" not in caplog.text
        assert "***5299" in caplog.text
