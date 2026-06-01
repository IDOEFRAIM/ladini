"""Market — Interpreter & Goal Planner.

Centralise l'appel LLM d'interprétation et l'automate de planification.
Le prompt système est construit dynamiquement à partir de `INTENT_CONFIG`
filtré par rôle (PRODUCER / BUYER) afin d'éviter qu'un LLM ne propose
une intention inappropriée pour le profil utilisateur.

Le `goal_planner` reste une machine à états purement déterministe : aucune
heuristique de texte, aucun appel LLM. Il consomme `interpreted_event` et
`detected_intent` produits en amont.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.intent import (
    INTENT_CONFIG,
    INTENT_ROLE,
)
from agriconnect.graphs.agents.market_coach.prompts import INTERPRETER_USER_PROMPT
from agriconnect.graphs.agents.market_coach.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime

logger = logging.getLogger("AgriConnect.Market.InterpreterRouting")

# =====================================================================
# ROLE-BASED INTENT FILTERING (anti-cross-pollution AG-UI)
# =====================================================================

# Sets DERIVED from intent.INTENT_ROLE — single source of truth.
# DO NOT hand-edit; modify intent.INTENT_ROLE in intent.py instead.
PRODUCER_INTENTS: frozenset = frozenset(
    k for k, role in INTENT_ROLE.items() if role == "PRODUCER"
)
BUYER_INTENTS: frozenset = frozenset(
    k for k, role in INTENT_ROLE.items() if role == "BUYER"
)
COMMON_INTENTS: frozenset = frozenset(
    k for k, role in INTENT_ROLE.items() if role == "BOTH"
)


def allowed_intents_for_role(role: str) -> frozenset:
    """Retourne l'ensemble des intentions autorisées pour un rôle utilisateur.

    `role` ∈ {"PRODUCER", "BUYER"}. Les "BOTH" sont toujours inclus.
    Toute intention présente dans `intent.INTENT_CONFIG` mais absente d'`INTENT_ROLE`
    est traitée comme PRODUCER par défaut (compat asc.) et logguée en warning.
    """
    role_up = str(role).upper().strip()
    missing = set(INTENT_CONFIG) - set(INTENT_ROLE)
    if missing:
        logger.warning(
            "Intents missing INTENT_ROLE entry (defaulting to PRODUCER): %s",
            sorted(missing),
        )
    base = BUYER_INTENTS if role_up == "BUYER" else PRODUCER_INTENTS
    return base | COMMON_INTENTS | frozenset(missing)


# =====================================================================
# ENTITY KEY REMAPPER (Normalisation vers les clés MCP standard)
# =====================================================================

_ENTITY_KEY_REMAP: Dict[str, str] = {
    "prix": "price_mentioned", "price": "price_mentioned", "montant": "price_mentioned",
    "montant_enchere": "price_mentioned", "offered_price": "price_mentioned", "max_price": "price_mentioned",
    "prix_unitaire": "price_mentioned", "quantite": "quantity_mentioned", "quantity": "quantity_mentioned",
    "qty": "quantity_mentioned", "volume": "quantity_mentioned", "quantity_kg": "quantity_mentioned",
    "quantity_for_sale": "quantity_mentioned", "unite": "unit_mentioned", "unit": "unit_mentioned",
    "produit": "product", "name": "product", "item_name": "product", "commodity": "product",
    "culture": "product", "product_name": "product", "zone": "zone_name", "region": "zone_name",
    "localite": "zone_name", "target_zone": "zone_name", "product": "product", "price_mentioned": "price_mentioned",
    "quantity_mentioned": "quantity_mentioned", "unit_mentioned": "unit_mentioned", "zone_name": "zone_name",
    "selection_index": "selection_index", "selected_value": "selected_value", "movement_type": "movement_type",
    "reason": "reason",
}


def _remap_entities(raw_entities: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise les clés d'entités extraites par le LLM vers les clés MCP standard."""
    normalized: Dict[str, Any] = {}
    for raw_key, value in raw_entities.items():
        if value in (None, "", [], {}):
            continue

        canonical_key = _ENTITY_KEY_REMAP.get(str(raw_key).lower().strip(), raw_key)

        if canonical_key in ("price_mentioned", "quantity_mentioned"):
            try:
                clean_str = str(value).replace(",", ".").replace(" ", "").replace("\xa0", "")
                normalized[canonical_key] = float(clean_str)
            except (ValueError, TypeError):
                logger.warning("Entity '%s' non numérique: %r — ignoré", canonical_key, value)
                continue
        elif canonical_key == "selection_index":
            try:
                normalized[canonical_key] = int(value)
            except (ValueError, TypeError):
                logger.warning("selection_index non entier: %r — ignoré", value)
                continue
        else:
            normalized[canonical_key] = str(value).strip() if isinstance(value, str) else value

    return normalized


