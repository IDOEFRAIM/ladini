# Market Coach — Rapport de remédiation architecturale (2026-09-08)

Suite directe de `MARKET_COACH_GRAPH_ARCHITECTURE_REVIEW_2026-09-08.md`
(audit) : ce document rapporte les CORRECTIONS RÉELLEMENT APPLIQUÉES au code,
en respectant l'ordre du mandat (P0 → P1 → P2 → durcissement FastPath →
scénarios de bout en bout) et sans refonte massive hors de ce périmètre.

---

## A. Fichiers modifiés

| Fichier | Modification | Raison |
|---|---|---|
| `core/state.py` | +`sales_publish_draft`, +`slot_enrichment_force_clarification`, +`clarification_reasons`, +`blocked_user_query`, +`error_message`, +`technical_details` ; −`should_replan` | P0-1, P1-3, P1-4, P1-1 |
| `core/state_profile.py` | Idem (miroir déclaratif) + `farm_creation_attempted`/`auto_farm_notice`/`error_creating_farm` | Cohérence des 3 registres |
| `flows/producer/state.py` | +`farm_creation_attempted`, `auto_farm_notice`, `error_creating_farm` (`ProducerContext`) | P0-2 corollaire |
| `flows/producer/farm_logic.py` | `ensure_farm_node` : les branches erreur/no-farm-id posent désormais `status`/`response_strategy`/`final_response` (elles ne le faisaient pas) | P0-2 |
| `core/graph_builder.py` | Edge fixe `ensure_farm_node→confirmation_gate` remplacé par `_route_after_farm_guard` ; edge fixe `mcp_tool_executor→procurement_finalizer→sales_finalizer→response_strategy` remplacé par `_route_after_mcp_executor` ; label `"to_strategy"`→`"to_ui"` (context_resolver) ; import `_route_after_executor` retiré | P0-2, P2-1, P2-2, P2-3 |
| `nodes/routing.py` | +`_route_after_farm_guard`, +`_route_after_mcp_executor` ; `_route_after_resolver` : `"to_strategy"`→`"to_ui"` ; `_route_after_executor` supprimée | P0-2, P2-1, P2-2, P2-3 |
| `core/router.py` | `DomainRouter.decide()` : garde `status=="COMPLETED"` déplacée AVANT la boucle de règles (priorité absolue) | P1-2 |
| `nodes/memory.py` | Relais `slot_enrichment_force_clarification`/`clarification_reasons` LLM→state ; suppression de la branche racine morte de `menu_snapshot_id` | P1-3, P1-4 |
| `nodes/cleanup.py` | +6 champs EPHEMERAL dans `_EPHEMERAL_REPLACE_FIELDS` ; −`should_replan` | P1-3, P1-4, P1-1 |
| `nodes/cleaner.py` | +reset `farm_creation_attempted` en fin de goal ; suppression de `_trim_history`/`_MAX_CHAT_HISTORY` (no-op mort) | P0-2 corollaire, P1-4 |
| `nodes/cognitive.py` | Docstring de décision P1-1 ; `should_replan` retiré des 2 patchs ; `forced_role` retiré (P1-5) ; `stale_wm_keys` littéral remplacé par l'union des `.KEYS` typés | P1-1, P1-5, P2-4 |
| `interpreter/routing.py` | `{**state,"forced_role":...}` retiré (P1-5) | P1-5 |
| `nodes/semantic_disambiguation.py` | Lecture `forced_role` retirée | P1-5 |
| `adapter.py` | `"should_replan": False` retiré de l'état initial | P1-1 |
| `flows/producer/contexts.py` **(nouveau)** | `BidWorkflowState`, `ProducerUpdateWorkflowState` — contrats typés | P2-4 |
| `flows/buyer/contexts.py` | +`WinnerGpsWorkflowState` | P2-4 |
| `tests/nodes/test_cognitive_guard_and_orchestrator.py` | 3 assertions `should_replan` retirées (couverture `next_step` intacte) | P1-1 |
| `tests/architecture/test_catalog_and_graph.py` | `declared` : retrait `_route_after_executor`, mise à jour `_route_after_resolver`, ajout `_route_after_farm_guard` et `_route_after_mcp_executor` | P0-2, P2-1, P2-2, P2-3 |

