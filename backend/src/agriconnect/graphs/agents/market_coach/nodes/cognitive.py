from __future__ import annotations

from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.base import get_node_logger
from agriconnect.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from agriconnect.graphs.agents.market_coach.core.goals import (
    NAVIGATION_BREAKOUT_GOALS,
)
from agriconnect.graphs.agents.market_coach.core.conversation_reset import (
    reset_abandoned_conversation_context,
)
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import (
    INTERRUPTION_CONFIDENCE_THRESHOLD,
)
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    to_tunnel_category,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.nodes.clarification import (
    has_out_of_tunnel_location_error,
)
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import (
    _label_for_field,
)
from agriconnect.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    _DISAMBIGUATION_CONFIDENCE_THRESHOLD,
    _detect_disambiguation_candidates,
    extract_disambiguation_intents,
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


def _classify_nominal_action(
    *,
    event: str,
    current_goal: Optional[str],
    already_expecting: bool,
    text_lower: str,
    detected_intent: str,
    confidence: float,
    entry: Optional[Dict[str, Any]],
    state: Dict[str, Any],
) -> tuple[str, str]:
    """Sous-classifie le tour NOMINAL (ni interruption, ni recovery/abandon
    de tunnel — ces trois-là ont déjà retourné plus haut dans
    `cognitive_guard`) en l'une des 4 actions restantes du vocabulaire
    `ConversationDecision` : CLARIFY, DISAMBIGUATE, CONTINUE_ACTIVE_GOAL,
    START_OR_PLAN_GOAL.

    (2026-09-08, correction topologique du bloc conversationnel, mandat
    §2-§14) : ces conditions étaient AUPARAVANT dispersées et recalculées
    indépendamment par les nœuds d'exécution eux-mêmes —
    `clarification_node::needs_clarification` (clause générique) et
    `semantic_disambiguation` (son propre garde de désambiguïsation) — sur
    un edge FIXE qui les visitait tous les deux à chaque tour, même
    nominal. Elles sont ici reproduites À L'IDENTIQUE (mêmes seuils, même
    ordre de priorité), pas réinventées : la topologie change (une seule
    décision, un seul routeur trivial en aval), la décision elle-même ne
    change pas.

    Priorité (identique à l'ordre de préséance qu'appliquaient les anciens
    nœuds d'exécution) :
        1. GPS hors-tunnel en échec — priorité ABSOLUE, inconditionnelle
           (ancien comportement : vérifié en tête de `clarification_node`,
           AVANT même son propre garde `needs_clarification`) ;
        2. Désambiguïsation lexicale légitime — le LLM n'a pas tranché
           avec confiance (ancien garde de `semantic_disambiguation`) ;
        3. Clarification générique — hors-tunnel, événement incompréhensible
           (ancienne 1ère clause de `needs_clarification` ; la clause
           `abandon_tunnel_max_retries` est traitée par sa PROPRE branche,
           plus haut dans `cognitive_guard`, jamais ici) ;
        4. Nominal : CONTINUE_ACTIVE_GOAL si un goal est verrouillé,
           START_OR_PLAN_GOAL sinon.

    Dette assumée (mandat §12, "InterpreterResult.alternatives si
    disponible") : `InterpreterResult` (interpreter/interpreter_result.py)
    n'expose PAS de champ `alternatives` aujourd'hui — seul le repli
    lexical `_detect_disambiguation_candidates` alimente `entry`. Ce hook
    n'est pas inventé ici (mandat : ne pas construire une architecture non
    demandée) ; documenté comme extension future possible.
    """
    if has_out_of_tunnel_location_error(state):
        return ConversationAction.CLARIFY, "location_out_of_tunnel"

    disambiguation_eligible = (
        event in {"NEW_TASK", "UNKNOWN"}
        and not already_expecting
        and bool(text_lower)
        and not (
            detected_intent not in ("", "UNKNOWN")
            and confidence >= _DISAMBIGUATION_CONFIDENCE_THRESHOLD
        )
        and entry is not None
        # (mandat §21/§22) : compte les intents CANONIQUES (via le même
        # helper que la construction du menu), pas la longueur brute
        # d'`options` — une option malformée (sans `intent`) ne doit pas
        # compter comme un candidat valide.
        and len(extract_disambiguation_intents(entry)) >= 2
    )
    if disambiguation_eligible:
        return ConversationAction.DISAMBIGUATE, "lexical_disambiguation_candidate"

    if (
        event in {"OUT_OF_SCOPE", "UNKNOWN", "REJECT"}
        and not already_expecting
        and not current_goal
    ):
        return ConversationAction.CLARIFY, "out_of_scope_or_unknown_without_tunnel"

    if current_goal:
        return ConversationAction.CONTINUE_ACTIVE_GOAL, "structured_reply_within_active_goal"
    return ConversationAction.START_OR_PLAN_GOAL, "new_task_or_no_active_goal"


