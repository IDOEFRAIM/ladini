"""Orchestration réseau du micro-prompt NEW_TASK (chantier "State Router +
micro-prompts", Incrément F, 2026-09-13).

Appelé pour la route `InterpretationRoute.NEW_TASK` (`state_router.py`) —
aucun tunnel actif — ET pour la reclassification qui suit une DEVIATION
confirmée par SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION (spec §27 : le message
ORIGINAL est rejoué ici, jamais reclassifié par la route qui a détecté la
déviation elle-même). Miroir structurel de `structured_action_micro.py` —
mêmes conventions de cache/repair/télémétrie/legacy fallback.

## Legacy fallback — SEULEMENT sur échec infrastructurel (spec §57)

Contrairement à SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION (qui ont une route
`legacy_fallback` partagée avec NEW_TASK dans `routing.py`), NEW_TASK EST
lui-même le filet de sécurité de ces trois routes — il n'y a plus personne
en aval vers qui retomber pour un cas "juste difficile". Un `UNKNOWN`
produit ici est donc un résultat sémantique TERMINAL et VALIDE (spec §23),
jamais un motif pour rappeler l'ancien interpréteur unifié : seule une
EXCEPTION infrastructurelle (timeout, tous providers indisponibles, JSON
structurellement irrécupérable après repair) déclenche
`NewTaskOutcome.LEGACY_FALLBACK`, tracé explicitement (spec §57)."""

from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import ValidationError

from ladini.core.idempotency import get_cached, increment, set_cached
from ladini.domain.quantity_unit import extract_unit_only_from_text
from ladini.graphs.agents.market_coach.interpreter.entities import (
    _fallback_quantity_unit_from_text,
    _remap_entities,
)
from ladini.graphs.agents.market_coach.interpreter.new_task_contract import (
    NewTaskDisposition,
    NewTaskInterpretation,
    NewTaskPromptContext,
    adapt_new_task_to_canonical,
)
from ladini.graphs.agents.market_coach.interpreter.new_task_prompts import (
    NEW_TASK_PROMPT_VERSION,
    build_new_task_repair_prompt,
    build_new_task_system_prompt,
    build_new_task_user_prompt,
)
from ladini.graphs.agents.market_coach.services.domain.product_validation import (
    _validate_and_sanitize_product,
)

logger = logging.getLogger("ladini.interpreter.new_task_micro")

# (2026-09-14, incident WhatsApp — root cause de la journée) : ce plafond
# n'avait JAMAIS reçu le même correctif que `selection_micro.py`/
# `active_slot_micro.py` (voir leurs docstrings, Phase B.1, 2026-09-12) —
# les modèles Groq RÉELLEMENT disponibles pour le profil INTERPRETER
# (`openai/gpt-oss-20b`/`120b`, tous les llama "instant" classiques étant
# décommissionnés, voir settings.py) sont des modèles DE RAISONNEMENT : ils
# consomment des tokens de "réflexion" internes, DÉCOMPTÉS de `max_tokens`,
# avant même d'émettre le JSON final. À 300, `openai/gpt-oss-20b` échouait
# SYSTÉMATIQUEMENT (100% des appels observés en prod, Groq HTTP 400 "Failed
# to validate JSON" / "max completion tokens reached before generating a
# valid document", `failed_generation` vide) — forçant un repli permanent
# sur le modèle de secours (moins fiable en jugement), responsable de la
# quasi-totalité des mauvaises classifications "confirmer"/"annuler"
# chassées ce jour-là. Ce prompt porte le schéma de sortie le PLUS large
# des 4 micro-prompts (disposition + intent + jusqu'à ~15 champs d'entités,
# `pricing_tiers` en tableau) — le plafond doit donc être AU MOINS aussi
# généreux que `structured_action_micro.py` (800), pas le plus bas des
# quatre comme il l'était. Valeur choisie avec marge, pas encore mesurée
# empiriquement comme les deux autres (spec §28 : plafond explicite,
# jamais illimité) — à ajuster si un nouvel échec systématique apparaît.
_MAX_TOKENS = 1200

_CACHE_TTL_SECONDS = 3600


class NewTaskOutcome(str, Enum):
    RESULT = "result"
    #: Échec infrastructurel — l'appelant retombe sur l'interpréteur unifié
    #: legacy, tracé explicitement `legacy_fallback=true` (spec §57).
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
        f"new_task_llm_cache:{NEW_TASK_PROMPT_VERSION}:{model_part}:"
        f"{message_sid}:{digest}"
    )


def _unknown_result(path: str) -> Dict[str, Any]:
    return {
        "interpreted_event": "UNKNOWN",
        "detected_intent": "UNKNOWN",
        "interpreter_confidence": 0.0,
        "extracted_entities": {},
        "raw_analysis": {"path": path},
    }