### Fichiers de test créés

`test_compiled_graph_channel_survival.py`, `test_ensure_farm_node_routing.py`,
`test_slot_enrichment_clarification_relay.py`,
`test_validator_terminal_state_stops_the_flow.py`,
`test_cognitive_decisions_are_consumed_or_removed.py`,
`test_p1_4_remaining_undeclared_channels.py`,
`test_mcp_executor_finalizer_routing.py`,
`test_p2_4_typed_working_memory_workflows.py`,
`test_fastpath_normal_path_equivalence.py`,
`test_e2e_compiled_finalizer_pipeline.py` — 10 fichiers, ~75 tests.

---

## B. Problèmes corrigés (avant → après)

### P0 — Bloquants

**P0-1 — `sales_publish_draft` non déclaré comme canal LangGraph**
- Avant : écrit par `sales_confirmation.py`, jamais dans `MarketAgentState` → filtré silencieusement à CHAQUE superstep compilé. Un draft SALES confirmé disparaissait avant `mcp_tool_executor`.
- Après : déclaré `Annotated[Optional[Dict], replace_value]` dans `core/state.py` + `FieldSpec(DURABLE)` dans `state_profile.py`. Preuve : `test_compiled_graph_channel_survival.py` — traversée `ainvoke` + reprise sur checkpoint, symétrique à `procurement_draft`/`preorder_draft`.

**P0-2 — `ensure_farm_node → confirmation_gate` edge fixe**
- Avant : les 6 issues du nœud (no-op / succès / WAITING_INPUT sélection / READ sans ferme / refus / erreur) convergeaient TOUTES vers `confirmation_gate`, y compris un menu de sélection multi-fermes non résolu — un menu ouvert par `ensure_farm_node` pouvait être immédiatement écrasé.
- Après : `_route_after_farm_guard` discrimine sur `final_response` (le nœud a lui-même conclu le tour) et `working_memory.available_mapping_kind=="farm"` (menu multi-fermes) → `to_confirmation` / `to_ui` / `to_response`. Les 2 branches d'erreur/no-farm-id, auparavant silencieuses, posent désormais `status=WAITING_INPUT` + réponse. Preuve : `test_ensure_farm_node_routing.py` (12 tests, dont un sous-graphe compilé avec nœuds sentinelles prouvant qu'un menu multi-fermes n'atteint JAMAIS `confirmation_gate`).

### P1 — Contrats d'état

**P1-1 — `should_replan`/`next_step`/`phase` calculés mais non consommés**
- Verdict (jugement motivé, options A/B du mandat) : `next_step`/`phase`/`reason` gardés comme télémétrie diagnostique documentée (le canal `cognitive_decision` partagé EST consommé via `action`, par `strategy.py`/`clarification.py` — pas mort) ; `should_replan` (booléen dérivé, zéro lecteur prouvé) supprimé.
- Après : `should_replan` retiré de `state.py`, `state_profile.py`, `cognitive.py` (2 sites), `adapter.py`, tests associés. Preuve : `test_cognitive_decisions_are_consumed_or_removed.py` (AST-based, évite les faux positifs de commentaires).

**P1-2 — `validator` COMPLETED pouvait retomber en traitement métier**
- Avant : `DomainRouter.decide()` évaluait d'abord les `RouteRule` spécifiques par goal, la garde `status in {ERROR,WAITING_INPUT}` (pas COMPLETED) arrivant après.
- Après : garde `status=="COMPLETED"` en tout premier, avant la boucle de règles — priorité absolue. Preuve : `test_validator_terminal_state_stops_the_flow.py` (4 tests, dont un balayage exhaustif de `_rules` prouvant que COMPLETED gagne contre toute règle).

**P1-3 — `slot_enrichment_force_clarification` écrit/lu à des adresses différentes**
- Avant : `enrich_payload_from_text` le posait dans le payload LLM ; le lecteur attendait `state.get(...)` — jamais relié.
- Après : `memory.py` relaie explicitement le flag (et `clarification_reasons`) du payload vers l'état. Champs déclarés `EPHEMERAL`. Preuve : `test_slot_enrichment_clarification_relay.py` (via le vrai déclencheur `SlotValidationError`, goal `BUYER_ADD_TO_CART`).