# =====================================================================
# DYNAMIC SYSTEM PROMPT BUILDER (Role-aware)
# =====================================================================

_PROMPT_CACHE: Dict[str, str] = {}


def _build_dynamic_interpreter_prompt(role: str = "PRODUCER") -> str:
    """Construit le prompt système avec UNIQUEMENT les intentions du rôle actif."""
    cache_key = (role or "PRODUCER").upper()
    if cache_key in _PROMPT_CACHE:
        return _PROMPT_CACHE[cache_key]

    allowed = allowed_intents_for_role(role)
    intent_lines: List[str] = []
    for intent_key, config in INTENT_CONFIG.items():
        if intent_key not in allowed:
            continue
        label = config.get("label", intent_key)
        required = config.get("required") or []
        req_str = ", ".join(required) if required else "aucun"
        intent_lines.append(f"  - {intent_key} : {label} [requis: {req_str}]")

    intent_catalog = "\n".join(intent_lines)
    role_label = "ACHETEUR" if cache_key == "BUYER" else "PRODUCTEUR"

    prompt = f"""\
Tu es l'Interprète conversationnel de Market Sense, un assistant WhatsApp
pour des {role_label}s agricoles au Burkina Faso. Le canal est bruité (slang,
fragments, audios mal transcrits).

Tu devez classer le message utilisateur et extraire des entités structurées.

Ta sortie OBLIGATOIRE est un JSON strict avec EXACTEMENT ces clés :
{{
  "interpreted_event": "<NEW_TASK | ANSWER | CONFIRM | REJECT | SELECTION | UPDATE | INTERRUPTION | OUT_OF_SCOPE | UNKNOWN>",
  "detected_intent": "<une intention du catalogue ci-dessous OU 'UNKNOWN'>",
  "interpreter_confidence": <float entre 0.0 et 1.0>,
  "extracted_entities": {{
      "product": "<str|null>",
      "quantity_mentioned": <float|null>,
      "unit_mentioned": "<KG|TONNE|SAC|PANIER|null>",
      "price_mentioned": <float|null>,
      "zone_name": "<str|null>",
      "selection_index": <int|null>,
      "selected_value": "<str|null>",
      "movement_type": "<IN|OUT|null>",
      "reason": "<str|null>"
  }}
}}

═══════════════════════════════════════════════════════════════
CATALOGUE OFFICIEL DES INTENTIONS POUR {role_label} (CONTRAT FERMÉ) :
═══════════════════════════════════════════════════════════════
{intent_catalog}

═══════════════════════════════════════════════════════════════
RÈGLES STRICTES DE CLASSIFICATION :
═══════════════════════════════════════════════════════════════
1. **interpreted_event** qualifie le TYPE conversationnel du message :
   - NEW_TASK     : l'utilisateur initie une nouvelle action métier.
   - ANSWER       : réponse à une question de type saisie/formulaire (slot-filling).
   - CONFIRM      : validation explicite du récapitulatif (oui, ok, d'accord, c'est bon, confirmer...).
   - REJECT       : refus ou annulation explicite (non, annule, stop, pas d'accord, quitter...).
   - SELECTION    : choix d'un élément dans une liste ou un menu AG-UI (index ou nom).
   - UPDATE       : correction explicite d'une info déjà fournie (ex: "Non pas 10 sacs mais plutôt 15").
   - INTERRUPTION : changement brusque de sujet en plein milieu d'un tunnel actif.
   - OUT_OF_SCOPE : message hors-domaine (politique, sport, religion, salutations vides sans but).
   - UNKNOWN      : impossible de comprendre ou message totalement incohérent.

2. **detected_intent** identifie l'intention métier PRÉCISE.
3. **ANTI-HALLUCINATION D'IDS** : Tu n'inventes jamais d'ID technique. Si choix de l'IHM :
   - chiffre pur ou ordinal ("le 2ème", "option 1") → `selection_index` (int).
   - nom propre ou texte ("l'offre de Diallo") → `selected_value` (str).

4. Tu réponds UNIQUEMENT le JSON, sans markdown, sans explication.
"""
    _PROMPT_CACHE[cache_key] = prompt
    return prompt


