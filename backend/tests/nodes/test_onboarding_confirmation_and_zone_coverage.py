"""Mandat onboarding 2026-09-26 — "ZONE NON COUVERTE + CONFIRMATION DE CRÉATION DE PROFIL".

Deux bugs réels reproduits puis corrigés, testés ici sur `onboarding_node` (le pont
LangGraph réel, pas seulement `run_onboarding_step` en isolation — voir
`test_onboarding_node_state_roundtrip.py` pour le contrat général de ce pont) :

1. Une localité non couverte ("Somgandé") bloquait l'onboarding avec un message sec
   ("Je n'ai pas trouve la region...") au lieu de conserver la déclaration et de
   continuer (voir `TestZoneCoverageNeverBlocksOnboarding`).
2. "ok" (et tout le vocabulaire canonique de confirmation) ne confirmait pas le profil
   — seul "je confirme" fonctionnait, un second passage était nécessaire (voir
   `TestDeterministicConfirmationDuringProfileConfirmation`).

`TestFullReportedScenario` rejoue exactement l'échange rapporté en production.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from ladini.graphs.agents.market_coach.flows.common.onboarding import onboarding_node
from tests.conftest import run


class _FakeRuntime:
    """`zones={"region": {"NAME_UPPER": {...}}, "hierarchy": {"NAME_UPPER": {...root...}}}`
    — miroir minimal de `get_zone_by_name`/`get_zone_hierarchy_by_name` (voir
    `services/database/base.py`), jamais le vrai moteur SQL (couvert séparément par
    `tests/unit/test_onboarding_zone_region_level.py`)."""

    def __init__(self, *, zones: Dict[str, Any] | None = None):
        self.llm = object()
        self._zones = zones or {}

    async def call_db(self, tool_name: str, **kwargs: Any):
        if tool_name == "get_zone_by_name":
            hit = self._zones.get("region", {}).get(str(kwargs.get("name") or "").upper())
            if hit:
                return {"status": "success", "data": {"id": hit["id"], "name": hit["name"]}}
            return {"status": "error", "message": "introuvable"}
        if tool_name == "get_zone_hierarchy_by_name":
            name = str(kwargs.get("name") or "")
            hit = self._zones.get("hierarchy", {}).get(name.upper())
            if hit:
                return {
                    "status": "success",
                    "data": {"matched": {"id": "child-1", "name": name}, "root": hit},
                }
            return {"status": "error", "message": "introuvable"}
        if tool_name == "create_user_profile":
            return {"status": "success", "data": {"id": "u1"}}
        if tool_name == "get_user_by_phone":
            return {"data": {"id": "u1"}}
        raise AssertionError(f"outil inattendu: {tool_name}")


def _patch_llm_extract(monkeypatch, outputs):
    """`outputs` : liste de dicts consommés dans l'ordre, un par appel LLM."""
    calls = {"n": 0}

    async def _fake(mc_runtime, text, context_hint=""):
        i = min(calls["n"], len(outputs) - 1)
        calls["n"] += 1
        base = {
            "role": None, "name": None, "zone": None, "confirm": None,
            "is_question": False, "reply": None,
        }
        base.update(outputs[i])
        return base

    import ladini.graphs.agents.market_coach.flows.common.onboarding as mod
    monkeypatch.setattr(mod, "_llm_extract_onboarding_all", _fake)


def _forbid_llm_extract(monkeypatch):
    """Le moindre appel fait échouer le test — preuve que le fast-path déterministe a
    intercepté le message SANS jamais consulter le LLM (mandat : "un mot exact ne doit
    jamais coûter un appel LLM")."""

    async def _fake(mc_runtime, text, context_hint=""):
        raise AssertionError(
            f"le fast-path déterministe aurait dû intercepter {text!r} sans appeler le LLM"
        )

    import ladini.graphs.agents.market_coach.flows.common.onboarding as mod
    monkeypatch.setattr(mod, "_llm_extract_onboarding_all", _fake)


def _carry(state_out: Dict[str, Any], *, text: str, **extra: Any) -> Dict[str, Any]:
    """Construit l'état du tour SUIVANT à partir du patch renvoyé par `onboarding_node`,
    exactement ce que le checkpointer LangGraph ferait persister/relire."""
    return {
        "is_onboarding": True,
        "user_phone": "+22670000001",
        "normalized_text": text,
        "onboarding_internal_step": state_out["onboarding_internal_step"],
        "onboarding_profile": state_out["onboarding_profile"],
        "transaction_payload": state_out["transaction_payload"],
        **extra,
    }


# =====================================================================
# Problème 1 — zone non couverte
# =====================================================================


