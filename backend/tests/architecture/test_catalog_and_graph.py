"""Invariants d'ARCHITECTURE — garde-fous anti-dérive.

Ces tests ne valident pas un comportement mais la COHÉRENCE STRUCTURELLE du
système. Ils cassent dès qu'on ajoute une intention mal déclarée, un nœud
orphelin ou une source de vérité dupliquée — avant le déploiement, pas en prod.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG, INTENT_ROLE, INTENT_DISAMBIGUATION,
)
from agriconnect.graphs.agents.market_coach.core.slots import _EXPECTED_INPUT_MAP
from agriconnect.graphs.agents.market_coach.core.goals import (
    ALL_BUYER_TUNNEL_GOALS, PRODUCER_RESOLVER_GOALS, PRODUCER_UPDATE_GOALS,
    PRODUCER_ESCROW_GOALS, NAVIGATION_BREAKOUT_GOALS,
)
from agriconnect.graphs.agents.market_coach.registry import load_all_actions, iter_actions
from agriconnect.graphs.agents.market_coach.core.base import validate_config_drift

load_all_actions()
REGISTERED = {intent for intent, _ in iter_actions()}
AUTO_RESOLVED = {"farm_id", "phone"}


class TestIntentCatalog:
    def test_every_intent_has_a_role(self):
        missing = sorted(g for g in INTENT_CONFIG if g not in INTENT_ROLE)
        assert not missing, f"intentions sans rôle : {missing}"

    def test_roles_use_only_known_values(self):
        bad = {g: r for g, r in INTENT_ROLE.items() if r not in {"PRODUCER", "BUYER", "BOTH"}}
        assert not bad, f"rôles invalides : {bad}"

    def test_every_intent_is_executable(self):
        """Soit un outil (dispatch générique), soit un flow dédié."""
        orphans = sorted(
            g for g, c in INTENT_CONFIG.items()
            if not (c or {}).get("tool_name") and not (c or {}).get("handled_by_flow")
        )
        assert not orphans, f"intentions inexécutables : {orphans}"

    def test_tooled_intents_have_a_registered_action(self):
        missing = sorted(
            g for g, c in INTENT_CONFIG.items()
            if (c or {}).get("tool_name") and not (c or {}).get("handled_by_flow")
            and g not in REGISTERED
        )
        assert not missing, f"outillées mais sans action enregistrée : {missing}"

    def test_registry_has_no_action_for_unknown_intent(self):
        extra = sorted(REGISTERED - set(INTENT_CONFIG))
        assert not extra, f"actions enregistrées hors catalogue : {extra}"

    def test_config_drift_validator_passes(self):
        """Le validateur officiel doit passer (il tolère les `handled_by_flow`)."""
        validate_config_drift()

    def test_required_business_slots_are_askable(self):
        """Tout champ requis NON technique doit avoir un expected_input connu,
        sinon l'agent ne saura pas le demander."""
        problems = []
        for goal, cfg in INTENT_CONFIG.items():
            for f in (cfg or {}).get("required") or []:
                if f in AUTO_RESOLVED or f.endswith("_id"):
                    continue
                if f not in _EXPECTED_INPUT_MAP:
                    problems.append(f"{goal}.{f}")
        # Champs connus non encore câblés — tolérés mais figés pour éviter
        # toute NOUVELLE régression silencieuse.
        known = {
            "CROP_RECORD_INTERVENTION.intervention_type",
            "CROP_RECORD_OBSERVATION.stage_label", "CROP_RECORD_OBSERVATION.observation",
            "CROP_UPDATE_SOIL.ph", "CROP_UPDATE_STAGE.stage_name",
            "PRODUCER_CONFIRM_DELIVERY_OTP.otp_code",
            "PROFILE_SET_GEO.latitude", "PROFILE_SET_GEO.longitude",
            "PROFILE_SET_PREFS.language", "PROFILE_SWITCH_ROLE.target_role",
            "SEARCH_NEARBY.latitude", "SEARCH_NEARBY.longitude",
            "SYSTEM_REPORT_ANOMALY.anomaly_type", "SYSTEM_REPORT_ANOMALY.description",
            "SYSTEM_COMMIT_TRANSACTION.staging_id",
        }
        new = sorted(set(problems) - known)
        assert not new, f"nouveaux champs requis non demandables : {new}"

    def test_disambiguation_references_existing_intents(self):
        bad = []
        for did, entry in INTENT_DISAMBIGUATION.items():
            for c in (entry or {}).get("candidates") or []:
                if c not in INTENT_CONFIG:
                    bad.append(f"{did}->{c}")
            for opt in (entry or {}).get("options") or []:
                k = opt[0] if isinstance(opt, (tuple, list)) else (opt or {}).get("intent")
                if k and k not in INTENT_CONFIG:
                    bad.append(f"{did}->{k}")
        assert not bad, f"références inconnues en désambiguïsation : {bad}"

    def test_every_disambiguation_entry_offers_a_real_choice(self):
        thin = [d for d, e in INTENT_DISAMBIGUATION.items() if len((e or {}).get("options") or []) < 2]
        assert not thin, f"menus à moins de 2 options : {thin}"

    def test_tunnel_and_breakout_goals_exist(self):
        flow_goals = (ALL_BUYER_TUNNEL_GOALS | PRODUCER_RESOLVER_GOALS
                      | PRODUCER_UPDATE_GOALS | PRODUCER_ESCROW_GOALS)
        unknown = sorted(g for g in flow_goals if g not in INTENT_CONFIG and g != "BUYER_CART_RESET")
        assert not unknown, f"goals de tunnel inconnus : {unknown}"
        unknown_bo = sorted(g for g in NAVIGATION_BREAKOUT_GOALS if g not in INTENT_CONFIG)
        assert not unknown_bo, f"goals breakout inconnus : {unknown_bo}"


