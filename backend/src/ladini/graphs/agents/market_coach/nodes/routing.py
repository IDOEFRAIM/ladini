from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.state import MarketAgentState

logger = get_node_logger("RoutingNodes")

# Tout statut de sécurité bloquant doit court-circuiter vers la stratégie
# (sinon la réponse de blocage est écrasée par l'interpréteur en aval).
_SECURITY_BLOCKING = frozenset(
    {
        "SCAM_DETECTED",
        "ACCOUNT_BLOCKED",
        "PROHIBITED_PRODUCT",
        "PROFILE_UNAVAILABLE",
        # (2026-09-08, refonte sécurité) : la détection de prompt injection
        # vit désormais dans security_moderation (voir sa docstring) et doit
        # court-circuiter comme les autres blocages durs — avant ce
        # correctif, ce statut n'était PAS dans cet ensemble malgré un
        # commentaire de core/state.py qui prétendait déjà le contraire.
        "PROMPT_INJECTION_DETECTED",
    }
)


def _route_after_security(state: MarketAgentState) -> str:
    """Court-circuite vers la stratégie sur fraude, compte restreint, produit
    interdit ou profil indisponible ; sinon poursuit vers `session_bootstrap`.

    (2026-09-08, correction topologique — `session_bootstrap` déplacé après
    `security_moderation`) : une entrée `ALLOW` ne va plus DIRECTEMENT à
    `input_interpreter` — elle doit d'abord passer par `session_bootstrap`
    (chargement profil/onboarding), qui décide lui-même (voir
    `_route_after_session_bootstrap` ci-dessous) si l'utilisateur peut
    atteindre l'interpréteur. `security_moderation` ne recalcule rien de
    plus : ce nœud reste le routeur trivial d'origine (Invariant C), seule
    sa cible ALLOW change de nom.

    (2026-09-08, revue de validation, Invariant C) : `security_decision`
    (vocabulaire fermé `SecurityDecision`, écrit par CHAQUE exécution de
    `security_moderation`) est désormais le signal PRIMAIRE — un routeur
    trivial, un seul champ, aucune reclassification. `security_status in
    _SECURITY_BLOCKING` / `status=="BLOCKED"` restent en repli défensif
    (même sémantique, pas une logique différente) pour tolérer un appel
    direct de `security_moderation` en dehors du graphe compilé (tests,
    scripts) où le champ `security_decision` pourrait manquer. `RESTRICT`
    (déclaré dans `SecurityDecision` mais sans émetteur réel aujourd'hui —
    voir sa docstring) est traité comme bloquant par anticipation : une
    dégradation partielle future ne doit pas atteindre l'interpréteur par
    défaut tant qu'elle n'a pas explicitement sa propre route."""
    if state.get("security_decision") in {"BLOCK", "RESTRICT"}:
        return "to_strategy"
    if state.get("security_status") in _SECURITY_BLOCKING:
        return "to_strategy"
    if str(state.get("status") or "").upper() == "BLOCKED" and state.get(
        "final_response"
    ):
        return "to_strategy"
    return "to_bootstrap"


def _route_after_session_bootstrap(state: MarketAgentState) -> str:
    """Routeur du nouveau nœud décisionnel `session_bootstrap` (2026-09-08,
    correction topologique du bloc d'entrée) : ce nœud ne se contente plus
    de router inconditionnellement vers `input_normalizer` — il décide
    maintenant lui-même de la suite selon l'état de session qu'il vient de
    résoudre.

    Signal trivial, sans nouvelle source de vérité : les 3 issues possibles
    sont déjà entièrement déterminées par des champs EXISTANTS, écrits par
    `session_bootstrap` lui-même sur CHAQUE exécution —
    - `status == "BLOCKED"` (posé par `_profile_unavailable_patch` sur échec
      MCP/exception) → profil indisponible, direction la stratégie de
      réponse (message d'erreur déjà préparé par le nœud) ;
    - `is_onboarding` (posé inconditionnellement par
      `services/onboarding.py::resolve_onboarding_state`, True ou False,
      sur tout chemin qui ne s'est pas déjà terminé en `BLOCKED` ci-dessus)
      → onboarding requis ;
    - sinon → contexte prêt, direction l'interpréteur.

    Pas de `session_status` introduit : `status`/`is_onboarding` tranchent
    déjà sans ambiguïté (mandat de correction §5 — ne pas dupliquer une
    source de vérité déjà suffisante)."""
    if str(state.get("status") or "").upper() == "BLOCKED":
        return "to_strategy"
    if state.get("is_onboarding"):
        return "to_onboarding"
    return "to_interpreter"


