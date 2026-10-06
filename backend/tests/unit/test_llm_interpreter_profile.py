"""Chantier "State Router + micro-prompts" — PHASE B.1 (2026-09-12) : profil
LLM dédié à l'interpréteur MarketCoach.

## Audit de configuration (résumé, voir le rapport final pour le détail)

- `LLMProfile` (`llm_gateway/types.py`) : capacité demandée par un node
  métier — jamais un nom de modèle. Avant cette phase : `FAST`/`REASONING`
  uniquement.
- `resolve_gateway(mc_runtime)` (`llm_gateway/gateway.py`) résout
  `mc_runtime.llm_gateway` (le vrai `LLMGateway` en production, un
  `LegacyOverrideGateway` en test) — indépendant du PROFIL, qui n'intervient
  qu'au moment de `.complete(profile=...)`.
- `settings.py` charge `LLM_FAST_PRIMARY`/`LLM_FAST_FALLBACK_1/2` et
  `LLM_REASONING_PRIMARY`/`LLM_REASONING_FALLBACK_1/2` — chaînes
  "provider:model" consommées par `registry.py::load_registry`.
- Consommateurs de `LLMProfile.FAST` AVANT cette phase :
  `llm_router.py::_FAST_PROFILE_GOALS` = {INPUT_NORMALIZATION,
  SECURITY_MODERATION, STATE_CLEANER} — et, temporairement, le micro-prompt
  SELECTION (Incrément B), qui partageait accidentellement CE MÊME profil
  (donc le même modèle Bedrock par défaut que ces 3 nodes d'infrastructure).

## Ce que cette phase change

Un profil `LLMProfile.INTERPRETER` dédié, résolu par ses propres réglages
(`LLM_INTERPRETER_PRIMARY`/`_FALLBACK_1`/`_FALLBACK_2`, défaut
`bedrock_gateway:qwen.qwen3-32b` primaire / `groq:openai/gpt-oss-20b` en
repli depuis le 2026-09-26 — voir settings.py pour l'historique complet,
Groq ayant été primaire entre le 2026-09-12 et cette date) — utilisé par :
  - `interpreter/selection_micro.py` (micro-prompt SELECTION) ;
  - `interpreter/routing.py` (interpréteur unifié legacy — NEW_TASK/
    ACTIVE_SLOT/STRUCTURED_ACTION, MÊME prompt/schéma/règles, SEUL le
    provider/modèle change, décision explicite spec §7).

AUCUN changement pour `LLMProfile.FAST`/`LLMProfile.REASONING` ni pour
leurs consommateurs existants — vérifié explicitement ci-dessous."""
from __future__ import annotations

from types import SimpleNamespace

from ladini.core.settings import settings as real_settings
from ladini.graphs.agents.market_coach.llm_gateway.registry import load_registry
from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile
from ladini.graphs.agents.market_coach.llm_router import get_profile_for_goal


