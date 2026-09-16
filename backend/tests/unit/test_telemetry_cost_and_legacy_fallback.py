"""Incrément G (2026-09-13, "Production LLM Hardening") — compteurs
Prometheus `legacy_fallback` (spec §35) et `llm_cost` (spec §23/§25-28).

Vérifie que `record_generation` incrémente ces compteurs À PARTIR de
`extra_metadata` (sac libre déjà établi) — jamais un crash si les métriques
Prometheus sont absentes (`prometheus_client` non installé/désactivé)."""
from __future__ import annotations

from ladini.core import telemetry


def _reset_and_init_metrics():
    # `_init_prometheus()` est un no-op si déjà initialisé (registre Prometheus
    # global — un ré-enregistrement du même nom lève `ValueError: Duplicated
    # timeseries`) : jamais de reset ici, seulement une init "au moins une
    # fois" — les assertions comparent un AVANT/APRÈS, jamais une valeur
    # absolue, donc l'état cumulé entre tests n'a pas d'importance.
    telemetry._init_prometheus()
    return telemetry._metrics


class TestLegacyFallbackCounter:
    def test_legacy_fallback_true_increments_the_counter(self):
        metrics = _reset_and_init_metrics()
        if not metrics:
            return  # prometheus_client absent dans cet environnement — no-op sûr
        before = metrics["legacy_fallback"].labels(prompt_family="structured_action")._value.get()
        telemetry.record_generation(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            output="{}",
            latency_s=0.1,
            extra_metadata={"legacy_fallback": True, "prompt_family": "structured_action"},
        )
        after = metrics["legacy_fallback"].labels(prompt_family="structured_action")._value.get()
        assert after == before + 1

    def test_legacy_fallback_false_does_not_increment(self):
        metrics = _reset_and_init_metrics()
        if not metrics:
            return
        before = metrics["legacy_fallback"].labels(prompt_family="new_task")._value.get()
        telemetry.record_generation(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            output="{}",
            latency_s=0.1,
            extra_metadata={"legacy_fallback": False, "prompt_family": "new_task"},
        )
        after = metrics["legacy_fallback"].labels(prompt_family="new_task")._value.get()
        assert after == before

    def test_missing_extra_metadata_never_crashes(self):
        _reset_and_init_metrics()
        telemetry.record_generation(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            output="{}",
            latency_s=0.1,
        )  # ne doit jamais lever


class TestCostCounter:
    def test_estimated_cost_usd_increments_the_cost_counter(self):
        metrics = _reset_and_init_metrics()
        if not metrics:
            return
        before = metrics["llm_cost"].labels(prompt_family="new_task", model="test-model")._value.get()
        telemetry.record_generation(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            output="{}",
            latency_s=0.1,
            extra_metadata={"estimated_cost_usd": 0.0025, "prompt_family": "new_task"},
        )
        after = metrics["llm_cost"].labels(prompt_family="new_task", model="test-model")._value.get()
        assert after == before + 0.0025

    def test_missing_cost_does_not_touch_the_counter(self):
        metrics = _reset_and_init_metrics()
        if not metrics:
            return
        before = metrics["llm_cost"].labels(prompt_family="unknown", model="test-model")._value.get()
        telemetry.record_generation(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            output="{}",
            latency_s=0.1,
        )
        after = metrics["llm_cost"].labels(prompt_family="unknown", model="test-model")._value.get()
        assert after == before
