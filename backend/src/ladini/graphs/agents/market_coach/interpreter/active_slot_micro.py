"""Orchestration réseau du micro-prompt ACTIVE_SLOT (chantier "State
Router + micro-prompts", Phase C, 2026-09-12).

Appelé UNIQUEMENT quand `interpreter/state_router.py::choose_interpretation_route`
résout à `InterpretationRoute.ACTIVE_SLOT` — voir le point de branchement
dans `interpreter/routing.py::_input_interpreter_impl`. Miroir structurel
de `interpreter/selection_micro.py` (Incrément B) — mêmes conventions de
cache/repair/télémétrie/legacy fallback, adapté au contrat ACTIVE_SLOT.

## Budget d'appels (même politique que SELECTION, spec Phase C §17/§39)

- Réponse normale (JSON valide dès le 1er essai) : **1 appel**.
- JSON invalide (schéma) : **+1 repair** (max), jamais de 3ᵉ appel — si le
  repair échoue encore, résultat `UNKNOWN` direct.
- `disposition=DEVIATION` (ou confiance insuffisante, traitée pareil) : ce
  module ne fait PAS de second appel lui-même — il retourne
  `ActiveSlotOutcome.DEVIATION`, et c'est l'APPELANT (`routing.py`) qui
  déclenche ensuite le classifier NEW_TASK existant (2e appel, comptabilisé
  séparément par `llm_call_index`, même compteur partagé que SELECTION).

## Sécurité FastPathPolicy (spec §19 — critique)

`core/policies.py::FastPathPolicy.for_buyer()` fait bypasser
`cognitive_guard` (le mécanisme d'interruption) pour tout événement ANSWER
dans un but acheteur du tunnel — une DEVIATION mal classée ANSWER
laisserait donc passer une vraie nouvelle tâche sans jamais être détectée
comme interruption. D'où la politique CONSERVATRICE ci-dessous : disposition
DEVIATION *ou* confiance sous le seuil d'interruption partagé
(`core/tunnel_manager.py::INTERRUPTION_CONFIDENCE_THRESHOLD`, réutilisé tel
quel — spec §20, pas de nouveau seuil inventé) retombent TOUS deux sur le
classifier NEW_TASK plutôt que de risquer un faux ANSWER.

## Profil / cache (spec §16/§17/§41)

Réutilise `LLMProfile.INTERPRETER` (Phase B.1) — PAS de 4ᵉ profil. Cache
dans un espace de noms distinct (`active_slot_llm_cache:...`), même
construction que `selection_micro.py::_cache_key`."""

from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import ValidationError

from ladini.core.idempotency import get_cached, increment, set_cached
from ladini.graphs.agents.market_coach.interpreter.active_slot_contract import (
    ActiveSlotContext,
    ActiveSlotDecision,
    ActiveSlotDisposition,
    adapt_active_slot_to_canonical,
)
from ladini.graphs.agents.market_coach.interpreter.active_slot_prompts import (
    ACTIVE_SLOT_PROMPT_VERSION,
    ACTIVE_SLOT_SYSTEM_PROMPT,
    build_active_slot_repair_prompt,
    build_active_slot_user_prompt,
)

logger = logging.getLogger("ladini.interpreter.active_slot_micro")

# Réponse attendue courte mais plus riche que SELECTION (multi-champs
# possibles) — la Phase B.1 a mesuré en LIVE que `max_tokens=120` échouait
# systématiquement sur les modèles de raisonnement Groq réellement
# disponibles (`openai/gpt-oss-*`, tokens de "réflexion" internes décomptés
# avant le JSON final). Réutilise la même valeur validée empiriquement
# (spec §17 : "Réutiliser cette valeur ou la primitive/configuration
# actuelle équivalente", "ne pas réduire agressivement avant validation
# live") — voir `selection_micro.py::_MAX_TOKENS`.
_MAX_TOKENS = 600

_CACHE_TTL_SECONDS = 3600