async def cognitive_guard(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Garde-fou conversationnel — PROPRIÉTAIRE UNIQUE de la décision de
    transition du tour (2026-09-08, correction topologique du bloc
    conversationnel).

    Question posée : « étant donné `InterpreterResult` (event/intent/
    confidence/entities) et le contexte conversationnel courant
    (goal verrouillé, interaction en attente, historique de retries), quelle
    transition doit suivre ce tour ? » La réponse est écrite dans
    `cognitive_decision` (réutilisation du champ existant, PAS un second
    canal concurrent — voir mandat §3) sous la forme :

        {"action": <str>, "reason": <str>, "confidence": <float>, ...}

    `action` est l'unique clé lue par le routeur en aval
    (`nodes/routing.py::_route_after_cognitive_guard`, TRIVIAL : un
    `dict.get` puis un mapping direct — il ne recalcule NI l'intent, NI la
    confiance, NI l'ambiguïté, NI les retries, NI l'interruption). Valeurs
    possibles, avec leur destination réelle dans le graphe compilé :

        START_OR_PLAN_GOAL          -> goal_planner
        CONTINUE_ACTIVE_GOAL        -> goal_planner
        INTERRUPT_ACTIVE_GOAL       -> goal_planner
        DISAMBIGUATE                -> semantic_disambiguation
        CLARIFY                     -> clarification_node
        recover_active_tunnel       -> clarification_node
        abandon_tunnel_max_retries  -> clarification_node

    Les deux derniers noms restent en minuscules/historiques
    DÉLIBÉRÉMENT (mandat §3) : `interpreter/strategy.py::response_strategy`
    (hors périmètre de ce chantier) compare CES chaînes littérales pour
    décider `RECOVERY`/`CLARIFICATION` — les renommer casserait ce
    consommateur sans le refondre. `INTERRUPT_ACTIVE_GOAL` (ex-
    `suspend_current_goal`) a, à l'inverse, été renommé librement : recherche
    exhaustive faite, AUCUN autre module ne consommait cette chaîne (voir
    rapport de refonte, section "Qui décide / qui exécute / qui route").

    (2026-09-08, revue de validation, §11 — historique, résolu par ce
    correctif) : ce nœud calculait déjà le CANDIDAT de désambiguïsation
    (`disambiguation_candidate`) mais ne décidait pas lui-même "faut-il
    désambiguïser ?" — cette décision vivait dans
    `nodes/semantic_disambiguation.py` (son propre seuil de confiance). Ce
    n'était PAS un bug de duplication (un seul endroit décidait), mais un
    propriétaire différent de celui de la topologie cible. C'est corrigé
    ici : voir `_classify_nominal_action` pour le détail phase par phase
    ce nœud (interruption, recovery, abandon — inchangés par ce
    correctif)."""
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
    # Distinct de `in_tunnel` : vrai dès qu'UNE interaction est en attente,
    # même sans goal verrouillé (ex: DISAMBIGUATION_PENDING). Même
    # définition que l'ancien `already_expecting` de
    # `semantic_disambiguation`/`nothing_pending` de `clarification_node`
    # (négation) — un seul calcul désormais, consulté par
    # `_classify_nominal_action`.
    already_expecting = pending.kind != InteractionKind.NONE

    # (2026-09-08, P1-5 audit architectural) : `forced_role` supprimé — ce
    # canal n'a jamais existé (voir interpreter/routing.py), la branche
    # gauche de ce `or` était morte.
    user_role = str(state.get("user_role") or "").upper()

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

    # (2026-09-08, mandat §9 : "une seule décision, un seul propriétaire")
    # `cognitive_guard` calcule le candidat de désambiguïsation UNE fois ;
    # `clarification_node`/`semantic_disambiguation` le CONSULTENT au lieu
    # de rappeler `_detect_disambiguation_candidates` chacun de leur côté
    # (avant ce correctif : jusqu'à 3 recalculs indépendants du même texte,
    # sans garantie qu'ils restent d'accord entre eux).
    entry = _detect_disambiguation_candidates(text_lower, user_role)
    updates["disambiguation_candidate"] = entry
    if entry:
        # (2026-09-08, clôture Bloc 1, mandat §21/§22) : `options` est la
        # SEULE source canonique des intents candidats — un ancien champ
        # `candidates` séparé (redondant, retiré du catalogue
        # `INTENT_DISAMBIGUATION`) n'est plus lu ici.
        for intent_key, _label in extract_disambiguation_intents(entry):
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

    # (2026-09-08, refonte responsabilités des nœuds d'entrée, mandat §7 —
    # "amélioration obligatoire") : cette interruption ne vérifiait AUCUNE
    # confiance — une intention concurrente classée à confiance quasi
    # nulle suspendait quand même aveuglément un tunnel fiable en cours.
    # Seuil aligné sur celui déjà utilisé pour la désambiguïsation
    # (`_DISAMBIGUATION_CONFIDENCE_THRESHOLD`, cohérence intentionnelle
    # avec le reste de l'interpréteur/policy) : sous ce seuil, l'intention
    # concurrente reste journalisée dans `intent_competition` (visible
    # pour un futur déclenchement de désambiguïsation) mais NE casse PAS
    # le tunnel actif.
    # (2026-09-09, Bloc 2 passe finale — Invariant A : UN SEUL propriétaire
    # de « le goal actif est-il interrompu ? ») : le breakout de navigation
    # critique (`NAVIGATION_BREAKOUT_GOALS`, source unique `core/goals.py`,
    # dérivée du flag `breakout` d'INTENT_CONFIG) était jusqu'ici décidé par
    # `TunnelManager`, en aval, SANS que ce nœud soit consulté — donc une
    # intention de navigation à confiance 0.2 pouvait casser un tunnel que
    # CE nœud venait explicitement de refuser d'interrompre. Deux autorités
    # sur la même question. La décision est désormais prise ICI, et ICI
    # seulement ; `TunnelManager` ne garde que les invariants STRUCTURELS
    # (OTP inviolable), c.-à-d. le droit d'INTERDIRE une transition, jamais
    # celui d'en choisir une (voir sa docstring de classe).
    #
    # Pourquoi le breakout ignore le seuil de confiance : ce n'est pas une
    # intention métier concurrente à arbitrer, c'est une demande de
    # NAVIGATION ("voir mon panier", "mes commandes"). La refuser piège
    # l'utilisateur dans un tunnel dont il demande explicitement à sortir —
    # incident réel 2026-07-17, voir [[market-coach-turn-boundary-state]].
    if (
        current_goal
        and event == "NEW_TASK"
        and detected_intent not in {"UNKNOWN", str(current_goal).upper()}
        and (
            detected_intent in NAVIGATION_BREAKOUT_GOALS
            or confidence >= _DISAMBIGUATION_CONFIDENCE_THRESHOLD
        )
    ):
        interrupt_reason = (
            "critical_navigation_breakout"
            if detected_intent in NAVIGATION_BREAKOUT_GOALS
            else "competing_intent_high_confidence"
        )
        updates.update(
            {
                "interpreted_event": "INTERRUPTION",
                "intent_competition": competition,
                "cognitive_decision": {
                    **decision,
                    # (2026-09-08, correction topologique) : renommé depuis
                    # `suspend_current_goal` — recherche exhaustive faite,
                    # AUCUN lecteur externe (response_strategy.py signale
                    # l'interruption via `interpreted_event=="INTERRUPTION"`
                    # ci-dessus, pas via ce nom). Renommage donc sans risque,
                    # vers le vocabulaire canonique du mandat §3.
                    "action": ConversationAction.INTERRUPT_ACTIVE_GOAL,
                    "reason": interrupt_reason,
                },
            }
        )
        return updates

    # (2026-09-09, Bloc 2 passe finale — Invariant A, fermeture complète) :
    # `input_interpreter` (Bloc 1, GELÉ) peut lui aussi émettre
    # `interpreted_event="INTERRUPTION"` — quand l'utilisateur dévie pendant
    # une SELECTION/CONFIRMATION avec une confiance >= 0.60 (voir
    # `interpreter/routing.py`). Cet événement ne passait par AUCUNE
    # décision de ce nœud : il filait jusqu'à `TunnelManager`, qui
    # appliquait alors son PROPRE seuil — un second décideur, invisible
    # ici. Ce nœud RATIFIE désormais explicitement ces interruptions.
    #
    # La ratification réutilise EXACTEMENT le critère déjà appliqué en
    # amont (`INTERRUPTION_CONFIDENCE_THRESHOLD`, le seuil de
    # l'interpréteur ET celui historique de `TunnelManager`) : le
    # comportement utilisateur est donc inchangé — ce n'est pas un nouveau
    # jugement, c'est la reprise formelle d'une décision jusqu'ici
    # implicite, pour qu'UNE seule autorité approuve toute interruption
    # réellement appliquée. Une interruption NON ratifiée ici (confiance
    # sous le seuil) reste soumise, en aval, au refus de `TunnelManager` —
    # les deux verdicts concordent par construction (même seuil), sans
    # contradiction possible.
    if current_goal and event == "INTERRUPTION":
        if (
            detected_intent in NAVIGATION_BREAKOUT_GOALS
            or confidence >= INTERRUPTION_CONFIDENCE_THRESHOLD
        ):
            updates.update(
                {
                    "intent_competition": competition,
                    "cognitive_decision": {
                        **decision,
                        "action": ConversationAction.INTERRUPT_ACTIVE_GOAL,
                        "reason": (
                            "critical_navigation_breakout"
                            if detected_intent in NAVIGATION_BREAKOUT_GOALS
                            else "ratified_upstream_interruption"
                        ),
                    },
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
            # (2026-09-08, refonte responsabilités des nœuds d'entrée,
            # mandat §7) : `cognitive_guard` DÉCIDE de l'abandon (seuil de
            # retry dépassé) mais n'a plus besoin de connaître le détail
            # des mini-machines à états (`bid_phase`/`update_phase`/
            # `winner_gps_stage`/`preorder_workflow.gps_stage`...) ni du
            # nettoyage de `transaction_payload`/`selected_tool`/
            # `execution_result` — voir `core/conversation_reset.py` pour
            # l'historique complet de chaque champ réinitialisé (comportement
            # inchangé, seule l'organisation du code a bougé).
            updates.update(
                reset_abandoned_conversation_context(
                    state,
                    intent_competition=competition,
                    cognitive_decision={
                        **decision,
                        "reason": "unknown_event_in_tunnel_max_retries",
                    },
                )
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
                    "action": ConversationAction.RECOVER_ACTIVE_GOAL,
                    "reason": "unknown_event_in_tunnel_retry",
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
    # toute la durée du tunnel : dès que ce tour a été compris (on atteint
    # cette section nominale), on repart de zéro. Sans ça, un utilisateur qui
    # bute deux fois, se fait comprendre, puis bute une seule fois de plus se
    # faisait éjecter de son opération — alors qu'il progressait. Vérifié par
    # simulation avant/après (scénario UNKNOWN, UNKNOWN, ANSWER, UNKNOWN :
    # abandonnait au 4e tour, ne le fait plus).
    if in_tunnel and retry_count:
        updates["retry_count"] = 0

    # (2026-09-08, correction topologique du bloc conversationnel) : AVANT
    # ce correctif, cette section posait inconditionnellement
    # `action: "continue"` — voir docstring de `cognitive_guard` pour
    # l'historique complet. `_classify_nominal_action` tranche maintenant
    # RÉELLEMENT entre CLARIFY / DISAMBIGUATE / CONTINUE_ACTIVE_GOAL /
    # START_OR_PLAN_GOAL, avec les mêmes conditions que les nœuds d'exécution
    # appliquaient auparavant chacun de leur côté.
    action, reason = _classify_nominal_action(
        event=event,
        current_goal=current_goal,
        already_expecting=already_expecting,
        text_lower=text_lower,
        detected_intent=detected_intent,
        confidence=confidence,
        entry=entry,
        state=state,
    )
    updates.update(
        {
            "intent_competition": competition,
            "cognitive_decision": {**decision, "action": action, "reason": reason},
        }
    )
    logger.debug(
        "[CognitiveGuard] event=%s intent=%s goal=%s confidence=%.2f action=%s reason=%s",
        event,
        detected_intent,
        current_goal,
        confidence,
        action,
        reason,
    )
    return updates


__all__ = [
    "cognitive_guard",
]
