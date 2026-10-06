"""`agents/onboarding.py::run_onboarding_step` — adaptivité aux questions
posées PENDANT l'onboarding.

Bug réel (2026-08-13) : quand l'utilisateur pose une question ("c'est quoi
ce truc", "comment ça marche", "je crée un compte pour mon père, comment ça
se passe") au lieu de fournir une info d'inscription, l'agent répondait
TOUJOURS par la même question de collecte de champ ("il me faut encore :
ton nom et ta ville..."), en boucle, sans jamais répondre à la question.

Première correction : une clé `is_question` déclenchait un texte CANNED
(`_EXPLAIN_AGAIN`). Retour utilisateur sur CETTE approche : elle ne fait que
déplacer le problème — chaque nouveau cas de figure (doute, hostilité,
question inhabituelle) demanderait un nouveau texte codé en dur, sans jamais
couvrir ce qu'un vrai utilisateur peut dire. Le LLM doit générer la réponse
lui-même, à partir d'indices sur l'état de l'inscription (déjà connu /
manquant / nombre de relances) — pas d'un texte figé. Voir
[[onboarding-adaptive-questions-2026-08]].

Les textes canned (`_EXPLAIN_AGAIN`/`_EXPLAIN_AGAIN_SHORT`) ne servent plus
que de FILET DE SÉCURITÉ quand l'appel LLM échoue ou ne renvoie pas de
`reply` exploitable — ces tests le couvrent explicitement en simulant un
extracteur qui ne renvoie pas de `reply` (comme un `llm=None`/timeout réel)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ladini.agents.onboarding import (
    OnboardingState,
    OnboardingStep,
    run_onboarding_step,
)
from tests.conftest import run


def _extractor(**payload: Any):
    """Simule l'extracteur LLM. Capture le `context_hint` reçu sur l'objet
    retourné (`.last_hint`) pour les tests qui veulent l'inspecter."""
    calls: Dict[str, str] = {}

    async def _fake(text: str, context_hint: str) -> Dict[str, Optional[str]]:
        calls["last_hint"] = context_hint
        base = {
            "role": None, "name": None, "zone": None, "confirm": None,
            "is_question": False, "reply": None,
        }
        base.update(payload)
        return base

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


class TestAdaptiveQuestionHandling:
    def test_a_meta_question_uses_the_llm_generated_reply_verbatim(self):
        """La réponse doit venir du LLM (adaptée au message réel), pas d'un
        texte figé — c'est tout le point du fix."""
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "Non je veux savoir ce que tu fais",
            None,
            llm_extract_all=_extractor(
                is_question=True,
                reply="Ladini connecte producteurs et acheteurs par WhatsApp, sur mesure pour toi patron !",
            ),
        ))
        assert "sur mesure pour toi patron" in result.response_text
        # La question ne doit pas faire perdre le fil : on redemande toujours
        # les infos manquantes après avoir répondu.
        assert "producteur" in result.response_text.lower() or "acheteur" in result.response_text.lower()

    def test_no_llm_reply_falls_back_to_the_canned_explanation(self):
        """Filet de sécurité : si le LLM échoue ou ne renvoie pas de `reply`
        exploitable (extracteur=None, timeout, pas de client LLM), on ne
        laisse pas la question sans réponse — on retombe sur le texte figé."""
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "Comment ça marche ?",
            None,
            llm_extract_all=_extractor(is_question=True, reply=None),
        ))
        assert "Ladini" in result.response_text
        assert "connecte directement producteurs" in result.response_text

    def test_normal_onboarding_data_does_not_trigger_any_explanation(self):
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "Je suis Awa, productrice",
            None,
            llm_extract_all=_extractor(name="Awa", role="PRODUCER"),
        ))
        assert "Ladini" not in result.response_text
        assert ob_state.name == "Awa"
        assert ob_state.role == "PRODUCER"

    def test_repeated_questions_without_a_usable_llm_reply_stop_repeating_the_full_pitch(self):
        """Filet de sécurité, cas répété : si le LLM échoue à répétition,
        on ne rejoue quand même pas le MÊME pitch complet en boucle."""
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")

        first = run(run_onboarding_step(
            ob_state, "C'est quoi ce truc", None,
            llm_extract_all=_extractor(is_question=True, reply=None),
        ))
        assert "connecte directement producteurs" in first.response_text
        assert ob_state.explain_count == 1

        second = run(run_onboarding_step(
            ob_state, "Pourquoi je devrais te faire confiance ?", None,
            llm_extract_all=_extractor(is_question=True, reply=None),
        ))
        assert "connecte directement producteurs" not in second.response_text
        assert "prudence" in second.response_text.lower()
        assert ob_state.explain_count == 2

    def test_a_question_combined_with_real_data_still_credits_the_data(self):
        """Un message mixte ('je m'appelle Awa, mais c'est quoi ce service ?')
        ne doit ni ignorer la question ni perdre l'info donnée."""
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "Je m'appelle Awa, mais c'est quoi ce service ?",
            None,
            llm_extract_all=_extractor(name="Awa", is_question=True, reply="C'est simple Awa, je t'explique !"),
        ))
        assert ob_state.name == "Awa"
        assert "C'est simple Awa" in result.response_text

    def test_the_llm_extractor_receives_hints_about_known_and_missing_fields(self):
        """Le LLM doit recevoir ce qui est déjà connu / manquant / le nombre
        de relances — c'est CE contexte qui doit guider sa réponse, pas un
        texte pré-écrit pour chaque cas."""
        ob_state = OnboardingState(
            step=OnboardingStep.COLLECT_ROLE, phone="+22670000001",
            name="Awa", explain_count=1,
        )
        extractor = _extractor(is_question=True, reply="ok")
        run(run_onboarding_step(ob_state, "c'est quoi ce service ?", None, llm_extract_all=extractor))
        hint = extractor.calls["last_hint"]  # type: ignore[attr-defined]
        assert "nom=Awa" in hint
        assert "1" in hint  # explain_count déjà à 1 avant cet appel