**P1-4 — Audit WRITE/READ/DECLARATION/LIFECYCLE**
- 9 champs traités : `sales_publish_draft` (P0-1), `farm_creation_attempted`/`auto_farm_notice`/`error_creating_farm` (DECLARE, `ProducerContext`), `blocked_user_query`/`error_message`/`technical_details` (DECLARE, EPHEMERAL), `menu_snapshot_id` (REMOVE — branche racine morte), `chat_history` (REMOVE — troncature no-op permanente, aucun écrivain réel trouvé).
- Découverte non mandatée : `state_profile.py` se déclare "source unique" mais `nodes/cleanup.py` maintient sa PROPRE liste `_EPHEMERAL_REPLACE_FIELDS`/`_EPHEMERAL_MERGE_DICT_FIELDS`, réellement exécutée — un champ `EPHEMERAL` dans `state_profile.py` seul n'est PAS réinitialisé sans y être AUSSI ajouté. Chaque champ ajouté ici l'a été aux deux endroits.
- Preuve : `test_p1_4_remaining_undeclared_channels.py` (8 tests).

**P1-5 — `forced_role` fantôme**
- Avant : `interpreter/routing.py` injectait `{**state,"forced_role":role_up}` dans un dict LOCAL jamais retourné comme patch de nœud — donc jamais un canal d'état réel. `cognitive.py`/`semantic_disambiguation.py` le lisaient quand même (`state.get("forced_role") or state.get("user_role")`, branche gauche structurellement morte).
- Après : code mort supprimé des 3 sites (aucun remplacement — rien à déclarer, il n'a jamais existé comme canal).

### P2 — Lisibilité et modélisation

**P2-1 — Labels de route trompeurs**
- `context_resolver`/`_route_after_resolver` : `"to_strategy"` (menait en réalité à `ui_engine`, pas `response_strategy`) → `"to_ui"`.

**P2-2 — Conditional edge à sortie unique**
- `_route_after_executor` ne retournait qu'une seule valeur possible → supprimée, remplacée par un `add_edge` direct (devenu obsolète à son tour par P2-3, voir ci-dessous).

**P2-3 — Chaînage no-op des finalizers**
- Avant : `mcp_tool_executor → procurement_execution_finalizer → sales_execution_finalizer → response_strategy`, chaque finalizer se protégeant par son PROPRE garde interne (`draft absent → {}`).
- Après : `_route_after_mcp_executor` sélectionne RÉELLEMENT le finalizer sur la présence de `procurement_draft`/`sales_publish_draft` (signal prouvé stable — `mcp_tool_executor` ne les écrit jamais) ; les deux finalizers convergent directement vers `response_strategy`. Preuve : `test_mcp_executor_finalizer_routing.py` (23 tests, unitaire + sous-graphe compilé) + `test_e2e_compiled_finalizer_pipeline.py` (2 tests, VRAIS nœuds `mcp_tool_executor`/finalizers via `ainvoke`).

**P2-4 — `working_memory` non typé**
- 3 mini machines à états identifiées comme réellement auto-suffisantes (pas de bénéfice de sous-graphe démontré) : `bid_phase` (producteur, enchères), `update_phase` (producteur, mise à jour), `winner_gps_stage` (acheteur, étape GPS gagnant).
- Après : contrats typés `BidWorkflowState`/`ProducerUpdateWorkflowState` (nouveau `flows/producer/contexts.py`) et `WinnerGpsWorkflowState` (`flows/buyer/contexts.py`), chacun avec `.KEYS` déclaré + `.phase`/`.is_active`. `nodes/cognitive.py` (nettoyage à l'abandon de tunnel) importe désormais ces `.KEYS` au lieu d'une liste littérale recopiée à la main — comportement de nettoyage STRICTEMENT inchangé (test de non-régression dédié). Les sites d'écriture existants (`auctions.py`, `flow.py`, `order_tracking.py`) n'ont pas été touchés (aucun risque introduit).
- Preuve : `test_p2_4_typed_working_memory_workflows.py` (9 tests, dont un test comportemental reproduisant l'incident +22601479800).

### Durcissement FastPath (Phase 5)

- `retry_count` : n'est ni lu ni écrit dans `interpreter/routing.py` (FastPath ou LLM) — géré exclusivement en aval par `cognitive_guard`, sans distinction de source. Pas de 2e mécanisme à dupliquer.
- Goal-locking : validé UNIQUEMENT sur le chemin LLM (`_is_different_goal`, ~ligne 1486) — chaque branche FastPath renvoie `detected_intent=locked_goal` verbatim, structurellement incapable de dériver. Non dupliqué, preuve AST.
- Équivalence métier (Test J) : cas de repli documenté ("nombre nu ambigu pendant PRICE", `skip_numeric_shortcut`) — FastPath (LLM indisponible) et LLM (disponible) produisent le même `interpreted_event`/`detected_intent`/`price` pour la même entrée.
- Preuve : `test_fastpath_normal_path_equivalence.py` (3 tests).

---

## C. Nouvelle topologie réelle du graphe (post-correction)

Extraite directement des `add_edge`/`add_conditional_edges` de `core/graph_builder.py` — pas un schéma théorique.

```
role_guard → input_normalizer → security_moderation
  --[_route_after_security]--> {to_interpreter: input_interpreter, to_strategy: response_strategy}

input_interpreter → cognitive_guard → cognitive_orchestrator
  --[_route_after_cognitive]--> {to_clarification: clarification_node, to_onboarding: goal_planner (fast-path)}

clarification_node --[_route_after_clarification]--> {to_disambiguation: semantic_disambiguation, to_strategy: response_strategy}
semantic_disambiguation --[_route_after_disambiguation]--> {to_planner: goal_planner, to_strategy: response_strategy}
goal_planner --[_route_after_planner]--> {to_memory: memory_update, to_strategy: response_strategy}

memory_update → validator
  --[route_after_validator = DomainRouter.decide]--> {cart_management | negotiation_gate | order_tracking_node
                                                        | to_confirmation: context_resolver | to_farm_guard: ensure_farm_node
                                                        | to_resolver: context_resolver | to_strategy: response_strategy}

context_resolver --[_route_after_resolver]--> {to_confirmation: confirmation_gate, to_farm_guard: ensure_farm_node, to_ui: ui_engine}
                                                       (P2-1 : "to_strategy" → "to_ui")

ensure_farm_node --[_route_after_farm_guard]--> {to_confirmation: confirmation_gate, to_ui: ui_engine, to_response: response_strategy}
                                                       (P0-2 : remplace l'edge fixe)

confirmation_gate --[_route_after_confirmation]--> {to_executor: mcp_tool_executor, to_strategy: response_strategy}

mcp_tool_executor --[_route_after_mcp_executor]--> {to_procurement_finalizer: procurement_execution_finalizer,
                                                      to_sales_finalizer: sales_execution_finalizer,
                                                      to_response: response_strategy}
                                                       (P2-3 : remplace le chaînage no-op)
procurement_execution_finalizer → response_strategy
sales_execution_finalizer → response_strategy

cart_management / negotiation_gate / order_tracking_node → context_resolver  (tunnels acheteur spécialisés, inchangés)

response_strategy → state_cleaner → final_response → post_response_cleanup → END
```

---

## D. Matrice de transition (post-correction)

| Nœud | Sorties possibles | Condition | Destination | État produit | Terminal ? |
|---|---|---|---|---|---|
| `security_moderation` | to_interpreter / to_strategy | `security_status` bloquant OU `status=BLOCKED`+`final_response` | interpreter / response_strategy | — | Non / Oui |
| `cognitive_orchestrator` | to_clarification / to_onboarding | onboarding actif | clarification / goal_planner | `cognitive_decision` (télémétrie) | Non |
| `validator` (`DomainRouter.decide`) | tunnels / to_confirmation / to_farm_guard / to_resolver / to_strategy | goal, `status`, règles métier | selon table | statut validé | Variable (COMPLETED → to_strategy garanti, P1-2) |
| `context_resolver` | to_confirmation / to_farm_guard / to_ui | `status`, besoin de ferme | confirmation_gate / ensure_farm_node / ui_engine | — | Non |
| `ensure_farm_node` | to_confirmation / to_ui / to_response | `final_response` posé, `available_mapping_kind` | confirmation_gate / ui_engine / response_strategy | ferme résolue / menu / erreur | Variable (P0-2) |
| `confirmation_gate` | to_executor / to_strategy | `status=EXECUTING` | mcp_tool_executor / response_strategy | draft EXECUTING, `execution_authorized` | Non |
| `mcp_tool_executor` | to_procurement_finalizer / to_sales_finalizer / to_response | présence `procurement_draft` / `sales_publish_draft` | finalizer concerné / response_strategy | résultat MCP brut | Non (P2-3) |
| `procurement_execution_finalizer` | (edge fixe) | draft `EXECUTING` sinon no-op | response_strategy | draft EXECUTED/FAILED/EXECUTION_UNKNOWN | Oui |
| `sales_execution_finalizer` | (edge fixe) | draft `EXECUTING` sinon no-op | response_strategy | draft PUBLISHED/FAILED/EXECUTION_UNKNOWN | Oui |

---

## E. Tests (objectif → résultat)

| Test | Objectif | Résultat |
|---|---|---|
| `test_compiled_graph_channel_survival.py` | P0-1 : `sales_publish_draft` survit `ainvoke`+checkpoint | ✅ 8/8 |
| `test_ensure_farm_node_routing.py` | P0-2 : 6 issues de `ensure_farm_node` routées correctement, menu jamais écrasé | ✅ 12/12 |
| `test_slot_enrichment_clarification_relay.py` | P1-3 : flag relayé LLM→state, déclenche vraiment la clarification | ✅ 3/3 |
| `test_validator_terminal_state_stops_the_flow.py` | P1-2 : COMPLETED ne retombe jamais en traitement métier | ✅ 4/4 |
| `test_cognitive_decisions_are_consumed_or_removed.py` | P1-1 : `next_step`/`phase` non lus ailleurs (AST), `action` bien consommé | ✅ 3/3 |
| `test_p1_4_remaining_undeclared_channels.py` | P1-4 : survie+reset des 3 champs forensiques, suppression branches mortes | ✅ 8/8 |
| `test_mcp_executor_finalizer_routing.py` | P2-3 (Test K) : sélection correcte du finalizer, unitaire + sous-graphe | ✅ 23/23 |
| `test_p2_4_typed_working_memory_workflows.py` | P2-4 : contrats typés cohérents, nettoyage tunnel inchangé | ✅ 9/9 |
| `test_fastpath_normal_path_equivalence.py` | Phase 5 (Test J) : FastPath ≡ LLM pour la même entrée, retry/goal-lock non dupliqués | ✅ 3/3 |
| `test_e2e_compiled_finalizer_pipeline.py` | Phase 7 : segment `mcp_tool_executor→finalizer→response_strategy` via VRAI `ainvoke` | ✅ 2/2 |

Tests existants ajustés (aucune perte de couverture) :
`test_cognitive_guard_and_orchestrator.py` (3 assertions `should_replan` retirées),
`test_catalog_and_graph.py` (`declared` mis à jour, 2 nouvelles entrées ajoutées — closes une lacune de couverture P0-2 découverte pendant P2-2).

---

## F. Résultats d'exécution

Suite complète (`pytest tests/ -q`, hors 2 exclusions pré-existantes documentées
plus bas), exécutée après CHAQUE modification significative de ce mandat :

| Étape | FAILED | ERROR | Exit code |
|---|---|---|---|
| Après P2-1/P2-2 | 0 | 0 | 0 |
| Après P2-3 | 0 | 0 | 0 |
| Après P2-4 | 0 | 0 | 0 |
| Après Phase 5 (FastPath) | 0 | 0 | 0 |
| Après Phase 7 (E2E compilé) | 0 | 0 | 0 |

Répartition par catégorie (dernière exécution complète) :
- Tests d'architecture (dossier `tests/architecture/`, incluant les 10 nouveaux
  fichiers de ce mandat) : verts.
- Tests de graphe compilé (`test_catalog_and_graph.py::TestGraphWiring`,
  `test_e2e_compiled_finalizer_pipeline.py`, sous-graphes des fichiers P0-2/P2-3) : verts.
- Tests producteur (`tests/nodes/`, `flows/producer/` via `tests/architecture/`) : verts.
- Tests acheteur (`flows/buyer/` via `tests/architecture/`, `tests/integration/`) : verts.
- Tests checkpoint (`tests/integration/test_checkpointer_state_machine.py`,
  `test_compiled_graph_channel_survival.py`) : verts.
- Suite de régression complète : verte, seules 2 exclusions PRÉ-EXISTANTES
  (non liées à ce mandat, déjà identifiées avant son démarrage) :
  `test_create_auction_catalog_gate.py` (fixtures de date obsolètes) et
  `test_order_mutations_require_ownership.py` (faux positifs de grep) —
  ni l'une ni l'autre n'a été modifiée ni touchée par ce mandat.

Environ 75 nouveaux tests ajoutés, 0 test retiré (seules des assertions
devenues sans objet — `should_replan` — ont été retirées, sans perte de
couverture fonctionnelle, la couverture équivalente via `next_step` restant
en place).

---

## G. Risques restants

1. **Section 18 (mandat) — couverture partielle des Tests A/H/I/L génériques.**
   Ce mandat a livré des tests CIBLÉS pour chaque bug P0/P1/P2 identifié par
   l'audit (couvrant de fait une bonne partie de B/D/E/F/G/K), plus une
   extension du Test C existant (`test_catalog_and_graph.py`) et H
   (`TestGraphWiring`, déjà présent). Il n'existe PAS encore de Test A
   générique (« toute clé retournée par TOUT nœud ⊆ `MarketAgentState` »)
   ni de Test I générique (« tout goal WRITE a une confirmation démontrable »)
   balayant l'ensemble du catalogue d'intentions plutôt que les champs déjà
   identifiés comme problématiques. Risque : un futur nœud pourrait
   réintroduire la classe de bug P0-1/P1-4 sur un champ non encore audité,
   sans qu'un test générique ne le détecte automatiquement.

2. **Phase 7 — scénarios de bout en bout non couverts par un NOUVEAU test
   compilé dans cette passe** : variante multi-fermes de la publication
   producteur, parcours panier acheteur complet (recherche→vendeur→
   palier→quantité), négociation, interruption en plein tunnel, panne LLM
   Gateway. Ces scénarios sont couverts FONCTIONNELLEMENT par la suite
   existante (`tests/interpreter/`, `tests/unit/test_clarification_node.py`,
   `tests/integration/test_llm_gateway_provider_config_incident_2026_09_05.py`,
   `run_manual_smoke_tests()`) mais pas via un `ainvoke()` de bout en bout
   partant du texte utilisateur brut à travers TOUT le graphe — cela
   nécessiterait de simuler fidèlement le LLM Gateway complet
   (interpréteur + planner + validator), jugé hors de portée raisonnable de
   cette passe au vu du mandat "pas de refonte massive". Le segment le
   PLUS à risque (post-confirmation → exécution → finalisation, siège de
   P0-1/P2-3) EST couvert par un vrai `ainvoke()`
   (`test_e2e_compiled_finalizer_pipeline.py`).

3. **Section 20 — pilote `BuyerCartGraph`** : non évalué dans cette passe
   (dépendait de la stabilisation P0-P2 d'abord, mandat explicite). Reste à
   faire dans une passe ultérieure dédiée, si le besoin est démontré.

4. **`state_profile.py` n'est PAS la source unique qu'il prétend être**
   (découverte de ce mandat, non un bug introduit par lui) : tout nouveau
   champ `EPHEMERAL` doit être ajouté à la fois à `state_profile.py` ET à
   `nodes/cleanup.py::_EPHEMERAL_REPLACE_FIELDS`/`_EPHEMERAL_MERGE_DICT_FIELDS`
   pour que son cycle de vie documenté soit réellement appliqué — aucune
   garde automatique ne détecte un oubli de l'un des deux aujourd'hui.

Aucun autre risque connu n'a été identifié dans le périmètre traité
(P0/P1/P2/Phase 5/segment critique de Phase 7).
