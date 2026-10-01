from __future__ import annotations

import unicodedata
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ladini.core.formatting import fmt_num
from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.conversation_reset import (
    reset_abandoned_conversation_context,
)
from ladini.graphs.agents.market_coach.core.field_registry import STRUCTURED_FIELDS
from ladini.graphs.agents.market_coach.core.goals import (
    DRAFT_BASED_CONFIRMATION_GOALS,
    DRAFT_BASED_CONFIRMATION_STATE_KEY,
    NAVIGATION_BREAKOUT_GOALS,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    set_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    entities_satisfy_expected_input,
)
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal
from ladini.graphs.agents.market_coach.core.tunnel_manager import (
    INTERRUPTION_CONFIDENCE_THRESHOLD,
)
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.nodes.clarification import (
    has_out_of_tunnel_location_error,
)
from ladini.graphs.agents.market_coach.nodes.response_handlers import (
    _label_for_field,
)
from ladini.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    _DISAMBIGUATION_CONFIDENCE_THRESHOLD,
    _detect_disambiguation_candidates,
    extract_disambiguation_intents,
)
from ladini.graphs.agents.market_coach.utils import (
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


_CARRY_FORWARD_KEYS = ("product", "unit", "zone_name")


_TERMINATING_EVENTS = frozenset({"REJECT", "CANCEL"})


def _entity_carry_forward(
    state: Dict[str, Any],
    current_goal: Optional[str],
    in_tunnel: bool,
    event: str = "",
) -> Optional[Dict[str, Any]]:
    if not (in_tunnel and current_goal):
        return None
    if event in _TERMINATING_EVENTS:
        return None
    stable = state.get("stable_entities") or {}
    entities = dict(state.get("extracted_entities") or {})
    carried = False
    for key in _CARRY_FORWARD_KEYS:
        if not entities.get(key) and stable.get(key):
            entities[key] = stable[key]
            carried = True
    return entities if carried else None


# (2026-10-01, Étape 9A/9B — ambiguïté hors tunnel) : libellés COURTS,
# orientés action, pour le menu de clarification — délibérément DISTINCTS
# du `label` générique d'`INTENT_CONFIG` (qui décrit l'intention pour un
# contexte de log/catalogue LLM, pas pour un bouton WhatsApp), même principe
# déjà établi par `interpreter/intent.py::INTENT_DISAMBIGUATION` (ses
# propres `options` ont toujours leur libellé propre, jamais une reprise
# verbatim d'INTENT_CONFIG). Repli sur le label générique pour toute
# intention future absente d'ici — jamais une intention silencieusement
# sans libellé.
_INTENT_SELECTION_SHORT_LABELS: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": "🛒 Les mettre en vente",
    "STOCK_REGISTER_HARVEST": "📦 Les enregistrer dans mon stock",
}


def _short_label_for_goal(goal: str) -> str:
    override = _INTENT_SELECTION_SHORT_LABELS.get(goal)
    if override:
        return override
    return str((INTENT_CONFIG.get(goal) or {}).get("label", goal))


def _facts_summary_text(facts: Dict[str, Any]) -> str:
    """Résumé déterministe des faits déjà compris — AUCUN appel LLM, ces
    valeurs ont déjà été extraites/normalisées en amont. Volontairement
    minimal (produit/quantité/unité) : les autres champs éventuels
    (zone, prix...) restent dans `facts` (préservés pour l'Étape 9C) sans
    alourdir cette phrase d'accroche."""
    product = facts.get("product")
    quantity = facts.get("quantity")
    unit = facts.get("unit")
    qty_part = None
    if quantity is not None:
        unit_label = str(unit).strip().lower() if unit else ""
        qty_part = f"{fmt_num(quantity)} {unit_label}".strip()
    if qty_part and product:
        return f"Vous avez {qty_part} de {product}."
    if product:
        return f"Vous avez mentionné : {product}."
    if qty_part:
        return f"Vous avez mentionné : {qty_part}."
    return "J'ai compris des informations, mais pas encore ce que vous voulez en faire."


