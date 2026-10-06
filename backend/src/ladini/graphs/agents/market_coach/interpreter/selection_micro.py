"""Orchestration réseau du micro-prompt SELECTION (chantier "State Router +
micro-prompts", INCRÉMENT B, 2026-09-12).

Appelé UNIQUEMENT quand `interpreter/state_router.py::choose_interpretation_route`
résout à `InterpretationRoute.SELECTION` — voir le point de branchement dans
`interpreter/routing.py::_input_interpreter_impl`. Toutes les autres routes
(NEW_TASK/ACTIVE_SLOT/STRUCTURED_ACTION) continuent d'utiliser l'interpréteur
unifié historique, inchangé par ce module.

## Budget d'appels (spec §13/§25/§33)

- Sélection normale (JSON valide dès le 1er essai) : **1 appel**.
- JSON invalide (schéma/bornes) : **+1 repair** (max), jamais de 3ᵉ appel —
  si le repair échoue encore, résultat `UNKNOWN` direct, PAS d'escalade
  GPT-OSS 120B automatique.
- `event=INTERRUPTION` : ce module ne fait PAS de second appel lui-même —
  il retourne `SelectionOutcome.INTERRUPTION`, et c'est l'APPELANT
  (`routing.py`) qui déclenche ensuite le classifier NEW_TASK existant
  (comptabilisé séparément par `llm_call_index`, même compteur partagé).

## Cache / idempotence (spec §29/§56)

Réutilise EXACTEMENT les mêmes primitives que l'interpréteur unifié
(`core/idempotency.get_cached`/`set_cached`/`increment`) — pas un second
système. La clé est sensible à `message_sid` + `SELECTION_PROMPT_VERSION` +
modèle + hash(system+user), dans un espace de noms distinct
(`selection_llm_cache:...`) de celui de l'interpréteur unifié
(`interp_llm_cache:...`) : aucune collision possible, chaque famille de
prompt a son propre cache."""

from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ladini.core.idempotency import get_cached, increment, set_cached
from ladini.graphs.agents.market_coach.interpreter.selection_contract import (
    SelectionEvent,
    SelectionInterpretation,
    adapt_selection_to_canonical,
    index_within_bounds,
)
from ladini.graphs.agents.market_coach.interpreter.selection_prompts import (
    SELECTION_PROMPT_VERSION,
    SELECTION_SYSTEM_PROMPT,
    build_selection_repair_prompt,
    build_selection_user_prompt,
)

logger = logging.getLogger("ladini.interpreter.selection_micro")

# (2026-09-12, Phase B.1 — validation live) : la sortie JSON elle-même est
# extrêmement courte (spec §23), mais les modèles Groq RÉELLEMENT
# disponibles aujourd'hui pour le profil INTERPRETER (`openai/gpt-oss-20b`/
# `120b` — tous les llama "instant" classiques sont décommissionnés, voir
# settings.py) sont des modèles DE RAISONNEMENT : ils consomment des tokens
# de "réflexion" internes, DÉCOMPTÉS de `max_tokens`, avant même d'émettre
# le JSON final. `max_tokens=120` mesuré en échec RÉEL et systématique
# (Groq HTTP 400 "max completion tokens reached before generating a valid
# document") — jamais un appel utile n'aboutissait. 600 est la valeur
# vérifiée empiriquement suffisante sur ce prompt (marge incluse) ; plafond
# explicite, jamais illimité (spec §28), mais plus haut que prévu
# initialement car le paysage de modèles Groq a changé depuis la rédaction
# de la spec.
_MAX_TOKENS = 600

_CACHE_TTL_SECONDS = 3600


class SelectionOutcome(str, Enum):
    #: Un résultat canonique final est prêt — l'appelant le retourne tel quel.
    RESULT = "result"
    #: `event=INTERRUPTION` — l'appelant doit retomber sur le classifier
    #: NEW_TASK existant avec le message original (spec §12/§13/§18).
    INTERRUPTION = "interruption"
    #: Échec infrastructurel (pas un simple JSON invalide, déjà couvert par
    #: le repair) — l'appelant doit retomber sur l'interpréteur unifié
    #: legacy, tracé explicitement `legacy_fallback=true` (spec §26).
    LEGACY_FALLBACK = "legacy_fallback"


def _cache_key(
    message_sid: Optional[str],
    system_prompt: str,
    user_prompt: str,
    requested_model: Optional[str],
) -> Optional[str]:
    """Même construction que `routing.py::_llm_cache_key`, espace de noms
    distinct (`selection_llm_cache` vs `interp_llm_cache`) — voir docstring
    de module."""
    if not message_sid:
        return None
    digest = hashlib.sha256(
        f"{system_prompt}\x00{user_prompt}".encode("utf-8")
    ).hexdigest()[:16]
    model_part = requested_model or "unknown_model"
    return (
        f"selection_llm_cache:{SELECTION_PROMPT_VERSION}:{model_part}:"
        f"{message_sid}:{digest}"
    )


