"""Goal Planner — deterministic state machine for intent lifecycle.

Extracted from ``interpreter/routing.py`` so that the planner logic
(~400 lines of pure state-machine rules) lives in its own module.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any, Dict, Optional

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    get_pending_interaction,
    set_pending_interaction,
    to_tunnel_category,
)
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import tunnel_manager
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)

# Source UNIQUE des clés de menu/sélection volatiles (nodes/cleanup.py) : le
# purge de verrou sur REJECT DOIT vider exactement le même univers de clés que
# le nettoyage de fin de tour, sinon un menu périmé survit à une annulation
# d'intention. Importer d'ici évite une 2e liste qui dériverait.
from agriconnect.graphs.agents.market_coach.nodes.cleanup import (
    _GENERIC_SELECTION_KEYS as _MENU_SELECTION_KEYS,
)
from agriconnect.graphs.agents.market_coach.nodes.cleanup import (
    _MENU_CACHE_KEYS,
)
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime

logger = logging.getLogger("AgriConnect.Market.GoalPlanner")

# Verrous de tunnel propres au goal_planner (≠ clés de menu). L'union purgée
# sur REJECT = ces verrous + tout l'état de menu/sélection (importé ci-dessus).
_TUNNEL_LOCK_KEYS = ("active_goal", "locked_intent", "step_index")
_GOAL_LOCK_CLEAR_KEYS = (*_TUNNEL_LOCK_KEYS, *_MENU_SELECTION_KEYS, *_MENU_CACHE_KEYS)


# ── Shared constants (also used by routing.py interpreter) ──────────

INTENT_TO_GOAL_MAP: Dict[str, str] = {}

# Alias de compat — source canonique : core/goals.py (flag `breakout`
# d'INTENT_CONFIG). Même ensemble que TunnelManager.CRITICAL_BREAKOUT_INTENTS.
from agriconnect.graphs.agents.market_coach.core.goals import (  # noqa: E402
    NAVIGATION_BREAKOUT_GOALS as _NAVIGATION_INTENTS,
)


def _init_intent_to_goal_map(
    producer_intents: frozenset, buyer_intents: frozenset, common_intents: frozenset
) -> None:
    """Populate INTENT_TO_GOAL_MAP once role-based sets are available.

    Called from routing.py at module-load time to avoid a circular import
    (this module must not import role sets directly from routing).
    """
    INTENT_TO_GOAL_MAP.clear()
    for intent_key in producer_intents | buyer_intents | common_intents:
        INTENT_TO_GOAL_MAP[intent_key] = intent_key


# ── Buyer product request heuristic ────────────────────────────────

_BUYER_PRODUCT_HINTS = (
    "je veux",
    "je voudrais",
    "j'aimerais",
    "je cherche",
    "je recherche",
    "il me faut",
    "besoin de",
    "j'ai besoin",
    "cherche",
)
# Marqueurs de SUIVI/gestion d'une commande existante (≠ acte d'achat). Ils
# désactivent le repli "demande produit". IMPORTANT : ces marqueurs sont
# comparés au MOT ENTIER (borne \b), pas en sous-chaîne — sinon "commander"
# (= acheter) était capté par "commande" (= ma commande à suivre) et l'achat
# retombait à tort sur le suivi de commandes.
_BUYER_PRODUCT_EXCLUDES = (
    "commande",
    "commandes",
    "suivre",
    "statut",
    "paiement",
    "appels",
    "appel",
    "prix",
)
_BUYER_FILLER_WORDS = frozenset(
    {
        "je",
        "veux",
        "voudrais",
        "cherches",
        "cherche",
        "recherche",
        "du",
        "de",
        "des",
        "de la",
        "d",
        "un",
        "une",
        "le",
        "la",
        "les",
        "il",
        "me",
        "faut",
        "besoin",
        "avoir",
        "jai",
        "j",
        "ai",
        "pour",
        "acheter",
        # "commander"/"commandez" = verbe d'achat, jamais un produit : filtré de
        # l'extraction pour que "je veux commander des tomates" donne "tomates".
        "commander",
        "commandez",
    }
)


def _looks_like_buyer_product_request(clean_text: str) -> bool:
    if not clean_text:
        return False
    if not any(hint in clean_text for hint in _BUYER_PRODUCT_HINTS):
        return False
    if any(
        _re.search(rf"\b{_re.escape(ex)}\b", clean_text)
        for ex in _BUYER_PRODUCT_EXCLUDES
    ):
        return False
    tokens = _re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ']+", clean_text)
    meaningful = [t for t in tokens if t not in _BUYER_FILLER_WORDS]
    return bool(meaningful)


def _extract_buyer_product(clean_text: str) -> Optional[str]:
    """Extraction déterministe du produit d'une demande d'achat.

    Utilisée UNIQUEMENT en repli quand le LLM est indisponible (429/timeout) :
    retire les mots de remplissage et les verbes d'achat, garde les tokens
    porteurs de sens. Ce n'est PAS le chemin nominal — dès que le LLM répond,
    c'est lui qui extrait le produit.
    """
    if not clean_text:
        return None
    tokens = _re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ']+", clean_text)
    meaningful = [t for t in tokens if t not in _BUYER_FILLER_WORDS]
    return " ".join(meaningful) if meaningful else None


# ── Goal planner node ──────────────────────────────────────────────


async def goal_planner(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Machine à états pure pour la gestion du cycle de vie des intentions."""
    event = str(state.get("interpreted_event") or "UNKNOWN").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    working = state.get("working_memory") or {}
    current_goal = (
        state.get("current_goal")
        or working.get("active_goal")
        or working.get("locked_intent")
    )
    # Restauration du pseudo-goal DISAMBIGUATION_PENDING entre deux tours.
    # `_lock` (plus bas) refuse volontairement de verrouiller ce pseudo-goal dans
    # working_memory.active_goal, et post_response_cleanup remet current_goal à
    # None. Résultat : la réponse de l'utilisateur au menu ("2") arrivait avec
    # current_goal=None → la RÈGLE 0bis ne se déclenchait pas → menu ré-affiché en
    # boucle ("Que souhaitez-vous choisir ?"). Le flag working_memory
    # .disambiguation_pending, lui, SURVIT (keep_selection_channel dans cleanup) :
    # on s'en sert pour reconstituer le pseudo-goal et laisser la RÈGLE 0bis
    # résoudre la sélection.
    if not current_goal and working.get("disambiguation_pending"):
        current_goal = "DISAMBIGUATION_PENDING"
    # (2026-09-02, refonte "no legacy shim") : `expected_input` n'est plus lu
    # depuis `state` — dérivé de `pending_interaction`, seule source
    # canonique, via `to_tunnel_category` (traduction vers le vocabulaire
    # grossier attendu par TunnelManager, catégorie DISTINCTE du kind précis).
    # Un seul point de traduction : tout le reste de cette fonction (in_tunnel,
    # libellés de statut, les deux appels tunnel_manager.evaluate ci-dessous)
    # consomme cette même variable, plus jamais `state.get("expected_input")`.
    expected_input = to_tunnel_category(get_pending_interaction(state))
    goal_stack = list(state.get("goal_stack") or [])
    in_tunnel = bool(current_goal and expected_input and expected_input != "NONE")
    text = (
        str(state.get("normalized_text") or state.get("user_query") or "")
        .strip()
        .lower()
    )
    if not text:
        messages = state.get("messages") or []
        if isinstance(messages, list):
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                role2 = str(msg.get("role") or "").lower().strip()
                if role2 != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    text = content.strip().lower()
                    break
    is_short = (
        len(text) <= 4 or text.isdigit() or text in {"oui", "non", "ok", "yes", "no"}
    )

    updates: Dict[str, Any] = {
        "status": "PLANNING",
        "interruption_detected": False,
    }

    logger.info(
        "[GoalPlanner IN] event=%s intent=%s current_goal=%s expected=%s",
        event,
        detected_intent,
        current_goal,
        expected_input,
    )

    def _lock(
        goal: Optional[str], extra: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        wm = dict(working)
        if goal and goal != "DISAMBIGUATION_PENDING":
            wm["active_goal"] = goal
            wm["locked_intent"] = goal
            wm.setdefault("step_index", 0)
        if extra:
            wm.update(extra)
        return wm

    def _clear_goal_lock() -> Dict[str, Any]:
        """Sur REJECT/annulation : supprime les verrous de tunnel ET tout
        l'état de menu/sélection (univers de clés partagé avec nodes/cleanup.py,
        source unique — cf. `_GOAL_LOCK_CLEAR_KEYS`), pour ne pas laisser un
        menu périmé actif après l'abandon de l'intention en cours."""
        wm = dict(working)
        for k in _GOAL_LOCK_CLEAR_KEYS:
            wm.pop(k, None)
        return wm

    def _purge_transaction_state() -> Dict[str, Any]:
        """Purge totale des champs transactionnels/AG-UI lors d'un switch d'intention."""
        return {
            "transaction_payload": {"__reset__": True},
            "draft_payload": {"__reset__": True},
            "stable_entities": {"__reset__": True},
            "missing_fields": [],
            "completed_fields": [],
            "last_missing_field": None,
            "expected_candidates": [],
            "available_mapping": {},
            "confirmation_summary": None,
            "selected_tool": None,
            "selected_tool_args": {"__reset__": True},
            "execution_result": {"__reset__": True},
            "retry_count": 0,
            "vendor_selection_context": {"__reset__": True},
            # (2026-09-02, refonte state canonique, G-2) : symétrique de la
            # ligne au-dessus — un switch de goal/intention doit invalider le
            # palier choisi exactement comme il invalide le vendeur choisi.
            # Absent jusqu'ici : un `tier_selection_context` pouvait survivre
            # à un changement d'intention et être réutilisé pour un NOUVEAU
            # produit sans aucun rapport (confirmé par audit — voir
            # `domain/selection_actions.py::build_selection_context`, qui
            # lit ce champ tel quel sans jamais vérifier qu'il correspond
            # encore au vendeur/produit courant).
            "tier_selection_context": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "preorder_workflow": {"__reset__": True},
            # A form belongs to exactly one goal instance — leaving it set
            # while switching to an unrelated goal can misroute the NEXT
            # turn straight back into form_node and leaks the abandoned
            # form's slots via `form_data` (merge_dict).
            "active_form": None,
            "form_step": None,
            "form_data": {"__reset__": True},
            # (2026-09-02) Politique d'invalidation centralisée (mandat §8) :
            # un switch de goal/intention efface aussi le discriminant
            # canonique — sans ceci, un `pending_interaction` persisté
            # (CONFIRM_ACTION/ENTER_FIELD/...) pouvait survivre à un
            # changement d'intention et fuiter dans le nouveau parcours.
            **clear_pending_interaction("goal_changed"),
        }

    def _with_goal_metadata(
        payload: Dict[str, Any], goal_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        target_goal = goal_hint
        if target_goal is None:
            target_goal = payload.get("current_goal")
            if not target_goal:
                wm_payload = payload.get("working_memory") or {}
                target_goal = (
                    wm_payload.get("active_goal")
                    or wm_payload.get("locked_intent")
                    or state.get("current_goal")
                )
        goal_key = str(target_goal or "").upper().strip()
        cfg = INTENT_CONFIG.get(goal_key, {})
        lifecycle = str(cfg.get("lifecycle_mode") or "").upper().strip()
        if lifecycle not in {"CREATE", "UPDATE", "READ"}:
            lifecycle = "READ"
        payload["goal_metadata"] = {
            "lifecycle_mode": lifecycle,
            "update_mode": lifecycle == "UPDATE",
        }
        return payload

    # RÈGLE 0bis — RÉSOLUTION DE DÉSAMBIGUÏSATION
    if current_goal == "DISAMBIGUATION_PENDING":
        override_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if (
            event in {"INTERRUPTION", "NEW_TASK"}
            and override_goal
            and override_goal != "DISAMBIGUATION_PENDING"
        ):
            logger.info(
                "[Disambiguation Override] event=%s intent=%s -> current_goal=%s",
                event,
                detected_intent,
                override_goal,
            )
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": override_goal,
                    "goal_status": "ACTIVE",
                    "interruption_detected": event == "INTERRUPTION",
                    "working_memory": {
                        **_lock(override_goal),
                        "disambiguation_pending": False,
                        "available_mapping_kind": None,
                    },
                    **_purge_transaction_state(),
                },
                override_goal,
            )

        extracted = state.get("extracted_entities") or {}
        mapping = dict(state.get("available_mapping") or {})

        trigger_id = str(
            (state.get("working_memory") or {}).get("disambiguation_trigger_id") or ""
        ).strip()
        if trigger_id:
            entry = INTENT_DISAMBIGUATION.get(trigger_id)
            if entry:
                rebuilt: Dict[str, str] = {}
                for i, opt in enumerate(entry.get("options") or [], start=1):
                    intent_key = None
                    if isinstance(opt, (tuple, list)) and len(opt) >= 2:
                        intent_key = opt[0]
                    elif isinstance(opt, dict):
                        intent_key = opt.get("intent")
                    if intent_key:
                        rebuilt[str(i)] = str(intent_key)
                if rebuilt:
                    if mapping and mapping != rebuilt:
                        logger.warning(
                            "[Disambiguation] stale mapping overridden by trigger=%s",
                            trigger_id,
                        )
                    mapping = rebuilt
                    logger.info(
                        "[Disambiguation] rebuilt mapping from trigger=%s with %d options",
                        trigger_id,
                        len(mapping),
                    )

        sel_idx = extracted.get("selection_index")
        sel_val = extracted.get("selected_value")

        resolved_intent: Optional[str] = None
        if sel_idx is not None:
            resolved_intent = mapping.get(str(sel_idx))
        if resolved_intent is None and sel_val is not None:
            resolved_intent = mapping.get(str(sel_val))

        if (
            resolved_intent is None
            and detected_intent in INTENT_TO_GOAL_MAP
            and detected_intent not in {"", "UNKNOWN", "DISAMBIGUATION_PENDING"}
        ):
            resolved_intent = detected_intent

        if resolved_intent and resolved_intent in INTENT_TO_GOAL_MAP:
            logger.info(
                "[Disambiguation Resolved] selection=%s promoted to current_goal=%s",
                sel_idx or sel_val,
                resolved_intent,
            )
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": resolved_intent,
                    "goal_status": "ACTIVE",
                    "interruption_detected": False,
                    "expected_candidates": [],
                    "available_mapping": {},
                    "missing_fields": [],
                    "completed_fields": [],
                    "working_memory": {
                        **_lock(resolved_intent),
                        "disambiguation_pending": False,
                        "available_mapping_kind": None,
                    },
                    **clear_pending_interaction("disambiguation_resolved"),
                },
                resolved_intent,
            )
        # Sélection invalide ou pas encore reçue : on garde le menu actif.
        updates["current_goal"] = "DISAMBIGUATION_PENDING"
        updates["goal_status"] = "WAITING_INPUT"
        updates.update(
            set_pending_interaction(
                InteractionKind.SELECTION_MENU,
                goal="DISAMBIGUATION_PENDING",
                context_ref="intent_disambiguation",
            )
        )
        updates["confirmation_summary"] = None
        updates["response_strategy"] = "SELECTION_MENU"
        updates["working_memory"] = {
            **dict(working),
            "disambiguation_pending": True,
            "available_mapping_kind": "intent_disambiguation",
        }
        return _with_goal_metadata(updates)

    # RÈGLE 1 — CANCEL/REJECT (Annulation explicite)
    if event == "REJECT":
        # Source unique : `expected_input` ci-dessus est déjà dérivé de
        # `pending_interaction` (voir plus haut) — plus de lecture directe de
        # `waiting_for_confirmation`.
        waiting_confirm = str(expected_input or "").upper() == "CONFIRMATION"
        if not waiting_confirm:
            return _with_goal_metadata(
                {
                    "status": "WAITING_INPUT",
                    "current_goal": None,
                    "goal_status": "IDLE",
                    "interruption_detected": False,
                    "response_strategy": "CLARIFICATION",
                    "working_memory": _clear_goal_lock(),
                    **_purge_transaction_state(),
                }
            )

        # Rejet pendant confirmation : laisser confirmation_gate gérer la logique.
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1bis — TUNNEL LOCKING (Maintien des formulaires d'IHM)
    if event in {"CONFIRM", "SELECTION", "ANSWER", "UPDATE"}:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1ter — PERSISTENCE PAR DÉFAUT
    if (
        current_goal
        and detected_intent == "UNKNOWN"
        and event in {"UNKNOWN", "NEW_TASK"}
    ):
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = (
            "ACTIVE" if expected_input in (None, "", "NONE") else "WAITING_INPUT"
        )
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1quater — VERROUILLAGE PENDANT SLOT-FILLING
    if (
        current_goal
        and event == "NEW_TASK"
        and expected_input not in (None, "", "NONE")
    ):
        confidence = float(state.get("interpreter_confidence") or 0.0)
        td = tunnel_manager.evaluate(
            current_goal=current_goal,
            expected_input=expected_input,
            incoming_event=event,
            incoming_intent=detected_intent,
            confidence=confidence,
        )
        if td.allow_interrupt:
            new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
            if new_goal:
                if current_goal:
                    goal_stack.append(current_goal)
                updates["current_goal"] = new_goal
                updates["goal_status"] = "ACTIVE"
                updates["interruption_detected"] = True
                updates["suspended_goal"] = current_goal
                updates["suspended_payload"] = state.get("transaction_payload") or {}
                updates["goal_stack"] = goal_stack
                updates["working_memory"] = _lock(new_goal)
                updates.update(_purge_transaction_state())
                logger.info(
                    "[GoalPlanner] TunnelManager allowed NEW_TASK switch: %s → %s (reason=%s)",
                    current_goal,
                    new_goal,
                    td.reason,
                )
                return _with_goal_metadata(updates)
        # Tunnel stays locked — re-assert current goal.
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    if is_short and current_goal:
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = (
            "ACTIVE" if expected_input in {None, "NONE"} else "WAITING_INPUT"
        )
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 2 — POLLUTION PENDANT SLOT-FILLING
    if event == "UNKNOWN" and in_tunnel:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "WAITING_INPUT"
        updates["detected_intent"] = str(current_goal).upper()
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 3 — OUT_OF_SCOPE
    if event == "OUT_OF_SCOPE":
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4 — INTERRUPTION (Suspension d'un workflow IHM en cours)
    if event == "INTERRUPTION":
        confidence = float(state.get("interpreter_confidence") or 0.0)
        td = tunnel_manager.evaluate(
            current_goal=current_goal,
            expected_input=expected_input,
            incoming_event=event,
            incoming_intent=detected_intent,
            confidence=confidence,
        )
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        # `allow_interrupt` protège un tunnel ACTIF d'un déraillement. S'il n'y
        # a PAS de tunnel actif (`current_goal` vide — ex: un menu orphelin
        # affiché sans goal de rattachement, comme une liste de commandes
        # restée à l'écran), il n'y a rien à protéger : une « interruption » de
        # rien = une nouvelle tâche → on bascule directement. Sans ce
        # `or not current_goal`, l'utilisateur restait piégé dans le menu, sa
        # nouvelle demande (« je veux des tomates ») ignorée en boucle.
        if (
            new_goal
            and new_goal != current_goal
            and (td.allow_interrupt or not current_goal)
        ):
            if current_goal:
                goal_stack.append(current_goal)
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": new_goal,
                    "goal_stack": goal_stack,
                    "goal_status": "ACTIVE",
                    "interruption_detected": True,
                    "suspended_goal": current_goal,
                    "suspended_payload": state.get("transaction_payload") or {},
                    **_purge_transaction_state(),
                    "working_memory": _lock(new_goal),
                },
                new_goal,
            )
        if td.allow_interrupt and new_goal and new_goal == current_goal:
            # Same goal, new entities: user is mid-confirmation for one
            # instance of this goal (e.g. BUYER_REQUEST "poulets") and just
            # re-triggered the SAME goal with DIFFERENT entities ("je veux
            # des poussins"). `new_goal != current_goal` above is False so
            # this never restarted — the stale payload survived and the
            # recap kept showing the old product forever. Purge the
            # transaction so the freshly extracted entities populate a
            # clean slate instead of merging onto an already-confirmed-
            # looking recap.
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": new_goal,
                    "goal_status": "ACTIVE",
                    "interruption_detected": True,
                    **_purge_transaction_state(),
                    "working_memory": _lock(new_goal),
                },
                new_goal,
            )
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4bis — RESUME (Reprise d'une tâche suspendue dans goal_stack)
    if event == "RESUME":
        if goal_stack:
            resumed = goal_stack.pop()
            restored_payload = dict(state.get("suspended_payload") or {})
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": resumed,
                    "goal_stack": goal_stack,
                    "goal_status": "ACTIVE",
                    "interruption_detected": False,
                    "suspended_goal": None,
                    "suspended_payload": {"__reset__": True},
                    # Full canonical purge FIRST — clears whatever the
                    # interrupting goal accumulated in its own
                    # transaction_payload/vendor_selection_context/
                    # negotiation_context/preorder_workflow/selected_tool_args/
                    # execution_result/draft_payload/form_data (previously this
                    # branch hand-rolled a shorter reset list and missed all of
                    # these, letting the interrupting goal's fields leak into
                    # the resumed one) — THEN restore the resumed goal's own
                    # payload via the combined reset+populate sentinel: a plain
                    # merge of `restored_payload` onto the just-purged (but not
                    # yet actually empty, since it's one reducer call) old value
                    # would let any key present in the interrupting goal's
                    # payload but absent from `restored_payload` survive the
                    # merge — this sentinel guarantees a true replace instead.
                    **_purge_transaction_state(),
                    "transaction_payload": {"__reset__": True, **restored_payload},
                    "working_memory": _lock(resumed),
                },
                resumed,
            )
        # Aucun goal suspendu — traiter comme clarification
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 5 — NEW_TASK (Instanciation et purge des champs AG-UI)
    if event == "NEW_TASK":
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if new_goal:
            updates["current_goal"] = new_goal
            updates["goal_status"] = "ACTIVE"
            updates["working_memory"] = _lock(new_goal)
            # Une NOUVELLE tâche repart TOUJOURS d'un état transactionnel PROPRE.
            # Les slots de la tâche précédente (produit, quantité, prix...) ne
            # doivent JAMAIS fuiter dans la nouvelle — les entités du message
            # courant sont ré-appliquées juste après par memory_update
            # (extracted_entities), donc rien de légitime n'est perdu.
            #
            # Auparavant on ne purgeait que si le goal changeait, ou si le
            # produit différait. Deux failles : (1) deux demandes d'achat de
            # suite = même goal BUYER_REQUEST → pas de purge ; (2) une fois
            # "234 kg" fuité dans le payload sauvegardé AVEC le produit tomates,
            # la comparaison produit==produit ne purgeait plus jamais → fuite
            # auto-entretenue dans l'état persistant. Purge inconditionnelle =
            # résilience : impossible qu'un slot mort survive à une nouvelle
            # tâche. Une purge sur un payload déjà vide est sans effet.
            updates.update(_purge_transaction_state())
            logger.info("[GoalPlanner OUT] new_goal=%s", new_goal)
            logger.debug("[GoalPlanner OUT] new_goal=%s", new_goal)
            return _with_goal_metadata(updates)

    # Fallback par défaut vers clarification
    updates["current_goal"] = current_goal
    updates["response_strategy"] = "CLARIFICATION"
    updates["status"] = "WAITING_INPUT"
    updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
    if current_goal:
        updates["detected_intent"] = str(current_goal).upper()
    updates["working_memory"] = _lock(current_goal)
    return _with_goal_metadata(updates)


__all__ = [
    "INTENT_TO_GOAL_MAP",
    "_NAVIGATION_INTENTS",
    "_init_intent_to_goal_map",
    "_looks_like_buyer_product_request",
    "_extract_buyer_product",
    "_BUYER_PRODUCT_HINTS",
    "_BUYER_PRODUCT_EXCLUDES",
    "_BUYER_FILLER_WORDS",
    "goal_planner",
]
