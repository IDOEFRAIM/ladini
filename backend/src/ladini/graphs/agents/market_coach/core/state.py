"""MarketAgentState — Single Source of Truth pour la machine à états MarketCoach.

Contrat strict d'état partagé entre tous les nodes du graphe LangGraph.
Implémente le paradigme d'Analyse Pilotée par l'Attente (Expectation-Driven)
pour le canal WhatsApp.

Règles de conception :
  - Tous les reducers sont **purs** (pas de filtrage caché). Un node qui
    écrit None ou une chaîne vide signale explicitement un reset.
  - Les listes sont **remplacées intégralement** (jamais accumulées en silence).
  - Les dictionnaires utilisent un **merge shallow** (la nouvelle valeur écrase
    l'ancienne pour les clés en collision).
  - Le node `Goal Planner` est SEUL responsable de l'écriture de `current_goal`,
    `goal_stack`, `suspended_goal`. Les autres nodes ne touchent jamais ces clés.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from typing_extensions import Annotated, TypedDict

# =====================================================================
# REDUCERS — canonical source: ladini.agents.reducers
# Re-exported here for backward compatibility.
# =====================================================================
from ladini.agents.reducers import (  # noqa: F401
    _KEEP,
    _KeepSentinel,
    mark_deleted,
    merge_dict,
    replace_list,
    replace_value,
)

# =====================================================================
# DOMAIN CONTEXTS — héritage TypedDict
# Les champs métier qui n'ont aucun sens hors d'un domaine sont déclarés
# dans `flows/buyer/state.py` (BuyerContext) et `flows/producer/state.py`
# (ProducerContext). MarketAgentState les hérite, donc l'adressage runtime
# (`state["active_cart"]`, etc.) reste strictement plat — rien à réécrire
# côté nodes ou résolveurs de contexte.
# =====================================================================
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    DISAMBIGUATION_MENU_GOAL_SHIM,
)
from ladini.graphs.agents.market_coach.flows.buyer.state import BuyerContext
from ladini.graphs.agents.market_coach.flows.producer.state import ProducerContext

# =====================================================================
# EVENT TYPES
# =====================================================================

UserEvent = Literal[
    "NEW_TASK",
    "ANSWER",
    "CONFIRM",
    "REJECT",
    "SELECTION",
    "UPDATE",
    "INTERRUPTION",
    "RESUME",
    "OUT_OF_SCOPE",
    "UNKNOWN",
    "ONBOARDING_INPUT",
]


# =====================================================================
# MAIN STATE
# =====================================================================


class MarketAgentState(BuyerContext, ProducerContext, TypedDict, total=False):
    # ================================================================
    # 1. RAW INPUT LAYER
    # ================================================================

    user_query: Annotated[str, replace_value]

    # Payload d'un message interactif WhatsApp (bouton quick-reply / ligne de
    # liste) reçu ce tour-ci : id/valeur du clic, ou "CONFIRM"/"REJECT" pour
    # une confirmation binaire. Amorcé par l'orchestrateur depuis le webhook.
    # Sa présence fait court-circuiter l'appel LLM de `input_interpreter`
    # (résolution directe en SELECTION/CONFIRM/REJECT — zéro token). Éphémère :
    # remis à None en fin de tour par post_response_cleanup.
    interactive_selection: Annotated[Optional[str], replace_value]

    # Position GPS reçue et DÉJÀ persistée ce tour (webhook Twilio, appel
    # SYNCHRONE avant l'enqueue Celery depuis 2026-09-02 — voir
    # core/location.py) — signal purement conversationnel pour l'étape finale
    # de l'onboarding. Amorcé par l'orchestrateur. Éphémère :
    # remis à False en fin de tour par post_response_cleanup.
    location_shared: Annotated[bool, replace_value]

    # Issue EXACTE de la persistance ci-dessus (core.location.LocationOutcome,
    # sérialisée en str) + les coordonnées SI acceptées — évite au graphe de
    # relire la DB (source de la course webhook/tâche corrigée le
    # 2026-09-02, voir flows/buyer/gps_delivery_gate.py::resolve_gps_stage).
    # Éphémères, mono-tour, comme location_shared.
    location_outcome: Annotated[Optional[str], replace_value]

    location_lat: Annotated[Optional[float], replace_value]

    location_lon: Annotated[Optional[float], replace_value]

    normalized_text: Annotated[str, replace_value]

    # (2026-09-08, refonte input_normalizer) : signal EXPLICITE — plus de
    # troncature silencieuse (mandat §4). True quand `_MAX_INPUT_LEN` a été
    # dépassé sur le texte brut ou normalisé.
    input_truncated: Annotated[bool, replace_value]

    detected_language: Annotated[str, replace_value]

    translated_text: Annotated[str, replace_value]

    audio_file_path: Annotated[Optional[str], replace_value]

    transcribed_audio: Annotated[Optional[str], replace_value]

    timestamp: Annotated[float, replace_value]

    # ================================================================
    # 2. USER / SESSION CONTEXT
    # ================================================================

    user_phone: Annotated[str, replace_value]

    session_id: Annotated[str, replace_value]

    # (2026-09-12) Identifiant STABLE de l'événement WhatsApp entrant (même
    # valeur que `msg:{MessageSid}`, la clé de dédoublonnage webhook — voir
    # `api/routes/twilio_webhook.py`). Injecté dans l'état initial par
    # `orchestrator.py::_run_market`, jamais réécrit en cours de tour — sert
    # de clé au cache d'interprétation LLM
    # (`interpreter/routing.py::_cached_llm_completion`) pour qu'un retry
    # Celery de `process_agent_task` (`autoretry_for=(Exception,)`) ne
    # repaie jamais un appel LLM déjà réussi pour CE message.
    message_sid: Annotated[Optional[str], replace_value]

    # (2026-09-08, purge redondance) : `role` (doublon exact de `user_role`,
    # écrit une seule fois par `role_guard.py` comme défaut initial puis
    # JAMAIS mis à jour ensuite — contrairement à `user_role`, rafraîchi à
    # chaque chargement de profil par `input_normalizer`/`profile_loader`)
    # retiré du contrat. Risque réel qu'il posait : `domain/model.py::
    # DomainContext.from_state` lisait `state.get("role") or state.get(
    # "user_role")` — un `or` qui privilégiait TOUJOURS la valeur figée de
    # `role_guard` (souvent "PRODUCER" par défaut) sur la valeur réellement à
    # jour de `user_role`, silencieusement, sans qu'aucun test ne puisse le
    # détecter (les deux sont identiques au 1er tour, seul un rôle corrigé
    # PLUS TARD dans la conversation révélait l'écart). `user_role` est
    # désormais l'UNIQUE champ — un site qui a besoin du rôle courant le lit
    # directement, plus de second nom à tenir synchronisé.
    user_role: Annotated[str, replace_value]

    user_name: Annotated[Optional[str], replace_value]

    zone_name: Annotated[Optional[str], replace_value]

    zone_id: Annotated[Optional[str], replace_value]

    user_context_loaded: Annotated[bool, replace_value]

    user_id: Annotated[Optional[str], replace_value]

    is_onboarding: Annotated[bool, replace_value]

    onboarding_step: Annotated[Optional[str], replace_value]

    onboarding_internal_step: Annotated[Optional[str], replace_value]

    onboarding_mode: Annotated[Optional[str], replace_value]

    onboarding_profile: Annotated[Dict[str, Any], replace_value]

    turn_count: Annotated[int, replace_value]

    proactive_hint: Annotated[Optional[str], replace_value]

    conversation_progress: Annotated[Optional[Dict[str, Any]], replace_value]

    # ================================================================
    # 3. SECURITY / TRUST
    # ================================================================

    security_status: Annotated[
        Literal[
            "SAFE",
            "SUSPICIOUS",
            "WARNING",
            "SCAM_DETECTED",
            "BLOCKED",
            "ACCOUNT_BLOCKED",
            "PROHIBITED_PRODUCT",
            "PROFILE_UNAVAILABLE",
            "PROMPT_INJECTION_DETECTED",
        ],
        replace_value,
    ]

    security_reason: Annotated[Optional[str], replace_value]

    # (2026-09-08, revue de validation du bloc refondu, Invariant C) :
    # décision fermée écrite par CHAQUE exécution de `security_moderation`
    # (voir `nodes/security_moderation.py::SecurityDecision`) — LE champ
    # que `nodes/routing.py::_route_after_security` doit lire en priorité,
    # un routeur trivial sans reclassification. EPHEMERAL, recalculé
    # chaque tour (security_moderation s'exécute inconditionnellement).
    security_decision: Annotated[
        Optional[Literal["ALLOW", "BLOCK", "RESTRICT"]], replace_value
    ]

    # (2026-09-08, clôture Bloc 1, mandat §6 — "formaliser la dette security
    # fail-open") : `security_moderation` reste délibérément FAIL-OPEN sur
    # panne externe (account gate indisponible, timeout de modération) —
    # jamais bloquer un utilisateur sain pour une panne technique. Avant ce
    # correctif, cette dégradation ne vivait que dans un log (`logger.
    # warning`), invisible au reste du graphe/à l'observabilité. `True`
    # signifie : ce tour a été autorisé (ALLOW) malgré une vérification de
    # sécurité incomplète. NON branché vers `mcp_tool_executor` — un ALLOW
    # conversationnel dégradé ne vaut PAS autorisation d'exécuter une
    # mutation sensible ; cette distinction reste une dette explicite,
    # reprise lors de l'audit confirmation/executor (Bloc transactionnel).
    # EPHEMERAL comme `security_decision` : recalculé chaque tour.
    security_degraded: Annotated[Optional[bool], replace_value]
    security_degraded_reason: Annotated[
        Optional[Literal["ACCOUNT_GATE_UNAVAILABLE", "MODERATION_TIMEOUT", "MODERATION_UNAVAILABLE"]],
        replace_value,
    ]

    trust_score: Annotated[Optional[float], replace_value]

    requires_human: Annotated[bool, replace_value]

    # (2026-09-08, P1-4 audit architectural) : trace forensique du message
    # bloqué pour injection de prompt (`input_normalizer.py`) — le blocage
    # LUI-MÊME fonctionne déjà pleinement via `security_status=
    # "PROMPT_INJECTION_DETECTED"` (déclaré ci-dessus, consommé par
    # `nodes/routing.py::_SECURITY_BLOCKING`) ; ce champ n'était que le
    # TEXTE BRUT de la tentative, supprimé silencieusement par le graphe
    # compilé — perdu pour toute revue de sécurité a posteriori sur le
    # checkpoint. Déclaré, jamais un nouveau comportement inventé (aucun
    # nouveau lecteur ajouté ici).
    blocked_user_query: Annotated[Optional[str], replace_value]

    # (2026-09-08, P1-4 audit architectural) : `_safe_node` (utils.py) les
    # pose sur toute exception de nœud non rattrapée — supprimés
    # silencieusement par le graphe compilé, motif de crash visible
    # SEULEMENT dans les logs serveur, jamais dans le checkpoint persisté.
    # `technical_details` = `str(exc)` BRUT : jamais destiné à un affichage
    # utilisateur direct (fuite d'information potentielle) — déclarés pour
    # survivre au checkpoint à des fins de diagnostic/observabilité
    # (Langfuse, revue manuelle), pas pour un nouveau rendu.
    error_message: Annotated[Optional[str], replace_value]
    technical_details: Annotated[Optional[str], replace_value]

    # (Phase 2 hardening, commit 11) : nom de classe SEUL de l'exception avalée par
    # `_safe_node` — voir sa docstring de champ `technical_details` ci-dessus pour le
    # même raisonnement de survie au checkpoint. Lu par `core/turn_trace.py::
    # capture_pre_cleanup` pour peupler `TurnTrace.error_class` sur les erreurs de
    # nœud, qui n'atteignent jamais les blocs `except` d'`Orchestrator.handle()`.
    error_class: Annotated[Optional[str], replace_value]

    # ================================================================
    # 4. INTERPRETER OUTPUT
    # ================================================================

    interpreted_event: Annotated[UserEvent, replace_value]

    # Peuplé UNIQUEMENT quand interpreted_event=="UNKNOWN" — distingue POURQUOI
    # (ambiguïté, action invalide, échec LLM, aucune action produite, hors
    # périmètre, erreur technique) au lieu de tout aplatir sous un seul
    # UNKNOWN opaque. Voir interpreter/interpreter_result.py::UnknownReason.
    # Consommé par le router (§26 "interdiction du bypass") et par
    # Langfuse/Prometheus pour le diagnostic (§24/§38 du mandat refonte).
    unknown_reason: Annotated[Optional[str], replace_value]

    detected_intent: Annotated[str, replace_value]

    interpreter_confidence: Annotated[float, replace_value]

    validation_status: Annotated[Optional[str], replace_value]

    # (2026-09-08, P1-3 audit architectural) : `interpreter/strategy.py`
    # (garde-fou de PLUS HAUTE PRIORITÉ de la fonction) lit ces deux champs
    # en RACINE d'état — `services/domain/slot_enrichment.py` les écrivait
    # dans `transaction_payload`, une adresse différente, jamais déclarée
    # nulle part. `nodes/memory.py` (seul appelant) relaie désormais
    # explicitement vers cette adresse racine. EPHEMERAL, mono-tour — la
    # décision « forcer une clarification » ne doit jamais survivre au tour
    # qui l'a produite.
    slot_enrichment_force_clarification: Annotated[Optional[bool], replace_value]
    clarification_reasons: Annotated[Optional[List[str]], replace_value]

    extracted_entities: Annotated[Dict[str, Any], merge_dict]

    raw_analysis: Annotated[Dict[str, Any], merge_dict]

    # (2026-09-08, refonte responsabilités des nœuds d'entrée, mandat §9) :
    # candidat de désambiguïsation lexicale calculé UNE fois par
    # `cognitive_guard` (source unique) — `clarification_node` et
    # `semantic_disambiguation` le consultent au lieu de recalculer chacun
    # `_detect_disambiguation_candidates` indépendamment. EPHEMERAL,
    # mono-tour : ne doit jamais survivre au tour qui l'a produit.
    disambiguation_candidate: Annotated[Optional[Dict[str, Any]], replace_value]

    intent_competition: Annotated[List[Dict[str, Any]], replace_list]

    cognitive_decision: Annotated[Dict[str, Any], merge_dict]

    # ================================================================
    # 5. GOAL MANAGEMENT
    # ================================================================

    current_goal: Annotated[Optional[str], replace_value]

    # Hint pour render_clarification uniquement — PAS un ownership de
    # current_goal/goal_stack/suspended_goal (réservé au Goal Planner, voir
    # docstring module). Écrit par state_cleaner_node quand un goal se
    # termine en ERROR/FAILED (avant que current_goal soit lui-même remis à
    # None par post_response_cleanup), consommé et effacé en un coup par
    # render_clarification au tour suivant si l'utilisateur répond par
    # quelque chose d'incompréhensible (ex: "annuler" après une erreur) —
    # évite que le fallback générique ignore ce que l'utilisateur venait de
    # faire. Voir [[market-coach-turn-boundary-state]].
    last_terminated_goal: Annotated[Optional[str], replace_value]

    pending_goal: Annotated[Optional[str], replace_value]

    goal_stack: Annotated[List[str], replace_list]

    goal_status: Annotated[
        Literal[
            "ACTIVE",
            "IDLE",
            "WAITING_INPUT",
            "WAITING_CONFIRMATION",
            "EXECUTING",
            "COMPLETED",
            "FAILED",
            "INTERRUPTED",
        ],
        replace_value,
    ]

    current_plan_id: Annotated[Optional[str], replace_value]

    goal_metadata: Annotated[Dict[str, Any], merge_dict]

    # ================================================================
    # 6. EXPECTATION ENGINE (Focus Formulaire IHM)
    # ================================================================

    # Source canonique UNIQUE de "qu'attend-on de l'utilisateur ce tour ?"
    # (refonte architecturale 2026-09-02, voir core/pending_interaction.py
    # pour le contrat complet). Les anciens champs concurrents
    # (`expected_input: Literal["PRODUCT","PRICE",...]`, `waiting_for_
    # confirmation: bool`) ont été retirés du contrat — plus aucun node du
    # chemin de production ne les lit ni ne les écrit. Ils peuvent encore
    # apparaître, en LECTURE SEULE, dans le JSON déjà persisté d'anciens
    # checkpoints (Postgres `agri_workspaces`, colonne `metadata` — voir
    # `scripts/migrate_pending_interaction.py` pour le migrateur one-shot) ;
    # `get_pending_interaction()` porte un pont de transition isolé qui les y
    # relit encore, documenté et borné dans le temps. Sérialisé en dict (pas
    # un objet Python direct) car `replace_value` transite par le checkpoint
    # JSONB.
    pending_interaction: Annotated[Optional[Dict[str, Any]], replace_value]

    # (2026-09-03, refonte transactionnelle PROCUREMENT_CREATE_REQUEST) :
    # `domain/procurement_draft.py::ProcurementDraft` sérialisé — état
    # transactionnel CANONIQUE de l'appel d'offres en construction/
    # confirmation. `replace_value`, PAS `merge_dict` : chaque mutation
    # (`with_updates`) produit une version ENTIÈRE nouvelle qui remplace
    # intégralement la précédente — jamais de fusion partielle qui pourrait
    # laisser un champ d'affichage désynchronisé du contrat d'exécution
    # (root cause de l'incident 2026-09-03 : `quantity_display` figé dans
    # `transaction_payload`, un canal `merge_dict`). `transaction_payload`
    # reste utilisé pour la COLLECTE initiale des champs (avant que le
    # draft n'existe) et pour l'exécution MCP (`draft.execution_payload()`
    # y est copié explicitement au moment de CONFIRM) — jamais comme source
    # de vérité concurrente une fois ce champ posé.
    procurement_draft: Annotated[Optional[Dict[str, Any]], replace_value]

    # (2026-09-03, migration transactionnelle PREORDER) :
    # `domain/preorder_draft.py::PreorderDraft` sérialisé — même contrat que
    # `procurement_draft` ci-dessus (`replace_value`, jamais `merge_dict`).
    # PostgreSQL (`preorder_draft_store.py`) reste la source CANONIQUE ; ce
    # champ n'est qu'une PROJECTION de travail, rafraîchie à chaque tour par
    # `flows/buyer/preorder_confirmation.py`. `preorder_workflow` (ci-dessous
    # dans `BuyerContext`) n'est plus qu'une PROJECTION dérivée de ce champ
    # une fois qu'un draft existe (voir `_phase_projection` dans ce module) —
    # jamais une seconde source de vérité indépendante.
    preorder_draft: Annotated[Optional[Dict[str, Any]], replace_value]

    # (2026-09-08, P0-1 audit architectural) : `domain/sales_publish_draft.py
    # ::SalesPublishDraft` sérialisé — MÊME contrat que `procurement_draft`/
    # `preorder_draft` ci-dessus (`replace_value`, jamais `merge_dict`).
    # `nodes/confirmation_gate.py` l'écrit (bootstrap v1 + relecture),
    # `flows/producer/sales_confirmation.py` et
    # `flows/producer/sales_execution_finalizer.py` le lisent — ce champ
    # existait déjà à ces 3 sites, mais N'ÉTAIT DÉCLARÉ NULLE PART : LangGraph
    # supprime silencieusement toute clé absente du schéma d'état à la
    # traversée du graphe COMPILÉ (jamais lors d'un appel direct de nœud avec
    # `dict.update`, ce qui a caché le bug à tous les tests existants).
    # Conséquence en production : `SALES_PUBLISH_PRODUCT` recréait un
    # brouillon v1 (nouvelle ligne DB) à CHAQUE tour au lieu de progresser
    # vers la confirmation — la question « Confirmez-vous ? » ne pouvait
    # jamais être résolue. Voir
    # docs/MARKET_COACH_GRAPH_ARCHITECTURE_REVIEW_2026-09-08.md §P0-1.
    sales_publish_draft: Annotated[Optional[Dict[str, Any]], replace_value]

    # (Phase 2, 2026-09) : `domain/recurring_need_draft.py::RecurringNeedDraft`
    # sérialisé — MÊME contrat que `procurement_draft`/`preorder_draft`/
    # `sales_publish_draft` ci-dessus (`replace_value`, jamais `merge_dict`).
    # DÉCLARÉ ICI explicitement (voir l'incident `sales_publish_draft`
    # documenté juste au-dessus : un canal écrit sans être déclaré dans ce
    # schéma d'état est silencieusement supprimé par LangGraph sur un graphe
    # COMPILÉ, jamais lors d'un appel direct de nœud — donc invisible aux
    # tests qui appellent le flow directement).
    recurring_need_draft: Annotated[Optional[Dict[str, Any]], replace_value]

    last_agent_question: Annotated[Optional[str], replace_value]

    expected_candidates: Annotated[List[str], replace_list]

    last_missing_field: Annotated[Optional[str], replace_value]

    # ================================================================
    # 7. WORKING MEMORY
    # ================================================================

    working_memory: Annotated[Dict[str, Any], merge_dict]

    transaction_payload: Annotated[Dict[str, Any], merge_dict]

    draft_payload: Annotated[Dict[str, Any], merge_dict]

    stable_entities: Annotated[Dict[str, Any], merge_dict]

    volatile_entities: Annotated[Dict[str, Any], merge_dict]

    available_mapping: Annotated[Dict[str, str], replace_value]

    # (2026-09-09, audit ui_engine — clôture UI_ENGINE) : écrit par
    # `nodes/ui_engine.py` (seul écrivain) depuis l'introduction du menu
    # snapshot, mais JAMAIS déclaré ici — même classe de bug que P0-1
    # (`sales_publish_draft`, voir plus haut) : LangGraph supprimait
    # silencieusement ce champ à la traversée du graphe COMPILÉ. Sans
    # conséquence fonctionnelle observée jusqu'ici UNIQUEMENT parce que le
    # lecteur réel (`nodes/memory.py`) lit la copie
    # `working_memory["menu_snapshot_id"]`, jamais celle-ci — voir le
    # COMPATIBILITY_SHIM documenté dans `nodes/ui_engine.py`. Déclaré
    # maintenant pour que ce top-level devienne la source CANONIQUE réelle
    # (au lieu d'une écriture qui ne survivait jamais). DURABLE comme
    # `available_mapping`/`expected_candidates` : doit survivre jusqu'au
    # tour où l'utilisateur répond au menu.
    menu_snapshot_id: Annotated[Optional[str], replace_value]

    pending_cleanup: Annotated[Optional[Dict[str, Any]], replace_value]

    # ================================================================
    # 8. SLOT TRACKING (Deltas du validateur)
    # ================================================================

    required_fields: Annotated[List[str], replace_list]

    missing_fields: Annotated[List[str], replace_list]

    completed_fields: Annotated[List[str], replace_list]

    validation_errors: Annotated[List[str], replace_list]

    warnings: Annotated[List[str], replace_list]

    # ================================================================
    # 9. INTERRUPTIONS / MULTI-TASK
    # ================================================================

    interruption_detected: Annotated[bool, replace_value]

    # (2026-09-14, incident WhatsApp #9) : posé par `interpreter/routing.py`
    # quand la route SELECTION a jugé un message sans rapport avec le menu
    # affiché (interruption), mais que la reclassification qui a suivi n'a
    # PAS pu identifier d'intention métier (`UNKNOWN`/`OUT_OF_SCOPE`) — lu
    # par `nodes/rendering/menus.py::render_selection_menu` pour ne jamais
    # générer de note d'accompagnement adaptative laissant croire que le
    # menu réaffiché (potentiellement un tout autre tunnel périmé) répond au
    # message. Distinct de `interruption_detected` (qui signifie l'inverse :
    # une interruption RÉUSSIE, un changement de but effectif).
    interruption_unresolved: Annotated[bool, replace_value]

    interruption_type: Annotated[Optional[str], replace_value]

    interruption_payload: Annotated[Dict[str, Any], merge_dict]

    suspended_goal: Annotated[Optional[str], replace_value]

    suspended_payload: Annotated[Dict[str, Any], merge_dict]

    # ================================================================
    # 10. CONFIRMATION / EXECUTION
    # ================================================================

    # `waiting_for_confirmation` (ancien bool) retiré du contrat — voir
    # `pending_interaction.kind == CONFIRM_ACTION` (core/pending_interaction.py).

    is_certified: Annotated[bool, replace_value]

    confirmation_summary: Annotated[Optional[str], replace_value]

    # Goal ET payload pour lesquels `confirmation_summary` a été calculé —
    # voir `nodes/rendering/confirm.py::render_confirmation`. Incident réel
    # (2026-08-27) : un acheteur commandait des chèvres, mais la
    # confirmation affichée parlait de "35 KG de champignons" — un résumé
    # PÉRIMÉ d'une tentative abandonnée bien plus tôt dans la conversation,
    # jamais réinvalidé (aucun nœud du chemin UNKNOWN→CONFIRMATION ne
    # repasse par `confirmation_gate`, la SEULE source qui régénère
    # `confirmation_summary`). Le GOAL seul ne suffit pas à détecter la
    # péremption : l'ancienne ET la nouvelle tentative étaient toutes deux
    # `BUYER_PREORDER_INIT` (même type d'opération, produit différent) — le
    # payload complet est donc comparé au rendu : tout écart (produit,
    # quantité, prix...) invalide le résumé et force une reconstruction à
    # la volée depuis le payload courant.
    confirmation_summary_goal: Annotated[Optional[str], replace_value]

    confirmation_summary_payload: Annotated[Optional[Dict[str, Any]], replace_value]

    # Réponse COURTE générée par le LLM quand l'utilisateur dit autre chose
    # qu'un oui/non pendant une confirmation en attente (question,
    # correction, remarque) — affichée AVANT le récap par
    # nodes/rendering/confirm.py au lieu de le répéter mot pour mot. Voir
    # confirmation_gate.py::_llm_deviation_reply et
    # [[onboarding-adaptive-questions-2026-08]] (même principe).
    confirmation_deviation_note: Annotated[Optional[str], replace_value]

    # Horodatage (epoch seconds) auquel confirmation_gate a levé cette
    # confirmation en attente. Sert de garde-fou de péremption : si trop de
    # temps s'écoule sans réponse claire (oui/non), la confirmation est
    # abandonnée silencieusement plutôt que ré-affichée indéfiniment sur un
    # message sans rapport (ex: partage GPS reçu bien après coup). Voir
    # confirmation_gate.py::_CONFIRMATION_TTL_SECONDS.
    confirmation_raised_at: Annotated[Optional[float], replace_value]

    execution_authorized: Annotated[bool, replace_value]

    execution_result: Annotated[Dict[str, Any], merge_dict]

    # ================================================================
    # 11. MCP / TOOL EXECUTION
    # ================================================================

    selected_tool: Annotated[Optional[str], replace_value]

    selected_tool_args: Annotated[Dict[str, Any], merge_dict]

    tool_execution_history: Annotated[List[Dict[str, Any]], replace_list]

    retry_count: Annotated[int, replace_value]

    # ================================================================
    # 12. RESPONSE GENERATION
    # ================================================================

    final_response: Annotated[Optional[str], replace_value]

    ag_ui_component: Annotated[Optional[Dict[str, Any]], replace_value]

    reply_audio_url: Annotated[Optional[str], replace_value]

    response_strategy: Annotated[
        Optional[
            Literal[
                "ASK_MISSING_FIELD",
                "CONFIRMATION",
                "SELECTION_MENU",
                "SUCCESS",
                "ERROR",
                "RECOVERY",
                "CLARIFICATION",
                "INTERRUPTION_HANDLER",
                "ONBOARDING",
            ]
        ],
        replace_value,
    ]

    onboarding_prompt: Annotated[Optional[str], replace_value]

    # ================================================================
    # 13. SYSTEM FLAGS
    # ================================================================

    status: Annotated[
        Literal[
            "START",
            "INTERPRETING",
            "PLANNING",
            "VALIDATING",
            "WAITING_INPUT",
            "WAITING_CONFIRMATION",
            "EXECUTING",
            "COMPLETED",
            "ERROR",
            "BLOCKED",
        ],
        replace_value,
    ]

    is_locked: Annotated[bool, replace_value]

    # (2026-09-08, P1-1 audit architectural) `should_replan` supprimé —
    # dérivé de `cognitive_decision.next_step`, sans AUCUN lecteur nulle
    # part (contrairement à `cognitive_decision` lui-même, dont la clé
    # `action` pilote `interpreter/strategy.py`/`nodes/clarification.py`).
    # `cognitive_orchestrator` (qui produisait `next_step`) a depuis été
    # supprimé À SON TOUR (nœud ET fonction, revue de validation du
    # 2026-09-08 le même jour) — voir `core/graph_builder.py` en tête de
    # fichier pour l'historique complet.

    should_interrupt: Annotated[bool, replace_value]

    # ================================================================
    # 14. SLOT-FILLING (DRY form engine)
    # ================================================================

    active_form: Annotated[Optional[str], replace_value]

    form_data: Annotated[Dict[str, Any], merge_dict]

    form_step: Annotated[Optional[str], replace_value]

    # ================================================================
    # 15. UI ENGINE (MenuRequest pipeline)
    # ================================================================

    # Menu en attente de transformation par ui_engine.
    # Posé par le context_resolver (via DomainRouter), consommé par ui_engine.
    pending_menu: Annotated[Optional[Any], replace_value]

    # NB : les champs spécifiques aux domaines (buyer/producer) sont hérités
    # depuis BuyerContext / ProducerContext déclarés en haut du module.
    # Voir `flows/buyer/state.py` et `flows/producer/state.py`.


def entities_said_this_turn(state: Dict[str, Any]) -> Dict[str, Any]:
    """Entités DITES dans le message courant : `extracted_entities` du tour, moins les clés
    héritées par `cognitive_guard` (`cognitive_decision.carried_entities`, jamais dites ici).

    Source unique pour toute décision qui dépend de « qu'a dit l'utilisateur MAINTENANT ? »
    (ex. un refus porteur de valeurs = correction, un « non » nu = refus). Jamais
    `transaction_payload`, qui accumule les tours précédents."""
    if not isinstance(state, dict):
        return {}
    entities = dict(state.get("extracted_entities") or {})
    for key in (state.get("cognitive_decision") or {}).get("carried_entities") or ():
        entities.pop(key, None)
    return entities


def resolve_current_goal(state: Dict[str, Any]) -> Optional[str]:
    """Résout le goal métier RÉELLEMENT en cours pour CE tour (2026-09-08,
    purge de redondance).

    `current_goal` est remis à `None` entre certains tours tant qu'aucun
    tunnel/confirmation/champ n'est explicitement en attente (voir
    `nodes/cleanup.py::post_response_cleanup`, `_keep_goal_channel`) —
    `working_memory.active_goal` sert alors de mémoire de secours, purgée
    séparément, à la terminaison RÉELLE du goal (voir
    `nodes/cleaner.py::state_cleaner_node`).

    Point de résolution UNIQUE : avant cette centralisation, la chaîne
    `state.get("current_goal") or working_memory.get("active_goal")`
    était recopiée indépendamment dans 6 fichiers (`core/router.py` ×2,
    `interpreter/strategy.py`, `interpreter/routing.py` ×2,
    `interpreter/goal_planner.py`) — plus un 3ᵉ alias désormais retiré,
    `working_memory.locked_intent`, qui dupliquait `active_goal` à
    l'IDENTIQUE sur chacun de ses ~15 sites d'écriture (jamais une seule
    divergence trouvée à l'audit — un pur doublon, pas une donnée
    distincte). Une chaîne recopiée plusieurs fois est un risque de dérive
    par construction (un site oublié lors d'une future correction) ; un
    seul point de résolution l'élimine structurellement.

    Distinct de `nodes/rendering/common.py::resolve_goal_for_ui` — CE
    dernier ajoute délibérément des replis supplémentaires
    (`suspended_goal`, `detected_intent`) pour ne JAMAIS renvoyer `None`
    dans un badge d'UI ; cette fonction-ci reste stricte (peut renvoyer
    `None`) pour la logique métier (routage, machine à états)."""
    if not isinstance(state, dict):
        return None
    goal = state.get("current_goal")
    if goal:
        return goal
    working_memory = state.get("working_memory")
    if isinstance(working_memory, dict):
        return working_memory.get("active_goal")
    return None


# =====================================================================
# API CANONIQUE D'ÉCRITURE DU GOAL (Phase 2 hardening, commit 6, mandat §18)
# =====================================================================
# `resolve_current_goal` ci-dessus est le point de LECTURE canonique unique.
# Ces deux fonctions sont son symétrique en ÉCRITURE pour la représentation
# de secours `working_memory.active_goal` (celle que `resolve_current_goal`
# consulte quand `current_goal` est retombé à `None` entre deux tours — voir
# sa docstring). `current_goal` lui-même reste, par contrat (voir docstring
# de module ci-dessus), écrit UNIQUEMENT par `goal_planner` via `updates[...]`
# directement : ce n'est pas une représentation concurrente à unifier, c'est
# LA source primaire dont ceci est le repli, et `goal_planner` est son seul
# propriétaire déclaré — rien à canoniser de plus là sans dupliquer un
# mécanisme qui n'a qu'un seul écrivain.
#
# Portée délibérément étroite (mandat §22, "ne bascule pas tout d'un coup") :
# seules `active_goal`/`step_index` sont couvertes ici. Le purge plus large du
# menu/sélection (`goal_planner._clear_goal_lock`, qui appelle `clear_goal_lock`
# ci-dessous PUIS efface en plus ses propres clés de menu) reste une
# responsabilité propre à `goal_planner` — un sur-ensemble orthogonal, pas une
# seconde représentation du goal lui-même.
def lock_goal(
    working_memory: Optional[Dict[str, Any]], goal: Optional[str]
) -> Dict[str, Any]:
    """SET/TRANSITION : verrouille `goal` comme repli `active_goal` dans
    `working_memory`. Un appel avec un NOUVEAU goal transitionne implicitement
    depuis l'ancien (la clé est écrasée) ; un appel avec le MÊME goal ne fait
    que le réaffirmer sans réinitialiser `step_index` (`setdefault`) — un tour
    qui confirme un goal déjà actif ne doit pas remettre sa progression à
    zéro. Jamais verrouillé pour le goal factice `DISAMBIGUATION_MENU_GOAL_SHIM`
    (un menu de désambiguïsation n'est pas un goal métier résumable)."""
    wm = dict(working_memory or {})
    if goal and goal != DISAMBIGUATION_MENU_GOAL_SHIM:
        wm["active_goal"] = goal
        wm.setdefault("step_index", 0)
    return wm


def clear_goal_lock(working_memory: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """CLEAR : efface `active_goal`/`step_index` avec la sémantique DELETE
    (`mark_deleted`, jamais `.pop()` — un `.pop()` avant un patch `merge_dict`
    est un no-op silencieux, bug B1/commit C2 : l'ABSENCE d'une clé dans un
    patch `merge_dict` signifie "inchangé", pas "supprimé")."""
    wm = dict(working_memory or {})
    mark_deleted(wm, "active_goal", "step_index")
    return wm


__all__ = [
    "MarketAgentState",
    "entities_said_this_turn",
    "BuyerContext",
    "ProducerContext",
    "UserEvent",
    "replace_value",
    "replace_list",
    "merge_dict",
    "_KEEP",
    "resolve_current_goal",
    "lock_goal",
    "clear_goal_lock",
]
