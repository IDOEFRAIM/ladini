from __future__ import annotations

from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    get_pending_interaction,
    to_tunnel_category,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import (
    _label_for_field,
)
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    _DISAMBIGUATION_CONFIDENCE_THRESHOLD,
    _detect_disambiguation_candidates,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _compute_progress,
)

logger = get_node_logger("CognitiveNode")


def _build_proactive_hint(
    goal: Optional[str],
    progress: Optional[Dict[str, Any]],
    payload: Dict[str, Any],
) -> Optional[str]:
    if not goal or not progress:
        return None
    pct = progress.get("pct", 0)
    remaining = progress.get("remaining") or []
    goal_label = (INTENT_CONFIG.get(goal) or {}).get("label", goal)

    if pct == 0:
        return f"Nouvelle opération : {goal_label}"
    if pct >= 100:
        return "Toutes les informations sont réunies, prêt pour confirmation."
    if len(remaining) == 1:
        field_label = _label_for_field(goal, remaining[0])
        return f"Plus qu'une info : {field_label}."
    return f"Progression : {pct}% — encore {len(remaining)} infos nécessaires."


def _entity_carry_forward(
    state: Dict[str, Any],
    current_goal: Optional[str],
    in_tunnel: bool,
) -> Optional[Dict[str, Any]]:
    if not (in_tunnel and current_goal):
        return None
    stable = state.get("stable_entities") or {}
    entities = dict(state.get("extracted_entities") or {})
    carried = False
    for key in ("product", "unit", "zone_name"):
        if not entities.get(key) and stable.get(key):
            entities[key] = stable[key]
            carried = True
    return entities if carried else None


def _should_trigger_disambiguation(
    competition: List[Dict[str, Any]],
    confidence: float,
) -> bool:
    intents = {str(c.get("intent")) for c in competition if c.get("intent")}
    return len(intents) > 1 and confidence < _DISAMBIGUATION_CONFIDENCE_THRESHOLD