def _parse_and_validate(
    raw_content: str, classifiable_intents: frozenset
) -> Tuple[Optional[NewTaskInterpretation], str]:
    try:
        payload = json.loads(raw_content or "{}")
    except Exception:
        return None, "le JSON n'a pas pu être décodé"
    try:
        decision = NewTaskInterpretation.model_validate(payload)
    except ValidationError as exc:
        return None, f"schéma invalide ({exc.errors()[0].get('msg', 'erreur')})"
    if (
        decision.disposition == NewTaskDisposition.NEW_TASK
        and decision.intent not in classifiable_intents
    ):
        return None, (
            f"intent '{decision.intent}' hors catalogue — choisis une "
            "valeur EXACTE du catalogue fourni"
        )
    return decision, ""


def _validation_status_for(entities: Dict[str, Any]) -> Optional[str]:
    """Calculé côté Python à partir de `quantity`/`unit` déjà normalisés —
    spec §9 : ne pas demander au LLM une valeur que Python peut déduire
    lui-même sans ambiguïté (le LLM a déjà dit `unit=null` s'il n'a rien lu
    de littéral — voir le prompt). `None` si `quantity` est absent : le
    signal n'a de sens QUE quand une quantité a été extraite."""
    if entities.get("quantity") is None:
        return None
    return "VALID" if entities.get("unit") else "INVALID_MISSING_UNIT"


async def run_new_task_microprompt(
    state: Dict[str, Any],
    mc_runtime: Any,
    text: str,
    prompt_context: NewTaskPromptContext,
    locked_goal: Optional[str],
    classifiable_intents: Dict[str, str],
) -> Tuple[NewTaskOutcome, Optional[Dict[str, Any]]]:
    """Point d'entrée unique — voir le branchement dans `routing.py`. Ne
    lève jamais côté logique métier : toute exception infrastructurelle
    doit être catchée par L'APPELANT, qui retombe alors sur
    `NewTaskOutcome.LEGACY_FALLBACK` lui-même (même discipline que
    `selection_micro.py`/`active_slot_micro.py`/`structured_action_micro.py`).

    `classifiable_intents` : {intent_key: label} déjà filtré par l'appelant
    via `_classifiable_intents()` (spec §3, source unique)."""
    from ladini.core.telemetry import get_trace_id, record_generation
    from ladini.graphs.agents.market_coach.llm_gateway import (
        LLMProfile,
        resolve_gateway,
    )

    message_sid = state.get("message_sid")
    system_prompt = build_new_task_system_prompt(classifiable_intents)
    user_prompt = build_new_task_user_prompt(prompt_context, text)

    gateway = resolve_gateway(mc_runtime)
    requested_model = gateway.primary_model_name(LLMProfile.INTERPRETER)
    cache_key = _cache_key(message_sid, system_prompt, user_prompt, requested_model)

    base_metadata: Dict[str, Any] = {
        "message_sid": message_sid,
        "prompt_version": NEW_TASK_PROMPT_VERSION,
        "prompt_family": "new_task",
        "interpretation_route": "new_task",
        "current_goal": locked_goal,
        "primary_model": requested_model,
    }

    cached_raw = get_cached(cache_key)
    if cached_raw:
        try:
            decision = NewTaskInterpretation.model_validate(json.loads(cached_raw))
        except Exception:
            decision = None
        if decision is not None:
            logger.info(
                "[Interpreter NEW_TASK] résultat réutilisé du cache (retry "
                "sans nouveau coût LLM)"
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
                    extra_metadata={**base_metadata, "cache_hit": True, "repair_retry": False},
                )
            except Exception:
                pass
            return NewTaskOutcome.RESULT, await _finalize(
                decision, locked_goal, mc_runtime, "new_task_micro", text
            )

    call_count_key = f"llm_call_count:{message_sid}" if message_sid else None

    async def _call(messages: Any, *, repair: bool) -> Tuple[str, Optional[str]]:
        llm_call_index = (
            increment(call_count_key, ttl_seconds=300) if call_count_key else None
        )
        completion = await gateway.complete(
            profile=LLMProfile.INTERPRETER,
            messages=messages,
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
            },
        )
        actual = getattr(completion, "model", None)
        return completion.choices[0].message.content or "{}", actual

    classifiable_set = frozenset(classifiable_intents.keys())

    raw_content, actual_model = await _call(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        repair=False,
    )
    decision, reason = _parse_and_validate(raw_content, classifiable_set)

    if decision is None:
        logger.info(
            "[Interpreter NEW_TASK] réponse invalide (%s) — 1 repair retry",
            reason,
        )
        # Le repair RÉ-INCLUT le message utilisateur original (conversation
        # multi-tour system/user/assistant/user, jamais un appel isolé) —
        # sans ça, un modèle de repli (spec §31) livré à lui-même sur
        # UNIQUEMENT "corrige ton JSON" improvise des entités qui n'ont plus
        # aucun rapport avec le message (observé en live : "besoin de mais"
        # a produit quantity=892/price=250 au repair, deux nombres absents
        # du texte — le modèle avait perdu tout ancrage sur le message réel).
        repair_prompt = build_new_task_repair_prompt(reason)
        raw_content, actual_model = await _call(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": raw_content},
                {"role": "user", "content": repair_prompt},
            ],
            repair=True,
        )
        decision, reason = _parse_and_validate(raw_content, classifiable_set)

    if decision is None:
        logger.warning(
            "[Interpreter NEW_TASK] JSON toujours invalide après repair "
            "(%s) — UNKNOWN",
            reason,
        )
        if actual_model and actual_model != requested_model:
            logger.info(
                "[Interpreter NEW_TASK] fallback_used=true primary=%s "
                "actual=%s",
                requested_model,
                actual_model,
            )
        return NewTaskOutcome.RESULT, _unknown_result(
            "new_task_micro_repair_failed"
        )

    if actual_model and actual_model != requested_model:
        logger.info(
            "[Interpreter NEW_TASK] fallback_used=true primary=%s actual=%s "
            "reason=primary_unavailable",
            requested_model,
            actual_model,
        )

    if cache_key:
        set_cached(cache_key, decision.model_dump_json(), ttl_seconds=_CACHE_TTL_SECONDS)

    # Spec §31 : "ne considère PAS tous les modèles fallback comme
    # équivalents par défaut" — même garde que l'ancien chemin legacy
    # (`raw_analysis.degraded_model`, consommé par `nodes/memory.py` pour
    # durcir son anti-hallucination QUAND un modèle plus faible a répondu).
    # `actual_model` absent (Gateway qui ne le renseigne pas) reste prudent
    # : traité comme dégradé, jamais supposé être le modèle principal.
    degraded_model = (actual_model is None) or (actual_model != requested_model)

    return NewTaskOutcome.RESULT, await _finalize(
        decision,
        locked_goal,
        mc_runtime,
        "new_task_micro",
        text,
        model_used=actual_model,
        degraded_model=degraded_model,
    )


