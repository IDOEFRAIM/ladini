"""Onboarding — modèle géographique RÉGION (17 régions du Burkina Faso).

Verrouille : source unique (`domain/burkina_regions.py`), résolution chef-lieu/surnom/langage
naturel -> région canonique, validation stricte, onboarding de bout en bout pour chaque
région, aucune question de sous-zone, héritage Kadiogo/Guiriko préservé.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict

import pytest

from ladini.domain.burkina_regions import (
    REGIONS,
    canonical_region,
    normalize_place,
    resolve_region,
)
from ladini.graphs.agents.market_coach.flows.common.onboarding import onboarding_node
from tests.conftest import run

CAPITAL_TO_REGION = {
    "Dédougou": "Bankui", "Gaoua": "Djôrô", "Fada N'Gourma": "Goulmou",
    "Fada N’Gourma": "Goulmou", "Bobo-Dioulasso": "Guiriko", "Ouagadougou": "Kadiogo",
    "Kaya": "Kuilsé", "Dori": "Liptako", "Koudougou": "Nando", "Tenkodogo": "Nakambé",
    "Manga": "Nazinon", "Ziniaré": "Oubri", "Bogandé": "Sirba", "Djibo": "Soum",
    "Banfora": "Tannounyan", "Diapaga": "Tapoa", "Tougan": "Sourou", "Ouahigouya": "Yaadga",
}


def test_there_are_exactly_17_canonical_regions_with_stable_slugs():
    assert len(REGIONS) == 17
    assert len({r.slug for r in REGIONS}) == 17 and len({r.name for r in REGIONS}) == 17
    assert all(r.capital for r in REGIONS)


@pytest.mark.parametrize("region", REGIONS, ids=lambda r: r.slug)
def test_every_canonical_region_is_accepted_by_name_and_slug(region):
    assert canonical_region(region.name) is region
    assert canonical_region(region.slug) is region
    assert canonical_region(region.name.upper()) is region
    assert resolve_region(region.name).region is region


@pytest.mark.parametrize("capital,expected", CAPITAL_TO_REGION.items())
def test_a_capital_resolves_to_its_region(capital, expected):
    result = resolve_region(capital)
    assert result.status == "RESOLVED" and result.region.name == expected


@pytest.mark.parametrize("bad", ["Atlantide", "", "xyz", "region-42", "Casablanca", "Saaba"])
def test_unknown_or_forged_values_are_rejected_by_the_strict_validator(bad):
    assert canonical_region(bad) is None


def test_the_strict_validator_does_not_accept_a_capital_or_nickname():
    assert canonical_region("Ouagadougou") is None  # chef-lieu : résolution, pas valeur valide
    assert canonical_region("Bobo") is None


@pytest.mark.parametrize(
    "a,b",
    [
        ("djoro", "Djôrô"), ("DJORO", "Djôrô"), ("goulmou", "Goulmou"), ("fada", "Goulmou"),
        ("Fada N Gourma", "Goulmou"), ("Fada N’Gourma", "Goulmou"), ("kuilse", "Kuilsé"),
        ("NAKAMBE", "Nakambé"), ("Gwiriko", "Guiriko"),
    ],
)
def test_case_accents_and_apostrophes_converge(a, b):
    assert resolve_region(a).region.name == b


@pytest.mark.parametrize(
    "text,expected",
    [
        ("je suis à Ouaga", "Kadiogo"), ("j'habite à Bobo", "Guiriko"),
        ("je suis vers Kaya", "Kuilsé"), ("je suis dans le Kadiogo", "Kadiogo"),
        ("je suis à Banfora", "Tannounyan"), ("je viens de Dori", "Liptako"),
        ("Je suis dans le Nando.", "Nando"),
        ("je suis actuellement à Casablanca mais mon exploitation est à Bobo", "Guiriko"),
    ],
)
def test_natural_language_resolves_to_the_canonical_region(text, expected):
    assert resolve_region(text).region.name == expected


def test_ambiguous_inputs_are_never_resolved_arbitrarily():
    assert resolve_region("je suis au Burkina").status == "AMBIGUOUS"
    both = resolve_region("entre Ouaga et Bobo")
    assert both.status == "AMBIGUOUS" and both.region is None and len(both.candidates) == 2
    assert resolve_region("Casablanca").status == "UNKNOWN"


def test_normalize_place_folds_case_accents_and_apostrophes():
    assert normalize_place("  Fada N’GOURMA ") == "fada n gourma"


# ---------------------------------------------------------------------------
# Onboarding de bout en bout
# ---------------------------------------------------------------------------


class _Runtime:
    """DB minimale : `legacy_roots` simule des zones racines déjà en base (ex. Kadiogo)."""

    def __init__(self, legacy_roots: Dict[str, str] | None = None):
        self.llm = object()
        self.legacy = {k.upper(): v for k, v in (legacy_roots or {}).items()}
        self.created: list[Dict[str, Any]] = []

    async def call_db(self, tool_name: str, **kwargs: Any):
        if tool_name == "get_zone_by_name":
            zid = self.legacy.get(str(kwargs.get("name") or "").upper())
            if zid:
                return {"status": "success", "data": {"id": zid, "name": kwargs["name"]}}
            return {"status": "error", "message": "introuvable"}
        if tool_name == "get_zone_hierarchy_by_name":
            return {"status": "error", "message": "introuvable"}
        if tool_name == "create_user_profile":
            self.created.append(kwargs["data"])
            return {"status": "success", "data": {"id": "u1"}}
        if tool_name == "get_user_by_phone":
            return {"data": {"id": "u1"}}
        raise AssertionError(f"outil inattendu: {tool_name}")


def _patch_llm(monkeypatch, outputs):
    calls = {"n": 0}

    async def _fake(mc_runtime, text, context_hint=""):
        out = outputs[min(calls["n"], len(outputs) - 1)]
        calls["n"] += 1
        return {"role": None, "name": None, "zone": None, "confirm": None,
                "is_question": False, "reply": None, **out}

    import ladini.graphs.agents.market_coach.flows.common.onboarding as mod

    monkeypatch.setattr(mod, "_llm_extract_onboarding_all", _fake)


def _carry(out: Dict[str, Any], text: str) -> Dict[str, Any]:
    return {
        "is_onboarding": True, "user_phone": "+22670000001", "normalized_text": text,
        "onboarding_internal_step": out["onboarding_internal_step"],
        "onboarding_profile": out["onboarding_profile"],
        "transaction_payload": out["transaction_payload"],
    }


@pytest.mark.parametrize("region", REGIONS, ids=lambda r: r.slug)
def test_onboarding_completes_with_every_one_of_the_17_regions(monkeypatch, region):
    """Via le chef-lieu : la région canonique est stockée, sans sous-zone, sans ligne DB."""
    rt = _Runtime()
    _patch_llm(monkeypatch, [{"role": "PRODUCER", "name": "Awa", "zone": region.capital}])
    state = {"is_onboarding": True, "user_phone": "+22670000001",
             "normalized_text": f"Awa producteur a {region.capital}"}
    r1 = run(onboarding_node(state, rt))
    assert r1["onboarding_profile"]["zone_name"] == region.name
    assert r1["onboarding_profile"]["declared_location"] == region.name  # valeur canonique
    assert r1["onboarding_profile"]["coverage_status"] == "COVERED"
    assert "sous-zone" not in r1["onboarding_prompt"].lower()
    assert "sous zone" not in r1["onboarding_prompt"].lower()
    assert r1["onboarding_step"] == "COMPLETED"  # récap atteint : la région suffit

    r2 = run(onboarding_node(_carry(r1, "oui"), rt))
    assert r2["status"] == "SUCCESS"
    assert rt.created[-1]["declared_location"] == region.name


def test_bobo_in_a_sentence_becomes_guiriko_and_onboarding_goes_on(monkeypatch):
    rt = _Runtime()
    _patch_llm(monkeypatch, [{"role": "BUYER", "zone": "Bobo"}])
    state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "je suis à Bobo"}
    r1 = run(onboarding_node(state, rt))
    assert r1["onboarding_profile"]["zone_name"] == "Guiriko"
    assert "Guiriko" in r1["onboarding_prompt"]
    assert "sous-zone" not in r1["onboarding_prompt"].lower()
    assert "appelles" in r1["onboarding_prompt"].lower()  # il reste le nom à demander


def test_country_only_asks_the_region_and_never_picks_kadiogo(monkeypatch):
    rt = _Runtime()
    _patch_llm(monkeypatch, [{"role": "BUYER", "name": "Awa", "zone": "Burkina"}])
    state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "je suis au Burkina"}
    r1 = run(onboarding_node(state, rt))
    assert r1["onboarding_profile"]["zone_name"] is None
    assert r1["onboarding_profile"]["coverage_status"] in (None, "")
    assert "quelle *région*" in r1["onboarding_prompt"].lower()
    assert "Kadiogo" in r1["onboarding_prompt"]  # proposée dans la liste, jamais choisie


@pytest.mark.parametrize("name,zid", [("Kadiogo", "z-kad"), ("Guiriko", "z-gui")])
def test_legacy_root_zones_kadiogo_and_guiriko_keep_working(monkeypatch, name, zid):
    rt = _Runtime({name: zid})
    _patch_llm(monkeypatch, [{"role": "BUYER", "name": "Awa", "zone": name}])
    state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": f"Awa a {name}"}
    r1 = run(onboarding_node(state, rt))
    assert r1["onboarding_profile"]["zone_id"] == zid  # lié à la zone opérationnelle existante
    assert r1["onboarding_profile"]["zone_name"] == name


def test_a_region_without_operational_zone_row_is_not_blocked(monkeypatch):
    rt = _Runtime({"Kadiogo": "z-kad"})
    _patch_llm(monkeypatch, [{"role": "BUYER", "name": "Awa", "zone": "Nando"}])
    state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "Awa a Nando"}
    r1 = run(onboarding_node(state, rt))
    assert r1["onboarding_profile"]["zone_name"] == "Nando"
    assert r1["onboarding_profile"]["zone_id"] is None
    assert r1["onboarding_step"] == "COMPLETED"


def test_onboarding_never_reads_or_asks_a_sub_zone():
    """La sous-zone n'est jamais requise : ni l'agent d'onboarding ni son pont LangGraph ne
    la lisent (un utilisateur existant avec sous-zone n'est donc jamais réinterrogé)."""
    import ladini.agents.onboarding as ob
    import ladini.graphs.agents.market_coach.flows.common.onboarding as bridge

    for module in (ob, bridge):
        src = inspect.getsource(module).lower()
        assert "sub_zone" not in src and "subzone" not in src
        assert "quelle sous-zone" not in src