def _out_of_tunnel_ambiguity_response(
    *, facts: Dict[str, Any], candidate_goals: List[str]
) -> Dict[str, Any]:
    intro = _facts_summary_text(facts)
    ask_text = f"{intro}\nSouhaitez-vous :"
    buttons = [
        {"id": goal, "title": _short_label_for_goal(goal)} for goal in candidate_goals
    ]
    return {
        "final_response": ask_text,
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "QuickReplies"],
            "kwargs": {
                "body": ask_text,
                "buttons": buttons,
                "metadata": {"candidate_goals": candidate_goals},
            },
        },
    }


# (2026-10-01, Étape 9A/9B clôture — backstop déterministe) : le micro-prompt
# NEW_TASK sait désormais rapporter `AMBIGUOUS` lui-même, mais rien ne
# GARANTIT qu'il le fasse — un LLM peut rester confiant (même à 0.99) sur
# UNE intention pour une déclaration pourtant structurellement ambiguë
# ("j'ai 90 L de miel" sans aucun verbe d'action). Ce garde est un FILET DE
# SÉCURITÉ qui s'exécute APRÈS le jugement du LLM, jamais à sa place :
# uniquement quand (a) aucun tunnel n'est actif, (b) l'intention choisie
# appartient à un groupe connu d'intentions mutuellement compatibles avec
# la MÊME déclaration de faits nus, (c) les faits eux-mêmes sont présents
# (produit+quantité — sans ça, rien à préserver, rien à clarifier), et (d)
# AUCUN signal d'action explicite n'est détecté dans le texte — auquel cas
# il reclassifie localement en AMBIGUOUS, quelle que soit la confiance
# rapportée (mandat : "confidence=0.99 ne doit jamais suffire à contourner
# une ambiguïté métier structurelle"). Délibérément PAS une whitelist de
# phrases complètes (mandat Étape 9A/9B §11) : seulement quelques RADICAUX
# verbaux distinctifs, un filet de sécurité conservateur qui peut au pire
# demander une clarification de trop, jamais exécuter la mauvaise action.
_BACKSTOP_AMBIGUOUS_GOAL_GROUPS: Tuple[FrozenSet[str], ...] = (
    frozenset({"SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"}),
)

#: Radicaux (jamais des phrases entières) prouvant un verbe d'action
#: explicite — repli sur substring après normalisation NFKD, pas un
#: tokenizer : ces radicaux sont assez distinctifs pour ne pas produire de
#: faux positif plausible en français courant du domaine.
_SALES_ACTION_SIGNAL_STEMS: FrozenSet[str] = frozenset(
    {"vendre", "vends", "vendu", "vente"}
)
_STOCK_ACTION_SIGNAL_STEMS: FrozenSet[str] = frozenset(
    {"enregistr", "declar", "stock"}
)
_ACTION_SIGNAL_STEMS: FrozenSet[str] = _SALES_ACTION_SIGNAL_STEMS | _STOCK_ACTION_SIGNAL_STEMS


def _fold_text(text: str) -> str:
    clean = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(ch for ch in clean if not unicodedata.combining(ch))


def _has_explicit_action_signal(text: str) -> bool:
    folded = _fold_text(text)
    return any(stem in folded for stem in _ACTION_SIGNAL_STEMS)


def _backstop_ambiguous_group(
    *, current_goal: Optional[str], event: str, detected_intent: str, facts: Dict[str, Any], text: str
) -> Optional[FrozenSet[str]]:
    """Retourne le groupe d'intentions mutuellement compatibles si le
    backstop doit reclassifier CE tour en AMBIGUOUS, sinon `None` — jamais
    appliqué si un tunnel est actif (Étape 7 reste seule autorité dans ce
    cas) ni si l'événement n'est pas un NEW_TASK à intention unique."""
    if current_goal or event != "NEW_TASK":
        return None
    group = next((g for g in _BACKSTOP_AMBIGUOUS_GOAL_GROUPS if detected_intent in g), None)
    if group is None:
        return None
    if not facts.get("product") or facts.get("quantity") is None:
        return None
    if _has_explicit_action_signal(text):
        return None
    return group