class ActiveSlotOutcome(str, Enum):
    #: Un résultat canonique final est prêt — l'appelant le retourne tel quel.
    RESULT = "result"
    #: Déviation confirmée (ou confiance insuffisante) — l'appelant doit
    #: retomber sur le classifier NEW_TASK existant avec le message
    #: original (spec §11/§12/§18/§20).
    DEVIATION = "deviation"
    #: Échec infrastructurel — l'appelant doit retomber sur l'interpréteur
    #: unifié legacy, tracé explicitement `legacy_fallback=true` (spec §52).
    LEGACY_FALLBACK = "legacy_fallback"


def _cache_key(
    message_sid: Optional[str],
    system_prompt: str,
    user_prompt: str,
    requested_model: Optional[str],
) -> Optional[str]:
    """Même construction que `selection_micro.py::_cache_key`, espace de
    noms distinct (`active_slot_llm_cache` vs `selection_llm_cache` vs
    `interp_llm_cache`)."""
    if not message_sid:
        return None
    digest = hashlib.sha256(
        f"{system_prompt}\x00{user_prompt}".encode("utf-8")
    ).hexdigest()[:16]
    model_part = requested_model or "unknown_model"
    return (
        f"active_slot_llm_cache:{ACTIVE_SLOT_PROMPT_VERSION}:{model_part}:"
        f"{message_sid}:{digest}"
    )


def _known_entities_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Informations déjà connues pertinentes pour ce tour — même filtre que
    `nodes/rendering/ask.py::generate_llm_question` (exclut IDs/téléphone/
    valeurs vides), réutilisé pour rester cohérent avec ce que l'utilisateur
    a déjà vu formulé dans les questions précédentes de l'agent."""
    payload = state.get("transaction_payload")
    if not isinstance(payload, dict):
        return {}
    return {
        k: v
        for k, v in payload.items()
        if v not in (None, "", [], {}) and not k.endswith("_id") and k != "phone"
    }


def _parse_and_validate(
    raw_content: str,
) -> Tuple[Optional[ActiveSlotDecision], str]:
    """Retourne `(None, raison)` si invalide — la raison alimente le prompt
    de repair, jamais renvoyée à l'utilisateur final."""
    try:
        payload = json.loads(raw_content or "{}")
    except Exception:
        return None, "le JSON n'a pas pu être décodé"
    try:
        decision = ActiveSlotDecision.model_validate(payload)
    except ValidationError as exc:
        return None, f"schéma invalide ({exc.errors()[0].get('msg', 'erreur')})"
    return decision, ""


def _outcome_for_decision(
    decision: ActiveSlotDecision, context: ActiveSlotContext
) -> Tuple[ActiveSlotOutcome, Optional[Dict[str, Any]]]:
    from ladini.graphs.agents.market_coach.core.tunnel_manager import (
        INTERRUPTION_CONFIDENCE_THRESHOLD,
    )

    if decision.disposition == ActiveSlotDisposition.UNKNOWN:
        return ActiveSlotOutcome.RESULT, adapt_active_slot_to_canonical(decision, context)
    if decision.disposition == ActiveSlotDisposition.DEVIATION:
        return ActiveSlotOutcome.DEVIATION, None
    if decision.confidence < INTERRUPTION_CONFIDENCE_THRESHOLD:
        # Politique conservatrice (spec §19/§20) : une ANSWER/UPDATE/REJECT
        # peu sûre est traitée comme une déviation potentielle plutôt que de
        # risquer un faux ANSWER qui bypasserait cognitive_guard.
        logger.info(
            "[Interpreter ACTIVE_SLOT] confiance %.2f < seuil %.2f pour "
            "disposition=%s — repli conservateur vers NEW_TASK",
            decision.confidence,
            INTERRUPTION_CONFIDENCE_THRESHOLD,
            decision.disposition.value,
        )
        return ActiveSlotOutcome.DEVIATION, None
    return ActiveSlotOutcome.RESULT, adapt_active_slot_to_canonical(decision, context)


