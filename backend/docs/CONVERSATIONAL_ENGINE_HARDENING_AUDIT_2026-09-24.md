# Moteur conversationnel Ladini — Audit de stabilisation architecturale (Phase 1)

_2026-09-24. Audit en lecture seule : aucun code de production n'a été modifié. Chaque constat
porte une référence `fichier:ligne`. Les constats marqués **CONFIRMÉ** ont été reproduits par
un script qui exécute les vrais nœuds et le vrai reducer (`agents/reducers.py::merge_dict`). Les
constats marqués **PROBABLE** sont déduits d'une lecture du code, sans reproduction._

Référence de départ : `GROQ_API_KEY=dummy OTEL_SDK_DISABLED=true pytest` → **4290 passed,
208 skipped, 0 failed**. Sans ces deux variables, 20 tests de topologie échouent (le graphe
compilé exige une clé LLM) et le process reste bloqué à la sortie sur l'exporter OTel. C'est une
dette d'environnement CI, pas une régression.

---

## 0. Résumé exécutif

Le moteur n'a pas un problème isolé. Il a **sept propriétaires concurrents pour une même
décision** : « ce message continue-t-il la tâche en cours, ou en démarre-t-il une autre ? ». Ce
constat est central. Chaque couche a reçu, au fil des incidents, sa propre règle
d'interruption, sa propre notion de « goal courant » et sa propre sémantique d'effacement.
Les bugs récents viennent tous des coutures entre ces couches.

Les 10 constats les plus importants :

| # | Sév. | Constat | Statut |
|---|---|---|---|
| 1 | **P0** | `RecurringNeedDraft` n'est **jamais persisté en PostgreSQL**. La docstring affirme le contraire. La table `recurring_need_drafts` existe (migration 0003) mais aucun code n'y écrit. Un timeout MCP est classé `FAILED` et non `EXECUTION_UNKNOWN`, et aucune réconciliation n'existe. Un utilisateur qui relance après un échec apparent crée donc un **doublon de besoin récurrent**, ce qui mène à des commandes récurrentes en double. | CONFIRMÉ (code) |
| 2 | **P1** | `goal_planner._clear_goal_lock()` retire les verrous par `dict.pop()`. Or `merge_dict` **ignore les clés absentes**. Après un REJECT hors confirmation, `current_goal=None` mais `working_memory.active_goal` survit, et `resolve_current_goal` **ressuscite le goal rejeté** au tour suivant. C'est la même classe de bug que celle corrigée dans `memory.py` au commit `0c93ca0`. | **CONFIRMÉ** (reproduit) |
| 3 | **P1** | `response_strategy` réécrit **tout** tour REJECT en `CLARIFICATION`/`WAITING_INPUT`, y compris une annulation réussie (`status=COMPLETED`). `state_cleaner` ne voit donc jamais la fin du goal : `transaction_payload` et `working_memory.active_goal` fuient. Les tests d'intégration récurrents ne l'exécutent pas, ce qui masque le bug. | **CONFIRMÉ** (reproduit) |
| 4 | **P1** | `recurring_need_draft` est absent de `goal_planner._purge_transaction_state`, de `conversation_reset` et de `state_profile`. Un changement de goal le laisse dans l'état. Sous pression de taille, le shrink tier-3 du checkpointer le **supprime silencieusement** (champ non DURABLE). | CONFIRMÉ (grep + script) |
| 5 | **P1** | Unités : `memory.py:217` passe l'unité **du produit principal** par `canonical_unit_label`, qui fait `TETE→UNITE` (`utils.py:277`). Les `additional_items` et les items issus d'une clarification reçoivent `default_unit_for_product` (→`TETE`). Le matching compare les unités **littéralement** (`domain/recurring_supply/matching.py:53`). Un coq principal stocké en `UNITE` ne matchera jamais une offre en `TETE`. **Non-matching silencieux.** | CONFIRMÉ (code) |
| 6 | **P1** | La règle `is_short` du planner (`goal_planner.py:582`) passe **avant** sa branche INTERRUPTION (`:609`). Un message de 4 caractères ou moins (« maïs », « riz », « prix ») dont `cognitive_guard` a **approuvé** l'interruption reste prisonnier de l'ancien goal. | **CONFIRMÉ** (reproduit) |
| 7 | **P1** | Aucune sérialisation par conversation. Deux messages quasi simultanés (deux workers Celery, ou deux requêtes webchat) chargent le même état, et le dernier flush gagne (lost update). Côté webchat, l'`Orchestrator` est partagé par tout le process et `_session_workspaces` est indexé par `workspace_id` : la requête B écrase l'ancrage de A, puis `detach` de A retire celui de B. | CONFIRMÉ (code) |
| 8 | **P1** | Sur timeout ou exception, l'orchestrateur **flushe l'état partiel du tour** (checkpoints déjà posés en RAM, sans cleanup). Le tour suivant repart d'un état mi-tour. | CONFIRMÉ (code) |
| 9 | **P1** | Un retry Celery après échec du *dispatch* **rejoue tout le graphe** sur un état déjà avancé. Un « oui » peut alors confirmer autre chose, par exemple via la promotion « orphan CONFIRM → BUYER_PREORDER_CONFIRM ». Le WebChat n'a **aucune** clé d'idempotence (pas de `message_sid`). | PROBABLE |
| 10 | **P2** | La télémétrie de tour lit l'état **après** `post_response_cleanup`, qui a déjà mis `detected_intent`/`interpreter_confidence`/`current_goal` à `None`. Les colonnes intent/confidence sont quasi toujours vides. Le WebChat n'a aucune télémétrie. Le message utilisateur et la réponse sont stockés en clair. | CONFIRMÉ (code) |

