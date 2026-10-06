"""Reproduction de l'incident réel 2026-09-05 — LLM Gateway.

    Utilisateur : « je veux voir les enchères »
    Avant  : bedrock_gateway x2 -> 401 (token expiré) -> groq refusé par
             `get_groq_sdk()` (LLM_PROVIDER != groq) -> `application_error`
             -> tous les candidats "épuisés" -> UNKNOWN -> clarification_node
             retente EXACTEMENT les mêmes providers -> mêmes échecs ->
             réponse générique "Je n'ai pas bien saisi".
    Racine : `registry.py` déclare des candidats INDÉPENDANTS par provider
             (Modèle A), mais `core/get_llm.py::get_groq_sdk()` refusait de
             construire un client Groq tant que `settings.LLM_PROVIDER`
             n'était pas "groq"/"auto" — une condition héritée du client
             LEGACY unique, sans rapport avec la disponibilité réelle de
             l'identifiant Groq pour CE candidat de repli.

Ce fichier prouve, avec la VRAIE `LLMGateway` (registry/circuit réels, Redis
fake) branchée sur `input_interpreter` PUIS `clarification_node` — les deux
points d'appel réels du tour, jamais un attrapé isolé :

    1. Une fois le garde retiré, le repli groq du 3e candidat FONCTIONNE —
       le tour aboutit à une intention exploitée, pas à UNKNOWN.
    2. Quand TOUS les candidats sont réellement inutilisables (identifiants
       manquants), `input_interpreter` échoue avec
       `unknown_reason=TECHNICAL_FAILURE` et `clarification_node`, sur le
       MÊME tour, ne déclenche AUCUN second appel réseau — repli déterministe
       honnête à la place du mensonge "je n'ai pas bien saisi".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from tests.conftest import make_state, run
from tests.unit.llm_gateway.conftest import make_fake_redis

from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
)
from ladini.graphs.agents.market_coach.llm_gateway.gateway import LLMGateway
from ladini.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from ladini.graphs.agents.market_coach.llm_gateway.types import (
    LLMProfile,
    ModelCandidate,
)
from ladini.graphs.agents.market_coach.nodes.clarification import (
    clarification_node,
)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content="{}", model=None):
        self.choices = [_Choice(content)]
        self.model = model


class _SecurityTokenExpired(Exception):
    """401 — même forme que les exceptions SDK OpenAI/Groq (`status_code`),
    voir `error_classification.py`."""

    status_code = 401


class _AuthExpiredClient:
    """Simule la passerelle `bedrock_gateway` avec un token de sécurité
    expiré — 401 sur CHAQUE tentative, exactement le log de l'incident."""

    def __init__(self):
        self.calls: List[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise _SecurityTokenExpired("security token expired (401)")


class _GroqSucceedsClient:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload
        self.calls: List[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        import json

        return _Completion(json.dumps(self._payload), model=kwargs.get("model"))


def _incident_candidates() -> List[ModelCandidate]:
    """Exactement la chaîne de repli REASONING observée dans l'incident
    (`.env.example` : LLM_REASONING_PRIMARY/FALLBACK_1/FALLBACK_2)."""
    return [
        ModelCandidate(
            provider="bedrock_gateway",
            model="deepseek.v3.2",
            profile=LLMProfile.REASONING,
            priority=0,
            timeout_seconds=2.0,
        ),
        ModelCandidate(
            provider="bedrock_gateway",
            model="openai.gpt-oss-120b",
            profile=LLMProfile.REASONING,
            priority=1,
            timeout_seconds=2.0,
        ),
        ModelCandidate(
            provider="groq",
            model="llama-3.3-70b-versatile",
            profile=LLMProfile.REASONING,
            priority=2,
            timeout_seconds=2.0,
        ),
    ]


class _StubRegistry:
    def __init__(self, candidates: List[ModelCandidate]):
        self._candidates = candidates

    def candidates_for(self, profile):
        return list(self._candidates)

    def primary_model_name(self, profile):
        return self._candidates[0].model if self._candidates else None


def _make_real_gateway(
    *, client_factory, settings_overrides: Dict[str, Any] | None = None
) -> LLMGateway:
    store: Dict[str, Any] = {}
    health = HealthRegistry(redis_client=make_fake_redis(store))
    circuit = CircuitBreaker(
        health,
        failure_threshold=3,
        cooldown_seconds=30.0,
        half_open_probes=1,
        probe_lock_seconds=10.0,
    )
    base_settings: Dict[str, Any] = dict(
        LLM_FAST_BUDGET_SECONDS=10.0,
        LLM_REASONING_BUDGET_SECONDS=10.0,
        ADMIN_ALERT_WEBHOOK_URL="",
        SENTRY_ENVIRONMENT="test",
        MOCK_EXTERNAL_APIS=False,
        llm_api_key="",
        OPENAI_API_KEY="",
    )
    base_settings.update(settings_overrides or {})
    settings = SimpleNamespace(**base_settings)
    return LLMGateway(
        registry=_StubRegistry(_incident_candidates()),
        health_registry=health,
        circuit_breaker=circuit,
        settings=settings,
        client_factory=client_factory,
    )


class _RealGatewayRuntime:
    """Runtime minimal exposant un VRAI `LLMGateway` (pas
    `LegacyOverrideGateway`) — `resolve_gateway()`/`resolve_profile()`
    (llm_gateway/gateway.py) le consomment tel quel.

    `.llm` (2026-09-02, garde infra `routing.py:1196/1228`) : `input_
    interpreter` vérifie encore la PRÉSENCE d'un client LLM avant de tenter
    quoi que ce soit (garde "démarrage dégradé, aucun LLM configuré du
    tout") — mais l'appel RÉEL passe désormais par `resolve_gateway()`, donc
    seule la non-nullité compte ici, jamais sa valeur."""

    def __init__(self, gateway: LLMGateway):
        self.llm = object()
        self.llm_gateway = gateway
        self.profile_answer = LLMProfile.REASONING


class TestIncidentFixedGroqFallbackNowWorks:
    def test_two_config_errors_then_a_working_groq_fallback_reaches_a_real_intent(self):
        """Post-fix (`core/get_llm.py::get_groq_sdk`, plus de garde
        LLM_PROVIDER) : le 3e candidat groq, identifiant valide, RÉUSSIT — le
        tour n'atterrit plus sur UNKNOWN."""
        bedrock_client = _AuthExpiredClient()
        groq_client = _GroqSucceedsClient(
            {
                "disposition": "NEW_TASK",
                "intent": "BUYER_LIST_AUCTIONS",
                "confidence": 0.9,
                "entities": {},
            }
        )
        gateway = _make_real_gateway(
            client_factory=lambda provider: {
                "bedrock_gateway": bedrock_client,
                "groq": groq_client,
            }[provider],
            # bedrock_gateway EST configuré (OPENAI_API_KEY présent) — c'est
            # justement le token derrière cette clé qui a expiré, découvert
            # SEULEMENT à l'appel (401) : le pré-check §7 ne prétend jamais
            # remplacer le disjoncteur, seulement éviter les appels
            # STRUCTURELLEMENT impossibles.
            settings_overrides={
                "OPENAI_API_KEY": "sk-configured-but-token-expired",
                "llm_api_key": "sk-real-groq-key",
            },
        )
        rt = _RealGatewayRuntime(gateway)
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text="je veux voir les enchères",
            user_query="je veux voir les enchères",
            expected_input="NONE",
            current_goal=None,
            user_role="BUYER",
        )

        result = run(interp(state, rt))

        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "BUYER_LIST_AUCTIONS"
        assert result["unknown_reason"] is None
        # Les 2 tentatives bedrock_gateway ont bien eu lieu (401 classé
        # CONFIG), le repli groq a été tenté UNE fois et a réussi.
        assert len(bedrock_client.calls) == 2
        assert len(groq_client.calls) == 1


class TestTotalOutageNeverProducesASecondWastedLlmCall:
    def test_all_candidates_unusable_yields_technical_failure_and_clarification_makes_zero_extra_calls(
        self,
    ):
        """Scénario §11 du brief : bedrock_gateway en 401 (2 tentatives
        RÉELLES — la config existe, c'est le token qui a expiré, ce que le
        pré-check §7 ne peut/doit pas deviner sans appel), ET aucun
        `GROQ_API_KEY` configuré (le pré-check §7 saute CE candidat — zéro
        appel réseau). `input_interpreter` doit rendre
        `unknown_reason=TECHNICAL_FAILURE` ; `clarification_node`, dans la
        FOULÉE du même tour, ne doit déclencher NI un appel bedrock_gateway
        NI un appel groq supplémentaire.

        Rôle PRODUCER (pas BUYER comme le message de l'incident) : isole le
        comportement de la Gateway de `goal_planner.py::_degraded_fallback`
        (repli lexical PRÉEXISTANT, restreint à BUYER, hors du périmètre de
        CE chantier — §34 "ne pas refactor l'interpréteur") — sinon ce test
        verrouillerait accidentellement le comportement d'un autre mécanisme."""
        bedrock_client = _AuthExpiredClient()
        groq_client = _GroqSucceedsClient({})  # ne doit JAMAIS être appelé

        def _client_factory(provider: str):
            if provider == "groq":
                # Si le pré-check de disponibilité échoue à faire son travail,
                # ce test le détecterait quand même via groq_client.calls.
                return groq_client
            return bedrock_client

        gateway = _make_real_gateway(
            client_factory=_client_factory,
            settings_overrides={
                "OPENAI_API_KEY": "sk-configured-but-token-expired",
                "llm_api_key": "",  # GROQ_API_KEY absent
            },
        )
        rt = _RealGatewayRuntime(gateway)
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="je veux voir les enchères",
            user_query="je veux voir les enchères",
            expected_input="NONE",
            current_goal=None,
            user_role="PRODUCER",
        )

        interpreter_result = run(interp(state, rt))

        assert interpreter_result["interpreted_event"] == "UNKNOWN"
        assert interpreter_result["unknown_reason"] == "TECHNICAL_FAILURE"
        assert len(bedrock_client.calls) == 2  # les 2 candidats bedrock RÉELLEMENT tentés
        assert len(groq_client.calls) == 0  # jamais tenté — pré-check CREDENTIAL_MISSING

        calls_to_bedrock_before_clarification = len(bedrock_client.calls)
        calls_to_groq_before_clarification = len(groq_client.calls)

        # Le tour continue : le state final (fusionné) atteint clarification_node.
        # (2026-09-08, correction topologique du bloc conversationnel) : dans
        # le graphe réel, `cognitive_guard` s'exécute entre les deux et
        # décide CLARIFY pour cet état (event=UNKNOWN, rien en attente,
        # aucun goal actif — voir `nodes/cognitive.py::
        # _classify_nominal_action`) ; `clarification_node` lui fait
        # désormais confiance au lieu de recalculer. Ce test appelant les
        # deux nœuds directement (hors graphe compilé, pour isoler la
        # Gateway), la décision est injectée ici pour rester représentative
        # du tour réel.
        merged_state = {
            **state,
            **interpreter_result,
            "cognitive_decision": {"action": "CLARIFY"},
        }
        clarification_result = run(clarification_node(merged_state, rt))

        assert clarification_result["response_strategy"] == "CLARIFICATION"
        assert "indisponible" in clarification_result["final_response"].lower()
        assert "bien saisi" not in clarification_result["final_response"].lower(), (
            "une panne d'infrastructure ne doit jamais être présentée comme "
            "une incompréhension du message utilisateur (§12/§15 du brief)"
        )
        # AUCUN appel réseau supplémentaire déclenché par clarification_node —
        # le cœur du bug corrigé (§11 : "second appel LLM inutile").
        assert len(bedrock_client.calls) == calls_to_bedrock_before_clarification
        assert len(groq_client.calls) == calls_to_groq_before_clarification
