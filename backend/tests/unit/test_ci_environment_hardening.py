"""Verrouille le comportement du mandat Phase 2, commit 13 : `pytest -q` doit tourner
proprement SANS `GROQ_API_KEY=dummy` ni `OTEL_SDK_DISABLED=true` posés manuellement dans
le shell — les deux étaient auparavant nécessaires (voir `tests/conftest.py` pour le
premier, `tests/unit/test_telemetry_worker_metrics.py` pour le second) :

  - sans `GROQ_API_KEY`, `tests/architecture/test_entry_block_topology.py` et
    `test_cognitive_decisions_are_consumed_or_removed.py` levaient `RuntimeError`
    (`build_graph(role=...)` sans `llm_client` retombe sur `get_llm()`) ;
  - sans `OTEL_SDK_DISABLED`, `tests/unit/test_telemetry_worker_metrics.py` laissait un
    `MeterProvider`/`TracerProvider` réel, pointé sur un endpoint injoignable, sans
    arrêt explicite — thread résiduel, source d'erreurs `RuntimeError: release unlocked
    lock` observées en pleine exécution d'un test SANS RAPPORT, plus tard dans la suite.

Ce test lance un VRAI sous-processus `pytest` sur exactement ces trois fichiers, avec un
environnement d'où les deux variables sont ABSENTES (`env` filtré, pas juste `unset` dans
le shell parent qui ne prouverait rien d'un sous-processus enfant) — la seule façon de
prouver que plus AUCUNE variable manuelle n'est requise, plutôt que de relire le code et
espérer. Borné à 60s : un dépassement est en soi un signal de régression (le hang que ce
commit ferme), pas seulement un échec de test."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]

_TARGET_FILES = (
    "tests/architecture/test_entry_block_topology.py",
    "tests/architecture/test_cognitive_decisions_are_consumed_or_removed.py",
    "tests/unit/test_telemetry_worker_metrics.py",
)


def test_pytest_succeeds_without_groq_api_key_or_otel_sdk_disabled_set():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GROQ_API_KEY", "LADINI_APIKEY", "OTEL_SDK_DISABLED")
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *_TARGET_FILES],
        cwd=str(_BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"pytest a échoué SANS GROQ_API_KEY/OTEL_SDK_DISABLED ambiants "
        f"(returncode={result.returncode}):\nSTDOUT:\n{result.stdout[-4000:]}\n"
        f"STDERR:\n{result.stderr[-2000:]}"
    )
    assert "PytestUnhandledThreadExceptionWarning" not in result.stdout, (
        "un thread d'arrière-plan (OTel) a levé une exception non gérée — "
        "voir test_telemetry_worker_metrics.py::fresh_telemetry"
    )
