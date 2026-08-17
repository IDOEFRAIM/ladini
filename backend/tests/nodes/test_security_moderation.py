"""`nodes/security_moderation.py` — gate d'entrée sécurité : compte
bloqué/banni, produits interdits (avec strikes + bannissement), détection de
scam. Zéro DB : `StubRuntime`/mocks directs sur `ModerationGateway`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import StubRuntime, make_state, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


@pytest.fixture(autouse=True)
def _reset_terms_cache():
    """Le cache des termes interdits est un dict GLOBAL au module — sans
    reset entre tests, un test antérieur pollue silencieusement les suivants
    (faux cache-hit, ou l'inverse)."""
    import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod
    mod._terms_cache["at"] = 0.0
    mod._terms_cache["terms"] = None
    yield
    mod._terms_cache["at"] = 0.0
    mod._terms_cache["terms"] = None


# =====================================================================
# _unwrap
# =====================================================================

class TestUnwrap:
    def test_non_dict_returns_empty_dict(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _unwrap
        assert _unwrap("not a dict") == {}
        assert _unwrap(None) == {}

    def test_flat_business_shape_is_returned_as_is(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _unwrap
        payload = {"account_status": "ACTIVE"}
        assert _unwrap(payload) is payload

    def test_enveloped_data_is_unwrapped(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _unwrap
        payload = {"ok": True, "data": {"account_status": "ACTIVE"}}
        assert _unwrap(payload) == {"account_status": "ACTIVE"}

    def test_unrecognized_shape_without_data_is_returned_as_is(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _unwrap
        payload = {"random_key": 1}
        assert _unwrap(payload) == {"random_key": 1}


# =====================================================================
# _fold
# =====================================================================

class TestFold:
    def test_folds_accents_and_lowercases(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _fold
        assert _fold("Héroïne") == "heroine"

    def test_empty_text_yields_empty_string(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _fold
        assert _fold("") == ""
        assert _fold(None) == ""


# =====================================================================
# _match_prohibited
# =====================================================================

class TestMatchProhibited:
    def test_single_word_term_matches_whole_word(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _match_prohibited
        assert _match_prohibited("je vends de la cocaine", ["cocaine"]) == "cocaine"

    def test_multi_word_term_matches_substring(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _match_prohibited
        assert _match_prohibited("j'ai une arme a feu a vendre", ["arme a feu"]) == "arme a feu"

    def test_accented_term_matches_folded_text(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _match_prohibited
        assert _match_prohibited("j'ai de l'heroine", ["héroïne"]) == "héroïne"

    def test_no_match_returns_none(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _match_prohibited
        assert _match_prohibited("je vends des tomates", ["cocaine", "arme"]) is None

    def test_empty_text_returns_none(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _match_prohibited
        assert _match_prohibited("", ["cocaine"]) is None


# =====================================================================
# _check_account_gate
# =====================================================================

class TestCheckAccountGate:
    def test_gateway_exception_fails_open(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def get_account_status(self, phone):
                raise RuntimeError("boom")

        monkeypatch.setattr(mod, "ModerationGateway", _BoomGateway)
        assert run(mod._check_account_gate(rt(), "+2260")) is None

    def test_banned_account_is_blocked(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_account_gate
        runtime = rt({"get_account_status": {"account_status": "BANNED"}})
        result = run(_check_account_gate(runtime, "+2260"))
        assert result["security_status"] == "ACCOUNT_BLOCKED"
        assert "banni" in result["final_response"]

    def test_blocked_account_is_blocked(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_account_gate
        runtime = rt({"get_account_status": {"account_status": "BLOCKED"}})
        result = run(_check_account_gate(runtime, "+2260"))
        assert result["status"] == "BLOCKED"
        assert "bloqué" in result["final_response"]

    def test_active_account_passes_through(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_account_gate
        runtime = rt({"get_account_status": {"account_status": "ACTIVE"}})
        assert run(_check_account_gate(runtime, "+2260")) is None

    def test_enveloped_response_is_unwrapped_before_reading_status(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_account_gate
        runtime = rt({"get_account_status": {"ok": True, "data": {"account_status": "BANNED"}}})
        result = run(_check_account_gate(runtime, "+2260"))
        assert result["security_status"] == "ACCOUNT_BLOCKED"


# =====================================================================
# _get_prohibited_terms_cached
# =====================================================================

class TestGetProhibitedTermsCached:
    def test_cache_miss_fetches_and_caches(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _get_prohibited_terms_cached
        runtime = rt({"get_prohibited_terms": {"terms": ["cocaine", "arme"]}})
        result = run(_get_prohibited_terms_cached(runtime))
        assert result == ["cocaine", "arme"]
        assert "get_prohibited_terms" in runtime.calls

    def test_cache_hit_skips_the_network_call(self):
        import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod
        import time
        mod._terms_cache["terms"] = ["cached_term"]
        mod._terms_cache["at"] = time.monotonic()
        runtime = rt({"get_prohibited_terms": {"terms": ["fresh_term"]}})
        result = run(mod._get_prohibited_terms_cached(runtime))
        assert result == ["cached_term"]
        assert "get_prohibited_terms" not in runtime.calls

    def test_expired_cache_refetches(self):
        import time
        import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod
        mod._terms_cache["terms"] = ["stale_term"]
        # Bug réel confirmé (CI, reproductible) : `at = 0.0` supposait que
        # `time.monotonic()` vaut TOUJOURS bien plus que le TTL (300s) —
        # faux par construction : la doc Python dit explicitement que le
        # point de référence de `time.monotonic()` est NON SPÉCIFIÉ (souvent
        # l'uptime système). Sur un runner CI fraîchement démarré,
        # `time.monotonic()` peut valoir < 300 au moment du test — `0.0`
        # n'est alors PAS détecté comme périmé, le cache-hit renvoie
        # silencieusement `stale_term` au lieu de refetch. Seul CE test
        # révèle le bug (ses voisins ci-dessous acceptent la même valeur de
        # repli, qu'il y ait eu refetch ou pas — l'ambiguïté masquait le
        # problème). Fix : calculer un timestamp GARANTI périmé PAR RAPPORT
        # au `time.monotonic()` réel de ce process, jamais une valeur
        # absolue supposée.
        mod._terms_cache["at"] = time.monotonic() - mod._TERMS_CACHE_TTL_SECONDS - 1
        runtime = rt({"get_prohibited_terms": {"terms": ["fresh_term"]}})
        result = run(mod._get_prohibited_terms_cached(runtime))
        assert result == ["fresh_term"]

    def test_gateway_exception_falls_back_to_stale_cache(self):
        import time
        import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod
        mod._terms_cache["terms"] = ["stale_but_usable"]
        mod._terms_cache["at"] = time.monotonic() - mod._TERMS_CACHE_TTL_SECONDS - 1

        class _BoomRuntime:
            llm = None

            async def call_db(self, tool_name, **kwargs):
                raise RuntimeError("network down")

        result = run(mod._get_prohibited_terms_cached(_BoomRuntime()))
        assert result == ["stale_but_usable"]

    def test_empty_terms_response_falls_back_to_previous_cache(self):
        import time
        import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod
        mod._terms_cache["terms"] = ["previous"]
        mod._terms_cache["at"] = time.monotonic() - mod._TERMS_CACHE_TTL_SECONDS - 1
        runtime = rt({"get_prohibited_terms": {"terms": []}})
        result = run(mod._get_prohibited_terms_cached(runtime))
        assert result == ["previous"]


# =====================================================================
# _check_prohibited
# =====================================================================

class TestCheckProhibited:
    def test_no_terms_available_passes_through(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_prohibited
        runtime = rt({"get_prohibited_terms": {"terms": []}})
        assert run(_check_prohibited(runtime, "+2260", "je vends des tomates")) is None

    def test_no_match_passes_through(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_prohibited
        runtime = rt({"get_prohibited_terms": {"terms": ["cocaine"]}})
        assert run(_check_prohibited(runtime, "+2260", "je vends des tomates")) is None

    def test_match_below_ban_threshold_returns_a_warning(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_prohibited
        runtime = rt({
            "get_prohibited_terms": {"terms": ["cocaine"]},
            "record_moderation_strike": {"strikes": 1, "banned": False},
        })
        result = run(_check_prohibited(runtime, "+2260", "je vends de la cocaine"))
        assert result["security_status"] == "PROHIBITED_PRODUCT"
        assert "1/3" in result["final_response"]

    def test_match_at_ban_threshold_returns_a_permanent_ban(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_prohibited
        runtime = rt({
            "get_prohibited_terms": {"terms": ["cocaine"]},
            "record_moderation_strike": {"strikes": 4, "banned": True},
        })
        result = run(_check_prohibited(runtime, "+2260", "je vends de la cocaine"))
        assert result["security_status"] == "ACCOUNT_BLOCKED"
        assert "banni" in result["final_response"]

    def test_strike_recording_failure_is_best_effort_and_still_warns(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.nodes.security_moderation as mod

        class _BoomGateway:
            def __init__(self, rt):
                pass

            async def get_prohibited_terms(self):
                return {"terms": ["cocaine"]}

            async def record_moderation_strike(self, **kwargs):
                raise RuntimeError("strike recording down")

        monkeypatch.setattr(mod, "ModerationGateway", _BoomGateway)
        result = run(mod._check_prohibited(rt(), "+2260", "je vends de la cocaine"))
        assert result["security_status"] == "PROHIBITED_PRODUCT"

    def test_no_phone_skips_strike_recording_but_still_warns(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import _check_prohibited
        runtime = rt({"get_prohibited_terms": {"terms": ["cocaine"]}})
        result = run(_check_prohibited(runtime, "", "je vends de la cocaine"))
        assert result["security_status"] == "PROHIBITED_PRODUCT"
        assert "record_moderation_strike" not in runtime.calls


# =====================================================================
# security_moderation (nœud principal)
# =====================================================================

class TestSecurityModerationNode:
    def test_already_blocked_upstream_is_respected(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(status="BLOCKED", final_response="Profil indisponible", security_status="PROFILE_UNAVAILABLE")
        result = run(security_moderation(state, rt()))
        assert result == {"security_status": "PROFILE_UNAVAILABLE"}

    def test_account_gate_blocks_before_anything_else(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="bonjour")
        runtime = rt({"get_account_status": {"account_status": "BANNED"}})
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "ACCOUNT_BLOCKED"

    def test_empty_text_defaults_to_safe(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="")
        runtime = rt({"get_account_status": {"account_status": "ACTIVE"}})
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
        assert result["trust_score"] == 1.0

    def test_no_phone_skips_the_account_gate_entirely(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="", normalized_text="")
        runtime = rt()
        result = run(security_moderation(state, runtime))
        assert "get_account_status" not in runtime.calls

    def test_prohibited_product_blocks_before_scam_check(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="je vends de la cocaine")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": ["cocaine"]},
            "record_moderation_strike": {"strikes": 1, "banned": False},
        })
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "PROHIBITED_PRODUCT"

    def test_no_security_service_on_runtime_defaults_safe(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="bonjour, je vends du mais")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
        assert result["trust_score"] == 0.8

    def test_moderate_content_awaitable_clean_result_is_safe(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="bonjour, je vends du mais")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        runtime.security = SimpleNamespace(moderate_content=AsyncMock(return_value={"is_scam": False, "status": "OK"}))
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
        assert result["trust_score"] == 1.0

    def test_moderate_content_detects_scam_via_is_scam_flag(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="envoyez votre code OTP maintenant")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        runtime.security = SimpleNamespace(
            moderate_content=AsyncMock(return_value={"is_scam": True, "reason": "phishing pattern"})
        )
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SCAM_DETECTED"
        assert result["security_reason"] == "phishing pattern"
        assert result["status"] == "BLOCKED"

    def test_moderate_content_detects_scam_via_status_field(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="suspect message")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        runtime.security = SimpleNamespace(
            moderate_content=AsyncMock(return_value={"status": "SCAM_DETECTED"})
        )
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SCAM_DETECTED"

    def test_moderate_content_timeout_degrades_to_safe(self):
        import asyncio
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation

        async def _slow(text):
            await asyncio.sleep(10)

        state = make_state(user_phone="+2260", normalized_text="bonjour")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        runtime.security = SimpleNamespace(moderate_content=_slow)
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
        assert result["trust_score"] == 0.3

    def test_moderate_content_exception_degrades_to_safe(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation

        async def _boom(text):
            raise RuntimeError("moderation service down")

        state = make_state(user_phone="+2260", normalized_text="bonjour")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        runtime.security = SimpleNamespace(moderate_content=_boom)
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
        assert result["trust_score"] == 0.3

    def test_moderate_content_non_dict_result_is_safe_with_medium_trust(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="bonjour")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        runtime.security = SimpleNamespace(moderate_content=AsyncMock(return_value="not-a-dict"))
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
        assert result["trust_score"] == 0.5

    def test_moderate_content_sync_non_awaitable_result_is_handled(self):
        from agriconnect.graphs.agents.market_coach.nodes.security_moderation import security_moderation
        state = make_state(user_phone="+2260", normalized_text="bonjour")
        runtime = rt({
            "get_account_status": {"account_status": "ACTIVE"},
            "get_prohibited_terms": {"terms": []},
        })
        # `moderate_content` synchrone (pas de coroutine) : le nœud doit gérer
        # les deux formes, awaitable ET valeur directe.
        runtime.security = SimpleNamespace(moderate_content=lambda text: {"is_scam": False})
        result = run(security_moderation(state, runtime))
        assert result["security_status"] == "SAFE"
