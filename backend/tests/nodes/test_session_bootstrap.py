"""`nodes/session_bootstrap.py` — amorçage technique de session : rôle par
défaut (ex-`role_guard`), chargement du profil utilisateur, activation de
l'onboarding (2026-09-08, refonte responsabilités des nœuds d'entrée ;
revue de validation le même jour).

(Revue de validation) : deux responsabilités ont été RETIRÉES de ce nœud
suite à la revue — le préchargement des fermes (`preload_farms`, dette de
"God node" identifiée : pas un contexte universel, un producteur
uniquement, et `ensure_farm_node` a déjà son propre repli) et le
bookkeeping `working_memory.active_tunnel_label` (code mort, aucun
lecteur). `TestNoFarmPreloadDrift`/`TestNoDeadTunnelBookkeeping` ci-dessous
verrouillent ce retrait pour éviter toute résurgence."""
from __future__ import annotations

from unittest.mock import AsyncMock

from tests.conftest import make_state, run


def _patch_deps(monkeypatch, *, profile_updates=None, onboarding_side_effect=None):
    import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod

    monkeypatch.setattr(mod, "load_user_profile", AsyncMock(return_value=profile_updates or {"user_context_loaded": False}))
    if onboarding_side_effect is not None:
        monkeypatch.setattr(mod, "resolve_onboarding_state", onboarding_side_effect)
    else:
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)


# =====================================================================
# Rôle par défaut (ex-role_guard)
# =====================================================================

class TestDefaultRole:
    def test_missing_user_role_becomes_unknown_not_producer(self, monkeypatch):
        """(mandat §3, interdiction explicite) : une valeur inconnue ne doit
        JAMAIS tomber sur PRODUCER par défaut."""
        _patch_deps(monkeypatch)
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_role="", user_phone="")
        result = run(session_bootstrap(state, None))
        assert result["user_role"] == "UNKNOWN"

    def test_existing_user_role_is_not_overwritten(self, monkeypatch):
        _patch_deps(monkeypatch)
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_role="BUYER", user_phone="")
        result = run(session_bootstrap(state, None))
        assert "user_role" not in result


# =====================================================================
# Chargement profil / onboarding
# =====================================================================

class TestPhoneExtractionAndProfileLoading:
    def test_no_phone_skips_profile_loading_entirely(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        load_profile = AsyncMock()
        monkeypatch.setattr(mod, "load_user_profile", load_profile)
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)
        state = make_state(user_phone="", user_query="bonjour")
        run(mod.session_bootstrap(state, None))
        load_profile.assert_not_awaited()

    def test_phone_from_state_updates_fallback_is_used(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        load_profile = AsyncMock(return_value={"user_context_loaded": True, "user_role": "PRODUCER"})
        monkeypatch.setattr(mod, "load_user_profile", load_profile)
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)
        state = make_state(user_phone="", state_updates={"phone": "+2260"})
        run(mod.session_bootstrap(state, None))
        load_profile.assert_awaited_once_with("+2260", None)

    def test_already_loaded_context_skips_profile_reload(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        load_profile = AsyncMock()
        monkeypatch.setattr(mod, "load_user_profile", load_profile)
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)
        state = make_state(user_phone="+2260", user_context_loaded=True)
        result = run(mod.session_bootstrap(state, None))
        load_profile.assert_not_awaited()
        assert result["is_onboarding"] is False
        assert result["onboarding_step"] == "COMPLETED"

    def test_orchestrator_flagged_onboarding_skips_mcp_call(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        load_profile = AsyncMock()
        monkeypatch.setattr(mod, "load_user_profile", load_profile)
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)
        state = make_state(user_phone="+2260", is_onboarding=True)
        result = run(mod.session_bootstrap(state, None))
        load_profile.assert_not_awaited()
        assert result["is_onboarding"] is True
        assert result["transaction_payload"]["phone"] == "+2260"

    def test_successful_profile_load_updates_role(self, monkeypatch):
        _patch_deps(monkeypatch, profile_updates={"user_context_loaded": True, "user_role": "PRODUCER"})
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        assert result["user_role"] == "PRODUCER"

    def test_new_user_activates_onboarding(self, monkeypatch):
        _patch_deps(monkeypatch, profile_updates={"_new_user": True})
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        assert result["is_onboarding"] is True
        assert result["onboarding_step"] == "COLLECT_ROLE"
        assert result["transaction_payload"]["phone"] == "+2260"

    def test_profile_unavailable_returns_early_with_clear_message(self, monkeypatch):
        _patch_deps(monkeypatch, profile_updates={"_profile_unavailable": True})
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        assert result["status"] == "BLOCKED"
        assert result["security_status"] == "PROFILE_UNAVAILABLE"

    def test_load_user_profile_exception_returns_profile_unavailable(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        monkeypatch.setattr(mod, "load_user_profile", AsyncMock(side_effect=RuntimeError("mcp down")))
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)
        state = make_state(user_phone="+2260")
        result = run(mod.session_bootstrap(state, None))
        assert result["status"] == "BLOCKED"
        assert result["security_status"] == "PROFILE_UNAVAILABLE"