async def run_active_slot_microprompt(
    state: Dict[str, Any],
    mc_runtime: Any,
    text: str,
    context: ActiveSlotContext,
) -> Tuple[ActiveSlotOutcome, Optional[Dict[str, Any]]]:
    """Point d'entrée unique de ce module — voir le branchement dans
    `routing.py`. Ne lève jamais côté logique métier : toute exception
    infrastructurelle doit être catchée par L'APPELANT, qui retombe alors
    sur `ActiveSlotOutcome.LEGACY_FALLBACK` lui-même (même discipline que
    `selection_micro.py::run_selection_microprompt`)."""
    from ladini.core.telemetry import get_trace_id, record_generation
    from ladini.graphs.agents.market_coach.llm_gateway import (
        LLMProfile,
        resolve_gateway,
    )

    message_sid = state.get("message_sid")
    known_entities = _known_entities_from_state(state)

    system_prompt = ACTIVE_SLOT_SYSTEM_PROMPT
    user_prompt = build_active_slot_user_prompt(
        goal=context.goal or "AUCUN",
        category=context.category,
        field_name=context.field_name or "",
        known_entities=known_entities,
        normalized_text=text,
    )

    gateway = resolve_gateway(mc_runtime)
    requested_model = gateway.primary_model_name(LLMProfile.INTERPRETER)
    cache_key = _cache_key(message_sid, system_prompt, user_prompt, requested_model)

    base_metadata: Dict[str, Any] = {
        "message_sid": message_sid,
        "prompt_version": ACTIVE_SLOT_PROMPT_VERSION,
        "prompt_family": "active_slot",
        "interpretation_route": "active_slot",
        "current_goal": context.goal,
        "expected_input": context.category,
    }

    cached_raw = get_cached(cache_key)
    if cached_raw:
        try:
            decision = ActiveSlotDecision.model_validate(json.loads(cached_raw))
        except Exception:
            decision = None
        if decision is not None:
            logger.info(
                "[Interpreter ACTIVE_SLOT] résultat réutilisé du cache "
                "(retry sans nouveau coût LLM)"
            )
            try:
                record_generation(
                    model=requested_model or "unknown",
                    messages=[{"role": "user", "content": user_prompt}],
                    output=decision.model_dump_json(),
                    latency_s=0.0,
                    usage=None,
                    name="llm_gateway_completion",
                    agent_node="input_interpreter",
                    extra_metadata={
                        **base_metadata,
                        "cache_hit": True,
                        "repair_retry": False,
                        "legacy_fallback": False,
                    },
                )
            except Exception:
                pass
            return _outcome_for_decision(decision, context)

    call_count_key = f"llm_call_count:{message_sid}" if message_sid else None

    async def _call(user_content: str, *, repair: bool) -> str:
        llm_call_index = (
            increment(call_count_key, ttl_seconds=300) if call_count_key else None
        )
        completion = await gateway.complete(
            profile=LLMProfile.INTERPRETER,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=_MAX_TOKENS,
            request_id=get_trace_id(),
            agent_node="input_interpreter",
            extra_metadata={
                **base_metadata,
                "cache_hit": False,
                "llm_call_index": llm_call_index,
                "repair_retry": repair,
                "legacy_fallback": False,
            },
        )
        return completion.choices[0].message.content or "{}"

    raw_content = await _call(user_prompt, repair=False)
    decision, reason = _parse_and_validate(raw_content)

    if decision is None:
        logger.info(
            "[Interpreter ACTIVE_SLOT] réponse invalide (%s) — 1 repair retry", reason
        )
        repair_prompt = build_active_slot_repair_prompt(reason=reason)
        raw_content = await _call(repair_prompt, repair=True)
        decision, reason = _parse_and_validate(raw_content)

    if decision is None:
        logger.warning(
            "[Interpreter ACTIVE_SLOT] JSON toujours invalide après repair "
            "(%s) — UNKNOWN",
            reason,
        )
        return ActiveSlotOutcome.RESULT, {
            "interpreted_event": "UNKNOWN",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": 0.0,
            "extracted_entities": {},
            "raw_analysis": {"path": "active_slot_micro_repair_failed"},
        }

    if cache_key:
        set_cached(cache_key, decision.model_dump_json(), ttl_seconds=_CACHE_TTL_SECONDS)

    return _outcome_for_decision(decision, context)


__all__ = ["ActiveSlotOutcome", "run_active_slot_microprompt"]
