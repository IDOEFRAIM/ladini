"""Exécute le FIELD CORPUS. Mode scripté (CI) par défaut ; `FIELD_CORPUS_REAL=1` rejoue avec le VRAI modèle et écrit un rapport.

    FIELD_CORPUS_REAL=1 REDIS_URL=redis://localhost:6379/15 python -m pytest tests/field_corpus -s -p no:cacheprovider
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from tests.field_corpus.cases import CASES, DOMAIN_LIMITS, SCRIPTED, SELLER_CASES
from tests.field_corpus.harness import (
    CONTEXTS,
    REAL,
    SELLER_CONTEXTS,
    FieldConv,
    SellerConv,
    business_snapshot,
    context_reached,
    is_blind_redisplay,
    reply,
    seller_snapshot,
)

REPORT = Path(os.environ.get("FIELD_CORPUS_REPORT", "field_corpus_report.json"))
_results: List[Dict[str, Any]] = []


def evaluate(case: Dict[str, Any], before: Dict[str, Any], after: Dict[str, Any], text: str) -> List[str]:
    """Liste des invariants VIOLÉS (vide = cas réussi)."""
    exp = case["expect"]
    failures: List[str] = []
    low = text.lower()
    if "chosen" in exp and after["chosen"] != exp["chosen"]:
        failures.append(f"chosen={after['chosen']!r} attendu {exp['chosen']!r}")
    if "cart" in exp and sorted(after["cart"]) != sorted(exp["cart"]):
        failures.append(f"cart={after['cart']} attendu {exp['cart']}")
    if "vendors" in exp and after["n_vendors"] != exp["vendors"]:
        failures.append(f"vendors={after['n_vendors']} attendu {exp['vendors']}")
    for key, value in (exp.get("slots") or {}).items():
        if after["slots"].get(key) != value:
            failures.append(f"slot {key}={after['slots'].get(key)!r} attendu {value!r}")
    if exp.get("preserved") and before != after:
        failures.append(f"état métier modifié : {before} -> {after}")
    for needle in exp.get("has", []):
        if needle.lower() not in low:
            failures.append(f"réponse sans « {needle} »")
    if exp.get("any_of") and not any(n.lower() in low for n in exp["any_of"]):
        failures.append(f"réponse sans aucun de {exp['any_of']}")
    for needle in exp.get("not_has", []):
        if needle.lower() in low:
            failures.append(f"réponse contient « {needle} »")
    if exp.get("no_redisplay") and is_blind_redisplay(text):
        failures.append("menu rejoué à l'aveugle")
    return failures


def _drive(case: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    for _attempt in range(3 if REAL else 1):
        conv = FieldConv(mode="real" if REAL else "scripted", scripted=SCRIPTED)
        st: Dict[str, Any] = {}
        for turn in CONTEXTS[case["ctx"]]:
            st = conv.say(turn)
        if not REAL or context_reached(case["ctx"], st):
            break
    else:
        pytest.skip(f"contexte {case['ctx']} non atteint après 3 essais (échec de MISE EN PLACE, pas de la phrase)")
    for turn in case.get("pre", []):
        st = conv.say(turn)
    before = business_snapshot(st)
    st = conv.say(case["utt"])
    return before, business_snapshot(st), reply(st)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_field_case(case):
    if not REAL and (case["id"] in DOMAIN_LIMITS or (case["utt"].lower() not in SCRIPTED and case["id"] not in _SCRIPTED_FREE)):
        pytest.skip("pas de compréhension scriptée pour ce cas (mesuré seulement avec le vrai modèle)")
    before, after, text = _drive(case)
    failures = evaluate(case, before, after, text)
    _results.append({"id": case["id"], "utterance": case["utt"], "relation": case["relation"], "ok": not failures, "failures": failures,
                     "reply": text[:240].replace("\n", " / ")})
    if REAL:
        print(f"\n## {'OK ' if not failures else 'KO '} {case['id']:<28} | {case['utt']!r:<44} | {failures or ''} | {text[:110].replace(chr(10), ' / ')!r}")
        REPORT.write_text(json.dumps(_results, ensure_ascii=False, indent=1), encoding="utf-8")
        return  # le vrai modèle est MESURÉ, pas affirmé : le rapport donne le taux, pas un échec de CI
    assert not failures, failures


#: cas déterministes qui ne dépendent pas de la compréhension du modèle (raccourcis fermés / chemins sans LLM).
_SCRIPTED_FREE: set = {"J1_bare_5_quantity", "J2_bare_500_in_menu"}  # chemins fermés (chiffre nu) : sans LLM


_PUBLISH_TOOLS = ("publish", "create_product", "add_product", "register_product")


@pytest.mark.parametrize("case", SELLER_CASES, ids=[c["id"] for c in SELLER_CASES])
def test_seller_field_case(case):
    if not REAL:
        pytest.skip("cas vendeur mesuré avec le vrai modèle (pas de compréhension scriptée)")
    for _attempt in range(3):
        conv = SellerConv(mode="real")
        st: Dict[str, Any] = {}
        for turn in SELLER_CONTEXTS[case["ctx"]]:
            st = conv.say(turn)
        if context_reached(case["ctx"], st):
            break
    else:
        pytest.skip(f"contexte {case['ctx']} non atteint après 3 essais (échec de MISE EN PLACE, pas de la phrase)")
    st = conv.say(case["utt"])
    after = seller_snapshot(st)
    exp = case["expect"]
    failures: List[str] = []
    for key, value in (exp.get("slots") or {}).items():
        got = after["slots"].get(key)
        ok = (str(got).lower().startswith(str(value).lower().rstrip("s")) if isinstance(value, str) else got == value)
        if not ok:
            failures.append(f"slot {key}={got!r} attendu {value!r}")
    if exp.get("no_publish") and any(any(t in name for t in _PUBLISH_TOOLS) for name, _ in conv.rt.tool_log):
        failures.append("un outil de publication a été appelé")
    text = reply(st)
    _results.append({"id": case["id"], "utterance": case["utt"], "relation": case["relation"], "ok": not failures, "failures": failures,
                     "reply": text[:240].replace("\n", " / ")})
    print(f"\n## {'OK ' if not failures else 'KO '} {case['id']:<28} | {case['utt']!r:<44} | {failures or ''} | {text[:110].replace(chr(10), ' / ')!r}")
    REPORT.write_text(json.dumps(_results, ensure_ascii=False, indent=1), encoding="utf-8")