# =====================================================================
# FAST-PATH SÉCURISÉ (Zéro Heuristique de Token Floue)
# =====================================================================

# Patterns déterministes pour RESUME ("reprendre la tâche suspendue").
# Activés uniquement quand `state.suspended_goal` est non vide.
_RESUME_PATTERNS = (
    "continue", "continuer", "continué",
    "reprends", "reprendre", "reprend",
    "on reprend", "on continue",
    "oui continue", "ok continue", "vasy", "vas-y",
    "finis", "finir", "termine", "on termine",
    "reviens", "retourà",
    "comme avant",
)


def _interpret_fast_path(state: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Court-circuite le LLM uniquement pour les actions structurelles pures d'AG-UI."""
    expected = state.get("expected_input")
    clean = text.strip().lower()
    working = state.get("working_memory") or {}
    locked_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")

    if not clean:
        return None

    is_short = len(clean) <= 4 or clean.isdigit() or clean in {"oui", "non", "ok", "yes", "no"}
    if is_short and locked_goal:
        if clean in {"oui", "ok", "yes"}:
            return {
                "interpreted_event": "CONFIRM",
                "detected_intent": str(locked_goal).upper(),
                "interpreter_confidence": 1.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_intent_lock_confirm"},
            }
        if clean in {"non", "no"}:
            return {
                "interpreted_event": "REJECT",
                "detected_intent": str(locked_goal).upper(),
                "interpreter_confidence": 1.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "fast_path_intent_lock_reject"},
            }
        if clean.isdigit() and (expected == "SELECTION" or state.get("expected_candidates")):
            return {
                "interpreted_event": "SELECTION",
                "detected_intent": str(locked_goal).upper(),
                "interpreter_confidence": 1.0,
                "extracted_entities": {"selection_index": int(clean)},
                "raw_analysis": {"path": "fast_path_intent_lock_selection"},
            }
        if expected in ("PRICE", "QUANTITY"):
            try:
                val = float(clean.replace(",", "."))
            except ValueError:
                val = None
            if val is not None:
                entity_key = "price_mentioned" if expected == "PRICE" else "quantity_mentioned"
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": str(locked_goal).upper(),
                    "interpreter_confidence": 1.0,
                    "extracted_entities": {entity_key: val},
                    "raw_analysis": {"path": "fast_path_intent_lock_numeric"},
                }

    # Fast-path 0 : RESUME explicite si une tâche est suspendue dans la pile
    suspended = state.get("suspended_goal")
    goal_stack = state.get("goal_stack") or []
    if (suspended or goal_stack):
        for pat in _RESUME_PATTERNS:
            if pat in clean and len(clean) <= 40:
                return {
                    "interpreted_event": "RESUME",
                    "detected_intent": "UNKNOWN",
                    "interpreter_confidence": 1.0,
                    "extracted_entities": {},
                    "raw_analysis": {"path": "fast_path_resume", "matched": pat},
                }

    # Fast-path 1 : Choix d'un index numérique pur sur un composant Menu / Liste AG-UI
    if clean.isdigit():
        candidates = state.get("expected_candidates") or []
        if expected == "SELECTION" or len(candidates) > 0:
            return {
                "interpreted_event": "SELECTION",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 1.0,
                "extracted_entities": {"selection_index": int(clean)},
                "raw_analysis": {"path": "fast_path_selection_index"},
            }

    # Fast-path 2 : Saisie de valeur numérique directe lors du slot-filling actif (ex: prix ou quantité)
    if expected in ("PRICE", "QUANTITY"):
        num_match = _re.match(r"^[\s]*(\d+[\s,.]?\d*)\s*(kg|tonne|sac|fcfa|f|cfa)?[\s]*$", clean)
        if num_match:
            try:
                val = float(num_match.group(1).replace(",", ".").replace(" ", ""))
            except ValueError:
                pass
            else:
                entity_key = "price_mentioned" if expected == "PRICE" else "quantity_mentioned"
                entities: Dict[str, Any] = {entity_key: val}
                unit_raw = num_match.group(2)
                if unit_raw:
                    unit_map = {"kg": "KG", "tonne": "TONNE", "sac": "SAC", "fcfa": None, "f": None, "cfa": None}
                    mapped_unit = unit_map.get(unit_raw.lower())
                    if mapped_unit:
                        entities["unit_mentioned"] = mapped_unit
                return {
                    "interpreted_event": "ANSWER",
                    "detected_intent": "UNKNOWN",
                    "interpreter_confidence": 0.95,
                    "extracted_entities": entities,
                    "raw_analysis": {"path": "fast_path_numeric_answer"},
                }

    return None


