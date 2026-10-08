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
from ladini.domain.edit_validation import message_carries_value
from ladini.domain.quantity_unit import (
    convert_quantity,
    extract_unit_only_from_text,
    find_bare_number_candidates,
    find_convertible_quantity_pairs,
    parse_compound_quantity,
    parse_quantity_unit_from_text,
)
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
    raw_content: str, classifiable_intents: frozenset, text: str = ""
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
    if decision.disposition in (NewTaskDisposition.CONFIRM, NewTaskDisposition.REJECT) and message_carries_value(text):
        # Le modèle n'est pas l'autorité : un accord/refus PUR ne contient pas de valeur. « vas-y mais plutôt 20 » / « non 5 » CORRIGENT quelque chose ;
        # exécuter l'ancienne confirmation serait une mutation fausse. 1 relance (repair) ; si le modèle persiste -> UNKNOWN (clarification), jamais d'exécution.
        logger.info("business_edit_unsafe_blocked | reason=%s_carries_value", decision.disposition.value.lower())
        return None, (
            f"{decision.disposition.value} ne peut pas porter une valeur nouvelle : le message contient un nombre, donc il CORRIGE ou "
            "modifie quelque chose — réponds NEW_TASK avec l'intention de modification (ex. BUYER_EDIT_CART + cart_edit, ou le même intent "
            "avec is_correction), ou UNKNOWN si tu ne sais pas"
        )
    if decision.disposition == NewTaskDisposition.AMBIGUOUS:
        unknown = [g for g in decision.candidate_goals if g not in classifiable_intents]
        if unknown:
            return None, (
                f"candidate_goals {unknown!r} hors catalogue — chaque valeur "
                "doit être EXACTE du catalogue fourni"
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


_PURE_AGREEMENT_SYSTEM = (
    "Un utilisateur répond à un récapitulatif en attente de validation. Dis si son message ne fait QU'ACCEPTER ou REFUSER en bloc "
    "(« oui », « ok vas-y », « non », « laisse tomber »), ou s'il demande AUSSI un changement : retirer un article, modifier ou corriger "
    "une valeur, en remplacer une. Réponds uniquement par le JSON {\"asks_for_change\": true|false}."
)


async def _asks_for_change(gateway: Any, text: str, base_metadata: Dict[str, Any], requested_model: Optional[str]) -> bool:
    """Seconde opinion INDÉPENDANTE sur un CONFIRM/REJECT libre : le modèle n'est pas l'autorité d'une confirmation. Ne bloque que sur un « true » explicite ;
    réponse illisible / erreur d'infrastructure -> on ne bloque pas (la décision principale vient d'aboutir, aucune raison de punir l'utilisateur)."""
    from ladini.graphs.agents.market_coach.llm_gateway import LLMProfile

    try:
        completion = await gateway.complete(
            profile=LLMProfile.INTERPRETER,
            messages=[{"role": "system", "content": _PURE_AGREEMENT_SYSTEM}, {"role": "user", "content": f'Message : "{text}"'}],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=40,
            agent_node="input_interpreter",
            extra_metadata={**base_metadata, "cache_hit": False, "repair_retry": False, "purpose": "pure_agreement_check"},
        )
        data = json.loads(completion.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001 - voir docstring : jamais punir l'utilisateur pour une panne de la vérification
        return False
    return isinstance(data, dict) and data.get("asks_for_change") is True


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
    decision, reason = _parse_and_validate(raw_content, classifiable_set, text)

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
        decision, reason = _parse_and_validate(raw_content, classifiable_set, text)

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

    if decision.disposition in (NewTaskDisposition.CONFIRM, NewTaskDisposition.REJECT) and (
        prompt_context.cart_pending or prompt_context.draft_context
    ):
        if await _asks_for_change(gateway, text, base_metadata, requested_model):
            logger.info("business_edit_unsafe_blocked | reason=%s_asks_for_change", decision.disposition.value.lower())
            return NewTaskOutcome.RESULT, _unknown_result("new_task_confirm_asks_for_change")

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
    raw_entities["ambiguous_groups"] = [
        {k: v for k, v in group.items() if v is not None}
        for group in raw_entities.get("ambiguous_groups") or []
    ]
    raw_entities["orphan_quantities"] = [
        {k: v for k, v in orphan.items() if v is not None}
        for orphan in raw_entities.get("orphan_quantities") or []
    ]
    entities = _remap_entities(raw_entities)
    # Bug réel production (2026-09-24, "lifecycle de clarification ambiguous_groups") :
    # `_remap_entities` (générique, partagé par toutes les routes) élimine toute valeur
    # "vide" (`slot_has_value([]) == False`) — correct pour un scalaire absent, mais `state[
    # "extracted_entities"]`/`transaction_payload` sont tous deux des canaux `merge_dict`
    # (`core/state.py`) : une clé OMISE du patch de CE tour n'efface jamais l'ancienne valeur,
    # elle la LAISSE TELLE QUELLE. Résultat observé : après une clarification "57 moutons
    # chèvres", TOUT message suivant sans nouvelle ambiguïté (ex: "je veux 14 coqs chaque
    # semaine") gardait l'ANCIEN `ambiguous_groups` pour toujours — la clarification devenait
    # collante ("sticky"), rejouée à l'identique quel que soit le nouveau message. `additional_
    # items` partage exactement la même forme (liste, même schéma NEW_TASK) et le même risque
    # structurel, corrigé ici par prudence symétrique. `orphan_quantities` (2026-09-26, voir
    # `new_task_contract.py::NewTaskOrphanQuantity`) partage la MÊME forme et le MÊME risque —
    # ajoutée ici dès sa création plutôt que de laisser un futur oubli la rendre "collante" à son
    # tour. Ces trois clés sont donc TOUJOURS présentes dans `entities` (même `[]`), pour que
    # `merge_dict` écrase bien l'ancienne valeur à chaque tour au lieu de la laisser survivre
    # indéfiniment.
    entities["additional_items"] = raw_entities["additional_items"]
    entities["ambiguous_groups"] = raw_entities["ambiguous_groups"]
    entities["orphan_quantities"] = raw_entities["orphan_quantities"]

    # FALLBACK numérique (parité avec le chemin legacy, `routing.py`) : un
    # message SANS AUCUNE quantité extraite par le LLM (disposition UNKNOWN à
    # confiance nulle sur un message nu, ex: "mets plutot 15 kg" — pire cas
    # réaliste, testé par `TestRealCorrectionUpdatesTheSameDraft`) doit quand
    # même atteindre le draft si le texte porte lui-même un nombre+unité sans
    # ambiguïté. Manquait à ce micro-prompt tant que `_interpret_fast_path`
    # interceptait par avance tout message NEW_TASK/CONFIRMATION porteur d'un
    # nombre (H5, `_confirmation_correction`, désormais désactivée dès qu'un
    # classifieur réel est disponible) : ce micro-prompt n'était alors jamais
    # atteint pour ce motif. Jamais l'inverse — on n'écrase pas une quantité
    # que le LLM a positivement fournie (voir la garde anti-ancrage ci-après
    # pour CE cas). Suspendu pour un message multi-produits
    # (`additional_items` non vide) : un item a déjà sa PROPRE quantité, la
    # deviner par un simple scan du texte entier reviendrait à confondre les
    # quantités de plusieurs produits distincts. Même suspension pour
    # `orphan_quantities` non vide (2026-09-26) : une quantité SANS produit est déjà identifiée
    # ailleurs dans le texte — un simple scan risquerait de la reprendre ici comme si elle était
    # celle du produit principal.
    _adjacent = (
        {}
        if raw_entities.get("additional_items")
        or raw_entities.get("orphan_quantities")
        # CORRECTION d'un PRIX (« ah non c 350 le prix ») : le nombre du texte est ce prix, jamais une 2e quantité à inventer.
        or (raw_entities.get("is_correction") and raw_entities.get("price") is not None)
        else (_fallback_quantity_unit_from_text(text) or {})
    )
    if entities.get("quantity") is None and _adjacent.get("quantity") is not None:
        entities["quantity"] = _adjacent["quantity"]
        if _adjacent.get("unit"):
            entities["unit"] = _adjacent["unit"]

    # Garde anti-ancrage (incident réel vécu : le LLM "s'ancre" parfois sur
    # une unité mentionnée plus tôt dans la conversation — ex: TONNE — et
    # continue de la répéter malgré des corrections explicites en kg dans
    # LE message courant). Le prompt new_task_v2 interdit déjà cela par
    # instruction (règle "lecture littérale"), mais cette garde Python
    # reste la protection de dernier recours déjà éprouvée par le chemin
    # legacy (`routing.py`, même principe) : le TEXTE de l'utilisateur
    # prime TOUJOURS sur une unité posée par le LLM.
    if entities.get("quantity") is not None:
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

    # ── QUANTITÉ COMPOSÉE vs QUANTITÉ ORPHELINE (Phase 2.5 + correctif 2026-09-26) ──
    # Un produit est-il nommé CE tour ? Distingue deux familles de messages structurellement
    # IDENTIQUES pour un simple scan de texte ("N1 unité1 [produit] et N2 unité2") mais
    # sémantiquement opposées : (a) une correction de quantité SEULE, sans produit re-nommé
    # (le produit est déjà connu du tour précédent — ex: "non j'ai dit 2 tonnes et 250 kg") : le
    # texte ENTIER ne décrit qu'UNE seule quantité, fragmentée/tronquée par le LLM ; (b) un
    # message qui NOMME un produit ET porte un second nombre — incident réel 2026-09-26, "150 kg
    # tomate et 200 kg chaque semaine" : le second nombre n'est PAS une fraction de la quantité
    # du produit nommé, c'est une DEUXIÈME quantité dont le produit reste à préciser. Aucune
    # regex ne peut distinguer ces deux cas sur la forme seule (voir le rapport de mission) — le
    # signal fiable est : un produit a-t-il été dit CE tour ?
    _named_product_this_turn = bool(raw_entities.get("product"))
    if (
        _named_product_this_turn
        and (decision.intent or "").upper() == "CREATE_RECURRING_NEED"
        and entities.get("quantity") is not None
        and not raw_entities.get("additional_items")
        and not raw_entities.get("orphan_quantities")
    ):
        # Garde métier générique (mandat §10, 2026-09-26) : le texte porte-t-il une DEUXIÈME
        # quantité candidate que ni le LLM (`orphan_quantities`, ci-dessus — le cas normal une
        # fois le prompt à jour) ni `additional_items` n'expliquent ? Filet de sécurité
        # STRUCTUREL (jamais un mot précis en dur, jamais un nom d'espèce/d'animal) pour le cas
        # où le LLM n'a pas suivi la consigne : jamais une somme implicite dans la quantité du
        # produit déjà connu — la quantité EXCÉDENTAIRE devient elle-même un orphelin, jamais
        # perdue ni fusionnée. Deux formes, ni l'une ni l'autre spécifique à un produit précis :
        if entities.get("unit") in ("KG", "TONNE"):
            # (a) quantité de poids/volume — "150 kg tomate et 200 kg chaque semaine" : la
            # deuxième quantité porte elle-même une unité littérale convertible.
            _pairs = find_convertible_quantity_pairs(text)
            if len(_pairs) >= 2:
                _primary_kg = convert_quantity(entities["quantity"], entities["unit"], "KG")
                _extra = [
                    p
                    for p in _pairs
                    if _primary_kg is None or convert_quantity(p.quantity, p.unit, "KG") != _primary_kg
                ]
                if _extra and len(_extra) < len(_pairs):
                    _orphan = _extra[0]
                    logger.warning(
                        "[Interpreter NEW_TASK] Quantité orpheline détectée dans le texte "
                        "('%s' → %.1f %s en plus de %r/%r) — clarification requise, jamais "
                        "une somme implicite.",
                        text,
                        _orphan.quantity,
                        _orphan.unit,
                        entities.get("quantity"),
                        entities.get("unit"),
                    )
                    entities["orphan_quantities"] = [
                        {"quantity": _orphan.quantity, "unit": _orphan.unit}
                    ]
        else:
            # (b) quantité SANS unité de poids/volume littérale — bétail compté en TETE ("40
            # chèvres et 20 chaque semaine"), sac/panier sans répétition du mot ("3 sacs de riz
            # et 2 chaque semaine"), ou tout produit compté sans mot d'unité du tout. Incident
            # réel 2026-09-26 (suite) : `find_convertible_quantity_pairs` ne voit QUE le
            # KG/TONNE — un nombre nu comme "20" lui est structurellement invisible, laissant
            # ces cas entièrement dépendants du LLM. `find_bare_number_candidates` (jamais un mot
            # d'espèce/d'animal — seulement durée/prix/plage, des rôles GÉNÉRIQUES) comble ce
            # trou pour tout produit compté sans unité littérale, pas seulement le bétail.
            _bare = find_bare_number_candidates(text, exclude_values=[entities["quantity"]])
            if _bare:
                _orphan_qty = _bare[0]
                logger.warning(
                    "[Interpreter NEW_TASK] Quantité orpheline SANS unité détectée dans le "
                    "texte ('%s' → %.1f en plus de %r) — clarification requise, jamais une "
                    "somme implicite.",
                    text,
                    _orphan_qty,
                    entities.get("quantity"),
                )
                entities["orphan_quantities"] = [{"quantity": _orphan_qty, "unit": None}]
    elif entities.get("quantity") is not None and not raw_entities.get("additional_items") and not raw_entities.get("orphan_quantities"):
        # Même garde ANTI-TRONCATURE que `routing.py` (incident réel 2026-09-03, "2 tonnes et
        # 250 kg" → LLM tronqué en "2 TONNE") — jusqu'ici câblée UNIQUEMENT sur le chemin legacy.
        # Neutre quand le texte ne porte qu'une seule paire ou des unités non convertibles
        # (résultat alors identique, aucune correction). Ne s'applique QUE quand aucun produit
        # n'est nommé ce tour (voir ci-dessus) : c'est précisément ce qui la rend sûre — un
        # message SANS produit re-nommé ne peut décrire qu'UNE seule quantité en cours de
        # correction, jamais une deuxième quantité pour un AUTRE produit.
        _compound = parse_compound_quantity(text)
        if (
            _compound.unit == "KG"
            and _compound.quantity is not None
            and (
                entities.get("quantity") != _compound.quantity
                or entities.get("unit") != _compound.unit
            )
        ):
            _single = parse_quantity_unit_from_text(text)
            if _compound.quantity != _single.quantity:
                logger.warning(
                    "[Interpreter NEW_TASK] Quantité composée détectée dans le "
                    "texte ('%s' → %.1f KG) — retenue contre LLM=%r/%r "
                    "(anti-troncature).",
                    text,
                    _compound.quantity,
                    entities.get("quantity"),
                    entities.get("unit"),
                )
                entities["quantity"] = _compound.quantity
                entities["unit"] = _compound.unit

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