def _parse_and_validate(
    raw_content: str, num_candidates: int
) -> Tuple[Optional[SelectionInterpretation], str]:
    """Retourne `(None, raison)` si invalide — la raison alimente le prompt
    de repair (spec §24), jamais renvoyée à l'utilisateur final."""
    try:
        payload = json.loads(raw_content or "{}")
    except Exception:
        return None, "le JSON n'a pas pu être décodé"
    try:
        interpretation = SelectionInterpretation.model_validate(payload)
    except ValidationError as exc:
        return None, f"schéma invalide ({exc.errors()[0].get('msg', 'erreur')})"
    if not index_within_bounds(interpretation, num_candidates):
        return None, (
            f"selection_index hors bornes (doit être entre 1 et {num_candidates})"
        )
    return interpretation, ""


def _outcome_for_interpretation(
    interpretation: SelectionInterpretation,
    state: Optional[Dict[str, Any]] = None,
    text: str = "",
) -> Tuple[SelectionOutcome, Optional[Dict[str, Any]]]:
    if interpretation.event == SelectionEvent.INTERRUPTION:
        return SelectionOutcome.INTERRUPTION, None
    result = adapt_selection_to_canonical(interpretation)
    if interpretation.reference is not None:
        return SelectionOutcome.RESULT, _apply_generic_reference(result, interpretation.reference, state)
    return SelectionOutcome.RESULT, _apply_domain_date_reference(result, state, text)


def _clarify(result: Dict[str, Any], message: str, reason: str) -> Dict[str, Any]:
    """Aucune mutation : message ciblé (affiché par `render_selection_menu`), pas de menu rejoué, pas de retry."""
    return {
        **result,
        "interpreted_event": "UNKNOWN",
        "interpreter_confidence": 0.3,
        "extracted_entities": {},
        "raw_analysis": {
            **(result.get("raw_analysis") or {}),
            "selection_clarification": message,
            "selection_resolution": reason,
            "interaction_mode": "CLARIFICATION",
        },
    }


