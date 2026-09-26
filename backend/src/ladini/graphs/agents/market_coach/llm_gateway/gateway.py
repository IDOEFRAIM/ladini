"""LLM Gateway — point d'entrée public unique pour tout appel LLM métier.

`await get_llm_gateway().complete(profile=..., messages=..., response_format=...)`

Orchestre : Model Registry (quels candidats pour ce profil) → Circuit Breaker
(lesquels sont utilisables MAINTENANT, sans appel réseau pour ceux qui ne le
sont pas) → budget global (arrête la chaîne de repli avant de transformer une
requête courte en requête longue, §17) → exécution via les adapters EXISTANTS
de `core/get_llm.py` (aucune réécriture) → Health Registry (rapporte le
résultat) → télémétrie (Langfuse/Prometheus via `core/telemetry.py` étendu) →
alerting (incident ouverture/récupération, dédupliqué).

Chaque candidat obtient AU PLUS 2 tentatives réseau consécutives (Incrément G,
2026-09-13, "Production LLM Hardening", spec §5/§6) — jamais plus : un
timeout/5xx/429/erreur réseau transitoire mérite UNE retentative COURTE
(backoff+jitter, `Retry-After` honoré si le provider le fournit — voir
`error_classification.py::should_retry_same_candidate`/`backoff_seconds`),
mais 400/401/403/413 ne sont JAMAIS retentés (un problème de prompt/contrat/
credentials ne se résout pas en réessayant identique). Cette 2e tentative
reste comptée comme UN SEUL résultat pour le disjoncteur (santé rapportée
une fois par candidat, pas une fois par tentative réseau) — la boucle sur
`candidates_for(profile)` reste la stratégie de repli inter-candidats,
inchangée.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from ladini.graphs.agents.market_coach.llm_gateway.alerting import (
    IncidentDeduplicator,
    LLMIncidentAlert,
    NotificationService,
    build_notifier,
)
from ladini.graphs.agents.market_coach.llm_gateway.availability import (
    is_candidate_usable,
)
from ladini.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
    Decision,
)
from ladini.graphs.agents.market_coach.llm_gateway.error_classification import (
    backoff_seconds,
    classify_llm_error,
    classify_llm_failure_kind,
    retry_after_seconds,
    should_retry_same_candidate,
)
from ladini.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from ladini.graphs.agents.market_coach.llm_gateway.registry import ModelRegistry
from ladini.graphs.agents.market_coach.llm_gateway.types import (
    ErrorClass,
    LLMProfile,
    ModelCandidate,
)

logger = logging.getLogger("ladini.llm_gateway")

# Marge de sécurité : un candidat n'est tenté que s'il reste au moins ce
# temps sur le budget global APRÈS lui avoir réservé son propre timeout —
# évite de lancer une tentative qui n'a de toute façon pas le temps de finir
# proprement avant `wait_for` du budget.
_MIN_VIABLE_SECONDS = 1.5
_SAFETY_MARGIN_SECONDS = 0.5


class LLMGatewayExhausted(RuntimeError):
    """Tous les candidats éligibles pour ce profil ont échoué, étaient
    indisponibles (circuit OPEN / config manquante) ou le budget global était
    épuisé. Les nodes métier attrapent cette exception exactement comme ils
    attrapaient avant un timeout/erreur SDK direct — même repli
    UNKNOWN/dégradé, cause unique.

    `reason` (2026-09-05, §11/§12 du brief incident) distingue POURQUOI la
    chaîne de repli est vide, pour que l'appelant sache s'il a un sens
    d'essayer un second appel LLM (ex: `clarification_node`) ou non :

        ALL_CANDIDATES_UNAVAILABLE : aucune tentative réseau n'a eu lieu —
            chaque candidat était structurellement inutilisable (identifiant
            manquant) ou déjà en disjoncteur OPEN. Un second appel sur LA
            MÊME chaîne échouera pour EXACTEMENT la même raison — inutile.
        ALL_ATTEMPTS_FAILED : au moins un candidat a réellement été appelé
            et a échoué (transitoire/config découverte à l'appel).
        BUDGET_EXHAUSTED : le budget global du tour s'est épuisé avant même
            la première tentative viable.
        NO_CANDIDATES_CONFIGURED : le profil n'a aucun candidat déclaré en
            configuration — panne de configuration, pas d'exécution.
    """

    def __init__(self, message: str, *, reason: str = "ALL_ATTEMPTS_FAILED"):
        super().__init__(message)
        self.reason = reason


class LLMGateway:
    def __init__(
        self,
        *,
        registry: Optional[ModelRegistry] = None,
        health_registry: Optional[HealthRegistry] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        rate_limiter: Optional[Any] = None,
        notifier: Optional[NotificationService] = None,
        incident_dedup: Optional[IncidentDeduplicator] = None,
        settings: Any = None,
        client_factory: Optional[Any] = None,
    ):
        if settings is None:
            from ladini.core.settings import settings as _settings

            settings = _settings
        self._settings = settings

        self._registry = registry or ModelRegistry(settings)
        self._health = health_registry or HealthRegistry()
        self._circuit = circuit_breaker or CircuitBreaker(
            self._health,
            failure_threshold=int(getattr(settings, "LLM_CIRCUIT_FAILURE_THRESHOLD", 3)),
            cooldown_seconds=float(getattr(settings, "LLM_CIRCUIT_COOLDOWN_SECONDS", 30.0)),
            half_open_probes=int(getattr(settings, "LLM_HALF_OPEN_PROBES", 1)),
            probe_lock_seconds=float(getattr(settings, "LLM_PROBE_LOCK_SECONDS", 10.0)),
        )
        if rate_limiter is None:
            from ladini.graphs.agents.market_coach.llm_gateway.rate_limiter import (
                build_rate_limiter,
            )

            rate_limiter = build_rate_limiter(settings)
        self._rate_limiter = rate_limiter
        self._notifier = notifier or build_notifier(settings)
        self._incident_dedup = incident_dedup or IncidentDeduplicator()
        # Injectable pour les tests (évite de construire de vrais clients SDK) —
        # signature: (provider: str) -> objet exposant .chat.completions.create.
        self._client_factory = client_factory or _default_client_for_provider
        self._client_cache: Dict[str, Any] = {}
        self._environment = str(getattr(settings, "SENTRY_ENVIRONMENT", "development"))

    # ── API publique ─────────────────────────────────────────────────
    async def complete(
        self,
        *,
        profile: LLMProfile,
        messages: Any,
        response_format: Optional[dict] = None,
        request_id: Optional[str] = None,
        agent_node: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        # `extra_metadata` (2026-09-12, instrumentation Langfuse — voir
        # `record_generation`) : sac libre de dimensions d'observabilité
        # SUPPLÉMENTAIRES propres à l'appelant (ex: `message_sid`,
        # `prompt_version`, `cache_hit`, `llm_call_index`, `current_goal`,
        # `expected_input` pour `input_interpreter`) — paramètre EXPLICITE,
        # PAS dans `**kwargs` : `**kwargs` est directement éclaté dans l'appel
        # RÉEL au client LLM (`_call_candidate`, `call_kwargs.update(kwargs)`)
        # ; y glisser des clés d'observabilité casserait l'appel API. Ne
        # change AUCUN comportement fonctionnel — uniquement propagé vers
        # `_on_success`/`_on_failure` → `_emit_telemetry` → `record_generation`.
        structured_required = bool(response_format)
        candidates = [
            c
            for c in self._registry.candidates_for(profile)
            if c.supports(structured_output_required=structured_required)
        ]
        if not candidates:
            raise LLMGatewayExhausted(
                f"Aucun candidat configuré pour profile={profile.value} "
                f"(structured_output_required={structured_required})",
                reason="NO_CANDIDATES_CONFIGURED",
            )

        primary_key = candidates[0].key
        budget_seconds = self._budget_for(profile)
        deadline = time.monotonic() + budget_seconds
        attempt = 0
        budget_exhausted_before_any_attempt = False
        last_exc: Optional[BaseException] = None

        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= _MIN_VIABLE_SECONDS:
                logger.warning(
                    "LLM_BUDGET_EXHAUSTED | profile=%s budget=%.1fs — arrêt avant "
                    "candidat=%s",
                    profile.value,
                    budget_seconds,
                    candidate.key,
                )
                if attempt == 0:
                    budget_exhausted_before_any_attempt = True
                break

            # Pré-check de configuration (§6/§7, incident 2026-09-05) — AVANT
            # le disjoncteur : un candidat structurellement inutilisable
            # (identifiant manquant pour CE provider) n'a jamais eu de
            # "santé" à consulter, et ne doit jamais générer d'appel réseau
            # juste pour le découvrir (voir `availability.py`). Ne touche
            # PAS au disjoncteur/health — ce n'est pas une panne, c'est une
            # impossibilité structurelle connue à l'avance.
            availability = is_candidate_usable(candidate, self._settings)
            if not availability.usable:
                logger.info(
                    "LLM_CALL | profile=%s candidate=%s attempt=skip reason=%s",
                    profile.value,
                    candidate.key,
                    availability.reason,
                )
                continue

            circuit_decision = self._circuit.decide(candidate)
            if circuit_decision.decision == Decision.SKIP:
                logger.info(
                    "LLM_CALL | profile=%s candidate=%s attempt=skip reason=%s",
                    profile.value,
                    candidate.key,
                    circuit_decision.reason,
                )
                continue

            per_call_timeout = min(
                candidate.timeout_seconds, remaining - _SAFETY_MARGIN_SECONDS
            )
            if per_call_timeout <= 0:
                continue

            # Rate limiter partagé (spec §14-18) — désactivé par défaut
            # (RPM/TPM/MAX_INFLIGHT=0), donc no-op tant que l'opérateur ne
            # l'a pas explicitement configuré. Une seule attente courte
            # avant de basculer sur le candidat suivant (spec §18 : préférer
            # une attente courte à un 429→retry→429 en boucle) — jamais une
            # file d'attente longue qui dégraderait la latence perçue.
            estimated_tokens = _estimate_tokens(messages) + int(kwargs.get("max_tokens") or 0)
            rl_decision = self._rate_limiter.check_and_reserve(
                candidate.provider, estimated_tokens
            )
            if not rl_decision.allowed:
                logger.info(
                    "LLM_RATE_LIMITED | profile=%s candidate=%s reason=%s — attente courte",
                    profile.value,
                    candidate.key,
                    rl_decision.reason,
                )
                await asyncio.sleep(min(0.5, max(0.0, remaining - _MIN_VIABLE_SECONDS)))
                rl_decision = self._rate_limiter.check_and_reserve(
                    candidate.provider, estimated_tokens
                )
                if not rl_decision.allowed:
                    logger.info(
                        "LLM_CALL | profile=%s candidate=%s attempt=skip reason=%s",
                        profile.value,
                        candidate.key,
                        rl_decision.reason,
                    )
                    continue

            attempt += 1
            is_probe = circuit_decision.decision == Decision.PROBE
            t0 = time.perf_counter()
            # Spec §5/§6 : jusqu'à 2 tentatives réseau pour CE candidat —
            # la 2e UNIQUEMENT si le premier échec est d'un type qui mérite
            # un retry court (timeout/5xx/429/connexion — jamais 400/401/
            # 403/413, voir `should_retry_same_candidate`) ET s'il reste
            # assez de budget. Compte comme UN SEUL résultat pour le
            # disjoncteur (`provider_retry_count` tracé séparément,
            # jamais confondu avec `attempt`, qui reste l'index inter-
            # candidats pour la télémétrie existante).
            provider_retry_count = 0
            exc: Optional[Exception] = None
            completion = None
            try:
                for _network_try in range(2):
                    try:
                        completion = await self._call_candidate(
                            candidate,
                            messages=messages,
                            response_format=response_format,
                            timeout=per_call_timeout,
                            **kwargs,
                        )
                        exc = None
                        break
                    except Exception as call_exc:
                        exc = call_exc
                        if _network_try == 0:
                            kind = classify_llm_failure_kind(call_exc)
                            remaining_after = deadline - time.monotonic()
                            if (
                                should_retry_same_candidate(kind)
                                and remaining_after > _MIN_VIABLE_SECONDS
                            ):
                                delay = backoff_seconds(
                                    kind, 1, retry_after=retry_after_seconds(call_exc)
                                )
                                delay = min(
                                    delay, max(0.0, remaining_after - _MIN_VIABLE_SECONDS)
                                )
                                logger.info(
                                    "LLM_RETRY | profile=%s candidate=%s kind=%s delay_s=%.2f",
                                    profile.value,
                                    candidate.key,
                                    kind.value,
                                    delay,
                                )
                                if delay > 0:
                                    await asyncio.sleep(delay)
                                provider_retry_count = 1
                                per_call_timeout = min(
                                    candidate.timeout_seconds,
                                    (deadline - time.monotonic()) - _SAFETY_MARGIN_SECONDS,
                                )
                                if per_call_timeout <= 0:
                                    break
                                continue
                        break
            finally:
                self._rate_limiter.release_inflight(candidate.provider)

            if exc is not None:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                self._on_failure(
                    candidate=candidate,
                    profile=profile,
                    exc=exc,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    is_probe=is_probe,
                    primary_key=primary_key,
                    request_id=request_id,
                    agent_node=agent_node,
                    messages=messages,
                    structured_required=structured_required,
                    extra_metadata=extra_metadata,
                    provider_retry_count=provider_retry_count,
                )
                last_exc = exc
                continue

            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._on_success(
                candidate=candidate,
                profile=profile,
                completion=completion,
                latency_ms=latency_ms,
                attempt=attempt,
                is_probe=is_probe,
                is_fallback=candidate.key != primary_key,
                primary_key=primary_key,
                provider_retry_count=provider_retry_count,
                request_id=request_id,
                agent_node=agent_node,
                messages=messages,
                extra_metadata=extra_metadata,
                structured_required=structured_required,
            )
            return completion

        if attempt == 0:
            exhausted_reason = (
                "BUDGET_EXHAUSTED"
                if budget_exhausted_before_any_attempt
                else "ALL_CANDIDATES_UNAVAILABLE"
            )
        else:
            exhausted_reason = "ALL_ATTEMPTS_FAILED"
        logger.warning(
            "LLM_GATEWAY_EXHAUSTED | profile=%s reason=%s attempts=%d/%d",
            profile.value,
            exhausted_reason,
            attempt,
            len(candidates),
        )
        raise LLMGatewayExhausted(
            f"Tous les candidats du profil {profile.value} ont échoué ou "
            f"étaient indisponibles (budget={budget_seconds}s).",
            reason=exhausted_reason,
        ) from last_exc

    def primary_model_name(self, profile: LLMProfile) -> Optional[str]:
        return self._registry.primary_model_name(profile)

    def registry(self) -> ModelRegistry:
        return self._registry

    def health_registry(self) -> HealthRegistry:
        return self._health

    # ── Exécution d'un candidat ──────────────────────────────────────
    async def _call_candidate(
        self,
        candidate: ModelCandidate,
        *,
        messages: Any,
        response_format: Optional[dict],
        timeout: float,
        **kwargs: Any,
    ) -> Any:
        client = self._client_for(candidate.provider)
        call_kwargs: Dict[str, Any] = {"model": candidate.model, "messages": messages}
        if response_format is not None:
            call_kwargs["response_format"] = response_format
        call_kwargs.update(kwargs)

        return await asyncio.wait_for(
            asyncio.to_thread(lambda: client.chat.completions.create(**call_kwargs)),
            timeout=timeout,
        )

    def _client_for(self, provider: str) -> Any:
        cached = self._client_cache.get(provider)
        if cached is not None:
            return cached
        client = self._client_factory(provider)
        self._client_cache[provider] = client
        return client

    def _budget_for(self, profile: LLMProfile) -> float:
        # (2026-09-13, Incrément G, spec §21 "timeouts explicites par
        # profil") : INTERPRETER tombait auparavant dans l'"else" et
        # empruntait sans le vouloir le budget de REASONING (20s) — alors
        # que son timeout PAR CANDIDAT (registry.py, 8s, même valeur que
        # FAST) suggérait clairement une intention "rapide". Budget dédié
        # explicite plutôt qu'un partage accidentel.
        attr = {
            LLMProfile.FAST: "LLM_FAST_BUDGET_SECONDS",
            LLMProfile.INTERPRETER: "LLM_INTERPRETER_BUDGET_SECONDS",
        }.get(profile, "LLM_REASONING_BUDGET_SECONDS")
        return float(getattr(self._settings, attr, 15.0))

    # ── Callbacks succès/échec (santé + télémétrie + alerting) ───────
    def _on_success(
        self,
        *,
        candidate: ModelCandidate,
        profile: LLMProfile,
        completion: Any,
        latency_ms: float,
        attempt: int,
        is_probe: bool,
        is_fallback: bool,
        request_id: Optional[str],
        agent_node: Optional[str],
        messages: Any,
        structured_required: bool,
        primary_key: Optional[str] = None,
        provider_retry_count: int = 0,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        record = self._circuit.report_success(candidate, latency_ms)

        if is_probe and record is not None and record.state.value == "CLOSED":
            logger.info(
                "LLM_CIRCUIT_CLOSE | candidate=%s — probe réussi, trafic restauré",
                candidate.key,
            )
            self._safe_alert(
                candidate,
                lambda: self._incident_dedup.should_notify_recovery(candidate.key),
                LLMIncidentAlert(
                    incident_type="PRIMARY_DEGRADED",
                    severity="RECOVERY",
                    environment=self._environment,
                    profile=profile.value,
                    provider=candidate.provider,
                    model=candidate.model,
                    reason="probe HALF_OPEN réussi",
                ),
            )

        if is_fallback:
            logger.info(
                "LLM_FALLBACK | profile=%s to=%s attempt=%d reason=primary_unavailable",
                profile.value,
                candidate.key,
                attempt,
            )

        self._emit_telemetry(
            candidate=candidate,
            profile=profile,
            attempt=attempt,
            latency_s=latency_ms / 1000.0,
            status="success",
            completion=completion,
            error=None,
            request_id=request_id,
            agent_node=agent_node,
            messages=messages,
            structured_required=structured_required,
            # (2026-09-13, Incrément G, spec §10/§45) : bug corrigé — ce
            # champ valait toujours `None`, y compris quand `is_fallback`
            # était vrai. `fallback_from` doit porter le candidat PRIMAIRE
            # réellement contourné, pas juste un booléen implicite.
            fallback_from=primary_key if is_fallback else None,
            fallback_reason="primary_unavailable" if is_fallback else None,
            provider_retry_count=provider_retry_count,
            primary_key=primary_key,
            extra_metadata=extra_metadata,
        )

    def _on_failure(
        self,
        *,
        candidate: ModelCandidate,
        profile: LLMProfile,
        exc: BaseException,
        latency_ms: float,
        attempt: int,
        is_probe: bool,
        request_id: Optional[str],
        agent_node: Optional[str],
        messages: Any,
        structured_required: bool,
        primary_key: Optional[str] = None,
        provider_retry_count: int = 0,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        error_class = classify_llm_error(exc)
        failure_kind = classify_llm_failure_kind(exc)
        is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError))

        if error_class == ErrorClass.CONFIG:
            self._health.mark_config_error(
                candidate, str(exc), cooldown_seconds=self._circuit.cooldown_seconds
            )
            self._safe_alert(
                candidate,
                lambda: self._incident_dedup.should_notify_open(candidate.key),
                LLMIncidentAlert(
                    incident_type="MODEL_CONFIG_ERROR",
                    severity="CRITICAL",
                    environment=self._environment,
                    profile=profile.value,
                    provider=candidate.provider,
                    model=candidate.model,
                    reason=str(exc)[:300],
                ),
            )
        elif error_class == ErrorClass.TRANSIENT:
            record = self._circuit.report_failure(candidate, is_timeout=is_timeout)
            just_opened = (
                record is not None
                and record.state.value == "OPEN"
                and (is_probe or record.consecutive_failures >= 1)
            )
            # N'alerter que sur une VRAIE transition vers OPEN, pas à chaque
            # échec supplémentaire pendant que c'est déjà OPEN (§28).
            if just_opened:
                try:
                    from ladini.core.telemetry import record_circuit_open

                    record_circuit_open(candidate.provider, candidate.model)
                except Exception:
                    pass
            if just_opened:
                self._safe_alert(
                    candidate,
                    lambda: self._incident_dedup.should_notify_open(candidate.key),
                    LLMIncidentAlert(
                        incident_type="PRIMARY_DEGRADED",
                        severity="CRITICAL" if attempt == 1 else "WARNING",
                        environment=self._environment,
                        profile=profile.value,
                        provider=candidate.provider,
                        model=candidate.model,
                        reason="TIMEOUT" if is_timeout else type(exc).__name__,
                        p95_latency_ms=self._safe_p95(candidate.key),
                    ),
                )
            logger.warning(
                "LLM_CALL | profile=%s candidate=%s attempt=%d status=%s latency_ms=%.0f error=%s",
                profile.value,
                candidate.key,
                attempt,
                "timeout" if is_timeout else "error",
                latency_ms,
                exc,
            )
        else:
            # APPLICATION : ne touche PAS à la santé du candidat (§15) —
            # seulement visible en log/télémétrie pour investiguer le bug.
            logger.error(
                "LLM_CALL | profile=%s candidate=%s attempt=%d status=application_error error=%s",
                profile.value,
                candidate.key,
                attempt,
                exc,
                exc_info=True,
            )

        self._emit_telemetry(
            candidate=candidate,
            profile=profile,
            attempt=attempt,
            latency_s=latency_ms / 1000.0,
            status="error",
            completion=None,
            error=f"{type(exc).__name__}: {exc}",
            request_id=request_id,
            agent_node=agent_node,
            messages=messages,
            structured_required=structured_required,
            fallback_from=None,
            fallback_reason=None,
            provider_retry_count=provider_retry_count,
            failure_kind=failure_kind.value,
            primary_key=primary_key,
            extra_metadata=extra_metadata,
        )

    def _safe_alert(self, candidate: ModelCandidate, should_notify, alert: LLMIncidentAlert) -> None:
        """Enveloppe TOUT l'aller-retour de dédup+notification — jamais
        bloquant pour le tour utilisateur (même philosophie que
        `core/telemetry.py` : l'observabilité ne doit JAMAIS casser
        l'application). Une panne Redis/webhook au moment même où on essaie
        d'alerter une panne LLM ne doit pas EN PLUS faire échouer la réponse
        qui vient de réussir/échouer proprement."""
        try:
            if should_notify():
                self._notifier.send_incident(alert)
        except Exception as exc:
            logger.debug(
                "[llm_gateway] alerte non envoyée pour %s (%s) — non bloquant.",
                candidate.key,
                exc,
            )

    def _safe_p95(self, candidate_key: str) -> Optional[float]:
        try:
            return self._health.percentiles(candidate_key).get("p95")
        except Exception:
            return None

    def _emit_telemetry(
        self,
        *,
        candidate: ModelCandidate,
        profile: LLMProfile,
        attempt: int,
        latency_s: float,
        status: str,
        completion: Any,
        error: Optional[str],
        request_id: Optional[str],
        agent_node: Optional[str],
        messages: Any,
        structured_required: bool,
        fallback_from: Optional[str],
        fallback_reason: Optional[str],
        provider_retry_count: int = 0,
        failure_kind: Optional[str] = None,
        primary_key: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            from ladini.core.telemetry import record_generation

            output = None
            structured_valid: Optional[bool] = None
            usage = None
            estimated_cost_usd: Optional[float] = None
            if completion is not None:
                try:
                    output = completion.choices[0].message.content
                except Exception:
                    output = None
                usage_obj = getattr(completion, "usage", None)
                if usage_obj is not None:
                    prompt_tokens = getattr(usage_obj, "prompt_tokens", None)
                    completion_tokens = getattr(usage_obj, "completion_tokens", None)
                    usage = {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    }
                    # Spec §23 : coût estimé, prix venant de config (jamais
                    # codé en dur ici) — `None` si le modèle n'a pas de prix
                    # connu plutôt qu'une estimation inventée.
                    try:
                        from ladini.graphs.agents.market_coach.llm_gateway.cost import (
                            estimate_cost_usd,
                        )

                        estimated_cost_usd = estimate_cost_usd(
                            candidate.provider,
                            candidate.model,
                            prompt_tokens or 0,
                            completion_tokens or 0,
                        )
                    except Exception:
                        estimated_cost_usd = None
                if structured_required and output is not None:
                    structured_valid = _looks_like_json(output)

            # (spec §10/§45) : "requested" = candidat PRIMAIRE demandé pour
            # ce profil, "actual" = celui qui a réellement répondu (ou
            # échoué) — distincts dès qu'il y a fallback, jamais confondus.
            requested_provider, _, requested_model = (primary_key or candidate.key).partition(":")
            gateway_metadata: Dict[str, Any] = {
                "requested_provider": requested_provider or candidate.provider,
                "requested_model": requested_model or candidate.model,
                "actual_provider": candidate.provider,
                "actual_model": candidate.model,
                "provider_retry_count": provider_retry_count,
                "llm_call_category": "interpretation"
                if profile == LLMProfile.INTERPRETER
                else None,
            }
            if failure_kind is not None:
                gateway_metadata["failure_kind"] = failure_kind
            if estimated_cost_usd is not None:
                gateway_metadata["estimated_cost_usd"] = estimated_cost_usd
            if extra_metadata:
                gateway_metadata.update(extra_metadata)

            record_generation(
                model=candidate.model,
                messages=messages,
                output=output,
                latency_s=latency_s,
                usage=usage,
                error=error,
                name="llm_gateway_completion",
                profile=profile.value,
                provider=candidate.provider,
                attempt=attempt,
                fallback_from=fallback_from,
                fallback_reason=fallback_reason,
                structured_output_required=structured_required,
                structured_output_valid=structured_valid,
                request_id=request_id,
                agent_node=agent_node,
                extra_metadata=gateway_metadata,
            )
        except Exception:
            # Règle d'or telemetry.py : jamais bloquant pour le tour utilisateur.
            pass


def _estimate_tokens(messages: Any) -> int:
    """Estimation GROSSIÈRE (spec §16 : "ne nécessite pas une précision
    parfaite") du nombre de tokens d'entrée — mots × 1.3, même heuristique
    que celle utilisée pour les mesures de taille de prompt tout au long de
    ce chantier (voir les rapports A-F). Sert UNIQUEMENT à réserver un
    budget TPM avant l'appel, jamais la télémétrie de coût réelle (qui
    utilise `completion.usage`, l'usage EXACT renvoyé par le provider)."""
    try:
        text = " ".join(
            str(m.get("content") or "") for m in messages if isinstance(m, dict)
        )
        return int(len(text.split()) * 1.3)
    except Exception:
        return 0


def _looks_like_json(text: str) -> bool:
    try:
        import json

        json.loads(text)
        return True
    except Exception:
        return False


def _default_client_for_provider(provider: str) -> Any:
    """Mappe un nom de provider vers un client réel — réutilise TEL QUEL les
    adapters/factories existants de `core/get_llm.py` (aucune réécriture)."""
    from ladini.core.get_llm import (
        _BedrockAdapter,
        _GroqAdapter,
        get_bedrock_client,
        get_groq_sdk,
        get_openai_compatible_sdk,
    )

    if provider == "groq":
        return _GroqAdapter(get_groq_sdk(), provider="groq")
    if provider == "bedrock_gateway":
        # (2026-09-26, audit LLM_GATEWAY_EXHAUSTED) : `provider=` distingue
        # cette passerelle Bedrock (compatible OpenAI) du vrai SDK Groq —
        # les deux partagent `_GroqAdapter` (même protocole HTTP), mais SEUL
        # le vrai Groq doit tenter le repli legacy `_fallback_model_for` sur
        # 429 (un ID Groq notation slash n'a aucun sens pour cette
        # passerelle) — voir `core/get_llm.py::_GroqAdapter._create_via_primary`.
        return _GroqAdapter(get_openai_compatible_sdk(), provider="bedrock_gateway")
    if provider == "bedrock_native":
        return _BedrockAdapter(get_bedrock_client())
    raise LLMGatewayExhausted(
        f"Provider inconnu: {provider!r}", reason="PROVIDER_UNKNOWN"
    )


class LegacyOverrideGateway:
    """Compat tests/appelants legacy — `MarketRuntime(llm_client=...)` (voir
    `utils.py::MarketRuntime._llm_override`). Bypasse ENTIÈREMENT le
    registry/health/circuit (donc AUCUN accès réseau/Redis — critique pour
    la philosophie "AUCUN réseau" de la suite de tests, `tests/conftest.py`) :
    appelle le client fourni directement, exactement comme avant
    l'introduction du Gateway. Un seul client fixe, sans repli ni santé
    partagée — jamais utilisé en production (voir `get_llm_gateway()`)."""

    def __init__(self, client: Any, model_resolver: Any):
        self._client = client
        self._model_resolver = model_resolver  # callable() -> Optional[str]

    async def complete(
        self,
        *,
        profile: LLMProfile,
        messages: Any,
        response_format: Optional[dict] = None,
        request_id: Optional[str] = None,
        agent_node: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        # `extra_metadata` (2026-09-12, instrumentation Langfuse) accepté
        # pour compatibilité de signature avec le Gateway complet, mais
        # SANS EFFET ici : cette passerelle de test ne fait aucune
        # télémétrie (voir docstring de classe) — jamais transmis à
        # `call_kwargs`/au client réel.
        model = self._model_resolver() or "llama-3.3-70b-versatile"
        call_kwargs: Dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            call_kwargs["response_format"] = response_format
        call_kwargs.update(kwargs)
        return await asyncio.to_thread(
            lambda: self._client.chat.completions.create(**call_kwargs)
        )

    def primary_model_name(self, profile: LLMProfile) -> Optional[str]:
        return self._model_resolver()


def resolve_gateway(mc_runtime: Any) -> Any:
    """Résout `mc_runtime.llm_gateway` avec repli pour les runtimes minimaux
    qui n'exposent que `.llm`/`.model_answer` (tests ad-hoc — ex:
    `type("RT", (), {"llm": ..., "model_answer": ...})()`, un pattern présent
    dans plusieurs fichiers de tests légers). Même esprit défensif que
    l'ancien `getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile")`
    que ce Gateway remplace — jamais un `AttributeError` juste parce qu'un
    test n'a pas encore été mis à jour vers la forme complète `MarketRuntime`."""
    gateway = getattr(mc_runtime, "llm_gateway", None)
    if gateway is not None:
        return gateway
    llm = getattr(mc_runtime, "llm", None)
    return LegacyOverrideGateway(llm, lambda: getattr(mc_runtime, "model_answer", None))


def resolve_profile(mc_runtime: Any) -> LLMProfile:
    profile = getattr(mc_runtime, "profile_answer", None)
    if isinstance(profile, LLMProfile):
        return profile
    return LLMProfile.REASONING


# ── Singleton process-wide — §50 "une seule Gateway" ────────────────
_GATEWAY_SINGLETON: Optional[LLMGateway] = None


def get_llm_gateway(force_refresh: bool = False) -> LLMGateway:
    global _GATEWAY_SINGLETON
    if not force_refresh and _GATEWAY_SINGLETON is not None:
        return _GATEWAY_SINGLETON
    _GATEWAY_SINGLETON = LLMGateway()
    return _GATEWAY_SINGLETON