class TestInterpreterProfileIsLoadedFromItsOwnDedicatedSettings:
    """(2026-09-26, décision produit explicite, post-audit
    LLM_GATEWAY_EXHAUSTED) : Bedrock devient le PRIMAIRE du profil
    INTERPRETER — Groq passe en repli (conservé, pas retiré). Remplace la
    règle précédente (spec §4 du chantier State Router, "jamais Bedrock en
    position 0") qui reflétait un choix antérieur, différent — voir
    settings.py pour l'historique complet des deux décisions."""

    def test_default_settings_resolve_the_interpreter_profile_to_bedrock(self):
        registry = load_registry(real_settings)
        candidates = registry[LLMProfile.INTERPRETER]
        assert candidates, "aucun candidat configuré pour LLMProfile.INTERPRETER"
        primary = candidates[0]
        assert primary.provider == "bedrock_gateway"
        assert primary.model == "qwen.qwen3-32b"

    def test_groq_remains_configured_as_a_fallback_not_removed(self):
        # Bedrock primaire ne veut pas dire "Groq retiré" — la résilience
        # (disjoncteur -> repli) reste le point de cette chaîne à plusieurs
        # candidats.
        registry = load_registry(real_settings)
        providers = [c.provider for c in registry[LLMProfile.INTERPRETER]]
        assert "groq" in providers
        assert providers.index("groq") > 0, "Groq doit rester un repli, jamais le primaire"

    def test_interpreter_model_is_configurable_via_settings(self):
        # Spec §1 : "il doit être configurable" — pas un modèle en dur dans
        # le code (registry.py ne connaît que le FORMAT "provider:model",
        # jamais un nom de modèle littéral).
        fake_settings = SimpleNamespace(
            LLM_FAST_PRIMARY="groq:whatever-fast",
            LLM_FAST_FALLBACK_1="",
            LLM_FAST_FALLBACK_2="",
            LLM_REASONING_PRIMARY="groq:whatever-reasoning",
            LLM_REASONING_FALLBACK_1="",
            LLM_REASONING_FALLBACK_2="",
            LLM_INTERPRETER_PRIMARY="groq:llama-3.3-70b-versatile",
            LLM_INTERPRETER_FALLBACK_1="",
            LLM_INTERPRETER_FALLBACK_2="",
        )
        registry = load_registry(fake_settings)
        primary = registry[LLMProfile.INTERPRETER][0]
        assert primary.model == "llama-3.3-70b-versatile"


class TestFastProfileConsumersAreUntouched:
    def test_input_normalization_still_resolves_to_fast(self):
        assert get_profile_for_goal("INPUT_NORMALIZATION") == LLMProfile.FAST

    def test_security_moderation_still_resolves_to_fast(self):
        assert get_profile_for_goal("SECURITY_MODERATION") == LLMProfile.FAST

    def test_state_cleaner_still_resolves_to_fast(self):
        assert get_profile_for_goal("STATE_CLEANER") == LLMProfile.FAST

    def test_fast_profile_default_candidate_is_unchanged_by_this_phase(self):
        # La configuration FAST elle-même (celle qui sert ces 3 nodes) ne
        # doit RIEN avoir changé — toujours le même défaut Bedrock qu'avant
        # cette phase (voir settings.py, non modifié pour LLM_FAST_*).
        registry = load_registry(real_settings)
        fast_primary = registry[LLMProfile.FAST][0]
        assert fast_primary.provider == "bedrock_gateway"
        assert fast_primary.model == "qwen.qwen3-32b"

    def test_no_business_goal_accidentally_resolves_to_the_interpreter_profile(self):
        # `get_profile_for_goal` ne connaît que FAST/REASONING (voir
        # `llm_router.py`) — INTERPRETER est un profil INTERNE à
        # `interpreter/`, jamais choisi via la table goal→profil générique.
        for goal in ("INPUT_NORMALIZATION", "SECURITY_MODERATION", "STATE_CLEANER", "SALES_PUBLISH_PRODUCT", None):
            assert get_profile_for_goal(goal) != LLMProfile.INTERPRETER


class TestReasoningProfileIsUntouched:
    def test_reasoning_primary_is_unchanged(self):
        registry = load_registry(real_settings)
        reasoning_primary = registry[LLMProfile.REASONING][0]
        assert reasoning_primary.provider == "bedrock_gateway"
        assert reasoning_primary.model == "deepseek.v3.2"

    def test_gpt_oss_120b_is_reserved_as_a_reasoning_fallback_not_an_automatic_interpreter_escalation(self):
        # Spec §5 : gpt-oss-120b reste réservé au profil REASONING
        # (escalade future) — jamais candidat du profil INTERPRETER par
        # défaut.
        registry = load_registry(real_settings)
        interpreter_models = {c.model for c in registry[LLMProfile.INTERPRETER]}
        assert "openai/gpt-oss-120b" not in interpreter_models
        assert "openai.gpt-oss-120b" not in interpreter_models