async def _finalize(
    decision: NewTaskInterpretation,
    locked_goal: Optional[str],
    mc_runtime: Any,
    path: str,
    text: str = "",
    *,
    model_used: Optional[str] = None,
    degraded_model: Optional[bool] = None,
) -> Dict[str, Any]:
    """Normalisation des entités (réutilise `_remap_entities` — MÊME
    fonction que l'ancien chemin, pas une seconde logique de nettoyage
    d'unité divergente) + validation/sanitation du produit (MÊME fonction
    que legacy) + calcul déterministe de `validation_status` (spec §9)."""
    raw_entities = decision.entities.model_dump()
    raw_entities["pricing_tiers"] = [
        {k: v for k, v in tier.items() if v is not None}
        for tier in raw_entities.get("pricing_tiers") or []
    ]
    raw_entities["additional_items"] = [
        {k: v for k, v in item.items() if v is not None}
        for item in raw_entities.get("additional_items") or []
    ]
    entities = _remap_entities(raw_entities)

    # Garde anti-ancrage (incident réel vécu : le LLM "s'ancre" parfois sur
    # une unité mentionnée plus tôt dans la conversation — ex: TONNE — et
    # continue de la répéter malgré des corrections explicites en kg dans
    # LE message courant). Le prompt new_task_v2 interdit déjà cela par
    # instruction (règle "lecture littérale"), mais cette garde Python
    # reste la protection de dernier recours déjà éprouvée par le chemin
    # legacy (`routing.py`, même principe) : le TEXTE de l'utilisateur
    # prime TOUJOURS sur une unité posée par le LLM.
    if entities.get("quantity") is not None:
        _adjacent = _fallback_quantity_unit_from_text(text) or {}
        _text_unit = _adjacent.get("unit") or extract_unit_only_from_text(text)
        if _text_unit:
            if entities.get("unit") != _text_unit:
                entities["unit"] = _text_unit
        elif entities.get("unit"):
            # Aucune unité littérale dans CE message : le LLM n'a aucun
            # appui textuel pour celle qu'il propose — écartée plutôt que
            # risquer une unité fantôme (le défaut du registre s'applique
            # ensuite en aval, comme avant cette garde).
            entities.pop("unit", None)

    if "product" in entities:
        entities["product"] = await _validate_and_sanitize_product(
            entities.get("product"), mc_runtime
        )

    validation_status = _validation_status_for(entities)
    out = adapt_new_task_to_canonical(
        decision,
        entities=entities,
        validation_status=validation_status,
        locked_goal=locked_goal,
        path=path,
    )
    if model_used is not None or degraded_model is not None:
        out["raw_analysis"] = {
            **out["raw_analysis"],
            "model_used": model_used,
            "degraded_model": bool(degraded_model),
        }
    return out


__all__ = ["NewTaskOutcome", "run_new_task_microprompt"]