# =====================================================================
# Revue de validation (2026-09-08) — non-régression des 2 retraits
# =====================================================================

class TestNoFarmPreloadDrift:
    """Le préchargement des fermes n'est PAS un contexte "minimum
    universel" (mandat de revue §2) — retiré. `ensure_farm_node`
    (flows/producer/farm_logic.py, non audité mais vérifié comme
    self-sufficient) reste l'unique responsable, à la demande."""

    def test_module_no_longer_imports_preload_farms(self):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        assert not hasattr(mod, "preload_farms")

    def test_successful_profile_load_never_touches_user_farms_cache(self, monkeypatch):
        _patch_deps(monkeypatch, profile_updates={"user_context_loaded": True, "user_role": "PRODUCER"})
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        assert "user_farms_cache" not in result


class TestNoDeadTunnelBookkeeping:
    """`working_memory.active_tunnel_label` n'avait aucun lecteur nulle
    part dans le code — retiré (mandat de revue §2, "REMOVE", pas
    "MOVE LATER" : ce n'était pas seulement mal placé, c'était mort)."""

    def test_active_goal_no_longer_writes_working_memory(self, monkeypatch):
        _patch_deps(monkeypatch)
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="", current_goal="SALES_PUBLISH_PRODUCT", working_memory={})
        result = run(session_bootstrap(state, None))
        assert "working_memory" not in result


class TestTransactionPayloadPhoneShim:
    """(2026-09-08, clôture Bloc 1, mandat §8) : recherche exhaustive faite
    dans `src/ladini` — `transaction_payload["phone"]` a de VRAIS
    lecteurs hors périmètre audité (`flows/producer/farm_logic.py::
    ensure_farm_node`, `nodes/executor.py::mcp_tool_executor`/
    `_build_task_payload` — tous en REPLI après `state.get("user_phone")`).
    Cas B du mandat : conservé comme COMPATIBILITY_SHIM, pas supprimé.
    Ce test verrouille que `session_bootstrap` continue de le poser
    CHAQUE fois qu'il active l'onboarding, pour que ce filet reste vivant."""

    def test_new_user_onboarding_writes_both_user_phone_and_the_shim(self, monkeypatch):
        _patch_deps(monkeypatch, profile_updates={"_new_user": True})
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        # `user_phone` (primaire) ET le shim (repli) sont posés ENSEMBLE —
        # un futur lecteur qui ne trouve pas `user_phone` dans state (ex:
        # un tour ultérieur où ce champ n'a pas été réécrit) retombe sur
        # cette valeur, qui SURVIT aux tours (transaction_payload est un
        # merge_dict, jamais réinitialisé par ce nœud).
        assert result["user_phone"] == "+2260"
        assert result["transaction_payload"]["phone"] == "+2260"

    def test_orchestrator_flagged_onboarding_also_writes_the_shim(self, monkeypatch):
        import ladini.graphs.agents.market_coach.nodes.session_bootstrap as mod
        monkeypatch.setattr(mod, "load_user_profile", AsyncMock())
        monkeypatch.setattr(mod, "resolve_onboarding_state", lambda state, updates: False)
        state = make_state(user_phone="+2260", is_onboarding=True)
        result = run(mod.session_bootstrap(state, None))
        assert result["user_phone"] == "+2260"
        assert result["transaction_payload"]["phone"] == "+2260"


# =====================================================================
# Contrat architectural : ne doit pas empiéter sur les nœuds voisins
# =====================================================================

class TestPurityContract:
    def test_does_not_write_interpreted_event_or_detected_intent(self, monkeypatch):
        """(revue de validation) : ces deux champs sont le contrat de
        sortie d'`input_interpreter`, jamais de `session_bootstrap` — même
        pendant l'onboarding (`input_interpreter::_emit_onboarding` les
        pose lui-même, inconditionnellement, sur les 3 chemins onboarding)."""
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260", is_onboarding=True, onboarding_step="COLLECT_NAME")
        result = run(session_bootstrap(state, None))
        assert "interpreted_event" not in result
        assert "detected_intent" not in result
        assert "interpreter_confidence" not in result

    def test_does_not_write_current_goal(self, monkeypatch):
        _patch_deps(monkeypatch)
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        assert "current_goal" not in result

    def test_does_not_write_execution_authorized(self, monkeypatch):
        _patch_deps(monkeypatch)
        from ladini.graphs.agents.market_coach.nodes.session_bootstrap import session_bootstrap
        state = make_state(user_phone="+2260")
        result = run(session_bootstrap(state, None))
        assert "execution_authorized" not in result