# =====================================================================
# NODE 3 — INPUT INTERPRETER (Factory by role)
# =====================================================================

def make_input_interpreter(role: str = "PRODUCER"):
    """Crée un nœud `input_interpreter` configuré pour un rôle donné (AG-UI)."""
    role_up = str(role or "PRODUCER").upper().strip()
    allowed = allowed_intents_for_role(role_up)

    async def input_interpreter(state: MarketAgentState, mc_runtime: MarketRuntime) -> Dict[str, Any]:
        text = state.get("normalized_text") or state.get("user_query") or ""
        expected_input = state.get("expected_input")

        # 1. Traitement prioritaire par Fast-path structurel rigide
        fast = _interpret_fast_path(state, text)
        if fast is not None:
            return fast

        # 2. Sécurité d'exécution de l'infrastructure
        llm = getattr(mc_runtime, "llm", None)
        if llm is None:
            logger.warning("No LLM on runtime — interpreter returns UNKNOWN")
            return {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "no_llm"},
            }

        # 3. Résolution dynamique des contextes de prompts
        system_prompt = _build_dynamic_interpreter_prompt(role_up)
        user_prompt = INTERPRETER_USER_PROMPT.format(
            current_goal=state.get("current_goal") or "AUCUN",
            expected_input=expected_input or "NONE",
            last_agent_question=state.get("last_agent_question") or "—",
            expected_candidates=", ".join(state.get("expected_candidates") or []) or "—",
            normalized_text=text,
        )

        # 4. Appel LLM d'analyse sémantique et contextuelle
        try:
            completion = await asyncio.to_thread(
                lambda: llm.chat.completions.create(
                    model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.0,
                )
            )
            parsed = json.loads(completion.choices[0].message.content or "{}")
        except Exception as exc:
            logger.error("Interpreter LLM CRASH : %s — forcing UNKNOWN", exc, exc_info=True)
            return {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "llm_crash", "error": str(exc)},
            }

        # 5. Normalisation sémantique et gardes-fous anti-dérive
        raw_event = str(parsed.get("interpreted_event") or "UNKNOWN").upper().strip()
        if raw_event == "PROVIDE_INFO":
            raw_event = "ANSWER"

        # Protection stricte IHM : Si l'utilisateur dévie alors qu'on attend une action binaire/choix de bouton
        if expected_input in {"SELECTION", "CONFIRMATION"} and raw_event not in {"SELECTION", "CONFIRM", "REJECT"}:
            logger.warning("Dérive conversationnelle détectée : attendait %s, utilisateur a dévié.", expected_input)
            raw_event = "UNKNOWN"

        if raw_event not in {"NEW_TASK", "ANSWER", "CONFIRM", "REJECT", "SELECTION", "UPDATE", "INTERRUPTION", "RESUME", "OUT_OF_SCOPE", "UNKNOWN"}:
            raw_event = "UNKNOWN"

        raw_intent = str(parsed.get("detected_intent") or "UNKNOWN").upper().strip()
        if raw_intent != "UNKNOWN" and raw_intent not in allowed:
            logger.warning("LLM a retourné une intention hors-périmètre %s pour le rôle %s — ignoré", raw_intent, role_up)
            raw_intent = "UNKNOWN"

        try:
            confidence = max(0.0, min(1.0, float(parsed.get("interpreter_confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        return {
            "interpreted_event": raw_event,
            "detected_intent": raw_intent,
            "interpreter_confidence": confidence,
            "extracted_entities": _remap_entities(parsed.get("extracted_entities") or {}),
            "raw_analysis": {"path": "llm", "role": role_up},
        }

    return input_interpreter


# =====================================================================
# NODE 4 — GOAL PLANNER (Machine à États Pure orientée Formulaires UI)
# =====================================================================

INTENT_TO_GOAL_MAP: Dict[str, str] = {
    intent_key: intent_key for intent_key in PRODUCER_INTENTS | BUYER_INTENTS | COMMON_INTENTS
}


async def goal_planner(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Machine à états pure pour la gestion du cycle de vie des intentions."""
    event = str(state.get("interpreted_event") or "UNKNOWN").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    working = state.get("working_memory") or {}
    current_goal = state.get("current_goal") or working.get("active_goal") or working.get("locked_intent")
    expected_input = state.get("expected_input")
    goal_stack = list(state.get("goal_stack") or [])
    in_tunnel = bool(current_goal and expected_input and expected_input != "NONE")
    text = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()
    is_short = len(text) <= 4 or text.isdigit() or text in {"oui", "non", "ok", "yes", "no"}

    updates: Dict[str, Any] = {
        "status": "PLANNING",
        "interruption_detected": False,
    }

    def _lock(goal: Optional[str], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        wm = dict(working)
        if goal and goal != "DISAMBIGUATION_PENDING":
            wm["active_goal"] = goal
            wm["locked_intent"] = goal
            wm.setdefault("step_index", 0)
        if extra:
            wm.update(extra)
        return wm

    def _clear_goal_lock() -> Dict[str, Any]:
        """Conserve les caches, supprime uniquement les verrous de tunnel/UI."""
        wm = dict(working)
        for k in (
            "active_goal",
            "locked_intent",
            "step_index",
            "available_mapping_kind",
            "auction_menu",
            "bids_menu",
            "stocks_menu",
            "generic_menu",
            "disambiguation_pending",
            "disambiguation_trigger_id",
        ):
            wm.pop(k, None)
        return wm

    def _purge_transaction_state() -> Dict[str, Any]:
        """Purge totale des champs transactionnels/AG-UI lors d'un switch d'intention."""
        return {
            "transaction_payload": {},
            "stable_entities": {},
            "missing_fields": [],
            "completed_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "expected_candidates": [],
            "available_mapping": {},
            "waiting_for_confirmation": False,
            "confirmation_summary": None,
            "selected_tool": None,
            "selected_tool_args": {},
            "execution_result": {},
            "retry_count": 0,
        }

    # RÈGLE 0bis — RÉSOLUTION DE DÉSAMBIGUÏSATION
    # Si l'utilisateur a sélectionné dans le menu posé par semantic_disambiguation,
    # on lit la sélection directement depuis state.extracted_entities +
    # state.available_mapping (goal_planner s'exécute AVANT memory_update).
    if current_goal == "DISAMBIGUATION_PENDING":
        extracted = state.get("extracted_entities") or {}
        mapping = state.get("available_mapping") or {}
        sel_idx = extracted.get("selection_index")
        sel_val = extracted.get("selected_value")

        resolved_intent: Optional[str] = None
        if sel_idx is not None:
            resolved_intent = mapping.get(str(sel_idx))
        if resolved_intent is None and sel_val is not None:
            resolved_intent = mapping.get(str(sel_val))

        if resolved_intent and resolved_intent in INTENT_TO_GOAL_MAP:
            logger.info(
                "[Disambiguation Resolved] selection=%s promoted to current_goal=%s",
                sel_idx or sel_val, resolved_intent,
            )
            return {
                "status": "PLANNING",
                "current_goal": resolved_intent,
                "goal_status": "ACTIVE",
                "interruption_detected": False,
                "expected_input": "NONE",
                "expected_candidates": [],
                "available_mapping": {},
                "missing_fields": [],
                "completed_fields": [],
                "working_memory": {
                    **_lock(resolved_intent),
                    "disambiguation_pending": False,
                    "available_mapping_kind": None,
                },
            }
        # Sélection invalide ou pas encore reçue : on garde le menu actif.
        updates["current_goal"] = "DISAMBIGUATION_PENDING"
        updates["goal_status"] = "WAITING_INPUT"
        return updates

    # RÈGLE 1 — CANCEL/REJECT (Annulation explicite)
    # Si l'utilisateur rejette en dehors du contexte de confirmation, on purge.
    if event == "REJECT":
        waiting_confirm = bool(state.get("waiting_for_confirmation") or str(expected_input or "").upper() == "CONFIRMATION")
        if not waiting_confirm:
            return {
                "status": "WAITING_INPUT",
                "current_goal": None,
                "goal_status": "IDLE",
                "interruption_detected": False,
                "response_strategy": "CLARIFICATION",
                "working_memory": _clear_goal_lock(),
                **_purge_transaction_state(),
            }

        # Rejet pendant confirmation : laisser confirmation_gate gérer la logique.
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return updates

    # RÈGLE 1bis — TUNNEL LOCKING (Maintien des formulaires d'IHM)
    if event in {"CONFIRM", "SELECTION", "ANSWER", "UPDATE"}:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return updates

    if is_short and current_goal:
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = "ACTIVE" if expected_input in {None, "NONE"} else "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return updates

    # RÈGLE 2 — POLLUTION PENDANT SLOT-FILLING
    if event == "UNKNOWN" and in_tunnel:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "WAITING_INPUT"
        updates["detected_intent"] = str(current_goal).upper()
        updates["working_memory"] = _lock(current_goal)
        return updates

    # RÈGLE 3 — OUT_OF_SCOPE
    if event == "OUT_OF_SCOPE":
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return updates

    # RÈGLE 4 — INTERRUPTION (Suspension d'un workflow IHM en cours)
    if event == "INTERRUPTION":
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if new_goal and new_goal != current_goal:
            if current_goal:
                goal_stack.append(current_goal)
            return {
                "status": "PLANNING",
                "current_goal": new_goal,
                "goal_stack": goal_stack,
                "goal_status": "ACTIVE",
                "interruption_detected": True,
                "suspended_goal": current_goal,
                "suspended_payload": state.get("transaction_payload") or {},
                **_purge_transaction_state(),
                "working_memory": _lock(new_goal),
            }
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return updates

    # RÈGLE 4bis — RESUME (Reprise d'une tâche suspendue dans goal_stack)
    if event == "RESUME":
        if goal_stack:
            resumed = goal_stack.pop()
            return {
                "status": "PLANNING",
                "current_goal": resumed,
                "goal_stack": goal_stack,
                "goal_status": "ACTIVE",
                "interruption_detected": False,
                "suspended_goal": None,
                "transaction_payload": state.get("suspended_payload") or {},
                "suspended_payload": {},
                "stable_entities": {},
                "missing_fields": [],
                "completed_fields": [],
                "last_missing_field": None,
                "expected_input": "NONE",
                "expected_candidates": [],
                "available_mapping": {},
                "waiting_for_confirmation": False,
                "confirmation_summary": None,
                "working_memory": _lock(resumed),
            }
        # Aucun goal suspendu — traiter comme clarification
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return updates

    # RÈGLE 5 — NEW_TASK (Instanciation et purge des champs AG-UI)
    if event == "NEW_TASK":
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if new_goal:
            updates["current_goal"] = new_goal
            updates["goal_status"] = "ACTIVE"
            updates["working_memory"] = _lock(new_goal)
            if current_goal and new_goal != current_goal:
                updates.update(_purge_transaction_state())
            return updates

    # Fallback par défaut vers clarification
    updates["current_goal"] = current_goal
    updates["response_strategy"] = "CLARIFICATION"
    updates["status"] = "WAITING_INPUT"
    updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
    updates["working_memory"] = _lock(current_goal)
    return updates


# =====================================================================
# ROUTING AFTER VALIDATOR (Aiguillage avec typage d'état AG-UI)
# =====================================================================

def make_route_after_validator(role: str = "PRODUCER"):
    """Crée la fonction de routage conditionnel après le nœud `validator`."""
    role_up = str(role or "PRODUCER").upper().strip()

    def route_after_validator(state: MarketAgentState) -> str:
        """Détermine le prochain nœud du graphe en fonction de l'état de l'IHM."""
        interpreted_event = state.get("interpreted_event")
        missing_fields = state.get("missing_fields") or []
        expected_input = str(state.get("expected_input") or "NONE").upper().strip()
        strategy = str(state.get("response_strategy") or "").upper().strip()

        # 1. PROTECTION SÉCURITÉ ANTI-DÉRIVE (AG-UI Court-circuit)
        if interpreted_event in {"UNKNOWN", "OUT_OF_SCOPE"}:
            logger.info(
                "[%s ROUTER] Dérive détectée (%s) — Routage forcé vers response_strategy",
                role_up,
                interpreted_event
            )
            return "to_strategy"

        # 2. TUNNEL DE FORMULAIRE INCOMPLET
        if missing_fields:
            logger.info(
                "[%s ROUTER] Formulaire incomplet (%d champs manquants) — Routage vers response_strategy",
                role_up,
                len(missing_fields)
            )
            return "to_strategy"

        # 2B. MENU DE SÉLECTION (sans missing_fields) — ex: choix ferme / enchère / stock
        if state.get("status") == "WAITING_INPUT" and (expected_input == "SELECTION" or strategy == "SELECTION_MENU"):
            logger.info(
                "[%s ROUTER] Sélection attendue — Routage vers response_strategy",
                role_up,
            )
            return "to_strategy"

        # 3. ATTENTE DE VALIDATION UTILISATEUR
        if state.get("waiting_for_confirmation") or state.get("status") == "WAITING_CONFIRMATION":
            logger.info("[%s ROUTER] En attente de confirmation — Routage vers confirmation_gate", role_up)
            return "to_confirmation"

        # 4. FLUX NOMINAL
        logger.info("[%s ROUTER] Input validé et complet — Routage vers context_resolver", role_up)
        return "to_resolver"

    return route_after_validator


__all__ = [
    "PRODUCER_INTENTS",
    "BUYER_INTENTS",
    "COMMON_INTENTS",
    "allowed_intents_for_role",
    "_remap_entities",
    "_build_dynamic_interpreter_prompt",
    "_interpret_fast_path",
    "make_input_interpreter",
    "goal_planner",
    "make_route_after_validator",
    "INTENT_TO_GOAL_MAP",
]