Direction recommandée, détaillée au §13 : **pas de réécriture.** Il s'agit de centraliser trois
décisions (transition de tour, goal courant, sémantique d'effacement) derrière trois fonctions
pures testées, puis d'ajouter un harnais de test unique sur le **graphe compilé réel**, et de
faire passer tous les flows par lui.

---

## 1. Carte de l'architecture actuelle

### 1.1 Chemin réel d'un message

```
                     ┌──────────────────────────── ENTRÉES (4) ───────────────────────────┐
WhatsApp Cloud  ──►  api/routes/whatsapp_webhook.py  ─┐ signature, SETNX msg:{id} (1h, AVANT enqueue)
Twilio (repli)  ──►  api/routes/twilio_webhook.py    ─┤ photo/GPS/role_hint (code DUPLIQUÉ ×2)
API interne     ──►  api/routes/market.py            ─┤ (pas de message_sid)
                                                     ▼
                              Celery process_agent_task (api/tasks.py)
                              task_claim:{sid} 300s / task_done:{sid} 24h
                                                     │
WebChat (site)  ──►  api/routes/webchat.py ──────────┤ SYNCHRONE, dans le process API,
                     (pas de message_sid, pas de      │ pas de claim, pas de télémétrie
                      claim, pas de dispatcher)       ▼
                         orchestrator/orchestrator.py::Orchestrator.handle
                         ├─ WorkspaceResolver.resolve (agri_workspaces, PG)
                         ├─ résolution de rôle (role_hint Redis → metadata → profil MCP)  ◄─ 3e copie
                         ├─ inputs = {**ws.metadata (snapshot), current_goal: ws.active_goal, ...}  ◄─ 3e source de goal
                         ├─ GraphFactory.get_graph(role) ; wait_for(45s) ; circuit-breaker 48 checkpoints
                         └─ _sync_workspace + _flush_workspace (état partiel flushé même sur erreur)
                                                     ▼
LangGraph (core/graph_builder.py) — un seul graphe double rôle
 input_normalizer → security_moderation ─┬─► response_strategy (bloqué)
                                         └─► session_bootstrap ─┬─► onboarding_node ─► response_strategy
                                                                └─► input_interpreter
 input_interpreter (interpreter/routing.py, 3175 lignes)
   ├ fast-paths structurels (index menu, confirmation exacte, bouton)
   ├ 1.5–1.8 confirmations « nues » (appels DB, dépendants du RÔLE)
   ├ state_router.choose_interpretation_route → SELECTION | ACTIVE_SLOT | STRUCTURED_ACTION | NEW_TASK
   ├ micro-prompts LLM (selection/active_slot/structured_action/new_task) ou prompt unifié legacy
   └ POST-TRAITEMENT : réécrit raw_event (→INTERRUPTION/NEW_TASK/ANSWER/UNKNOWN), GONFLE la confiance à 0.9 ◄─ décideur d'interruption n°1
        │ FastPathPolicy : ANSWER/SELECTION + goal de tunnel acheteur → memory_update (saute guard+planner)
        ▼
 cognitive_guard (nodes/cognitive.py) — NEW_TASK→INTERRUPTION (≥0.85, breakout, cas spécial RECURRING) ◄─ n°2
   ├─► semantic_disambiguation ─► response_strategy
   ├─► clarification_node ─► {goal_planner | response_strategy}
   ├─► response_strategy (RECOVER)
   └─► goal_planner (interpreter/goal_planner.py) — REJECT/is_short/NEW_TASK+pending/INTERRUPTION ◄─ n°3
          └ TunnelManager.evaluate (seuil 0.60 propre, slots durs/mous)                      ◄─ n°4
        ▼
 memory_update (fusion extracted_entities → transaction_payload, merge_dict)
 validator (INTENT_CONFIG.required, pose ENTER_FIELD, efface les pending « validated_complete »)
 DomainRouter.decide (core/router.py) : règles par tunnel, puis REPLI legacy make_route_after_validator
   ├─► cart_management / negotiation_gate / order_tracking_node ─► ui_engine        (tunnels auto-suffisants)
   ├─► context_resolver ─► buyer_context_resolver | producer_context_resolver         (flows/*)
   │       └ recurring_need_flow / procurement / preorder / producer update / escrow… ◄─ n°5 (interruptions locales)
   ├─► confirmation_gate (générique + drafts PROCUREMENT/SALES) ─► mcp_tool_executor ─► finalizers
   └─► response_strategy
 response_strategy (interpreter/strategy.py) — RÉÉCRIT status/stratégie (REJECT → CLARIFICATION)    ◄─ n°6
 state_cleaner → final_response (nodes/rendering/*) → post_response_cleanup → END
        ▼
 WorkspaceCheckpointer (RAM pendant le tour) → flush PG agri_workspaces.langgraph_state (+metadata)
        ▼
 Celery : ResponsePlan → ResponseDispatcher (idempotence event_id) → WhatsApp/Twilio
 WebChat : texte renvoyé en HTTP
```

### 1.2 Commun vs divergent par canal

| Aspect | WhatsApp Cloud | Twilio | `/api/market` | WebChat |
|---|---|---|---|---|
| Exécution | Celery | Celery | Celery | **process API, synchrone** |
| Dédup webhook | `msg:{id}` SETNX, 1 h | idem | — | — |
| Dédup tâche (`task_claim`/`task_done`) | oui | oui | **non** (pas de sid) | **non** |
| Cache LLM par `message_sid` | oui | oui | non | non |
| Rôle | `workspace_type` collant + `pending_role_hint` → `force_role=True` | idem (code copié) | route, forcé | route, forcé ; **réécrit `ws.workspace_type`** |
| Repli de rôle dans l'orchestrateur (hint/profil) | seulement sans workspace | idem | jamais | jamais |
| `interactive_id` / GPS | oui | oui | non | non |
| Photo produit | tâches Celery | idem | non | en process + `reply_sink` ; **« photos <x> » non supporté** |
| Télémétrie `TurnRecorder` | oui (`channel="WHATSAPP"` en dur) | oui | oui | **aucune** |
| Envoi | `ResponseDispatcher` | idem | idem | HTTP direct |

**Logique métier dépendante du canal (non souhaitée).** Le rôle est fixé par le canal (route
webchat ou workspace collant). Or l'interpréteur a 7 branches `role_up` (`routing.py`) : les
confirmations « nues » 1.5–1.8 (réponse au digest récurrent : BUYER seulement ; transition de
livraison : PRODUCER seulement) et `_degraded_fallback` (BUYER seulement). Un même utilisateur
double rôle obtient donc un résultat métier différent selon la dernière route utilisée. Un
passage par le webchat `/buyer` rend aussi le workspace WhatsApp « buyer » de façon collante.

**Chemins legacy encore actifs :**
- repli `make_route_after_validator` (`routing.py:3047`) derrière `DomainRouter` ;
- `legacy_confirmation_bridge` (`pending_interaction.py`) ;
- prompt interprète unifié legacy (`UNIFIED_FALLBACK`) ;
- ré-injection du snapshot `ws.metadata` comme **entrées** du graphe à chaque tour
  (`orchestrator.py:343`), dont 6 clés qui ne sont même pas des champs d'état ;
- colonnes `active_goal`/`active_form`/`locked_agent` de `agri_workspaces` ;
- `Workspace.close_tunnel`/`tunnel_locked`, non lus par le graphe.

---

## 2. Inventaire des sources d'état (state owners)

| Élément | Écrit par | Lu par | Durée de vie | Persistant ? | Propriétaire déclaré | Invalidation | Autres représentations |
|---|---|---|---|---|---|---|---|
| `current_goal` | **~30 sites** : planner (10), `producer/flow.py` (14), `order_tracking.py` (13), `auctions.py`, `negotiation.py`, `preorder.py`, `procurement.py`, `cart.py`, `cart_service.py`, `confirmation_gate`, `executor`, `cognitive`, `memory`, `cleanup`, 4 micro-prompts, `rendering/ask.py`, `semantic_disambiguation` | 85 lectures directes, 13 via `resolve_current_goal` | remis à `None` en fin de tour sauf si un canal (confirmation/sélection/champ) reste ouvert (`cleanup.py`) | via checkpoint | « goal_planner seul écrivain » (faux) | `post_response_cleanup`, `conversation_reset`, flows | `working_memory.active_goal`, `ws.active_goal` (PG), `metadata.active_goal`/`current_goal`, `pending_interaction.goal`, `goal_metadata` |
| `working_memory.active_goal` | planner `_lock` + **~40 sites** dans `order_tracking.py`/`producer/flow.py`/`procurement.py`/`helpers.py`/`memory.py` | `resolve_current_goal`, `memory`, `validation`, `rendering/common` | DURABLE (merge_dict) | oui | planner | `state_cleaner` (seulement si status terminal) ; **pas** par `_clear_goal_lock` (bug #2) | voir ci-dessus |
| `goal_stack` / `suspended_goal` / `suspended_payload` | planner (INTERRUPTION/RESUME) | planner | DURABLE | oui | planner | RESUME | les drafts suspendus ne sont **pas** sauvegardés (purgés à l'interruption) |
| `previous_tunnel` / `current_tunnel` | *n'existent pas comme champs* | — | — | — | — | — | le tunnel est **dérivé** de `INTENT_CONFIG[goal]["tunnel"]` (bien) |
| `interpreted_event` | interpréteur, puis **réécrit** par `cognitive_guard` (NEW_TASK→INTERRUPTION) et `rendering` | tout le graphe | EPHEMERAL | non | interpréteur | cleanup | `cognitive_decision.action` (sémantique voisine), `interruption_detected` |
| `interpreter_confidence` | interpréteur (+ **gonflée à 0.9**) | guard, planner, désambiguïsation | EPHEMERAL | non | interpréteur | cleanup | — |
| `pending_interaction` | ~70 sites (34 SELECTION_MENU, 25 ENTER_FIELD, 25 CONFIRM_ACTION…) | `get_pending_interaction` (canonique) | DURABLE, **aucune expiration** (`created_at` non lu, sauf TTL locaux) | oui | écrivain unique par kind (déclaré) | resolve/clear, `validator` « validated_complete » | panier : dérivé de `vendor/tier_selection_context` ; `metadata.pending_interaction_kind` ; legacy `waiting_for_confirmation`/`expected_input` |
| slots actifs | *projection* `ActiveSlotContext` (non persistée, bien) | `state_router`, micro active_slot | tour | non | — | — | `last_missing_field`, `missing_fields` |
| `transaction_payload` | `memory_update` + flows | validator, flows, rendu | DURABLE merge_dict | oui | `memory_update` | `{"__reset__":True}` (planner, cleaner si terminal) | `draft_payload`, `form_data`, `stable_entities`, `suspended_payload`, drafts |
| drafts (4) | flows / `confirmation_gate` | flows, finalizers | DURABLE `replace_value` | état + **PG sauf recurring** | domaine `*_draft.py` | planner purge (3 sur 4), terminal → `None` | ligne PG (procurement/preorder/sales) |
| version / CAS | `with_updates` (+1) ; PG `compare_and_swap` | `ConfirmationTarget.matches` | draft | PG (3/4) | domaine | — | Redis claim `{type}_confirm:{id}:{v}` |
| thread / conversation id | `workspace_id = phone` | checkpointer, drafts (`conversation_id`) | illimitée | PG | orchestrateur | — | `session_id` (metadata) |
| contexte utilisateur | orchestrateur (profil/rôle) + `session_bootstrap` | tout | tour / DURABLE | partiel | double (orchestrateur ET bootstrap) | — | `user_role` vs `ws.workspace_type` vs `pending_role_hint` |
| confirmation | `confirmation_gate`, flows | router, strategy, rendu | DURABLE | oui | `confirmation_gate` (CONFIRM_ACTION) | TTL 600 s générique (`confirmation_gate.py:93`) | `confirmation_summary(_goal/_payload)`, `status=WAITING_CONFIRMATION`, `pending.target` |
| claims Redis | webhook, tâche, drafts, cache LLM | idem | 300 s à 24 h | Redis (fail-open) | — | TTL | `mcp_idempotency_records` (PG) |
| checkpointer LangGraph | `WorkspaceCheckpointer` | LangGraph | 5 checkpoints/ns, élagage par taille | PG `langgraph_state` | — | shrink tier 1-3 puis wipe | `metadata` (snapshot parallèle ré-injecté) |

**Sources de vérité concurrentes (à éliminer) :**
1. **Goal courant.** On en compte **cinq** : `current_goal`, `working_memory.active_goal`,
   `ws.active_goal` (colonne PG, ré-injectée), `metadata.current_goal/active_goal` (ré-injectés),
   `pending_interaction.goal`. `resolve_current_goal` n'en arbitre que deux, et seulement pour
   13 des 98 lecteurs.
2. **Décision d'interruption.** Six décideurs, voir §5.
3. **« On attend quoi ? »** `pending_interaction` persisté, contexte panier dérivé, legacy bridge,
   `status=WAITING_*`, `confirmation_summary`, `metadata.pending_interaction_kind`.
4. **Rôle.** `user_role`, `ws.workspace_type`, `pending_role_hint` (lu dans 3 fichiers),
   `transaction_payload.role`, profil DB.
5. **Unité canonique.** Deux normaliseurs contradictoires, voir §9.
6. **Draft récurrent.** L'état seul fait foi. La table PG existe mais aucun code n'y écrit.

---

## 3. Machine d'état conversationnelle

### 3.1 Vocabulaire actuel vs concepts cibles

| Concept cible | Représentation actuelle | Écart |
|---|---|---|
| NEW_TASK | `NEW_TASK` | ok |
| CONTINUATION | `ANSWER`, `SELECTION`, `UPDATE` (+ `CONTINUE_ACTIVE_GOAL`) | trois labels pour un seul concept, avec des consommateurs différents |
| INTERRUPTION | `INTERRUPTION` (interpréteur) **ou** NEW_TASK réécrit par le guard | la même chose sous deux origines, avec des seuils différents (0.60 / 0.85) |
| CLARIFICATION (réponse à) | `ANSWER`, ou rien (champ hors registre → route NEW_TASK) | pas d'événement pour « réponse à une clarification structurée » |
| CONFIRMATION | `CONFIRM` | ok |
| CORRECTION | `UPDATE` (parfois), `NEW_TASK`→INTERRUPTION même goal (« non plutôt… ») | **pas d'événement dédié** ; le résultat dépend du label LLM |
| CANCELLATION | `REJECT` (« annule », « stop ») | **confondue avec REJECTION** |
| REJECTION | `REJECT` (« non ») | ; sémantique **différente par flow** (doux pour procurement/preorder/sales, définitif pour recurring) |
| COMPLETION | `status=COMPLETED` (flow) | peut être **écrasé** par `response_strategy` (bug #3) |
| FAILURE | `status=ERROR/FAILED`, `unknown_reason=TECHNICAL_FAILURE` | ok, mais `FAILED` ≠ `EXECUTION_UNKNOWN` pour recurring |
| — | `RESUME`, `OUT_OF_SCOPE`, `UNKNOWN`, `ONBOARDING_INPUT` | à garder |

### 3.2 Matrice de transition cible (à valider)

États conversationnels : `IDLE`, `COLLECTING(draft,v)` (slot/clarification ouverts),
`AWAITING_CONFIRMATION(draft,v)`, `EXECUTING(draft,v)`, `TERMINAL(draft)`.

| État \ Événement | NEW_TASK (autre goal) | CONTINUATION / réponse valide | CORRECTION | CONFIRMATION | REJECTION | CANCELLATION | UNKNOWN |
|---|---|---|---|---|---|---|---|
| IDLE | → COLLECTING(nouveau draft v1) | → NEW_TASK si intent, sinon clarif. | clarif. | orphelin → clarif. (**jamais** de promotion implicite) | clarif. | « rien à annuler » | clarif. |
| COLLECTING(d,v) | draft d → **CANCELLED** (ou suspendu explicitement) ; pending consommé ; → COLLECTING(d′,1) | d → v+1 ; pending consommé ou reposé | d → v+1 | interdit (pas de cible) → redemande | = CANCELLATION (pas de confirmation en cours) | d → CANCELLED ; pending, slots, `active_goal` effacés → IDLE | retry ≤2 puis abandon = CANCELLATION |
| AWAITING_CONFIRMATION(d,v) | idem COLLECTING | idem CORRECTION | d → v+1 → AWAITING(d,v+1) | cible `(d,v)` exacte → EXECUTING ; cible périmée → STALE | **décision explicite par domaine** (doux → COLLECTING / définitif → CANCELLED), documentée dans une table | d → CANCELLED → IDLE | reste AWAITING (compteur) |
| EXECUTING(d,v) | autorisé, d continue en arrière-plan | « en cours » | refus | idempotent (ALREADY_EXECUTING) | refus | refus | « en cours » |
| TERMINAL(d) | nouveau draft d′ (**jamais** d) | → NEW_TASK | → NEW_TASK | idempotent (ALREADY_*) ; **jamais** de réactivation | no-op | no-op | IDLE |

**À nettoyer à chaque sortie vers IDLE ou TERMINAL :** `current_goal`, `working_memory.active_goal` et
`step_index` (**avec `None`, jamais `pop`**), `pending_interaction`, `transaction_payload`,
`draft_payload`, `stable_entities`, draft actif, `missing_fields`, `last_missing_field`,
`retry_count`, `confirmation_*`.
**Doit survivre :** identité, rôle, panier (`active_cart`), `goal_stack` (sauf CANCELLATION
explicite), historique borné.

---

## 4. Priorité de routage

### 4.1 Actuelle : répartie dans 7 couches (état de fait)

1. `input_interpreter` : fast-paths structurels, puis confirmations nues selon le rôle, puis choix de route
   par `state_router` (STRUCTURED_ACTION > SELECTION > ACTIVE_SLOT > NEW_TASK), puis
   **réécriture d'événement** (INTERRUPTION si ≥0.60 pendant SELECTION/CONFIRMATION, NEW_TASK si
   produit/goal différent pendant un slot, confiance gonflée à 0.9).
2. `FastPathPolicy` : ANSWER/SELECTION sur un goal de tunnel acheteur saute guard et planner.
3. `cognitive_guard` : NEW_TASK devient INTERRUPTION (≥0.85, breakout, **ou CREATE_RECURRING_NEED avec
   récurrence**, un cas métier en dur) ; retry/abandon ; désambiguïsation ; clarification.
4. `goal_planner` : menu de désambiguïsation > REJECT > continuation > UNKNOWN > NEW_TASK+pending
   > **is_short** > UNKNOWN en tunnel > OUT_OF_SCOPE > INTERRUPTION (TunnelManager) > RESUME >
   orphan CONFIRM > NEW_TASK.
5. `DomainRouter` : règles par tunnel, puis repli legacy (confirmation > dérive > champs manquants >
   sélection).
6. Flows : interruptions locales (`recurring_need` : réponse ambiguë vs tâche autonome ;
   `_is_bare_abandon` sur une liste de phrases).
7. `response_strategy` : sécurité > interruption > **REJECT** > tunnel panier > recovery > …

### 4.2 Proposée : une seule politique pure et testable

```python
decide_turn(snapshot: TurnSnapshot, interp: InterpreterResult) -> TurnDecision
```
Une fonction pure qui ne dépend que de faits (pending canonique, draft actif et son état, goal
canonique, résultat de l'interpréteur **non réécrit**). Ordre, déduit du domaine existant :

1. **Invariants de sécurité et structurels** : bloqué, OTP incassable, draft EXECUTING.
2. **Commandes globales explicites** : annulation (CANCELLATION), aide/menu (breakout de navigation).
3. **Décision sur la transaction active** : CONFIRMATION ciblée `(draft,version)`, REJECTION,
   CORRECTION du draft en attente de confirmation.
4. **Réponse valide à l'interaction en attente** : menu, slot, **clarification structurée**
   (validée par le résolveur du kind, pas par le LLM).
5. **Nouvelle tâche autonome forte** : intent ≠ goal courant, confiance ≥ seuil **unique**, ou
   preuve structurelle (produit différent). Elle interrompt, **même si le message est court**.
6. **Continuation du goal courant** : réponse partielle, UPDATE.
7. **Repli** : retry puis abandon, désambiguïsation lexicale, clarification générale.

`TurnDecision = {action, target_goal, draft_effect (KEEP|BUMP|CANCEL|SUSPEND|NONE), pending_effect
(CONSUME|KEEP|REPLACE), reason}`, écrit dans `cognitive_decision`. **Le planner applique la
décision** (verrous, stack, purge) sans la rejuger. L'interpréteur **cesse de réécrire**
`interpreted_event` : il expose des *signaux* (`different_product`, `different_goal`), et c'est la
politique qui décide.

---

## 5. Propriétaire de l'interruption : contrat actuel vs réel

Contrat écrit (docstrings, `test_interruption_ownership.py`) : `cognitive_guard` est l'unique
propriétaire. **Ce n'est pas le cas dans les faits :**

| Décideur | Règle | Fichier |
|---|---|---|
| interpréteur | expected ∈ {SELECTION, CONFIRMATION} et intent ≥0.60 : INTERRUPTION, sinon UNKNOWN | `routing.py:2745-2775` |
| interpréteur | slot + produit/goal différent : NEW_TASK, conf=max(conf,0.9) ; sinon NEW_TASK devient ANSWER | `routing.py:2700-2714` |
| interpréteur | « panier en attente » + CONFIRM devient NEW_TASK/BUYER_PREORDER_INIT | `routing.py:2780` |
| cognitive_guard | NEW_TASK ≥0.85 / breakout / **RECURRING en dur** → INTERRUPT | `cognitive.py:351-357` |
| goal_planner | `is_short` et REJECT **avant** INTERRUPTION ; NEW_TASK+pending → garde l'ancien goal | `goal_planner.py:494, 582, 609` |
| TunnelManager | son **propre** seuil 0.60 sans approbation du guard | `tunnel_manager.py` (`evaluate`) |
| flows | `recurring_need` décide « réponse à la clarification » vs « nouvelle tâche » ; `_is_bare_abandon` | `recurring_need.py` |
| response_strategy | REJECT → CLARIFICATION quel que soit le résultat du flow | `strategy.py:119` |

**Contrat proposé :** le guard (renommé `turn_policy`) décide, et c'est tout. L'interpréteur
fournit des signaux. Le planner et TunnelManager *appliquent* : TunnelManager ne garde que
l'interdit OTP. Les flows *exécutent* l'effet sur leur draft. `response_strategy` *rend*, sans
réécrire `status` quand un flow a posé un statut terminal. Un test d'architecture interdit toute
écriture de `interpreted_event` ou `current_goal` en dehors d'une liste blanche.

---

## 6. PendingInteraction

### 6.1 Kinds

| Kind | Créé par | Stockage | Consommé par | Expiration | Annulation / interruption | Détection de périmé |
|---|---|---|---|---|---|---|
| SELECT_PRODUCER / SELECT_PRICING_TIER / ENTER_PACKAGE_COUNT | **dérivé** de `vendor/tier_selection_context` | aucun (bien) | `cart_management` | celle du contexte | purge planner | par construction |
| ENTER_QUANTITY | dérivé **et** posé explicitement (12 sites) | double | cart / flows | aucune | purge | **ambigu** : persisté et dérivé coexistent |
| ENTER_FIELD | validator, flows (25 sites) | persisté | `state_router` (si le champ est au registre), sinon le flow lit `pending.field` lui-même | **aucune** | purge planner ; `validator` l'efface si « validated_complete » (sauf champs hors registre, correctif 0c93ca0) | aucune |
| CONFIRM_ACTION | `confirmation_gate`, flows | persisté + `target (draft,v)` pour les drafts | gate / flows | 600 s (générique seulement) | REJECT | `ConfirmationTarget.matches` |
| SELECTION_MENU | ui_engine, désambiguïsation, flows (34) | persisté + `available_mapping` + snapshot Redis | selection micro, flows | TTL locaux (600 s récurrent, 30 min suivi) | purge | `mapping_kind` |
| PROVIDE_LOCATION | gps_delivery_gate | persisté | gate | aucune | — | — |
| VERIFY_OTP | producteur escrow | persisté | resolver | aucune | **incassable** | — |
| CLARIFY_INTENT | déclaré | — | — | — | — | **jamais posé** (mort) |

`InteractionStatus` (RESOLVED/CANCELLED/EXPIRED/REPLACED) est **déclaré mais jamais utilisé**. Un
pending consommé est remis à `None`, donc un pending consommé ne peut pas « revenir ». Il peut
en revanche **ne jamais être consommé** et rester indéfiniment : aucune expiration globale.

### 6.2 ENTER_FIELD hors registre

`_EXPECTED_INPUT_MAP` (`core/slots.py`) détermine l'éligibilité ACTIVE_SLOT. Les champs
**créables** qui n'y figurent pas partent en classification NEW_TASK :
`ambiguous_quantity` (géré par le flow depuis 0c93ca0), `update_field`, `received_quantity`,
`reception_detail`, `order_id`, `cancellation_reason`. Il faut y ajouter les champs dynamiques
`weekly_days` (`RecurringNeedDraft.missing_fields()` → `pending_field=missing[0]`), `phone`,
`language`, `latitude`/`longitude`, `otp_code` (champs `required` hors carte).
**`weekly_days` est une instance latente de la même classe de bug que `ambiguous_quantity`.**
La réponse « lundi et jeudi » à « quels jours ? » ne passe pas par le micro ACTIVE_SLOT.

**Test d'architecture proposé :** un registre `PENDING_FIELD_CONSUMERS: {field: consumer}`. Chaque
`field_name=` littéral trouvé par AST, et chaque champ que peut produire un `missing_fields()`
de draft ou un `INTENT_CONFIG.required`, doit être soit dans `_EXPECTED_INPUT_MAP`, soit déclaré
avec un consommateur de flow **et** un résolveur structuré. Les champs cible structurés
(`ambiguous_quantity`) déclarent leur schéma de `target`.

---

## 7. Lifecycle des drafts

| | Procurement | Preorder | SalesPublish | RecurringNeed |
|---|---|---|---|---|
| États | DRAFT, CONFIRMED, EXECUTING, EXECUTED, FAILED, EXECUTION_UNKNOWN, CANCELLED | DRAFT, EXECUTING, EXECUTED, FAILED, EXECUTION_UNKNOWN, CANCELLED (+ phases paiement) | DRAFT, EXECUTING, **PUBLISHED**, FAILED, EXECUTION_UNKNOWN, CANCELLED | DRAFT, CONFIRMED, EXECUTING, EXECUTED, FAILED, EXECUTION_UNKNOWN, CANCELLED |
| Persistance PG + CAS | oui | oui | oui | **NON** (table migrée, aucun code n'y écrit) |
| Réconciliation EXECUTING / UNKNOWN | cron | cron | cron | **aucune** |
| Timeout ou ambiguïté | EXECUTION_UNKNOWN | idem | idem | **FAILED** |
| REJECT | doux (reste DRAFT) | doux | doux | **définitif** |
| Correction | `with_updates` v+1 | idem | idem | idem, **impossible d'effacer** (`slot_has_value` filtre `None`/`[]`) |
| Nouvelle tâche | purge état (ligne PG **orpheline** non terminale) | idem | idem | draft=None (non purgé par le planner) |
| Purgé par `_purge_transaction_state` | oui | oui | oui | **non** |
| Dans `state_profile` (DURABLE) | oui | oui | oui | **non** |
| `is_terminal()` | non (logique inline) | non | non | oui |
| Clé d'idempotence MCP | `procurement:{id}:{v}` | idem | idem | `recurring_need:{id}:{v}` |
| Claim de confirmation Redis | oui | oui | oui | oui (TTL 1 h) |

Invariants communs à imposer, par un protocole et une suite de tests paramétrée sur les 4, **sans
classe de base** :
- statut terminal : aucune transition sortante, `with_updates` lève une exception ;
- confirmation = `(draft_id, version)` exact ;
- chaque mutation réelle : version +1 ;
- réponse répétée : idempotente ;
- une nouvelle tâche ne réutilise jamais un draft terminal ;
- ambiguïté d'exécution : `EXECUTION_UNKNOWN` + réconciliation ;
- un draft retiré de l'état : sa ligne PG passe à CANCELLED (ou « ABANDONED ») dans la même
  transaction logique.

---

## 8. `transaction_payload` et sémantique de fusion

`merge_dict` (`agents/reducers.py:65`) :

| Patch | Effet réel | Attendu |
|---|---|---|
| clé absente | conservée | ne rien changer ✔ |
| `key: None` | mise à `None` | effacer ✔ |
| `key: []` | remplacée par `[]` | liste vide ✔ |
| `key: {}` (dict imbriqué) | **conservé** (merge récursif avec vide) | ✘ ambigu |
| `{}` au niveau racine | no-op | ✘ piège documenté |
| `dict.pop(k)` puis renvoi du dict | **clé conservée** | ✘ bug récurrent |

Sites `.pop()` sur un dict renvoyé ensuite dans un canal `merge_dict` : environ 40. Certains sont
locaux ou protégés par `__reset__`. Les cas avérés : **`goal_planner._clear_goal_lock`** (bug #2,
CONFIRMÉ) et `flows/producer/flow.py:292/457/676/772/1119` (`selection_index` / `selected_value`
« retirés » puis renvoyés : pas d'effet ; seule la purge de `memory.py` les rattrape).
`negotiation.py:310`.

**Correctif structurel proposé :** introduire un sentinel `DELETE` dans `merge_dict`, plus une aide
`clear_keys(*keys) -> {k: None}`. Un test d'architecture AST interdit `X.pop(` quand `X` provient
d'un `state.get(<canal merge_dict>)` et qu'il est renvoyé, avec liste blanche. Un test générique
paramétré sur **tous** les canaux `merge_dict` vérifie les quatre sémantiques du tableau.

---

## 9. Interpréteur, items, taxonomie, unités

### 9.1 Primary vs `additional_items` : asymétries confirmées

| Transformation | primary | additional_items |
|---|---|---|
| Sanitation produit (`_validate_and_sanitize_product`) | oui (+ **recherche catalogue live** si plus de 3 mots, `product_validation.py:67`) | **non** |
| Garde anti-ancrage d'unité (unité littérale du texte) | oui (`new_task_micro.py:367-386`) | **non** |
| Synonymes d'unité | `canonical_unit_label` (TETE→**UNITE**) dans `memory.py:217` | **aucun** (« têtes » brut jusqu'en DB) |
| Unité par défaut | registre de slots (`default_factory`) | `_clean_additional_items` / clarification (→TETE) |
| Validation contrat | `CONTRACTS` Pydantic | forme seulement |
| Rendu | `_render_item_line` | idem |
| Correction | champ à champ | **remplacement de liste entière**, impossible de vider |

**Recommandation.** Le modèle interne doit être `items: [Item]` (le primary = `items[0]`), avec un
`normalize_item()` **unique**. On garde la compatibilité `product/quantity/unit +
additional_items` **à la frontière interpréteur seulement**, et dans `execution_payload()` le temps
de la migration. Le gain est clair : trois bugs en une semaine viennent de cette asymétrie.

### 9.2 Unités : chaîne réelle

```
LLM brut ("têtes", "unités", "tete")
  → entities._remap_entities : .upper() seulement ("TÊTES")
  → new_task_micro : garde d'unité littérale (primary)
  → memory.py:217 canonical_unit_label : TETE→UNITE (primary)        ← normaliseur A
  → recurring_need._clean_additional_items : default TETE (items)      ← normaliseur B
  → ambiguous resolution : default_unit_for_product → TETE
  → recurring_supply._insert_one : unit.upper() (texte libre, AUCUNE contrainte DB)
  → matching._eligible : égalité littérale
  → rendu avant confirmation : draft.unit ; après : need.unit (DB)
```
**Représentation canonique proposée :** l'enum `KG|TONNE|SAC|PANIER|LITRE|TETE|UNITE` avec une
seule fonction `canonical_unit()` dans `domain/quantity_unit.py`. `TETE` reste distinct de `UNITE`
(sens métier). `canonical_unit_label` de `utils.py` est supprimé au profit de cette fonction. Une
contrainte CHECK en DB peut suivre, **après** un rapport sur les valeurs existantes (pas de
migration sans données).

### 9.3 Normalisation produit

Au moins quatre normaliseurs : `normalize_unit_token` (accents), `is_livestock_product` (œ),
`_sanitize_product_candidate` / `_validate_and_sanitize_product`, `fuzzy_match` et
`similarity_rank` SQL dans `_resolve_sub_category`. Cette dernière fonction **crée une
sous-catégorie** à partir du texte libre si le match n'est pas « confiant »
(`_get_or_create_sub_category_for_rfq`). Des variantes (« chèvres » / « chevre ») risquent donc
de créer des doublons de taxonomie. Il faut **une** fonction `canonical_product_key()` (casse,
accents, ligatures, singulier/pluriel) utilisée par l'interpréteur, le domaine et le SQL.

### 9.4 Taxonomie, listing et besoin

La résolution à la création d'un besoin récurrent passe par la **taxonomie** (`SubCategory`), ce
qui est correct. Il reste deux fuites vers le catalogue live :
(a) `_validate_and_sanitize_product` appelle `search_products` pour un produit principal de plus
de 3 mots, quelle que soit l'intention ;
(b) le bug historique « recurring → catalog search », corrigé par routage mais sans invariant.

Invariant à tester : aucun chemin atteignable depuis `CREATE_RECURRING_NEED` n'appelle un outil
dont la sémantique exige `is_available=true` (liste d'outils « catalogue live » déclarée dans
l'allowlist MCP).

---

## 10. Bugs et chemins de reproduction

Scripts de reproduction : exécution directe des nœuds réels et du reducer réel.

**B1 (P1, CONFIRMÉ) — un REJECT hors confirmation ressuscite le goal.**
```
state: current_goal=SALES_PUBLISH_PRODUCT, working_memory.active_goal=SALES_PUBLISH_PRODUCT,
       pending=ENTER_FIELD(price), event=REJECT ("non laisse tomber")
goal_planner → current_goal=None, working_memory patch = {} (clés « pop »)
merge_dict(old_wm, {}) → active_goal conservé
resolve_current_goal(état suivant) == "SALES_PUBLISH_PRODUCT"
```

**B2 (P1, CONFIRMÉ) — une annulation réussie n'est jamais terminale.**
`response_strategy({"interpreted_event":"REJECT","status":"COMPLETED","response_strategy":"SUCCESS",...})`
renvoie `{"response_strategy":"CLARIFICATION","status":"WAITING_INPUT"}`. `state_cleaner` n'efface
donc ni `transaction_payload` ni `working_memory.active_goal`. Combiné avec B1 et le constat #4,
« annule » pendant la collecte d'un besoin récurrent **ne l'annule pas** : le planner purge sans
toucher `recurring_need_draft`, le flow n'est jamais exécuté (`_route_after_planner` →
`to_strategy`), et le goal ressuscite au tour suivant avec le draft intact.

**B3 (P1, CONFIRMÉ) — un message court interruptif est avalé.**
`current_goal=CREATE_RECURRING_NEED`, `pending=ENTER_FIELD(quantity)`, message « prix » ou
« maïs », NEW_TASK à 0.95 → guard `INTERRUPT_ACTIVE_GOAL`, puis le planner **garde
CREATE_RECURRING_NEED** (`is_short`, `:582`). Contrôle : « mes commandes » interrompt correctement.

**B4 (P0, CODE) — doublon récurrent après timeout.**
Confirmation « oui » : `gw.create_recurring_needs` commit, puis timeout réseau ou MCP. Le
`MCPCallError` générique est classé `FAILED` (`recurring_need.py:403`) et l'utilisateur lit
« échec ». Il relance la demande, ce qui crée un nouveau draft_id, donc une nouvelle clé
d'idempotence, donc un **second besoin actif**. Même classe avec un crash du worker entre le
commit et le flush de l'état : le draft n'étant pas en PG, rien ne peut réconcilier.

**B5 (P1, CODE) — incompatibilité d'unité et non-matching silencieux.**
« 14 coqs et 20 chèvres chaque semaine » → primary « coq » `UNITE` (via `canonical_unit_label`),
chèvre `TETE`. Une offre producteur de coqs en `TETE` n'est jamais éligible (égalité littérale).
Cela explique aussi l'affichage « UNITE avant / TETE après » observé.

**B6 (P1, CODE) — perte de mise à jour concurrente.**
Deux messages A puis B à moins de T_turn d'écart, sur 2 workers : chacun charge `langgraph_state`
v0, et le dernier `save()` gagne. Les transitions de A (draft v+1, pending consommé) sont perdues.
Côté webchat, l'ancrage `_session_workspaces[phone]` est partagé entre requêtes concurrentes
(`checkpointer.py:455-470`). Le `detach` de A retire l'ancrage de B, et les checkpoints de B
passent alors par le chemin « legacy flow » (`:435`) qui écrit **en DB au milieu du tour**.

**B7 (P1, CODE) — état partiel persisté.**
Un `AGENT_TIMEOUT` à 45 s ou une exception déclenche `_flush_workspace(reason="timeout")` avec les
checkpoints staged du tour : planner et memory exécutés, cleanup non exécuté.

**B8 (P1, PROBABLE) — un retry Celery rejoue le graphe.**
Le dispatch WhatsApp échoue après un tour qui a muté l'état. `autoretry_for` relance le tour
complet : même message, même interprétation (cache LLM), **état déjà avancé**. Un « oui » sans
cible (draft devenu `None`) avec un panier non vide peut être promu `BUYER_PREORDER_CONFIRM`.

**B9 (P2, CODE) — message perdu.**
Le webhook pose `msg:{id}` (1 h) **avant** l'enqueue. Si `wait_for` de l'enqueue expire, le
message n'est pas traité et la redélivraison WhatsApp est rejetée comme doublon.

**B10 (P2, CODE)** — `recurring_need_draft` n'est pas DURABLE : le shrink tier-3 du checkpointer
le supprime et l'utilisateur perd son draft sans message.

**B11 (P2, CODE)** — une seule clarification ambiguë est traitée (`groups[0]`). Un second
`ambiguous_group` dans le même message est **perdu silencieusement**, ce qui viole « N items → N
items ».

**B12 (P2, CODE)** — `INTENT_TO_GOAL_MAP` n'est rempli que par effet de bord de l'import de
`interpreter/routing.py`. Un module ou un test qui importe `goal_planner` seul obtient un planner
qui ne connaît **aucun** goal : toutes les interruptions et nouvelles tâches sont refusées. Le
premier script de reproduction est tombé dans ce piège.

**B13 (P2, CODE)** — la télémétrie intent/confidence est vide (lue après cleanup) et
`channel="WHATSAPP"` est en dur.

---

## 11. Risques classés

**P0 : corruption, mauvaise transaction**
- B4 : doublon de besoin récurrent (pas de PG, pas d'`EXECUTION_UNKNOWN`, pas de réconciliation).
- Un draft récurrent vit uniquement dans le blob d'état. Si le flush échoue (`store.save` renvoie
  `False` avec un simple log), l'EXECUTING n'est pas tracé alors que le MCP a commité.

**P1 : fuite d'état, mauvais routage, mauvais goal**
- B1, B2, B3, B5, B6, B7, B8.
- Constat #4 (draft récurrent hors purge et hors profil).
- Sémantique REJECT divergente par domaine. « non plutôt 23 boeufs » donne un résultat qui
  dépend du label LLM (UPDATE : les chèvres restent ; REJECT : tout est annulé ; NEW_TASK :
  remplacement total).
- Logique métier dépendante du rôle ou du canal (confirmations nues, repli dégradé).
- ENTER_FIELD `weekly_days` hors registre.

**P2 : robustesse, UX**
- B9 à B13.
- Aucune expiration globale des pending.
- Lignes PG de drafts orphelines non terminales.
- Confiance gonflée (0.9) qui rend les seuils non interprétables.
- Normalisation produit multiple et création de sous-catégories.
- `CLARIFY_INTENT` et `InteractionStatus` morts.
- Harnais de test divergent de la topologie réelle.

**P3 : dette**
- `routing.py` fait 3175 lignes.
- 5 représentations du goal ; snapshot `metadata` ré-injecté ; `legacy_confirmation_bridge`.
- Code de rôle et de photo copié entre les webhooks WhatsApp et Twilio.
- `apply_patch` recopié dans chaque test d'intégration.
- `recurring_need_drafts` : table morte.

---

## 12. Tests manquants

1. **Harnais unique sur le graphe compilé réel** (`build_graph` + `ScriptedLLM` + MCP stub + PG
   réel optionnel), utilisé par tous les tests multi-tours. Les tests actuels réassemblent des
   sous-chaînes. `test_recurring_need_state_leak.py` saute `response_strategy`, `final_response`
   et, pour une partie, `cognitive_guard` : il **ne peut pas voir** B2.
2. Transcripts de régression obligatoires (§32 du mandat), à exécuter sur le graphe compilé, en
   WebChat **et** WhatsApp. Déjà couverts partiellement : « 10 kg tomate… », « 14 coqs… »,
   « 57 moutons chèvres », « 50 moutons et 7 chèvres », « reste », « de chaque », abandon nu,
   confirmation répétée. **Absents** : « 60 kg oignon tous les jours sauf dimanche »,
   « 20 moutons et le reste pour les chèvres » (formulation exacte), « je veux 30 poulets chaque
   semaine » pendant une clarification, « non plutôt 23 boeufs », « laisse, je veux… », « ancien
   catalogue actif → nouvelle demande récurrente » sur le graphe complet, « nouvelle demande après
   COMPLETED » sur le graphe complet.
3. Architecture :
   - tout champ d'état est profilé ;
   - tout ENTER_FIELD a un consommateur ;
   - aucun `.pop` sur un canal `merge_dict` ;
   - écrivains de `current_goal`/`interpreted_event` en liste blanche ;
   - tout draft est purgé par le planner **et** DURABLE ;
   - toute table de draft a un store utilisé ;
   - `INTENT_TO_GOAL_MAP` non vide sans importer `routing` ;
   - tout kind de pending a au moins un écrivain ;
   - les unités écrites en DB appartiennent à l'enum ;
   - aucun outil « catalogue live » atteignable depuis CREATE_RECURRING_NEED ;
   - stratégies de rendu couvrant tous les kinds.
4. Contrats de draft paramétrés sur les 4 types (§7).
5. Sémantique `merge_dict` générique sur tous les canaux (§8).
6. Parité de canal : même transcript via `Orchestrator.handle` avec `force_role` WebChat, puis
   via `process_agent_task` ; comparaison des effets métier (appels MCP, draft final), pas du
   texte.
7. Concurrence : deux `handle()` concurrents sur le même téléphone. Attendu : sérialisation ou
   rejet propre ; jamais de perte silencieuse.
8. Pannes LLM : timeout, JSON invalide, enum halluciné, `additional_items` partiel, unité absente,
   confiance faible. Le domaine refuse sans muter.
9. Retry : dispatch en échec, puis retry. Aucune seconde mutation, même réponse.
10. Property-based (Hypothesis n'est **pas** encore une dépendance) : une machine à états
    `RuleBasedStateMachine` sur `apply_domain_action` des 4 drafts, puis sur `decide_turn`.
    Invariants : terminal jamais actif, pas de confirmation sans cible, `item_count` non
    décroissant sans CORRECTION, CANCEL donne un état transactionnel vide. Intérêt fort pour les
    fonctions pures (drafts, politique, `merge_dict`). Intérêt faible sur le graphe entier (trop
    lent et non déterministe) : on se limite à la couche pure.

---

## 13. Refactors recommandés (minimum structurant)

1. **R1 — Harnais de test du graphe compilé** (aucun changement de production). C'est le
   prérequis de tout le reste.
2. **R2 — Sémantique d'effacement explicite** : sentinel `DELETE` / `clear_keys`, correction de
   `_clear_goal_lock` et des sites `.pop` avérés, test AST.
3. **R3 — Goal canonique** : `resolve_current_goal` devient le **seul** lecteur (lint), avec
   écrivains en liste blanche (`set_goal` / `clear_goal` qui écrivent **ensemble** `current_goal`
   et `working_memory.active_goal`). On arrête la ré-injection de `ws.active_goal`/`metadata`
   comme entrées du graphe, après vérification que le checkpoint suffit.
4. **R4 — Politique de tour unique** (`decide_turn`, §4.2). Elle absorbe les règles de
   l'interpréteur, du guard, du planner et de TunnelManager. On l'introduit d'abord **en mode
   ombre** (calcul + log des divergences avec la décision actuelle) et on bascule ensuite.
   L'interpréteur ne réécrit plus l'événement ni la confiance.
5. **R5 — Fin de tour honnête** : `response_strategy` ne réécrit pas un `status` terminal posé par
   un flow. Il faut distinguer REJECTION et CANCELLATION (nouvel événement `CANCEL` ou champ
   `reject_kind`), avec une table explicite par domaine.
6. **R6 — Durcissement de `RecurringNeedDraft`** : store PG (la table existe), EXECUTING persisté
   avant MCP, `EXECUTION_UNKNOWN` sur les erreurs ambiguës, réconciliation (cron existant à
   généraliser), ajout à la purge et au profil, `CANCELLED` de la ligne à l'abandon.
7. **R7 — Items et unités** : `items: [Item]` interne, `normalize_item()` unique,
   `canonical_unit()` unique (TETE ≠ UNITE), toutes les `ambiguous_groups` traitées.
8. **R8 — PendingInteraction** : registre des consommateurs, expiration globale
   (`created_at` + TTL par kind), suppression de `CLARIFY_INTENT`/`InteractionStatus` morts, fin
   du double ENTER_QUANTITY.
9. **R9 — Sérialisation par conversation** : verrou consultatif PG
   (`pg_advisory_xact_lock(hashtext(workspace_id))`) ou verrou Redis avec fencing token autour de
   `Orchestrator.handle`. Le message concurrent attend (borné) ou est remis en file. Il faut aussi
   corriger le partage `_session_workspaces` (clé par tour, pas par workspace). Sur timeout ou
   erreur : **ne pas** flusher l'état partiel (rollback au checkpoint d'entrée), sauf les drafts
   déjà persistés en PG.
10. **R10 — Idempotence du tour** : mettre en cache la *réponse* par `message_sid` avant le
    dispatch ; un retry réémet la réponse au lieu de rejouer le graphe. Le webhook pose `msg:{id}`
    **après** un enqueue réussi, ou le relâche en cas d'échec. WebChat : `client_message_id`
    obligatoire, qui sert de `message_sid`.
11. **R11 — Parité de canal** : un `ChannelAdapter` (transport + format) autour d'**un** point
    d'entrée `handle_turn()` ; résolution de rôle unique (plus de copies webhook/orchestrateur) ;
    retrait des branches `role_up` métier de l'interpréteur (le rôle affine le rendu, pas le
    routage).
12. **R12 — Observabilité** : un événement structuré `TURN_DECISION` émis par `decide_turn` (tous
    les champs du §21 du mandat, conversation hashée, sans texte brut) ; `note_final` lit un
    instantané **pré-cleanup** ; canal réel ; texte utilisateur haché ou tronqué selon la
    politique de rétention.

**Travail préparatoire à MONTHLY (documenté, non implémenté) :** R7 (items) et R6 (draft
persisté) sont nécessaires. `recurrence_type` est un CHECK DB (`DAILY|WEEKLY_DAYS|WEEKLY|ONE_OFF`)
et un `str` libre côté contrat : il faudra un enum partagé et une fonction de rendu unique avant
d'ajouter MONTHLY.

---

## 14. Refactors déconseillés

- Réécrire le graphe ou remplacer LangGraph. La topologie est saine ; le problème est la
  propriété des décisions.
- Une classe `BaseDraft` générique avec héritage. Mieux vaut un protocole et une suite de tests
  partagée. Les domaines diffèrent (paiement preorder, PUBLISHED sales).
- Fusionner les 4 stores de draft en une table unique. La migration n'est pas justifiée ; les
  helpers `draft_store_support` suffisent.
- Refondre `routing.py` d'un bloc. On en extrait seulement les règles de décision vers
  `decide_turn` ; la mécanique LLM reste.
- Supprimer `working_memory.active_goal` d'un coup. Il sert de mémoire inter-tours voulue ; on
  l'encapsule d'abord (R3).
- Ajouter des listes de mots-clés français pour trancher CORRECTION/CANCELLATION. Il faut
  s'appuyer sur un champ structuré du micro-prompt (`reject_kind`) validé par le domaine,
  conformément au principe « le LLM décide, pas des listes figées ».
- Modifier le schéma DB pour « nettoyer ». La matrice Drizzle/SQLAlchemy/PG ne montre que 3
  divergences (tables seed sans miroir Python, attendues). Seul R6 utilise une table **déjà
  migrée**.

---

## 15. Ordre exact des corrections (commits proposés, chacun vert)

| # | Commit | Contenu | Couvre |
|---|---|---|---|
| 1 | `test: compiled-graph conversation harness + characterization` | harnais R1 ; les transcripts du §32 en **caractérisation** (B1–B3, B5 en `xfail(strict=True)` documentés) ; baseline CI (clé LLM factice, OTel off) | preuves |
| 2 | `test(arch): structural invariants` | champs profilés, consommateurs ENTER_FIELD, drafts purgés/durables, `INTENT_TO_GOAL_MAP` autonome, stores de draft utilisés (en xfail si violés, levés au commit correctif) | CI |
| 3 | `fix(state): explicit clear semantics in merge channels` | R2 + B1 | P1 |
| 4 | `fix(recurring): durable RecurringNeedDraft + EXECUTION_UNKNOWN` | R6 + B4, B10 (+ tests PG réels) | **P0** |
| 5 | `fix(turn): terminal status survives response_strategy` | R5 (partie statut) + B2 | P1 |
| 6 | `refactor(goal): single goal accessor/mutator` | R3 | P1 |
| 7 | `feat(turn-policy): decide_turn in shadow mode` | R4 étape 1 + événement `TURN_DECISION` (R12) | observabilité |
| 8 | `refactor(turn-policy): switch ownership` | R4 étape 2 + B3 ; l'interpréteur ne réécrit plus | P1 |
| 9 | `fix(pending): consumer registry + expiry` | R8 (+ `weekly_days`) | P1/P2 |
| 10 | `refactor(items): canonical items + units` | R7 + B5, B11 | P1 |
| 11 | `fix(runtime): per-conversation serialization + no partial flush` | R9 + B6, B7 (+ test de concurrence) | P1 |
| 12 | `fix(idempotency): response replay + webhook claim ordering + webchat id` | R10 + B8, B9 | P1/P2 |
| 13 | `refactor(channels): single handle_turn + role resolution` | R11 (+ tests de parité) | P1 |
| 14 | `chore: remove dead legacy` | bridge, CLARIFY_INTENT, metadata ré-injectée, table morte (si R6 ne l'utilise pas) | P3 |

Les commits 3, 4 et 5 sont indépendants et peuvent être livrés en premier. Le 4 est le seul P0.
Le 8 est le seul changement de comportement conversationnel large ; il est précédé du mode ombre
(7) pour mesurer les divergences en production avant de basculer.

---

## Annexe A : clés d'idempotence actuelles

| Opération | Clé | Stockage / TTL | Faille |
|---|---|---|---|
| Webhook WhatsApp/Twilio | `msg:{message_id}` | Redis 1 h, posée avant enqueue | B9 |
| Tâche agent | `task_claim:{sid}` / `task_done:{sid}` | Redis 300 s / 24 h, fail-open | B8 (rejoue le graphe) |
| Interprétation LLM | `message_sid` | Redis 1 h | absente en WebChat |
| Envoi | `ResponsePlan.event_id = message_sid` | dispatcher | absente en WebChat |
| Confirmation de draft | `{type}_confirm:{draft_id}:{v}` | Redis 1 h, fail-open | recurring : bloque « en cours » 1 h après un crash |
| Exécution MCP | `{type}:{draft_id}:{v}` | PG `mcp_idempotency_records` | robuste ; inopérant si un nouveau draft est créé après un faux FAILED (B4) |
| Panier, négociation, suivi, update catalogue | aucune clé explicite (tunnels auto-suffisants) | — | à auditer en Phase 2 |

## Annexe B : TTL et nettoyage actuels

| Élément | Comportement |
|---|---|
| PendingInteraction | **aucune** expiration globale ; confirmation générique 600 s, menu récurrent 600 s, menu suivi 30 min |
| Drafts PG | aucun nettoyage des DRAFT/WAITING ; réconciliation seulement pour EXECUTING/UNKNOWN |
| Draft récurrent | vit dans l'état jusqu'à écrasement ; peut être supprimé par le shrink tier-3 |
| Workspace / working_memory | illimité ; 5 checkpoints/ns ; élagage par taille |
| Redis | 300 s à 24 h selon la clé (annexe A) |

Proposition (à valider) : pending expiré après 30 min (`EXPIRED`, message de reprise) ; draft non
terminal abandonné après 24 h (`CANCELLED`, cron) ; working_memory transactionnelle remise à zéro
après 24 h d'inactivité, identité et panier conservés.
