"""BUSINESS EDITS — sécurité (scripté, CI) et fiabilité du vrai modèle (multi-passes, hors CI).

    # CI (déterministe, aucun réseau) :
    python -m pytest tests/field_corpus/test_business_edits.py -q

    # Vrai modèle, 3 passes par cas critique, rapport dans FIELD_CORPUS_EDIT_REPORT (hors Git) :
    FIELD_CORPUS_REAL=1 FIELD_CORPUS_RUNS=3 REDIS_URL=redis://localhost:6379/15 \
      python -m pytest tests/field_corpus/test_business_edits.py -s -p no:cacheprovider -k real_model

Critère de release : UNSAFE_FAILURE == 0 sur TOUTES les passes (le test échoue sinon). Le taux de réussite directe est MESURÉ et rapporté, pas affirmé.
"""
from __future__ import annotations

import os

import pytest

from tests.field_corpus.business_edits import ALL_CASES
from tests.field_corpus.edit_eval import (
    DIRECT,
    UNSAFE,
    aggregate,
    dump,
    run_case,
    to_report,
)
from tests.field_corpus.harness import REAL

REPORT = os.environ.get("FIELD_CORPUS_EDIT_REPORT", "field_corpus_edit_report.json")
RUNS = int(os.environ.get("FIELD_CORPUS_RUNS", "3"))
_SCRIPTED_CASES = [c for c in ALL_CASES if c.get("scripted")]


@pytest.mark.parametrize("case", _SCRIPTED_CASES, ids=[c["id"] for c in _SCRIPTED_CASES])
def test_scripted_edit_is_applied_exactly_or_not_at_all(case):
    """Le MODÈLE est scripté (y compris FAUX pour les cas `adversarial`) ; résolveur, validateur, domaine et graphe sont réels."""
    if REAL:
        pytest.skip("mode vrai modèle : voir test_real_model_multirun")
    result = run_case(case, 1, real=False)
    assert result is not None
    assert result.outcome != UNSAFE, (result.before, result.after, result.events)
    assert result.outcome == DIRECT, (case["id"], result.before, result.after, result.events, result.reply)


def test_real_model_multirun():
    if not REAL:
        pytest.skip("évaluation du vrai modèle : FIELD_CORPUS_REAL=1")
    results = []
    for run in range(1, RUNS + 1):
        for case in (c for c in ALL_CASES if not c.get("scripted_only")):
            r = run_case(case, run, real=True)
            if r is None:
                continue
            results.append(r)
            print(f"\n## run{run} {r.outcome:<18} {case['id']:<34} {case['utt']!r:<40} raw={r.raw_model[:1]} events={r.events}")
    report = to_report(results, RUNS)
    dump(report, REPORT)
    print("\n## KPIS", report["kpis"], "\n## COUNTS", report["counts"])
    for cid, label in sorted(aggregate(results).items()):
        if label != "STABLE_PASS":
            print(f"## {label:<15} {cid}")
    assert report["counts"]["UNSAFE_FAILURE"] == 0, [k for k, v in aggregate(results).items() if v == "UNSAFE_FAILURE"]