class TestZoneCoverageNeverBlocksOnboarding:
    def test_a_covered_zone_resolves_directly_and_onboarding_proceeds(self, monkeypatch):
        _patch_llm_extract(monkeypatch, [{"role": "BUYER", "name": "Awa", "zone": "Ouagadougou"}])
        rt = _FakeRuntime(zones={"region": {"OUAGADOUGOU": {"id": "z-ouaga", "name": "Ouagadougou"}}})
        state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "Awa, a Ouagadougou"}
        result = run(onboarding_node(state, rt))
        assert result["onboarding_profile"]["zone_id"] == "z-ouaga"
        assert result["onboarding_profile"]["coverage_status"] == "COVERED"
        assert result["onboarding_step"] == "COMPLETED"

    def test_a_locality_attached_to_a_known_region_is_accepted_never_rejected(self, monkeypatch):
        """"Somgandé" ne résout à aucune région directement, mais EST une localité connue
        rattachée à "Ouagadougou" dans le référentiel (`get_zone_hierarchy_by_name`) — jamais
        un rejet sec, jamais un rattachement inventé (voir le stub : rien n'est en dur ici,
        c'est le stub qui SIMULE que le référentiel connaît déjà ce rattachement)."""
        _patch_llm_extract(monkeypatch, [{"role": "PRODUCER", "name": "Gilbert-prod", "zone": "Somgande"}])
        rt = _FakeRuntime(zones={"hierarchy": {"SOMGANDE": {"id": "z-ouaga", "name": "Ouagadougou"}}})
        state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "Gilbert-prod a Somgande"}
        result = run(onboarding_node(state, rt))
        assert result["onboarding_profile"]["zone_id"] == "z-ouaga"
        assert result["onboarding_profile"]["coverage_status"] == "NEARBY"
        assert result["onboarding_profile"]["declared_location"] == "Somgande"
        # La région stockée/affichée est TOUJOURS le nom canonique (Ouagadougou -> Kadiogo).
        assert result["onboarding_profile"]["zone_name"] == "Kadiogo"
        assert "Kadiogo" in result["onboarding_prompt"]
        assert "valide" not in result["onboarding_prompt"].lower()

    def test_a_completely_unknown_locality_asks_the_region_once_then_never_blocks(self, monkeypatch):
        """Localité inconnue de TOUS les niveaux ("Somgande", absente du stub) : on demande
        la région UNE fois (sans faire recommencer l'onboarding) ; une 2e réponse toujours
        non résolue part en hors-couverture, `declared_location` conservé tel quel."""
        _patch_llm_extract(monkeypatch, [{"role": "BUYER", "name": "Awa", "zone": "Somgande"}])
        rt = _FakeRuntime()  # aucune zone connue, ni région ni hiérarchie
        state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "Awa a Somgande"}
        r1 = run(onboarding_node(state, rt))
        assert r1["onboarding_profile"]["coverage_status"] in (None, "")
        assert "région" in r1["onboarding_prompt"].lower()
        assert "Somgande" in r1["onboarding_prompt"]
        assert r1["onboarding_profile"]["name"] == "Awa"  # rien perdu, rien à recommencer

        _patch_llm_extract(monkeypatch, [{"zone": "Somgande"}])
        result = run(onboarding_node(_carry(r1, text="Somgande"), rt))
        assert result["onboarding_profile"]["zone_id"] is None
        assert result["onboarding_profile"]["coverage_status"] == "OUT_OF_COVERAGE"
        assert result["onboarding_profile"]["declared_location"] == "Somgande"
        assert "Somgande" in result["onboarding_prompt"]
        assert "valide" not in result["onboarding_prompt"].lower()
        assert result["is_onboarding"] is True

    def test_an_out_of_coverage_profile_still_reaches_confirmation_and_creation(self, monkeypatch):
        _patch_llm_extract(monkeypatch, [{"role": "BUYER", "name": "Awa", "zone": "Somgande"}])
        rt = _FakeRuntime()
        state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": "Awa a Somgande"}
        r0 = run(onboarding_node(state, rt))
        r1 = run(onboarding_node(_carry(r0, text="Somgande"), rt))
        assert r1["onboarding_step"] == "COMPLETED"

        r2 = run(onboarding_node(_carry(r1, text="oui"), rt))
        assert r2["status"] == "SUCCESS"
        assert "Bienvenue patron Awa" in r2["onboarding_prompt"]


# =====================================================================
# Problème 2 — "ok" ne confirme pas
# =====================================================================