# Mapping ACTION -> destination. Table plate, aucune reclassification —
# `_route_after_cognitive_guard` (ci-dessous) doit rester un simple
# `dict.get`, jamais une réplique de la logique de `cognitive_guard`.
#
# RECOVER_ACTIVE_GOAL route DIRECTEMENT vers `response_strategy`, PAS vers
# `clarification_node` (2026-09-08, clôture Bloc 1, mandat §29) : preuve
# (voir `nodes/clarification.py` et son test dédié) que ce nœud est un
# NO-OP STRUCTUREL pour cette action — `response_strategy.py` construit
# déjà toute la réponse RECOVERY depuis `cognitive_action` seul, sans
# dépendre d'un `final_response` posé par `clarification_node`. Visiter ce
# nœud pour ne rien faire n'a plus de raison d'être.
# ABANDON_ACTIVE_GOAL reste routé vers `clarification_node` : CE nœud, lui,
# contribue réellement (message LLM contextualisé sur l'abandon) — voir
# `_route_after_clarification` pour le repli quand le LLM échoue.
# ASK_SWITCH_CONFIRMATION route DIRECTEMENT vers `response_strategy` (comme
# RECOVER_ACTIVE_GOAL ci-dessus) : `cognitive_guard` pose déjà lui-même
# `response_strategy="SUCCESS"` + `final_response` (le texte de la question
# de bascule) + le `pending_interaction` CONFIRM_ACTION dédié — aucun nœud
# supplémentaire (`goal_planner`/`validator`/`confirmation_gate`) n'a besoin
# de tourner ce tour-ci : le draft ACTIF ne doit surtout pas être touché
# tant que l'utilisateur n'a pas répondu (voir `nodes/cognitive.py`).
# ASK_INTENT_SELECTION (Étape 9A/9B) route de la MÊME façon et pour la MÊME
# raison : `cognitive_guard` pose déjà `response_strategy`/`final_response`/
# `pending_interaction` (CLARIFY_INTENT, faits+candidats) lui-même — jamais
# `goal_planner` (aucun goal ne doit être verrouillé avant le choix de
# l'utilisateur) ni `memory_update`/`validator` (aucun `transaction_payload`
# à toucher tant qu'aucun goal n'est choisi).
# RESOLVE_INTENT_CLARIFICATION (Étape 9C) route aussi vers `response_strategy`,
# mais pour la raison OPPOSÉE : `cognitive_guard` a DÉJÀ rejoué `validator`
# lui-même (`_resolve_intent_clarification`) et posé le patch complet
# (`current_goal`/`transaction_payload`/`pending_interaction` pour le
# prochain slot, ou l'annulation propre) — le reste du pipeline générique
# (`goal_planner`/`memory_update`/`validator`) ne doit PAS rejouer une
# deuxième fois par-dessus un état déjà résolu.
_COGNITIVE_ACTION_ROUTES = {
    ConversationAction.START_OR_PLAN_GOAL: "to_planner",
    ConversationAction.CONTINUE_ACTIVE_GOAL: "to_planner",
    ConversationAction.INTERRUPT_ACTIVE_GOAL: "to_planner",
    ConversationAction.DISAMBIGUATE: "to_disambiguation",
    ConversationAction.CLARIFY: "to_clarification",
    ConversationAction.ASK_SWITCH_CONFIRMATION: "to_strategy",
    ConversationAction.ASK_INTENT_SELECTION: "to_strategy",
    ConversationAction.RESOLVE_INTENT_CLARIFICATION: "to_strategy",
    ConversationAction.RECOVER_ACTIVE_GOAL: "to_strategy",
    ConversationAction.ABANDON_ACTIVE_GOAL: "to_clarification",
}


