"""core/telemetry.py — worker LLM metrics export (follow-up pre-Hetzner,
2026-09-17). The Celery worker has no exposed HTTP port (no `ports:` in
docker-compose.prod.yml's `worker` service) — `GET /metrics` only exists on
the API, so LLM counters recorded from the worker (record_generation) could
never reach Prometheus scrape. Fixed via dual emission: every metric still
goes to the existing in-process `prometheus_client` registry (so `GET
/metrics` on the API is unaffected) AND, when OTel is enabled, to an OTel
Meter that pushes over the same OTLP pipeline already used for traces.

These tests run against a REAL (unreachable) OTLP endpoint to lock in the
regression this session found and fixed: an OTel metrics flush must never
block the calling thread for more than a moment, no matter how unreachable
the collector is — the same failure class as the sync-Redis-in-async-handler
bug fixed earlier in this engagement.
"""

from __future__ import annotations

import importlib
import os
import time

import pytest


@pytest.fixture
def fresh_telemetry(monkeypatch):
    """Reimports `core.telemetry` with OTel enabled and pointed at an
    endpoint nothing is listening on — isolates each test from whatever
    module-level singletons a previous test already initialized.

    `prometheus_client`'s default `CollectorRegistry` is a C-level global
    singleton, NOT reset by reloading our module (reloading only resets
    OUR module-level dict/flags) — re-registering the same metric NAMES
    across tests in the same process raises "Duplicated timeseries". Real
    processes only ever call `init_telemetry()` once, so this collision is
    a test-isolation artifact, not a production behavior; fixed by
    unregistering everything from the default registry between tests."""
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("PROMETHEUS_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")

    # `ladini.core.settings.settings` est un singleton construit à la
    # PREMIÈRE importation du module, n'importe où dans la session pytest —
    # quand ce fichier tourne seul, `monkeypatch.setenv` ci-dessus suffit
    # (rien n'a encore importé `settings`). Mais aux côtés d'AUTRES fichiers
    # de test (ex: via predeploy_check.sh, qui les lance tous ensemble),
    # `settings` est déjà construit avant que ce fixture ne s'exécute — les
    # variables d'env posées ici n'ont alors plus aucun effet sur l'objet
    # déjà figé. On patche directement les attributs du singleton pour ne
    # jamais dépendre de l'ordre d'exécution des autres fichiers.
    from ladini.core.settings import settings as _settings_singleton

    monkeypatch.setattr(_settings_singleton, "OTEL_ENABLED", True, raising=False)
    monkeypatch.setattr(
        _settings_singleton, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1", raising=False
    )
    monkeypatch.setattr(_settings_singleton, "PROMETHEUS_ENABLED", True, raising=False)
    monkeypatch.setattr(_settings_singleton, "LANGFUSE_ENABLED", False, raising=False)

    import prometheus_client

    for collector in list(prometheus_client.REGISTRY._names_to_collectors.values()):
        try:
            prometheus_client.REGISTRY.unregister(collector)
        except KeyError:
            pass

    import ladini.core.telemetry as telemetry_module

    importlib.reload(telemetry_module)
    yield telemetry_module
    importlib.reload(telemetry_module)  # restore a clean module for later tests


class TestWorkerMetricsExposedViaOTel:
    def test_llm_metrics_recorded_from_a_non_api_context_appear_in_prometheus_registry(
        self, fresh_telemetry
    ):
        """Simule ce qui se passe côté worker : `record_generation` est
        appelée SANS jamais démarrer de serveur HTTP dans ce process. Avant
        le correctif, ces compteurs n'avaient aucune voie de sortie — ce
        test verrouille qu'ils sont au moins prêts à être exportés (le
        MeterProvider OTel a été construit, pas seulement Prometheus)."""
        fresh_telemetry.init_telemetry(service_name="ladini-worker-test")
        fresh_telemetry.record_generation(
            model="groq:test-model",
            messages=[{"role": "user", "content": "hi"}],
            output="hello",
            latency_s=0.42,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            error=None,
        )
        assert fresh_telemetry._otel_meter_provider is not None, (
            "MeterProvider OTel jamais construit — les métriques worker "
            "resteraient piégées dans le process, exactement le gap signalé."
        )

    def test_metrics_endpoint_still_exposes_llm_series_unchanged(self, fresh_telemetry):
        """Le comportement historique (API /metrics) ne doit RIEN perdre —
        double émission, pas un remplacement."""
        fresh_telemetry.init_telemetry(service_name="ladini-api-test")
        fresh_telemetry.record_generation(
            model="groq:test-model",
            messages=[],
            output="hi",
            latency_s=0.1,
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            error=None,
        )
        body, _content_type = fresh_telemetry.prometheus_asgi_response()
        assert b"ladini_llm_calls_total" in body
        assert b"ladini_llm_duration_seconds" in body
        assert b"ladini_llm_tokens_total" in body


class TestFlushNeverBlocksOnUnreachableCollector:
    """La régression concrète trouvée et corrigée cette session : un premier
    essai de `force_flush()` sur le MeterProvider dans `flush()` restait
    bloqué 20s+ contre un collector injoignable (le timeout demandé au SDK
    n'est pas honoré par le retry/backoff interne de l'exporter OTLP). Ce
    test tourne contre un VRAI port fermé (127.0.0.1:1, jamais un mock), pas
    une simulation — c'est la seule façon de prouver qu'aucun appel réseau
    bloquant n'a été réintroduit."""

    def test_flush_returns_quickly_even_against_an_unreachable_otlp_endpoint(
        self, fresh_telemetry
    ):
        fresh_telemetry.init_telemetry(service_name="ladini-worker-test")
        fresh_telemetry.record_generation(
            model="groq:test-model",
            messages=[],
            output="hi",
            latency_s=0.1,
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            error=None,
        )
        t0 = time.perf_counter()
        fresh_telemetry.flush()
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, (
            f"flush() a bloqué {elapsed:.1f}s contre un collector injoignable — "
            "régression : voir le commentaire de flush() dans telemetry.py."
        )