class TestDeterministicConfirmationDuringProfileConfirmation:
    def _confirm_ready_state(self, monkeypatch):
        _patch_llm_extract(monkeypatch, [{"role": "PRODUCER", "name": "Gilbert-prod", "zone": "Ouagadougou"}])
        rt = _FakeRuntime(zones={"region": {"OUAGADOUGOU": {"id": "z-ouaga", "name": "Ouagadougou"}}})
        state = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "Gilbert-prod, producteur a Ouagadougou",
        }
        r1 = run(onboarding_node(state, rt))
        assert r1["onboarding_step"] == "COMPLETED"
        return r1, rt

    @pytest.mark.parametrize(
        "word",
        [
            "ok", "oui", "je confirme", "confirme", "valide", "valider", "je valide",
            "c'est bon", "tout est bon", "go", "oui oui", "ok ok", "d'accord",
        ],
    )
    def test_every_canonical_confirmation_word_creates_the_profile_without_an_llm_call(
        self, monkeypatch, word
    ):
        r1, rt = self._confirm_ready_state(monkeypatch)
        _forbid_llm_extract(monkeypatch)
        r2 = run(onboarding_node(_carry(r1, text=word), rt))
        assert r2["status"] == "SUCCESS", f"{word!r} aurait dû confirmer immédiatement"
        assert "Bienvenue patron Gilbert-prod" in r2["onboarding_prompt"]
        # Mandat §9 : jamais le récapitulatif répété.
        assert "récapitulatif" not in r2["onboarding_prompt"].lower()

    def test_non_asks_for_a_correction_instead_of_creating_the_profile(self, monkeypatch):
        r1, rt = self._confirm_ready_state(monkeypatch)
        _forbid_llm_extract(monkeypatch)
        r2 = run(onboarding_node(_carry(r1, text="non"), rt))
        assert r2["status"] != "SUCCESS"
        assert "corriger" in r2["onboarding_prompt"].lower()

    def test_annule_is_treated_as_a_rejection_never_creates_the_profile(self, monkeypatch):
        r1, rt = self._confirm_ready_state(monkeypatch)
        _forbid_llm_extract(monkeypatch)
        r2 = run(onboarding_node(_carry(r1, text="annule"), rt))
        assert r2["status"] != "SUCCESS"

    def test_a_free_text_correction_during_confirmation_still_uses_the_llm(self, monkeypatch):
        """Non-régression : seul le vocabulaire FERMÉ court-circuite le LLM — une phrase
        libre contenant plus d'information ("non, je suis plutôt à Saaba") doit encore
        passer par l'extraction LLM pour capter la correction de zone."""
        r1, rt = self._confirm_ready_state(monkeypatch)
        _patch_llm_extract(monkeypatch, [{"zone": "Saaba"}])
        rt2 = _FakeRuntime(zones={"region": {"SAABA": {"id": "z-saaba", "name": "Saaba"}}})
        r2 = run(onboarding_node(_carry(r1, text="non, je suis plutot a Saaba"), rt2))
        assert r2["onboarding_profile"]["zone_name"] == "Saaba"

    def test_a_free_text_name_correction_during_confirmation_still_uses_the_llm(self, monkeypatch):
        r1, rt = self._confirm_ready_state(monkeypatch)
        _patch_llm_extract(monkeypatch, [{"name": "Gilbert"}])
        r2 = run(onboarding_node(_carry(r1, text="non, mon nom c'est Gilbert"), rt))
        assert r2["onboarding_profile"]["name"] == "Gilbert"


# =====================================================================
# Reproduction exacte de l'échange rapporté en production
# =====================================================================


class TestFullReportedScenario:
    def test_the_exact_reported_conversation_completes_without_blocking_or_repeating(
        self, monkeypatch
    ):
        # Tour 1 : "salut; je veux vendre mes boeufs" -> rôle producteur inféré par le LLM
        # (classification libre, simulée ici), nom/zone encore manquants.
        _patch_llm_extract(monkeypatch, [{"role": "PRODUCER"}])
        rt = _FakeRuntime()  # "somgande" absent du référentiel dans CE scénario
        state = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "salut; je veux vendre mes boeufs",
        }
        r1 = run(onboarding_node(state, rt))
        assert r1["is_onboarding"] is True
        assert r1["onboarding_profile"]["role"] == "PRODUCER"

        # Tour 2 : "je suis Gilbert-prod; je vis a somgande" -> nom capté, localisation
        # conservée, PAS de blocage sec malgré "somgande" absent du référentiel.
        _patch_llm_extract(monkeypatch, [{"name": "Gilbert-prod", "zone": "somgande"}])
        r2a = run(onboarding_node(_carry(r1, text="je suis Gilbert-prod; je vis a somgande"), rt))
        assert r2a["onboarding_profile"]["name"] == "Gilbert-prod"
        assert "région" in r2a["onboarding_prompt"].lower()  # région redemandée UNE fois

        # Tour 2b : même localité -> hors couverture, jamais bloqué.
        _patch_llm_extract(monkeypatch, [{"zone": "somgande"}])
        r2 = run(onboarding_node(_carry(r2a, text="somgande"), rt))
        assert r2["onboarding_profile"]["declared_location"] == "somgande"
        assert r2["onboarding_profile"]["coverage_status"] == "OUT_OF_COVERAGE"
        assert "valide" not in r2["onboarding_prompt"].lower()
        assert "somgande" in r2["onboarding_prompt"].lower()
        assert r2["onboarding_step"] == "COMPLETED"  # tous les champs "réglés" -> récap

        # Tour 3 : "ok" -> profil créé IMMÉDIATEMENT, jamais un second récap.
        _forbid_llm_extract(monkeypatch)
        r3 = run(onboarding_node(_carry(r2, text="ok"), rt))
        assert r3["status"] == "SUCCESS"
        assert "Bienvenue patron Gilbert-prod" in r3["onboarding_prompt"]
        assert "récapitulatif" not in r3["onboarding_prompt"].lower()