def _route_after_cognitive_guard(state: MarketAgentState) -> str:
    """Routeur TRIVIAL après `cognitive_guard` (2026-09-08, correction
    topologique du bloc conversationnel, mandat §4/§17) : une seule
    lecture, un mapping direct — AUCUNE reclassification de l'intent, de la
    confiance, de l'ambiguïté, des retries ou de l'interruption (toute
    cette décision appartient à `cognitive_guard`, voir sa docstring pour
    le contrat complet `ConversationDecision` et la liste exhaustive des
    actions possibles).

    (2026-09-08, clôture Bloc 1, mandat §18) : une action absente de
    `_COGNITIVE_ACTION_ROUTES` est une VIOLATION DE CONTRAT — `cognitive_guard`
    n'a produit aucune des 7 actions canoniques. Ce n'est PLUS transformé en
    silence : loggé en ERROR (observable en prod/monitoring) avant
    d'appliquer un repli sûr vers `clarification_node` (jamais un crash du
    tour utilisateur pour une violation de contrat interne — mais jamais
    invisible non plus)."""
    action = str((state.get("cognitive_decision") or {}).get("action") or "")
    destination = _COGNITIVE_ACTION_ROUTES.get(action)
    if destination is None:
        logger.error(
            "[RoutingNodes] CONTRACT VIOLATION — cognitive_decision.action=%r "
            "n'appartient pas au vocabulaire ConversationAction (%s). "
            "cognitive_guard a un bug de contrat. Repli défensif vers "
            "clarification_node.",
            action,
            sorted(ConversationAction.ALL),
        )
        return "to_clarification"
    return destination


def _route_after_onboarding(state: MarketAgentState) -> str:
    """Après `onboarding_node` : reprise de l'action (profil enfin complet), relâche (l'utilisateur change de sujet) ou
    réponse normale (une question de plus, ou l'onboarding classique). Lecture d'UN champ, aucune reclassification."""
    outcome = str(state.get("profile_gate_outcome") or "").upper()
    if outcome == "RESUME":
        return "to_resolver"
    if outcome == "RELEASE":
        return "to_interpreter"
    return "to_strategy"


def _route_after_resolver(state: MarketAgentState) -> str:
    """Redirige si l'état nécessite une interaction ou s'il est prêt pour confirmation."""
    # NB : le moteur formulaire DRY (form_node) a été retiré — plus aucun
    # resolver n'active `active_form`, donc plus de branche `to_form` ici.
    status = str(state.get("status") or "").upper()
    if status in {"WAITING_INPUT", "ERROR", "COMPLETED"}:
        # (2026-09-08, P2-1 audit architectural) : "to_strategy" renommé
        # "to_ui" — la cible RÉELLE de ce label dans graph_builder.py est
        # `ui_engine` (conversion pending_menu -> ag_ui_component), pas
        # `response_strategy`. Le label mentait sur la destination.
        return "to_ui"
    # Nominal path: ensure farm is present or auto-created before confirmation
    return "to_farm_guard"


def _route_after_confirmation(state: MarketAgentState) -> str:
    """Achemine vers l'exécuteur MCP si l'opération a été validée par l'utilisateur."""
    status = str(state.get("status") or "").upper()
    if status == "EXECUTING":
        return "to_executor"
    return "to_strategy"

# (2026-09-08, P2-2 : `_route_after_executor` d'origine, à sortie UNIQUE,
# supprimé — voir P2-3 juste en dessous, qui réintroduit un branchement
# après `mcp_tool_executor`, mais pour une raison DIFFÉRENTE et réelle
# cette fois : sélectionner LEQUEL des deux finalizers transactionnels agit.


def _route_after_mcp_executor(state: MarketAgentState) -> str:
    """P2-3 (audit architectural 2026-09-08) : les finalizers
    (`flows/buyer/procurement_execution_finalizer.py`,
    `flows/producer/sales_execution_finalizer.py`) étaient chaînés
    inconditionnellement — `mcp_tool_executor → procurement_finalizer →
    sales_finalizer → response_strategy` — chacun se protégeant par son
    PROPRE garde no-op interne (`if state.get("<son_draft>") is None:
    return {}`). Fonctionnel, mais ne passe pas à l'échelle : un 3ᵉ domaine
    transactionnel ajouterait un 3ᵉ maillon no-op inconditionnel.

    Signal de sélection : présence du draft transactionnel propre à CHAQUE
    domaine (`procurement_draft`/`sales_publish_draft`) — PAS
    `selected_tool` (mandat explicite : ne pas le supposer canonique sans
    vérifier). C'est EXACTEMENT le garde interne que chaque finalizer
    applique déjà lui-même, promu ici au niveau du routeur — pas une
    nouvelle classification inventée. Ni l'un ni l'autre n'est touché par
    `mcp_tool_executor` (vérifié : il LIT `procurement_draft` pour la clé
    d'idempotence, n'écrit AUCUN des deux) — signal stable à ce point du
    graphe, contrairement à `current_goal` (potentiellement remis à None
    par le patch de succès générique de `mcp_tool_executor` pour d'autres
    goals)."""
    if state.get("procurement_draft") is not None:
        return "to_procurement_finalizer"
    if state.get("sales_publish_draft") is not None:
        return "to_sales_finalizer"
    return "to_response"
