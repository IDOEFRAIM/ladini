"""Orchestration réseau du micro-prompt STRUCTURED_ACTION (chantier "State
Router + micro-prompts", Incrément D, 2026-09-12).

Appelé UNIQUEMENT quand `domain/selection_actions.py::build_selection_context`
résout un `expected_action` non nul (tunnel producteur/palier actif) ET que
le fast-path déterministe (`fast_path_action`, chiffre nu) n'a rien produit
— voir le point de branchement dans
`interpreter/routing.py::_input_interpreter_impl`, étape 0.5. Miroir
structurel de `selection_micro.py`/`active_slot_micro.py` — mêmes
conventions de cache/repair/télémétrie/legacy fallback.

## Sécurité renforcée (spec §46) : "il vaut mieux UNKNOWN qu'un mauvais
producteur ou mauvais tier"

Les événements `SELECTION`/`ANSWER` produits ici bypassent déjà
`cognitive_guard` pour les buts du tunnel panier acheteur
(`core/policies.py::FastPathPolicy.for_buyer`) — un ID mal résolu ou une
déviation mal classée ACTION exécuterait silencieusement une MAUVAISE
action métier (mauvais producteur, mauvais palier). Politique conservatrice
donc PLUS stricte que C.1 : toute résolution qui ÉCHOUE (index hors bornes,
`selected_value` ambigu, action rejetée par `validate_action`) retombe sur
`UNKNOWN` — jamais une supposition, jamais un fallthrough NEW_TASK inutile
(coûterait un 2e appel pour rien, la donnée n'était de toute façon pas
fiable). Seule une confiance insuffisante sur une ACTION par ailleurs
résoluble retombe sur `UNKNOWN` également (spec §46) — DEVIATION reste géré
séparément (fallthrough NEW_TASK, même principe que C.1)."""

from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import ValidationError

