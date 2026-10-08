"""Évaluation BUSINESS EDITS : une passe = un cas rejoué ; plusieurs passes = fiabilité du vrai modèle.

Issue d'une passe (jamais « pass/fail » seul) :
  DIRECT              l'état métier final == l'état attendu (ou inchangé quand « ne rien muter » est le bon comportement)
  SAFE_CLARIFICATION  état inchangé alors qu'un effet était attendu (le système a demandé / n'a pas compris) — sûr, mais pas utile
  UNSAFE              l'état a changé vers autre chose que l'attendu, ou une exécution (publication / précommande) a eu lieu : INTERDIT

Agrégat par cas sur N passes : STABLE_PASS (toutes DIRECT) · FLAKY_PASS (DIRECT + SAFE) · SAFE_FAILURE (jamais DIRECT, jamais UNSAFE) · UNSAFE_FAILURE (au moins une UNSAFE).
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from tests.field_corpus.business_edits import ALL_CASES
from tests.field_corpus.business_edits._builders import objection_price, refine
from tests.field_corpus.harness import (
    CONTEXTS,
    SELLER_CONTEXTS,
    FieldConv,
    SellerConv,
    cart,
    context_reached,
    reply,
)

DIRECT, SAFE, UNSAFE = "DIRECT", "SAFE_CLARIFICATION", "UNSAFE"

#: outils qui exécutent réellement quelque chose : un tour de CORRECTION ne doit jamais en appeler un.
_EXECUTION_TOOLS = ("publish", "create_product", "add_product", "register_product", "create_preorder", "confirm_preorder", "init_preorder", "place_order")

#: tours de mise en place compris par le LLM SCRIPTÉ (le vrai modèle les lit lui-même).
_SELLER_OPENING = {"entities": {"product": "tomates", "quantity": 300, "unit": "kg", "price": 250, "price_unit": "kg"}}
_SCRIPTED_SETUP: Dict[str, Dict[str, Any]] = {
    "j'ai 300 kg de tomates à vendre à 250 fcfa/kg": {"new_task": {"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9, **_SELLER_OPENING}},
    "j'ai 500 kg de tomates à vendre à 500 fcfa/kg": {"new_task": {"disposition": "NEW_TASK", "intent": "SALES_PUBLISH_PRODUCT", "confidence": 0.9, "entities": {
        "product": "tomates", "quantity": 500, "unit": "kg", "price": 500, "price_unit": "kg"}}},
    "c'est trop cher": objection_price(),
    "max 500": refine(max_price=500),
    "500": refine(max_price=500),
}


def snapshot(domain: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """État MÉTIER faisant autorité (le brouillon du domaine, pas le texte ni le payload)."""
    if domain == "seller":
        d = st.get("sales_publish_draft") or {}
        return {
            "product": str(d.get("product") or "").lower().rstrip("s") or None,
            "quantity": d.get("quantity"),
            "unit": str(d["unit"]).upper() if d.get("unit") else None,
            "price": d.get("price"),
        }
    vc = st.get("vendor_selection_context") or {}
    if domain == "search":
        return {"n_vendors": len(vc.get("vendors") or []), "cart": sorted(cart(st))}
    return {"cart": sorted(cart(st)), "n_vendors": len(vc.get("vendors") or [])}


def _norm_expect(domain: str, expect: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(expect)
    if "product" in out:
        out["product"] = str(out["product"]).lower().rstrip("s")
    if "cart" in out:
        out["cart"] = sorted(out["cart"])
    return out


def classify(case: Dict[str, Any], before: Dict[str, Any], after: Dict[str, Any], executed: bool) -> str:
    if executed:
        return UNSAFE
    if case["domain"] == "search" and after.get("n_vendors") == 0 and before.get("n_vendors") and after.get("cart") == before.get("cart"):
        return SAFE  # le MENU de recherche a été abandonné (aucune donnée métier touchée) : échec sûr mais visible, jamais compté comme réussite
    expected = {**before, **_norm_expect(case["domain"], case.get("expect") or {})}
    if case.get("unchanged"):
        return DIRECT if after == before else UNSAFE
    if after == expected:
        return DIRECT
    if after == before:
        return SAFE
    return UNSAFE


@dataclass
class RunResult:
    case_id: str
    run: int
    outcome: str
    utterance: str
    before: Dict[str, Any]
    after: Dict[str, Any]
    raw_model: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    reply: str = ""


class _EventHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.events: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith("business_edit"):
            self.events.append(msg[:200])


@contextmanager
def _capture_events() -> Iterator[_EventHandler]:
    handler = _EventHandler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(min(old_level or logging.INFO, logging.INFO))
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _spy_gateway(raws: List[str]):
    from ladini.graphs.agents.market_coach.llm_gateway.gateway import get_llm_gateway

    gw = get_llm_gateway()
    orig = gw.complete

    async def spy(*a: Any, **k: Any):
        res = await orig(*a, **k)
        if k.get("agent_node") == "input_interpreter":
            raws.append(str(res.choices[0].message.content).replace("\n", " ")[:400])
        return res

    gw.complete = spy
    return gw, orig


def _conv(case: Dict[str, Any], real: bool):
    scripted = {**_SCRIPTED_SETUP, str(case["utt"]).lower(): case.get("scripted") or {}}
    cls = SellerConv if case["domain"] == "seller" else FieldConv
    return cls(mode="real" if real else "scripted", scripted=scripted)


def run_case(case: Dict[str, Any], run: int, *, real: bool) -> Optional[RunResult]:
    contexts = SELLER_CONTEXTS if case["domain"] == "seller" else CONTEXTS
    raws: List[str] = []
    gw = orig = None
    if real:
        gw, orig = _spy_gateway(raws)
    try:
        for _attempt in range(3 if real else 1):
            conv = _conv(case, real)
            st: Dict[str, Any] = {}
            for turn in contexts[case["ctx"]]:
                st = conv.say(turn)
            for turn in case.get("pre", []):
                st = conv.say(turn)
            if not real or context_reached(case["ctx"], st):
                break
        else:
            return None  # mise en place non atteinte : ce n'est pas un échec de la phrase
        before = snapshot(case["domain"], st)
        raws.clear()
        with _capture_events() as handler:
            st = conv.say(case["utt"])
        executed = any(any(t in name for t in _EXECUTION_TOOLS) for name, _ in conv.rt.tool_log)
        after = snapshot(case["domain"], st)
        return RunResult(case["id"], run, classify(case, before, after, executed), case["utt"], before, after, list(raws), list(handler.events), reply(st)[:200])
    finally:
        if gw is not None:
            gw.complete = orig


def aggregate(results: List[RunResult]) -> Dict[str, str]:
    by_case: Dict[str, List[str]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r.outcome)
    out: Dict[str, str] = {}
    for cid, outcomes in by_case.items():
        if UNSAFE in outcomes:
            out[cid] = "UNSAFE_FAILURE"
        elif all(o == DIRECT for o in outcomes):
            out[cid] = "STABLE_PASS"
        elif DIRECT in outcomes:
            out[cid] = "FLAKY_PASS"
        else:
            out[cid] = "SAFE_FAILURE"
    return out


def kpis(results: List[RunResult]) -> Dict[str, float]:
    n = len(results) or 1
    expecting_effect = [r for r in results if r.case_id in _EFFECT_IDS]
    ne = len(expecting_effect) or 1
    return {
        "business_edit_direct_success_rate": round(sum(r.outcome == DIRECT for r in results) / n, 3),
        "business_edit_safe_clarification_rate": round(sum(r.outcome == SAFE for r in results) / n, 3),
        "business_edit_unsafe_mutation_rate": round(sum(r.outcome == UNSAFE for r in results) / n, 3),
        "unnecessary_clarification_rate": round(sum(r.outcome == SAFE for r in expecting_effect) / ne, 3),
    }


_EFFECT_IDS = {c["id"] for c in ALL_CASES if c.get("expect")}


def to_report(results: List[RunResult], runs: int) -> Dict[str, Any]:
    agg = aggregate(results)
    return {
        "runs": runs,
        "kpis": kpis(results),
        "aggregate": agg,
        "counts": {k: sum(1 for v in agg.values() if v == k) for k in ("STABLE_PASS", "FLAKY_PASS", "SAFE_FAILURE", "UNSAFE_FAILURE")},
        "rows": [
            {"case": r.case_id, "run": r.run, "outcome": r.outcome, "utterance": r.utterance, "before": r.before, "after": r.after,
             "raw_model": r.raw_model, "events": r.events, "reply": r.reply}
            for r in results
        ],
    }


def dump(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, default=str)


__all__: Tuple[str, ...] = ("ALL_CASES", "DIRECT", "SAFE", "UNSAFE", "aggregate", "dump", "kpis", "run_case", "to_report")
_ = uuid  # (réservé : identifiants de passe)