async def cognitive_guard(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    event = str(state.get("interpreted_event") or "").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    confidence = float(state.get("interpreter_confidence") or 0.0)
    pending = get_pending_interaction(state)
    expected_input = to_tunnel_category(pending)
    current_goal = state.get("current_goal")
    payload = dict(state.get("transaction_payload") or {})
    text_lower = (state.get("normalized_text") or state.get("user_query") or "").lower()
    retry_count = int(state.get("retry_count") or 0)
    in_tunnel = bool(current_goal and pending.kind != InteractionKind.NONE)

    user_role = str(state.get("forced_role") or state.get("user_role") or "").upper()

    updates: Dict[str, Any] = {}
    decision: Dict[str, Any] = {
        "event": event,
        "intent": detected_intent,
        "confidence": confidence,
        "expected_input": expected_input,
        "in_tunnel": in_tunnel,
        "bounded": True,
    }
    competition: List[Dict[str, Any]] = []

    entry = _detect_disambiguation_candidates(text_lower, user_role)
    if entry:
        for intent_key in entry.get("candidates") or []:
            competition.append(
                {
                    "intent": intent_key,
                    "source": "lexical_disambiguation",
                    "trigger": entry.get("id"),
                }
            )
    if detected_intent != "UNKNOWN":
        competition.append(
            {
                "intent": detected_intent,
                "source": "llm_interpreter",
                "confidence": confidence,
            }
        )

    carried_entities = _entity_carry_forward(state, current_goal, in_tunnel)
    if carried_entities:
        updates["extracted_entities"] = carried_entities
        decision["entity_carry_forward"] = True

    if (
        current_goal
        and event == "NEW_TASK"
        and detected_intent not in {"UNKNOWN", str(current_goal).upper()}
    ):
        updates.update(
            {
                "interpreted_event": "INTERRUPTION",
                "intent_competition": competition,
                "cognitive_decision": {**decision, "action": "suspend_current_goal"},
            }
        )
        return updates

    # Un partage de position WhatsApp natif n'a pas de texte à classifier —
    # l'interprète LLM renvoie donc systématiquement event=UNKNOWN pour ces
    # tours. Sans cette exclusion, tout gate GPS en cours de tunnel (ex.
    # finalize_winner / create_preorder, voir [[gps-delivery-burkina-faso-2026-08]])
    # tombait dans la récupération/abandon de tunnel ci-dessous au lieu de
    # jamais atteindre le resolver qui sait gérer location_shared.
    location_shared = bool(state.get("location_shared"))
    if event == "UNKNOWN" and in_tunnel and not location_shared:
        if retry_count >= 2:
            logger.warning(
                "[CognitiveGuard] Max retries reached for goal=%s — abandoning tunnel",
                current_goal,
            )
            # Chantier résilience 2026-08 : les mini machines à états
            # auto-suffisantes (flows/producer/auctions.py::bid_phase,
            # flows/producer/flow.py::update_phase) vivent EXCLUSIVEMENT dans
            # working_memory, jamais touché par le reset ci-dessous avant ce
            # correctif. Un abandon de tunnel en plein milieu (ex: bid_phase=
            # "CONFIRM") laissait ces clés stales — relancer le même goal peu
            # après (MARKET_BROWSE_REQUESTS / SALES_UPDATE_PRODUCTION /
            # SALES_UPDATE_PRODUCT) relisait alors une phase/un id périmé et
            # pouvait réafficher un récap obsolète au lieu de redémarrer
            # proprement. current_goal étant remis à None juste en dessous
            # (donc plus aucun tunnel actif), ces clés n'ont plus aucune
            # raison de survivre non plus.
            stale_wm_keys = (
                "bid_phase", "pending_bid_auction", "pending_bid_price",
                "pending_modify_bid",
                "update_phase", "update_cycle_id", "update_product_id",
                "update_pending",
                # (2026-09-02) Même bug, côté acheteur : `winner_gps_stage`
                # (flows/buyer/order_tracking.py) est la même famille de
                # mini-état auto-suffisant que bid_phase/update_phase
                # ci-dessus — jamais touchée par ce reset avant ce correctif,
                # alors que `current_goal` l'est. Un abandon de tunnel en
                # pleine étape GPS gagnant-enchère laissait ce flag actif :
                # relancer plus tard le suivi de commande retombait
                # directement sur l'étape GPS sans qu'aucun `pending_interaction`
                # actif ne l'explique (voir aussi `preorder_workflow.gps_stage`
                # ci-dessous, même classe de bug, incident réel +22601479800).
                "winner_gps_stage",
            )
            working_memory = dict(state.get("working_memory") or {})
            for key in stale_wm_keys:
                working_memory[key] = None

            updates.update(
                {
                    "current_goal": None,
                    "goal_status": "IDLE",
                    "status": "WAITING_INPUT",
                    # (2026-09-02) Symétrique à `stale_wm_keys` ci-dessus, mais
                    # `preorder_workflow` (flows/buyer/preorder.py) est un
                    # champ top-level `merge_dict` (BuyerContext), pas une clé
                    # de `working_memory` — donc hors de portée de la boucle
                    # au-dessus. `gps_stage`/`gps_default` sont la même
                    # famille de mini-état auto-suffisant que `bid_phase` :
                    # sans ce reset, un tunnel abandonné en pleine étape GPS
                    # précommande laisse `gps_stage=True` collé indéfiniment
                    # (merge_dict ne l'efface jamais tout seul), alors que
                    # `pending_interaction` (PROVIDE_LOCATION), lui, est bien
                    # effacé par `clear_pending_interaction` ci-dessous — les
                    # deux DOIVENT tomber ensemble, sinon `create_preorder`
                    # retombe sur `resolve_gps_stage` à la reprise sans aucun
                    # `pending_interaction` actif pour le justifier.
                    "preorder_workflow": {"gps_stage": None, "gps_default": None},
                    # merge_dict-reduced fields: a plain {} is a NO-OP under
                    # merge_dict (agents/reducers.py) — it PRESERVES the old
                    # value instead of clearing it. Only {"__reset__": True}
                    # actually empties the field. Writing plain {} here was
                    # the root cause of quantity/product/price from an
                    # abandoned goal silently surviving into the next goal's
                    # transaction_payload (current_goal was correctly reset
                    # to None, but the stale data underneath it was not).
                    "transaction_payload": {"__reset__": True},
                    "stable_entities": {"__reset__": True},
                    "missing_fields": [],
                    "completed_fields": [],
                    "last_missing_field": None,
                    "expected_candidates": [],
                    "available_mapping": {},
                    "retry_count": 0,
                    "confirmation_summary": None,
                    **clear_pending_interaction("tunnel_abandoned_max_retries"),
                    "selected_tool": None,
                    "selected_tool_args": {"__reset__": True},
                    "execution_result": {"__reset__": True},
                    "ag_ui_component": None,
                    "response_strategy": "CLARIFICATION",
                    "intent_competition": competition,
                    "cognitive_decision": {
                        **decision,
                        "action": "abandon_tunnel_max_retries",
                    },
                    "proactive_hint": "L'opération a été annulée. Dites-moi ce que vous souhaitez faire.",
                    "working_memory": working_memory,
                }
            )
            return updates

        # BUG CAPITAL (2026-08-19) : cette branche RAPPORTAIT `retry_count`
        # sans jamais l'INCRÉMENTER. Combiné au reset inconditionnel de
        # `retry_count` par `post_response_cleanup` en fin de CHAQUE tour
        # (voir _EPHEMERAL_REPLACE_FIELDS), le seuil `retry_count >= 2`
        # ci-dessus était donc littéralement INATTEIGNABLE : l'abandon de
        # tunnel `abandon_tunnel_max_retries` — la SEULE sortie de secours
        # automatique de tout l'agent — était du code mort. Un utilisateur
        # dont les messages ne sont pas classifiables (event=UNKNOWN) restait
        # piégé dans le même tunnel indéfiniment, à recevoir "je n'ai pas bien
        # saisi" à chaque tour, sans jamais que l'agent ne lâche prise de
        # lui-même. Reproduit et vérifié avant correctif (5 tours simulés →
        # `recover_active_tunnel` à l'infini). Le compteur est maintenant
        # incrémenté ici, ET préservé d'un tour à l'autre tant que le tunnel
        # vit (voir cleanup.py::_keep_goal_channel).
        updates.update(
            {
                "status": "WAITING_INPUT",
                "current_goal": current_goal,
                "goal_status": "WAITING_INPUT",
                "response_strategy": "RECOVERY",
                "retry_count": retry_count + 1,
                "intent_competition": competition,
                "cognitive_decision": {
                    **decision,
                    "action": "recover_active_tunnel",
                    "retry": retry_count + 1,
                },
            }
        )
        return updates

    entities = state.get("extracted_entities") or {}
    entity_count = sum(1 for v in entities.values() if v not in (None, "", [], {}))
    if in_tunnel and entity_count >= 2 and event == "ANSWER":
        decision["express_mode"] = True
        decision["entity_richness"] = entity_count

    progress_payload = {**payload}
    for k, v in entities.items():
        if v not in (None, "", [], {}):
            progress_payload[k] = v
    progress = _compute_progress(current_goal, progress_payload)
    if progress:
        updates["conversation_progress"] = progress
        hint = _build_proactive_hint(current_goal, progress, progress_payload)
        if hint:
            updates["proactive_hint"] = hint

    # Le compteur d'échecs mesure des échecs CONSÉCUTIFS, pas cumulés sur
    # toute la durée du tunnel : dès que ce tour a été compris (on atteint la
    # branche "continue"), on repart de zéro. Sans ça, un utilisateur qui
    # bute deux fois, se fait comprendre, puis bute une seule fois de plus se
    # faisait éjecter de son opération — alors qu'il progressait. Vérifié par
    # simulation avant/après (scénario UNKNOWN, UNKNOWN, ANSWER, UNKNOWN :
    # abandonnait au 4e tour, ne le fait plus).
    if in_tunnel and retry_count:
        updates["retry_count"] = 0

    updates.update(
        {
            "intent_competition": competition,
            "cognitive_decision": {**decision, "action": "continue"},
        }
    )
    return updates


async def cognitive_orchestrator(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    if state.get("is_onboarding"):
        event = str(state.get("interpreted_event") or "").upper()
        intent = str(state.get("detected_intent") or "UNKNOWN").upper()
        confidence = float(state.get("interpreter_confidence") or 0.0)
        return {
            "cognitive_decision": {
                "phase": "reason",
                "next_step": "onboarding",
                "reason": "onboarding",
                "loop": [
                    "perceive",
                    "think",
                    "decide",
                    "act",
                    "observe",
                    "reason",
                    "retry",
                ],
                "event": event,
                "intent": intent,
                "current_goal": None,
                "confidence": confidence,
            },
            "should_replan": False,
        }

    event = str(state.get("interpreted_event") or "").upper()
    intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    current_goal = str(state.get("current_goal") or "").upper()
    pending = get_pending_interaction(state)
    strategy = str(state.get("response_strategy") or "").upper()
    confidence = float(state.get("interpreter_confidence") or 0.0)
    competition = list(state.get("intent_competition") or [])
    in_tunnel = bool(current_goal and pending.kind != InteractionKind.NONE)

    phase = "perceive"
    next_step = "continue"
    reason = "nominal"

    if strategy in {"CLARIFICATION", "RECOVERY"}:
        phase = "reason"
        next_step = "respond"
        reason = strategy.lower()
    elif event == "INTERRUPTION":
        phase = "decide"
        next_step = "replan"
        reason = "interruption"
    elif _should_trigger_disambiguation(competition, confidence):
        phase = "think"
        next_step = "clarify"
        reason = "intent_competition"
    elif in_tunnel and (
        event in {"ANSWER", "UPDATE", "SELECTION", "CONFIRM", "REJECT"}
        or bool(state.get("location_shared"))
    ):
        phase = "act"
        next_step = "continue_tunnel"
        reason = "active_goal"
    elif intent == "UNKNOWN" and event in {"UNKNOWN", "OUT_OF_SCOPE"}:
        phase = "reason"
        next_step = "clarify"
        reason = "unknown_intent"

    return {
        "cognitive_decision": {
            "phase": phase,
            "next_step": next_step,
            "reason": reason,
            "loop": [
                "perceive",
                "think",
                "decide",
                "act",
                "observe",
                "reason",
                "retry",
            ],
            "event": event,
            "intent": intent,
            "current_goal": current_goal or None,
            "confidence": confidence,
        },
        "should_replan": next_step == "replan",
    }


__all__ = [
    "cognitive_guard",
    "cognitive_orchestrator",
]