from ladini.core.idempotency import get_cached, increment, set_cached
from ladini.domain.quantity_unit import text_states_a_quantity
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    SelectionContext,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_contract import (
    StructuredActionDecision,
    StructuredActionDisposition,
    StructuredActionPromptContext,
    adapt_structured_action_to_canonical,
    build_structured_action_prompt_context,
    resolve_and_validate,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_prompts import (
    STRUCTURED_ACTION_PROMPT_VERSION,
    build_structured_action_repair_prompt,
    build_structured_action_system_prompt,
    build_structured_action_user_prompt,
)

logger = logging.getLogger("ladini.interpreter.structured_action_micro")

# Sortie attendue très courte, mais même famille de modèle de raisonnement
# Groq que SELECTION/ACTIVE_SLOT (tokens de "réflexion" internes décomptés
# avant le JSON final, validation live Phase B.1).
#
# (2026-09-13, Incrément D.1) : relevé de 600 à 800 — le prompt
# SET_PACKAGE_COUNT s'est allongé (règle de changement de tier explicite +
# exemple) et `openai/gpt-oss-20b` a été observé en LIVE heurtant à nouveau
# "max completion tokens reached before generating a valid document" sur ce
# cas précis (600 devenait limite). 800 vérifié empiriquement suffisant.
_MAX_TOKENS = 800

_CACHE_TTL_SECONDS = 3600


class StructuredActionOutcome(str, Enum):
    RESULT = "result"
    #: Déviation confirmée — l'appelant doit retomber sur le classifier
    #: NEW_TASK existant avec le message original (spec §8/§29).
    DEVIATION = "deviation"
    #: Échec infrastructurel — l'appelant doit retomber sur l'interpréteur
    #: unifié legacy, tracé explicitement `legacy_fallback=true` (spec §49).
    LEGACY_FALLBACK = "legacy_fallback"


def _cache_key(
    message_sid: Optional[str],
    system_prompt: str,
    user_prompt: str,
    requested_model: Optional[str],
) -> Optional[str]:
    if not message_sid:
        return None
    digest = hashlib.sha256(
        f"{system_prompt}\x00{user_prompt}".encode("utf-8")
    ).hexdigest()[:16]
    model_part = requested_model or "unknown_model"
    return (
        f"structured_action_llm_cache:{STRUCTURED_ACTION_PROMPT_VERSION}:"
        f"{model_part}:{message_sid}:{digest}"
    )


def _unknown_result(path: str) -> Dict[str, Any]:
    return {
        "interpreted_event": "UNKNOWN",
        "detected_intent": "UNKNOWN",
        "interpreter_confidence": 0.0,
        "extracted_entities": {},
        "raw_analysis": {"path": path},
    }


def _reject_result(locked_goal: Optional[str], confidence: float) -> Dict[str, Any]:
    return {
        "interpreted_event": "REJECT",
        "detected_intent": str(locked_goal or "UNKNOWN").upper(),
        "interpreter_confidence": confidence,
        "extracted_entities": {},
        "raw_analysis": {"path": "structured_action_micro"},
    }


def _infer_missing_action(
    payload: Dict[str, Any], prompt_context: StructuredActionPromptContext
) -> Dict[str, Any]:
    """Complète un champ `action` manquant quand — et SEULEMENT quand — il
    est déjà déterminé SANS AMBIGUÏTÉ par le contexte + les champs
    effectivement remplis. Ce n'est PAS une supposition sur l'intention de
    l'utilisateur (déjà tranchée par le LLM via `disposition=ACTION` et les
    champs qu'il a choisi de remplir) — seulement un remplissage d'un champ
    de FORME que le modèle omet de façon répétée et déterministe en
    validation live (2026-09-13, Incrément D.1) malgré une instruction
    explicite ("Pose OBLIGATOIREMENT le champ action").

    Diagnostic confirmé (6 reproductions déterministes, 2 modèles Groq
    différents, `max_tokens` porté à 800 sans effet) : pour
    SET_PACKAGE_COUNT, le modèle reconnaît correctement un changement de
    conditionnement (il pose `selection_index`/`selected_value`) mais omet
    `action="SELECT_PRICING_TIER"`. Pour les 3 autres `expected_action`,
    il n'existe de toute façon QU'UNE SEULE action possible dans ce
    contexte — l'omission y est donc TOUJOURS non-ambiguë à combler.

    Ne modifie RIEN si `action` est déjà présent, ou si la disposition
    n'est pas `ACTION`, ou si le motif de champs ne permet pas de
    trancher sans ambiguïté (ex: SET_PACKAGE_COUNT sans AUCUN champ
    rempli — reste invalide, correctement rejeté ensuite)."""
    if payload.get("disposition") != "ACTION" or payload.get("action") is not None:
        return payload

    expected = prompt_context.expected_action
    if expected in (ActionType.SELECT_PRODUCER, ActionType.SELECT_PRICING_TIER, ActionType.SET_QUANTITY):
        # Une seule action possible dans ces contextes — jamais d'ambiguïté.
        payload = {**payload, "action": expected.value}
    elif expected == ActionType.SET_PACKAGE_COUNT:
        has_selection = payload.get("selection_index") is not None or bool(
            payload.get("selected_value")
        )
        has_package_count = payload.get("package_count") is not None
        if has_selection and not has_package_count:
            payload = {**payload, "action": ActionType.SELECT_PRICING_TIER.value}
        elif has_package_count and not has_selection:
            payload = {**payload, "action": ActionType.SET_PACKAGE_COUNT.value}
        # Sinon (ni l'un ni l'autre, ou les deux à la fois) : ambigu,
        # laissé tel quel — le validator Pydantic le rejettera à raison.
    return payload


def _drop_redundant_selected_value(
    payload: Dict[str, Any], prompt_context: StructuredActionPromptContext
) -> Dict[str, Any]:
    """Normalise le cas — découvert en live D.1, DIFFÉRENT de l'omission de
    `action` déjà traitée par `_infer_missing_action` ci-dessus — où le
    modèle remplit CORRECTEMENT `selection_index` mais ajoute EN PLUS
    `selected_value` (le label humain, en toute bonne foi, pour "confirmer"
    son choix). `StructuredActionDecision._check_one_semantic_action`
    exige exactement UN des deux champs (protection historique "LE PREMIER,
    C'EST À DIRE 5 L" contre un mélange ambigu) — mais ici les deux champs
    ne SONT PAS en conflit, ils décrivent la MÊME option deux fois. Rejeter
    une réponse par ailleurs sans ambiguïté serait la même erreur de
    catégorie que l'omission de `action` : confondre "le modèle a été
    surabondant" avec "le modèle est incertain".

    `_resolve_selection` (structured_action_contract.py) donne de toute
    façon la priorité à `selection_index` sur `selected_value` quand les
    deux sont fournis — cette fonction ne fait donc que rendre EXPLICITE,
    avant validation Pydantic, une précédence qui existe déjà en aval.
    Sécurité : ne supprime `selected_value` que si son texte correspond
    RÉELLEMENT au label de l'option désignée par `selection_index` (sinon
    c'est une vraie contradiction — laissée telle quelle, rejetée à raison
    par le validator, jamais un index deviné à la place d'un texte qui le
    contredit)."""
    if (
        payload.get("disposition") != "ACTION"
        or payload.get("action") not in ("SELECT_PRODUCER", "SELECT_PRICING_TIER")
    ):
        return payload
    index = payload.get("selection_index")
    value = payload.get("selected_value")
    if index is None or not value:
        return payload
    option = next((o for o in prompt_context.options if o.index == index), None)
    if option is None:
        return payload
    if str(value).strip().lower() not in option.label.lower():
        return payload  # vraie contradiction — laissé tel quel, à raison
    return {**payload, "selected_value": None}


def _parse_and_validate(
    raw_content: str,
    prompt_context: StructuredActionPromptContext,
) -> Tuple[Optional[StructuredActionDecision], str]:
    try:
        payload = json.loads(raw_content or "{}")
    except Exception:
        return None, "le JSON n'a pas pu être décodé"
    if isinstance(payload, dict):
        payload = _infer_missing_action(payload, prompt_context)
        payload = _drop_redundant_selected_value(payload, prompt_context)
    try:
        decision = StructuredActionDecision.model_validate(payload)
    except ValidationError as exc:
        return None, f"schéma invalide ({exc.errors()[0].get('msg', 'erreur')})"
    return decision, ""


def _proposed_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _outcome_for_decision(
    decision: StructuredActionDecision,
    prompt_context: StructuredActionPromptContext,
    domain_context: SelectionContext,
    locked_goal: Optional[str],
    text: Optional[str] = None,
) -> Tuple[StructuredActionOutcome, Optional[Dict[str, Any]]]:
    from ladini.graphs.agents.market_coach.core.tunnel_manager import (
        INTERRUPTION_CONFIDENCE_THRESHOLD,
    )

    if decision.disposition == StructuredActionDisposition.UNKNOWN:
        return StructuredActionOutcome.RESULT, _unknown_result("structured_action_micro")

    if decision.disposition == StructuredActionDisposition.DEVIATION:
        return StructuredActionOutcome.DEVIATION, None

    if decision.disposition == StructuredActionDisposition.REJECT:
        return (
            StructuredActionOutcome.RESULT,
            _reject_result(locked_goal, decision.confidence),
        )

    # disposition == ACTION
    if decision.confidence < INTERRUPTION_CONFIDENCE_THRESHOLD:
        # Spec §46 : "il vaut mieux UNKNOWN qu'un mauvais producteur ou
        # mauvais tier" — jamais une supposition sur une action qui MUTE
        # un panier/une commande.
        logger.info(
            "[Interpreter STRUCTURED_ACTION] confiance %.2f < seuil %.2f "
            "pour ACTION — repli UNKNOWN (jamais une supposition)",
            decision.confidence,
            INTERRUPTION_CONFIDENCE_THRESHOLD,
        )
        return StructuredActionOutcome.RESULT, _unknown_result(
            "structured_action_micro_low_confidence"
        )

    raw = resolve_and_validate(decision, prompt_context, domain_context)
    if raw is None:
        logger.warning(
            "[Interpreter STRUCTURED_ACTION] décision ACTION irrésolvable "
            "(index hors bornes, valeur ambiguë, ou rejetée par "
            "validate_action) — repli UNKNOWN"
        )
        return StructuredActionOutcome.RESULT, _unknown_result(
            "structured_action_micro_unresolved"
        )

    # Garde de provenance (incident 2026-10-01) : SET_QUANTITY/SET_PACKAGE_COUNT
    # produisent un événement ANSWER (fast-path acheteur, cognitive_guard
    # sauté). Un nombre extrait d'un message qui n'en contient AUCUN est une
    # invention du modèle (« je veux acheter du lait » → quantity=1) : jamais
    # une action qui MUTE le panier — UNKNOWN, le slot est redemandé.
    if (
        text is not None
        and raw["action"] in (ActionType.SET_QUANTITY, ActionType.SET_PACKAGE_COUNT)
        and not text_states_a_quantity(
            text, _proposed_number(raw.get("quantity", raw.get("package_count")))
        )
    ):
        logger.info(
            "BUYER_ACTIVE_SLOT_FASTPATH_REJECTED goal=%s expected_slot=%s "
            "reason=quantity_without_textual_support competing_product_present=false",
            locked_goal,
            raw["action"].value,
        )
        # DEVIATION (et non UNKNOWN) : le modèle a vu une « action » là où le message ne contient
        # aucun nombre — ce n'est PAS une réponse au slot ; le classifieur NEW_TASK reclassifie
        # (« je veux acheter du lait » = nouvelle demande, pas un « je n'ai pas compris »).
        return StructuredActionOutcome.DEVIATION, None

    return StructuredActionOutcome.RESULT, adapt_structured_action_to_canonical(
        raw, locked_goal, "structured_action_micro"
    )


async def run_structured_action_microprompt(
    state: Dict[str, Any],
    mc_runtime: Any,
    text: str,
    domain_context: SelectionContext,
    locked_goal: Optional[str],
) -> Tuple[StructuredActionOutcome, Optional[Dict[str, Any]]]:
    """Point d'entrée unique — voir le branchement dans `routing.py`. Ne
    lève jamais côté logique métier : toute exception infrastructurelle
    doit être catchée par L'APPELANT, qui retombe alors sur
    `StructuredActionOutcome.LEGACY_FALLBACK` lui-même (même discipline que
    `selection_micro.py`/`active_slot_micro.py`)."""
    prompt_context = build_structured_action_prompt_context(domain_context)
    if prompt_context is None:
        # Ne devrait pas arriver (l'appelant vérifie déjà expected_action
        # avant d'appeler ce module) — filet défensif minimal.
        return StructuredActionOutcome.RESULT, _unknown_result(
            "structured_action_micro_no_expected_action"
        )

    from ladini.core.telemetry import get_trace_id, record_generation
    from ladini.graphs.agents.market_coach.llm_gateway import (
        LLMProfile,
        resolve_gateway,
    )

    message_sid = state.get("message_sid")
    system_prompt = build_structured_action_system_prompt()
    user_prompt = build_structured_action_user_prompt(
        prompt_context=prompt_context, goal=locked_goal, normalized_text=text
    )

    gateway = resolve_gateway(mc_runtime)
    requested_model = gateway.primary_model_name(LLMProfile.INTERPRETER)
    cache_key = _cache_key(message_sid, system_prompt, user_prompt, requested_model)

    base_metadata: Dict[str, Any] = {
        "message_sid": message_sid,
        "prompt_version": STRUCTURED_ACTION_PROMPT_VERSION,
        "prompt_family": "structured_action",
        "interpretation_route": "structured_action",
        "current_goal": locked_goal,
        "expected_action": prompt_context.expected_action.value,
    }

    cached_raw = get_cached(cache_key)
    if cached_raw:
        try:
            decision = StructuredActionDecision.model_validate(json.loads(cached_raw))
        except Exception:
            decision = None
        if decision is not None:
            logger.info(
                "[Interpreter STRUCTURED_ACTION] résultat réutilisé du "
                "cache (retry sans nouveau coût LLM)"
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
            return _outcome_for_decision(
                decision, prompt_context, domain_context, locked_goal, text
            )

    call_count_key = f"llm_call_count:{message_sid}" if message_sid else None

    async def _call(user_content: str, *, repair: bool) -> Tuple[str, Optional[str]]:
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
        return completion.choices[0].message.content or "{}", getattr(
            completion, "model", None
        )

    raw_content, actual_model = await _call(user_prompt, repair=False)
    decision, reason = _parse_and_validate(raw_content, prompt_context)

    if decision is None:
        logger.info(
            "[Interpreter STRUCTURED_ACTION] réponse invalide (%s) — "
            "1 repair retry",
            reason,
        )
        repair_prompt = build_structured_action_repair_prompt(reason=reason)
        raw_content, actual_model = await _call(repair_prompt, repair=True)
        decision, reason = _parse_and_validate(raw_content, prompt_context)

    if decision is None:
        logger.warning(
            "[Interpreter STRUCTURED_ACTION] JSON toujours invalide après "
            "repair (%s) — UNKNOWN",
            reason,
        )
        return StructuredActionOutcome.RESULT, _unknown_result(
            "structured_action_micro_repair_failed"
        )

    if cache_key:
        set_cached(cache_key, decision.model_dump_json(), ttl_seconds=_CACHE_TTL_SECONDS)

    outcome, result = _outcome_for_decision(
        decision, prompt_context, domain_context, locked_goal, text
    )
    # (2026-09-13, Incrément G, spec §9/§33) : STRUCTURED_ACTION est le
    # profil de risque le plus élevé (exécute directement une action sur un
    # panier/une commande) — un fallback y est déjà rendu sûr par la garde
    # de confiance + `validate_action` (spec §46/§20 du chantier D), mais
    # doit en plus être TRACÉ explicitement pour qu'un dashboard puisse
    # distinguer "structured_action_v2 sur modèle primaire" de "sur
    # fallback" (spec §29) sans avoir à recouper les logs.
    degraded_model = bool(actual_model and actual_model != requested_model)
    if result is not None:
        result = {
            **result,
            "raw_analysis": {
                **result.get("raw_analysis", {}),
                "model_used": actual_model,
                "degraded_model": degraded_model,
                "high_risk_fallback": degraded_model,
            },
        }
    return outcome, result


__all__ = ["StructuredActionOutcome", "run_structured_action_microprompt"]