def _active_confirmation_switch_candidate(
    state: Dict[str, Any],
    *,
    current_goal: Optional[str],
    event: str,
    detected_intent: str,
    pending: Any,
) -> Optional[Tuple[str, str]]:
    """(Retour non-`None` = candidat de "switch d'intention pendant une confirmation
    active", voir son appelant.) Un NEW_TASK dont l'intention détectée porte le MÊME
    nom que `current_goal` — donc EXCLU du bloc d'interruption générique juste en
    dessous, qui suppose que "même nom de goal" == "continuation du même sujet" — mais
    dont le PRODUIT nommé diffère de celui du draft canonique déjà actif, pendant que ce
    draft est EN ATTENTE de confirmation (`CONFIRM_ACTION`). Ne compare QUE le produit :
    générique par construction, aucun nom de produit n'est jamais codé en dur ici.

    Retourne `(produit_du_draft_actif, produit_entrant)` si c'est le cas, `None` sinon
    (rien de spécial à faire — le flux normal, potentiellement `CONTINUE_ACTIVE_GOAL`,
    s'applique)."""
    if not (
        current_goal
        and event == "NEW_TASK"
        and detected_intent == str(current_goal).upper()
        and current_goal in DRAFT_BASED_CONFIRMATION_GOALS
        and pending.kind == InteractionKind.CONFIRM_ACTION
    ):
        return None
    incoming_product = str(
        (state.get("extracted_entities") or {}).get("product") or ""
    ).strip()
    if not incoming_product:
        return None
    draft_key = DRAFT_BASED_CONFIRMATION_STATE_KEY.get(current_goal)
    active_draft = state.get(draft_key) if draft_key else None
    if not isinstance(active_draft, dict):
        return None
    active_product = str(active_draft.get("product") or "").strip()
    if not active_product or active_product.lower() == incoming_product.lower():
        return None
    return active_product, incoming_product


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
    # Bug réel production (2026-09-24) : `state.get("current_goal")` seul est aveugle au tunnel
    # léger de `procurement.py::buyer_request_resolver` ("waiting_choice" — un `CONFIRM_ACTION`
    # sans champ verrouillé), qui laisse `current_goal` redevenir `None` entre deux tours
    # (`nodes/cleanup.py::post_response_cleanup`) tout en gardant le goal actif dans
    # `working_memory.active_goal` — exactement le repli que `resolve_current_goal` existe pour
    # centraliser (`core/state.py`, refonte 2026-09-08 "purge de redondance"), mais que ce fichier
    # avait manqué : `nodes/cognitive.py` lisait encore l'ancienne chaîne directe alors que
    # `goal_planner`/`router`/`routing`/`strategy` utilisent déjà toutes `resolve_current_goal`.
    # Conséquence tracée pas à pas via le VRAI graphe compilé + un VRAI appel Groq : ce nœud, seul
    # propriétaire documenté de la décision d'interruption, ne voyait AUCUN tunnel actif ici
    # (`if current_goal` était `False`) et n'arbitrait donc jamais rien — `goal_planner`,
    # lui, recouvrait bien `BUYER_REQUEST` via `resolve_current_goal` et reverrouillait
    # aveuglément (RÈGLE 1quater) une classification CREATE_RECURRING_NEED pourtant correcte et à
    # confiance élevée (0.95), sans que ce nœud n'ait even été consulté. `buyer_request_resolver`
    # basculait alors silencieusement vers `cart_management` (product+quantity présents) —
    # "14 coqs chaque semaine" devenait une tentative d'achat immédiat d'un produit absent du
    # catalogue au lieu d'un besoin récurrent.
    current_goal = resolve_current_goal(state)
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

    # Bug réel production (2026-09-24, mandat "coq/chèvre/mouton chaque semaine") :
    # `recurrence_type` n'existe QUE dans le schéma d'entités de CREATE_RECURRING_NEED
    # (`interpreter/new_task_contract.py::NewTaskEntities` — aucun autre goal ne le
    # renseigne, jamais deviné par le prompt). Sa seule présence est donc une preuve
    # STRUCTURELLE — pas une confiance à arbitrer — qu'un besoin récurrent complet et
    # non ambigu vient d'être exprimé, exactement le même principe que
    # `NAVIGATION_BREAKOUT_GOALS` ci-dessous (une demande de navigation n'est pas non
    # plus une "intention métier concurrente à arbitrer"). Sans ce bypass, tracé pas à
    # pas via le VRAI graphe compilé + un VRAI appel Groq : l'interpréteur classifie
    # correctement CREATE_RECURRING_NEED, mais si la confiance retournée reste
    # sous `_DISAMBIGUATION_CONFIDENCE_THRESHOLD` (0.85, seuil global volontairement
    # non touché ici — mandat §13, "pas de modification globale dangereuse"),
    # l'interruption est refusée, `goal_planner` reverrouille l'ancien
    # `current_goal=BUYER_REQUEST`, et `buyer_request_resolver` bascule alors
    # silencieusement vers `cart_management` (product+quantity présents) — un besoin
    # récurrent complet se transformait en tentative d'achat immédiat d'un produit
    # absent du catalogue.
    has_complete_recurring_signal = bool(
        str((state.get("extracted_entities") or {}).get("recurrence_type") or "").strip()
    )

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

    carried_entities = _entity_carry_forward(state, current_goal, in_tunnel, event)
    if carried_entities:
        said = state.get("extracted_entities") or {}
        updates["extracted_entities"] = carried_entities
        decision["entity_carry_forward"] = True
        # Clés HÉRITÉES (jamais dites dans CE message) : un consommateur qui doit distinguer
        # « dit maintenant » de « hérité » (ex. une correction de draft, où l'unité de
        # l'ancien produit serait fausse pour le nouveau) les exclut.
        decision["carried_entities"] = [
            k for k in _CARRY_FORWARD_KEYS if not said.get(k) and carried_entities.get(k)
        ]

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
    # (2026-09-30, "interruption d'une confirmation active par une nouvelle intention",
    # incident réel) : un NEW_TASK du MÊME type de goal que `current_goal` (ex: encore
    # SALES_PUBLISH_PRODUCT, mais pour un AUTRE produit) est explicitement EXCLU du bloc
    # d'interruption ci-dessous (`detected_intent not in {"UNKNOWN", current_goal_upper}`)
    # — conçu pour ne pas casser un tunnel sur sa propre continuation ("encore un peu de
    # lait", "ajoute 10kg"). Mais pendant une CONFIRM_ACTION sur un draft canonique
    # versionné (`DRAFT_BASED_CONFIRMATION_GOALS`), CE même silence laissait la nouvelle
    # intention retomber sur `_classify_nominal_action` -> CONTINUE_ACTIVE_GOAL : le
    # message ("je veux vendre mon miel") atteignait alors `resolve_domain_action` comme
    # une simple entité `product` à FUSIONNER dans le draft actif (fallthrough générique,
    # `domain/sales_publish_draft.py`) — un producteur qui n'avait PAS encore confirmé sa
    # vente de lait se retrouvait avec un draft "miel" portant encore le prix/la quantité/
    # le conditionnement du lait, jamais nettoyé, jamais réellement annulé. Détecté ICI, au
    # même niveau d'autorité que l'interruption générique (mandat §A1/A2) : ni une
    # correction (le PRODUIT diffère, pas juste un champ du même produit), ni une
    # interruption automatique (mandat §A3, "pas de switch automatique") — une QUESTION
    # fermée oui/non, voir `_active_confirmation_switch_candidate` ci-dessous.
    switch_candidate = _active_confirmation_switch_candidate(
        state,
        current_goal=current_goal,
        event=event,
        detected_intent=detected_intent,
        pending=pending,
    )
    if switch_candidate is not None:
        old_product, incoming_product = switch_candidate
        logger.info(
            "CONFIRMATION_INTERRUPTION_DETECTED | current_goal=%s | incoming_goal=%s | "
            "current_status=%s | action=ASK_SWITCH_CONFIRMATION",
            current_goal,
            detected_intent,
            state.get("status"),
        )
        target = {
            "old_goal": current_goal,
            "old_draft": dict(state.get(DRAFT_BASED_CONFIRMATION_STATE_KEY[current_goal]) or {}),
            "old_pending_interaction": pending.to_dict(),
            "incoming_goal": detected_intent,
            "incoming_entities": dict(state.get("extracted_entities") or {}),
        }
        ask_text = (
            f"Vous avez encore une vente de {old_product} en attente de confirmation. "
            f"Voulez-vous l'annuler et commencer la vente de {incoming_product} ?"
        )
        updates.update(
            {
                "response_strategy": "SUCCESS",
                "status": "WAITING_INPUT",
                "final_response": ask_text,
                "ag_ui_component": {
                    "lc_type": "constructor",
                    "id": ["ag_ui", "QuickReplies"],
                    "kwargs": {
                        "body": ask_text,
                        "buttons": [
                            {"id": "CONFIRM", "title": "✅ Oui, annuler et continuer"},
                            {"id": "REJECT", "title": "❌ Non, garder l'ancienne vente"},
                        ],
                        "metadata": {"goal": current_goal},
                    },
                },
                "pending_goal": detected_intent,
                "intent_competition": competition,
                "cognitive_decision": {
                    **decision,
                    "action": ConversationAction.ASK_SWITCH_CONFIRMATION,
                    "reason": "confirmation_switch_candidate",
                },
                **set_pending_interaction(
                    InteractionKind.CONFIRM_ACTION,
                    context_ref="confirmation_switch",
                    target=target,
                ),
            }
        )
        return updates

    # (2026-10-01, Étape 9A/9B — ambiguïté hors tunnel, incident-type "j'ai
    # 90 L de miel" sans aucun goal actif) : le micro-prompt NEW_TASK
    # (`new_task_contract.py::NewTaskDisposition.AMBIGUOUS`) rapporte que les
    # FAITS du message sont clairs mais qu'AUCUN signal d'action explicite ne
    # départage ≥2 intentions du catalogue également plausibles — jamais une
    # devinette "max confidence" (mandat §9). Gardé STRICTEMENT à
    # `not current_goal` : ce event ne peut structurellement provenir que de
    # la route NEW_TASK sans tunnel (`state_router.py`), mais le garde explicite
    # protège quand même contre toute reclassification NEW_TASK survenant
    # pendant un tunnel encore nominalement actif (ex: après une DEVIATION
    # ACTIVE_SLOT) — l'Étape 7 reste seule autorité sur ce cas-là.
    #
    # Ne verrouille JAMAIS `current_goal`, ne crée AUCUN draft, ne touche PAS
    # `transaction_payload` : route DIRECTEMENT vers `response_strategy`
    # (`ASK_INTENT_SELECTION`, voir `nodes/routing.py`), exactement comme
    # `ASK_SWITCH_CONFIRMATION` ci-dessus — `goal_planner`/`memory_update`/
    # `validator` ne tournent pas ce tour-ci (invariant "AMBIGUOUS => no
    # business side effect", mandat §17). Les faits+candidats survivent au
    # tour suivant via `pending_interaction` (DURABLE, `CLARIFY_INTENT`,
    # dans `SUBFLOW_OWNED_KINDS` — jamais résolu par le classifieur
    # générique ENTER_FIELD) : l'Étape 9C lira ce `target` pour reprendre
    # sans redemander produit/quantité/unité.
    _out_of_tunnel_facts = {
        k: v
        for k, v in (state.get("extracted_entities") or {}).items()
        if v not in (None, "", [], {})
    }
    # (2026-10-01, Étape 9A/9B clôture — backstop déterministe) : s'exécute
    # même si le LLM a répondu NEW_TASK à très haute confiance — voir
    # `_backstop_ambiguous_group` pour les 4 conditions exactes. Reclassifie
    # localement (jamais `state` directement) pour réutiliser TEL QUEL le
    # bloc AMBIGUOUS ci-dessous, seule source de construction de cette
    # clarification (aucune logique dupliquée).
    _backstop_group = _backstop_ambiguous_group(
        current_goal=current_goal,
        event=event,
        detected_intent=detected_intent,
        facts=_out_of_tunnel_facts,
        text=text_lower,
    )
    _backstop_triggered = _backstop_group is not None
    _backstop_candidate_goals: Optional[List[str]] = None
    if _backstop_group is not None:
        _backstop_candidate_goals = sorted(_backstop_group)
        logger.info(
            "OUT_OF_TUNNEL_AMBIGUITY_BACKSTOP | llm_intent=%s | llm_confidence=%.2f | "
            "candidate_goals=%s | explicit_action_signal=false | "
            "override_to_ambiguous=true",
            detected_intent,
            confidence,
            _backstop_candidate_goals,
        )
        event = "AMBIGUOUS"

    if not current_goal and event == "AMBIGUOUS":
        candidate_goals = (
            _backstop_candidate_goals
            if _backstop_candidate_goals is not None
            else [
                g for g in (state.get("candidate_goals") or []) if isinstance(g, str) and g
            ]
        )
        facts = _out_of_tunnel_facts
        if len(candidate_goals) >= 2:
            if not _backstop_triggered:
                logger.info(
                    "OUT_OF_TUNNEL_INTENT_AMBIGUOUS | extracted_fact_keys=%s | "
                    "candidate_goals=%s | resolution=AMBIGUOUS | "
                    "explicit_action_signal=false",
                    sorted(facts.keys()),
                    candidate_goals,
                )
            response = _out_of_tunnel_ambiguity_response(
                facts=facts, candidate_goals=candidate_goals
            )
            updates.update(
                {
                    "response_strategy": "SUCCESS",
                    "status": "WAITING_INPUT",
                    "intent_competition": competition,
                    "cognitive_decision": {
                        **decision,
                        "action": ConversationAction.ASK_INTENT_SELECTION,
                        "reason": (
                            "out_of_tunnel_ambiguity_backstop"
                            if _backstop_triggered
                            else "out_of_tunnel_intent_ambiguous"
                        ),
                        "candidate_goals": candidate_goals,
                    },
                    **response,
                    **set_pending_interaction(
                        InteractionKind.CLARIFY_INTENT,
                        context_ref="out_of_tunnel_intent_ambiguity",
                        target={"facts": facts, "candidate_goals": candidate_goals},
                    ),
                }
            )
            return updates
        # (garde-fou — ne devrait pas arriver, le contrat Pydantic exige déjà
        # ≥2 candidate_goals pour AMBIGUOUS) : moins de 2 candidats valides
        # après filtrage n'est structurellement plus une ambiguïté réelle —
        # repli sûr vers UNKNOWN, jamais un goal choisi au hasard.
        logger.warning(
            "[CognitiveGuard] AMBIGUOUS avec moins de 2 candidate_goals "
            "valides (%s) — repli sur UNKNOWN sûr",
            candidate_goals,
        )
        updates.update(
            {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "intent_competition": competition,
                "cognitive_decision": {
                    **decision,
                    "action": ConversationAction.CLARIFY,
                    "reason": "ambiguous_with_insufficient_candidates",
                },
            }
        )
        return updates

    # (2026-09-30, Étape 7 — continuité conversationnelle ANSWER vs NEW_TASK,
    # incident-type "je veux vendre mon miel" -> "Quelle quantité ?" -> "j'ai
    # 90 L") : un tunnel actif qui attend explicitement un slot (QUANTITY/
    # PRICE/UNIT/...) doit donner PRIORITÉ à une réponse compatible avec ce
    # slot, même si l'interpréteur a étiqueté le tour `NEW_TASK` avec une
    # intention concurrente à confiance élevée ("j'ai 90 L" ressemble
    # lexicalement à une déclaration de stock indépendante — voir mandat
    # §10). Jugé UNIQUEMENT sur les ENTITÉS EXTRAITES (quantity/unit/price/
    # ...), jamais sur une liste de formulations de texte codées en dur
    # (mandat §11) — `entities_satisfy_expected_input` (core/slots.py) est
    # la même table canonique que `to_tunnel_category`/`expected_input_for_
    # field`, pas un second registre.
    #
    # Exclut délibérément les deux cas juste en dessous (breakout de
    # navigation, signal récurrent complet) : ce sont des intentions
    # EXPLICITES et structurellement plus fortes qu'une simple compatibilité
    # de slot (mandat §3, priorité 2 avant priorité 3) — une quantité
    # accidentellement présente dans "voir mon panier" (improbable) ne doit
    # jamais empêcher la navigation. Le relabellage ANSWER/`current_goal`
    # réutilise EXACTEMENT le contrat déjà posé par `interpreter/
    # active_slot_contract.py::adapt_active_slot_to_canonical` pour la route
    # ACTIVE_SLOT — jamais une 2e convention : `goal_planner::RÈGLE 1bis`
    # (event in {CONFIRM,SELECTION,ANSWER,UPDATE}) reverrouille alors le
    # tunnel par un simple relock, SANS jamais passer par RÈGLE 1quater (qui
    # purgerait `transaction_payload` — correct pour une VRAIE nouvelle
    # instance du même goal, incorrect ici : ce n'est pas une nouvelle
    # instance, c'est la suite de celle déjà ouverte).
    slot_answer_compatible = (
        current_goal
        and event == "NEW_TASK"
        and detected_intent not in {"UNKNOWN", str(current_goal).upper()}
        and detected_intent not in NAVIGATION_BREAKOUT_GOALS
        and not (detected_intent == "CREATE_RECURRING_NEED" and has_complete_recurring_signal)
        and expected_input in SLOT_FILLING_INPUTS
        and entities_satisfy_expected_input(
            expected_input, state.get("extracted_entities") or {}
        )
    )
    if slot_answer_compatible:
        logger.info(
            "ACTIVE_SLOT_ANSWER_RESOLVED | current_goal=%s | expected_slot=%s | "
            "source=COGNITIVE_GUARD_PRIORITY | new_task_candidate_suppressed=%s",
            current_goal,
            expected_input,
            detected_intent,
        )
        updates.update(
            {
                "interpreted_event": "ANSWER",
                "detected_intent": str(current_goal).upper(),
                "intent_competition": competition,
                "cognitive_decision": {
                    **decision,
                    "action": ConversationAction.CONTINUE_ACTIVE_GOAL,
                    "reason": "slot_answer_priority_over_new_task",
                    "suppressed_new_task_candidate": detected_intent,
                },
            }
        )
        return updates

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
            or (detected_intent == "CREATE_RECURRING_NEED" and has_complete_recurring_signal)
        )
    ):
        interrupt_reason = (
            "critical_navigation_breakout"
            if detected_intent in NAVIGATION_BREAKOUT_GOALS
            else (
                "complete_recurring_need_signal"
                if detected_intent == "CREATE_RECURRING_NEED" and has_complete_recurring_signal
                else "competing_intent_high_confidence"
            )
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
    # (Phase 2 hardening, commit 7, bug H2 — GÉNÉRALISATION de l'exclusion
    # `location_shared` ci-dessus, même classe de bug) : une PendingInteraction
    # `ENTER_FIELD` déclarée STRUCTURED dans `core/field_registry.py` (candidats
    # + quantité totale pour "ambiguous_quantity", draft + champs dits pour
    # "correction_scope"...) sait résoudre ELLE-MÊME une réponse que le
    # classifieur LLM générique n'a pas su étiqueter (`event=UNKNOWN`) — voir
    # `flows/buyer/recurring_need.py::_resolve_ambiguous_group_reply`, qui
    # reparse `normalized_text` directement, sans dépendre d'`extracted_
    # entities`, et renvoie explicitement `None` si CE message ne concerne
    # manifestement pas la clarification (l'appelant retombe alors sur le
    # traitement autonome normal). Avant ce correctif, `cognitive_guard`
    # interceptait TOUJOURS ces réponses en RECOVER, jetant le message sans
    # jamais laisser le propriétaire de l'interaction se prononcer. Le
    # registre (pas seulement `target is not None`) est la source de
    # l'exemption : un champ SCALAIRE simple (ex: "product"/"price") n'a lui
    # aucune résolution propre hors du classifieur générique et reste donc
    # couvert par la RECOVER ci-dessous, inchangée, même s'il portait
    # accidentellement un `target`.
    structured_field_owns_resolution = (
        pending.kind == InteractionKind.ENTER_FIELD
        and pending.target is not None
        and pending.field in STRUCTURED_FIELDS
    )
    if (
        event == "UNKNOWN"
        and in_tunnel
        and not location_shared
        and not structured_field_owns_resolution
    ):
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