class TestGraphWiring:
    """Le graphe compilé doit être parcourable de bout en bout."""

    @pytest.fixture(scope="class")
    def compiled(self):
        from agriconnect.graphs.agents.market_coach.core.graph_builder import build_graph
        return build_graph(role="PRODUCER").get_graph()

    def _edges(self, g):
        out, inc = {}, {}
        for e in getattr(g, "edges", []):
            s, d = getattr(e, "source", None), getattr(e, "target", None)
            if s and d:
                out.setdefault(s, set()).add(d)
                inc.setdefault(d, set()).add(s)
        return out, inc

    def test_every_node_is_reachable_from_start(self, compiled):
        out, _ = self._edges(compiled)
        nodes = {n.id if hasattr(n, "id") else str(n) for n in compiled.nodes.values()}
        seen, stack = set(), ["__start__"]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(out.get(cur, ()))
        unreachable = sorted(n for n in nodes if n not in seen and n not in ("__start__", "__end__"))
        assert not unreachable, f"nœuds inatteignables : {unreachable}"

    def test_every_node_can_reach_end(self, compiled):
        _, inc = self._edges(compiled)
        nodes = {n.id if hasattr(n, "id") else str(n) for n in compiled.nodes.values()}
        seen, stack = set(), ["__end__"]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(inc.get(cur, ()))
        stuck = sorted(n for n in nodes if n not in seen and n not in ("__start__", "__end__"))
        assert not stuck, f"nœuds sans chemin vers END (blocage possible) : {stuck}"

    def test_routers_never_return_an_undeclared_branch(self):
        """Un routeur renvoyant une clé non déclarée provoque un KeyError runtime."""
        declared = {
            "_route_after_clarification": {"to_disambiguation", "to_strategy"},
            "_route_after_cognitive": {"to_clarification", "to_onboarding"},
            "_route_after_confirmation": {"to_executor", "to_strategy"},
            "_route_after_disambiguation": {"to_planner", "to_strategy"},
            "_route_after_executor": {"to_strategy"},
            "_route_after_planner": {"to_memory", "to_strategy"},
            "_route_after_resolver": {"to_confirmation", "to_farm_guard", "to_strategy"},
            "_route_after_security": {"to_interpreter", "to_strategy"},
        }
        root = pathlib.Path(__file__).resolve().parents[2] / "src/agriconnect/graphs/agents/market_coach"
        found: dict[str, set[str]] = {}
        for p in root.rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in declared:
                    for r in ast.walk(n):
                        if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant) \
                                and isinstance(r.value.value, str):
                            found.setdefault(n.name, set()).add(r.value.value)
        for fn, allowed in declared.items():
            extra = found.get(fn, set()) - allowed
            assert not extra, f"{fn} renvoie des branches non déclarées : {sorted(extra)}"


class TestNoDuplicatedSourceOfTruth:
    """Les duplications de tables constantes ont causé la majorité des bugs
    « par nœud » du projet. On verrouille l'unicité des plus sensibles."""

    def test_production_type_vocabulary_is_shared(self):
        from agriconnect.graphs.agents.market_coach.interpreter.entities import _PRODUCTION_TYPE_WORDS
        from agriconnect.graphs.agents.market_coach.services.domain.slot_enrichment import (
            PRODUCTION_TYPE_WORDS,
        )
        assert _PRODUCTION_TYPE_WORDS is PRODUCTION_TYPE_WORDS

    def test_ephemeral_working_keys_are_shared(self):
        from agriconnect.graphs.agents.market_coach.nodes.cleaner import _EPHEMERAL_WORKING_KEYS as A
        from agriconnect.graphs.agents.market_coach.nodes.memory import _EPHEMERAL_WORKING_KEYS as B
        assert set(A) == set(B)

    def test_confirmation_summary_has_a_single_builder(self):
        """`rendering/confirm.py` doit déléguer, pas re-formater à sa façon."""
        p = (pathlib.Path(__file__).resolve().parents[2]
             / "src/agriconnect/graphs/agents/market_coach/nodes/rendering/confirm.py")
        src = p.read_text(encoding="utf-8")
        assert "build_confirmation_summary" in src
        assert "def _build_summary" not in src, "un constructeur de récap concurrent est réapparu"