class TestFamilyRelationIsNotMisreadAsAName:
    def test_a_bare_family_relation_word_does_not_get_accepted_as_the_users_name(self):
        """Bug réel (2026-08-13) : "Bon je veux créer un compte pour mon
        daron" a été extrait comme name="daron" ('daron' = argot pour
        'papa'), donc le bot a répondu "Enchanté patron daron !" — cette
        classe de bug se corrige côté prompt LLM
        (utils.py::_llm_extract_onboarding_all), mais on verrouille ici que
        `run_onboarding_step` ne réclame PAS ce nom quand l'extracteur (une
        fois correctement prompté) renvoie bien name=None pour ce cas."""
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "Bon je veux créer un compte pour mon daron",
            None,
            llm_extract_all=_extractor(name=None),
        ))
        assert ob_state.name is None
        assert "daron" not in result.response_text.lower()

    def test_a_family_relation_followed_by_a_real_name_is_accepted(self):
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "C'est pour mon père Ibrahim",
            None,
            llm_extract_all=_extractor(name="Ibrahim"),
        ))
        assert ob_state.name == "Ibrahim"


class TestZoneResolutionNeverBlocksOnboarding:
    """Mandat 2026-09-26 : une localité qui ne résout à AUCUN niveau (ni région, ni
    rattachement hiérarchique) ne doit plus jamais bloquer l'onboarding avec un message sec
    listant des "zones valides" — remplace l'ancien `TestZoneCatalogHintListsAllValidZones`
    (incident 2026-08-26), dont le comportement verrouillé (rejet + liste) est précisément
    celui que ce mandat retire. Voir `test_onboarding_zone_region_level.py` pour les tests
    dédiés au resolver DB lui-même (`get_zone_by_name`/`get_zone_hierarchy_by_name`)."""

    class _ZoneStub:
        def __init__(self, *, region_found=False, hierarchy_root=None):
            self._region_found = region_found
            self._hierarchy_root = hierarchy_root

        async def call_db(self, tool_name: str, **kwargs: Any):
            if tool_name == "get_zone_by_name":
                if self._region_found:
                    return {"status": "success", "data": {"id": "z1", "name": kwargs.get("name")}}
                return {"status": "error", "message": "introuvable"}
            if tool_name == "get_zone_hierarchy_by_name":
                if self._hierarchy_root:
                    return {
                        "status": "success",
                        "data": {
                            "matched": {"id": "child-1", "name": kwargs.get("name")},
                            "root": self._hierarchy_root,
                        },
                    }
                return {"status": "error", "message": "introuvable"}
            raise AssertionError(f"outil inattendu: {tool_name}")

    def test_a_locality_matching_nothing_at_all_never_blocks_the_onboarding(self):
        runtime = self._ZoneStub(region_found=False, hierarchy_root=None)
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        # 1re réponse non résolue : la région est redemandée (une seule fois).
        r1 = run(run_onboarding_step(
            ob_state, "Je suis à Somgande", runtime, llm_extract_all=_extractor(zone="Somgande"),
        ))
        assert "région" in r1.response_text.lower()
        assert ob_state.coverage_status is None
        # 2e réponse toujours non résolue : hors couverture, jamais bloqué.
        result = run(run_onboarding_step(
            ob_state, "Somgande", runtime, llm_extract_all=_extractor(zone="Somgande"),
        ))
        assert "valide" not in result.response_text.lower()
        assert "Somgande" in result.response_text
        assert ob_state.declared_location == "Somgande"
        assert ob_state.coverage_status == "OUT_OF_COVERAGE"
        assert ob_state.zone_id is None
        assert "nom" in result.response_text.lower()

    def test_a_locality_attached_to_a_known_parent_zone_is_accepted_transparently(self):
        runtime = self._ZoneStub(hierarchy_root={"id": "root-1", "name": "Ouagadougou"})
        ob_state = OnboardingState(step=OnboardingStep.COLLECT_ROLE, phone="+22670000001")
        result = run(run_onboarding_step(
            ob_state,
            "Je suis à Somgande",
            runtime,
            llm_extract_all=_extractor(zone="Somgande"),
        ))
        assert ob_state.declared_location == "Somgande"
        assert ob_state.coverage_status == "NEARBY"
        assert ob_state.zone_id == "root-1"
        assert ob_state.zone_name == "Kadiogo"  # nom canonique de la région
        assert "Kadiogo" in result.response_text