def _apply_generic_reference(result: Dict[str, Any], reference: Any, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Désignation NATURELLE d'une option d'un menu générique (enchères, offres, stocks…) : résolue en Python contre les faits
    VISIBLES figés à l'affichage (`working_memory["menu_facts"]`). Péremption respectée ; le modèle ne produit jamais d'index
    ici, et une ambiguïté / un manque -> clarification ciblée sans aucune mutation."""
    import time

    from ladini.graphs.agents.market_coach.domain.menu_facts import (
        is_stale,
        visible_options,
    )
    from ladini.graphs.agents.market_coach.domain.selection_reference import (
        ReferenceType,
        Status,
        resolve_reference,
    )

    facts = ((state or {}).get("working_memory") or {}).get("menu_facts")
    if not isinstance(facts, dict) or not facts.get("options"):
        return _clarify(result, "Je ne retrouve pas la liste à laquelle tu fais référence — redemande-la et je te la réaffiche.", "no_menu_facts")
    if is_stale(facts, time.time()):
        logger.info("interaction_mode=CLARIFICATION | stale_menu | kind=%s", facts.get("kind"))
        return _clarify(result, "Cette liste date un peu et a pu changer — redemande-la et je te la réaffiche à jour.", "stale_menu")
    if reference.reference_type in (ReferenceType.PAGINATION, ReferenceType.REFINEMENT, ReferenceType.NONE_OF_THESE):
        return _clarify(result, "Je n'ai pas d'autres éléments à te montrer pour cette liste — choisis parmi ceux affichés.", "unsupported_reference")
    resolution = resolve_reference(reference, visible_options(facts))
    if resolution.status == Status.EXACT and resolution.index is not None:
        logger.info("interaction_mode=NATURAL_REFERENCE | generic_menu=%s | resolved_index=%s", facts.get("kind"), resolution.index)
        return {
            **result,
            "extracted_entities": {"selection_index": resolution.index},
            "raw_analysis": {**(result.get("raw_analysis") or {}), "reference_resolved": "reference", "interaction_mode": "NATURAL_REFERENCE"},
        }
    logger.info("interaction_mode=CLARIFICATION | status=%s | reason=%s", resolution.status.value, resolution.reason)
    return _clarify(result, resolution.message or "Je n'ai pas bien identifié l'option — peux-tu préciser ?", resolution.status.value)


def _apply_domain_date_reference(
    result: Dict[str, Any], state: Optional[Dict[str, Any]], text: str
) -> Dict[str, Any]:
    """B27 — « celle de demain », « celui du 5 octobre » : le modèle a extrait la référence ; le DOMAINE calcule la date et choisit
    l'option parmi les dates AFFICHÉES (jamais une date écrite par le modèle, jamais son index). Une seule option -> index
    posé par le domaine ; plusieurs -> clarification (`reference_candidates`) ; aucune -> le résultat du modèle reste soumis
    aux gardes habituelles."""
    reference = (result.get("raw_analysis") or {}).get("date_reference")
    if not reference or state is None:
        return result
    from ladini.graphs.agents.market_coach.interpreter.context_arbitration import (
        live_menu_view,
        resolve_date_reference,
    )

    view = live_menu_view(state)
    if view is None:
        return result
    kind, hits = resolve_date_reference(view, reference, text)
    raw = dict(result.get("raw_analysis") or {})
    if kind == "one":
        return {**result, "extracted_entities": {"selection_index": int(hits[0])},
                "raw_analysis": {**raw, "reference_resolved": "date"}}
    if kind == "many":
        return {**result, "interpreted_event": "UNKNOWN", "interpreter_confidence": 0.3,
                "extracted_entities": {"reference_candidates": hits},
                "raw_analysis": {**raw, "clarification_reason": "reference_not_unique"}}
    return result


async def run_selection_microprompt(
    state: Dict[str, Any],
    mc_runtime: Any,
    text: str,
    locked_goal: Optional[str],
) -> Tuple[SelectionOutcome, Optional[Dict[str, Any]]]:
    """Point d'entrée unique de ce module — voir le branchement dans
    `routing.py`. Ne lève jamais : toute exception infrastructurelle
    (gateway indisponible, timeout non catché plus bas) doit être catchée
    par L'APPELANT, qui retombe alors sur `SelectionOutcome.LEGACY_FALLBACK`
    lui-même (voir `routing.py`) — ce module reste volontairement simple, il
    ne gère pas sa propre exception de dernier recours pour ne pas dupliquer
    la logique de repli déjà présente côté appelant."""
    from ladini.core.telemetry import get_trace_id, record_generation
    from ladini.graphs.agents.market_coach.llm_gateway import (
        LLMProfile,
        resolve_gateway,
    )

    candidates: List[str] = [
        str(c) for c in (state.get("expected_candidates") or [])
    ]
    last_agent_question = state.get("last_agent_question")
    if not candidates:
        # B23 : le menu récurrent (liste des besoins / écran d'un besoin) ne publie pas `expected_candidates` — le modèle
        # voyait « (aucune option listée) » et « choisissait » au hasard. L'écran vivant est une ATTENTE : on montre ce
        # qu'il propose, le modèle tranche (réponse / interruption / ambigu) et Python valide les bornes ci-dessous.
        from ladini.graphs.agents.market_coach.interpreter.context_arbitration import (
            live_menu_view,
        )

        _view = live_menu_view(state)
        if _view is not None:
            candidates = list(_view["labels"])
            last_agent_question = last_agent_question or _view["title"]
    message_sid = state.get("message_sid")

    system_prompt = SELECTION_SYSTEM_PROMPT
    user_prompt = build_selection_user_prompt(
        current_goal=locked_goal or "AUCUN",
        last_agent_question=last_agent_question or "—",
        candidates=candidates,
        normalized_text=text,
    )

    gateway = resolve_gateway(mc_runtime)
    requested_model = gateway.primary_model_name(LLMProfile.INTERPRETER)
    cache_key = _cache_key(message_sid, system_prompt, user_prompt, requested_model)

    base_metadata: Dict[str, Any] = {
        "message_sid": message_sid,
        "prompt_version": SELECTION_PROMPT_VERSION,
        "prompt_family": "selection",
        "interpretation_route": "selection",
        "current_goal": locked_goal,
        "expected_input": "SELECTION",
    }

    cached_raw = get_cached(cache_key)
    if cached_raw:
        try:
            interpretation = SelectionInterpretation.model_validate(
                json.loads(cached_raw)
            )
        except Exception:
            interpretation = None
        if interpretation is not None:
            logger.info(
                "[Interpreter SELECTION] résultat réutilisé du cache "
                "(retry sans nouveau coût LLM)"
            )
            try:
                record_generation(
                    model=requested_model or "unknown",
                    messages=[{"role": "user", "content": user_prompt}],
                    output=interpretation.model_dump_json(),
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
            return _outcome_for_interpretation(interpretation, state, text)

    call_count_key = f"llm_call_count:{message_sid}" if message_sid else None

    async def _call(user_content: str, *, repair: bool) -> Optional[str]:
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
    interpretation, reason = _parse_and_validate(raw_content, len(candidates))

    if interpretation is None:
        logger.info(
            "[Interpreter SELECTION] réponse invalide (%s) — 1 repair retry", reason
        )
        repair_prompt = build_selection_repair_prompt(
            num_candidates=len(candidates), reason=reason
        )
        raw_content = await _call(repair_prompt, repair=True)
        interpretation, reason = _parse_and_validate(raw_content, len(candidates))

    if interpretation is None:
        # Repair épuisé (spec §25) : UNKNOWN direct, PAS d'escalade GPT-OSS
        # 120B automatique, PAS de 3ᵉ appel.
        logger.warning(
            "[Interpreter SELECTION] JSON toujours invalide après repair "
            "(%s) — UNKNOWN",
            reason,
        )
        return SelectionOutcome.RESULT, {
            "interpreted_event": "UNKNOWN",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": 0.0,
            "extracted_entities": {},
            "raw_analysis": {"path": "selection_microprompt_repair_failed"},
        }

    if cache_key:
        set_cached(
            cache_key, interpretation.model_dump_json(), ttl_seconds=_CACHE_TTL_SECONDS
        )

    return _outcome_for_interpretation(interpretation, state, text)


__all__ = ["SelectionOutcome", "run_selection_microprompt"]
