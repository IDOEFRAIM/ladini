# REVUE ARCHITECTURALE — GRAPHE `MARKET COACH`

_Audit du 2026-09-08. Environnement vérifié : **LangGraph 1.0.2**, langgraph-checkpoint 3.0.1, langchain-core 1.2.14. Aucun code modifié dans le cadre de cette revue (phase AUDIT uniquement, conformément à la consigne §17)._

> **Méthode** — Toutes les affirmations ci-dessous sont soit tirées du code lu intégralement, soit **prouvées empiriquement** par exécution (les scripts de preuve sont reproduits dans le rapport), soit sourcées sur la documentation officielle LangGraph. Aucune conclusion n'est tirée d'un commentaire de code sans vérification du comportement réel.

---

# 1. VERDICT EXÉCUTIF

## `SOLIDE` — avec 3 défauts structurels dont 2 provoquent des pannes silencieuses en production aujourd'hui

**Ce n'est pas « excellent »**, et ce n'est **pas** « problématique » non plus. La distinction est importante :

- **Le découpage conceptuel est juste.** La séparation interpréteur → cognitif → planificateur → validateur → routeur de domaine → flow métier → confirmation → exécution → rendu est une décomposition correcte et rare pour ce type d'agent. Elle n'a pas besoin d'être repensée.
- **La discipline anti-dérive est supérieure à la moyenne.** `core/goals.py`, `core/slots.py`, `core/pending_interaction.py`, `INTENT_CONFIG` : sources uniques dérivées, avec validation à l'import qui fait échouer fort en cas de divergence. C'est un actif rare, à préserver absolument.
- **Mais la topologie du graphe sous-représente l'espace de décision réel.** Plusieurs nœuds produisent des issues plus riches que ce que leurs edges savent exprimer, et — plus grave — **des données d'état critiques ne franchissent pas la frontière entre nœuds**, parce qu'elles ne sont pas déclarées comme canaux. Résultat : des correctifs corrects au niveau du nœud sont **annulés silencieusement** par la plomberie du graphe.

**La phrase qui résume l'audit :**

> Le problème du graphe Market Coach n'est pas qu'il soit mal conçu, c'est qu'**il ment**. Trois nœuds prennent des décisions que le graphe ne peut pas exprimer, et neuf clés d'état sont écrites par des nœuds sans jamais atteindre leur lecteur. Le code a raison, la topologie a tort, et rien ne le signale.

Cela répond directement à votre question de départ (« on a rencontré trop de problèmes ») : **une partie de ces problèmes ne vient pas de la logique métier, mais du contrat nœud↔graphe↔état qui n'est vérifié par rien.**

---

# 2. CE QU'IL FAUT CONSERVER ABSOLUMENT

Liste courte et volontairement sélective — uniquement ce qui est réellement solide et qu'une refonte détruirait :

| Élément | Pourquoi c'est solide |
|---|---|
| **`PendingInteraction` comme source canonique unique** (`core/pending_interaction.py`) | Résout par construction (résolveur, pas conteneur) la classe de bug « signal d'attente périmé ». Le fait qu'il **dérive** le tunnel panier à neuf de `vendor_selection_context`/`tier_selection_context` au lieu de le stocker est la bonne décision — c'est ce qui le rend non-périmable. À ne pas toucher. |
| **Les drafts transactionnels versionnés** (`ProcurementDraft`/`PreorderDraft`/`SalesPublishDraft`) | Machine à états explicite, immuable, versionnée, avec CAS Postgres et clé d'idempotence. C'est le meilleur morceau d'architecture du repo, et le modèle à généraliser (voir §10). Refuser l'abstraction prématurée entre les 3 était le bon appel. |
| **Les sources uniques dérivées + validation à l'import** (`core/goals.py::_validate_goal_drift`, `core/base.py::validate_config_drift`) | Transforme une classe entière de bugs de configuration en échec au démarrage. À étendre (§9), jamais à retirer. |
| **`INTENT_CONFIG` comme catalogue déclaratif** | Un intent = une ligne de config + un handler. Ajout d'un domaine métier sans toucher au graphe. C'est ce qui rend le système extensible aujourd'hui. |
| **Le LLM Gateway** (profils, disjoncteur partagé, pré-check, raisons structurées) | Correctement isolé du graphe : aucun nœud métier ne connaît un nom de modèle. Rien à changer. |
| **La séparation `state_cleaner` (avant rendu) / `post_response_cleanup` (après rendu)** | Subtile mais correcte — et la raison est documentée (l'orchestrateur lit l'état retourné par le dernier nœud). Un refactor naïf casserait l'envoi WhatsApp. |
| **`_safe_node`** comme wrapper universel | Point d'observabilité unique (`NODE_START`/`NODE_END`/`NODE_ERROR`) + isolation des exceptions. À garder, mais voir P1-4 (il perd le motif d'erreur). |

---

# 3. CE QUI DOIT ÊTRE CORRIGÉ

## P0 — Pannes silencieuses en production, prouvées

### P0-1. `sales_publish_draft` n'est pas un canal déclaré → toute la machine à états SALES est morte

**Preuve exécutée** (graphe réel, `MarketAgentState` réel, LangGraph 1.0.2) :

```python
declared[procurement_draft]   = True
declared[preorder_draft]      = True
declared[sales_publish_draft] = False

# Après un tour de graphe RÉEL :
#   procurement_draft   -> {'draft_id': 'P1', 'version': 1}
#   sales_publish_draft -> None        ← silencieusement supprimé
```

LangGraph **supprime sans erreur ni avertissement** toute clé absente du schéma d'état (vérifié : aucune exception levée, la clé disparaît simplement du state retourné).

**Chaîne de conséquences :**

1. `nodes/confirmation_gate.py:274` retourne `{"sales_publish_draft": candidate.to_dict(), ...}` → **droppé**.
2. `nodes/confirmation_gate.py:232` teste `if state.get("sales_publish_draft") is not None:` → **toujours faux**.
3. Donc `flows/producer/sales_confirmation.py::resolve_sales_confirmation` — l'unique autorité de mutation du draft — **n'est jamais atteinte depuis le graphe**.
4. Chaque tour sur `SALES_PUBLISH_PRODUCT` avec champs complets **re-crée un draft v1** (nouveau `draft_id`, **nouvelle ligne insérée dans `marketplace.sales_publish_drafts`**) et repose la même question « Confirmez-vous ? ».
5. Le `CONFIRM_ACTION.target` (draft_id + version) posé au tour N ne peut jamais correspondre au draft du tour N+1 → l'invariant `check_confirmation_target_invariant` ne peut pas être satisfait.
6. `flows/producer/sales_execution_finalizer.py:54` lit `SalesPublishDraft.from_dict(state.get("sales_publish_draft"))` → toujours `None`.

**Pourquoi les tests ne l'ont pas vu** : la quasi-totalité des tests (et le harnais `graph_builder.py::_run_producer_write_flow`) appellent les nœuds **directement** en Python et font `state.update(patch)` sur un `dict` ordinaire — les clés non déclarées y survivent. Le filtrage n'existe que dans le graphe compilé. **Aucun test ne traverse le graphe compilé pour ce chemin.**

**Correctif** : déclarer `sales_publish_draft` dans `core/state.py` (aux côtés de `procurement_draft`/`preorder_draft`, même reducer `replace_value`) **et** dans `core/state_profile.py` (`DURABLE`, même justification). C'est exactement le bug des « 3 registres à synchroniser » déjà documenté pour `tier_selection_context` (`state_profile.py:199`) — la 3ᵉ récidive de la même classe.

### P0-2. `ensure_farm_node → confirmation_gate` : un edge fixe détruit une décision utilisateur

`ensure_farm_node` (`flows/producer/farm_logic.py`) a **6 issues distinctes** :

| Issue | Ce que le nœud produit |
|---|---|
| no-op | `{}` (goal non farm-critical / farm_id déjà là / pas de téléphone) |
| **multi-fermes** | `status=WAITING_INPUT` + `SELECTION_MENU` + `final_response` + `available_mapping` |
| **READ sans ferme** | `status=WAITING_INPUT` + `ASK_CLARIFICATION` + `final_response` |
| **auto-provisioning refusé** | `status=WAITING_INPUT` + `ASK_CLARIFICATION` + `final_response` |
| échec création | `error_creating_farm=True` |
| succès | `transaction_payload.farm_id` + `stable_entities.farm_id` |

Le graphe déclare : `workflow.add_edge("ensure_farm_node", "confirmation_gate")` — **inconditionnel**.

Donc pour les 3 issues `WAITING_INPUT`, `confirmation_gate` s'exécute immédiatement après et **écrase** `final_response`, `response_strategy` et `pending_interaction` :
- goal `SALES_PUBLISH_PRODUCT` (draft-based) → bootstrap d'un draft + « Confirmez-vous ? » → **le menu « sur quelle exploitation ? » n'atteint jamais l'utilisateur** ;
- goal de LECTURE (`_READ_GOALS`) → `confirmation_gate` retourne `execution_authorized=True, status=EXECUTING` → l'exécuteur part **sans le `farm_id` qu'on venait de déclarer manquant**.

**Ironie documentée** : `flows/producer/faille.md` décrit précisément la faille « `ensure_farm_node` sélectionne arbitrairement la première ferme » comme un risque connu. Le correctif a bien été écrit dans le nœud (branche multi-fermes, `farm_logic.py:94-113`) — **mais la topologie l'annule**. Et le filet ultime de `nodes/executor.py::_auto_provision_farm_if_needed` (`get_or_create_farm`) re-choisit une ferme silencieusement. **La faille est donc toujours ouverte, malgré son correctif présent dans le code.**

**Correctif** : remplacer l'edge fixe par une décision explicite (conditionnelle ou `Command`, voir §6).

## P1 — Décisions inexprimables, états terminaux non terminaux, mécanismes de sécurité morts

### P1-1. `cognitive_orchestrator` : nœud décisionnel dont aucune décision n'est consommée

Il calcule `cognitive_decision.next_step ∈ {continue, respond, replan, clarify, continue_tunnel}` et `should_replan`. Recherche exhaustive dans tout `src/` :

- `should_replan` : écrit par `cognitive.py`, initialisé par `adapter.py`, remis à `False` par `cleanup.py`, déclaré dans `state.py`/`state_profile.py` — **jamais lu pour prendre une décision**.
- `next_step` : écrit dans `cognitive_decision`, **jamais lu** (les lecteurs de `cognitive_decision` ne lisent que la clé `action`, produite par `cognitive_**guard**`, un autre nœud).

Et l'unique edge conditionnel qui le suit (`_route_after_cognitive`) ne lit **pas** sa sortie : il lit `state["is_onboarding"]`, un drapeau posé par `input_normalizer` bien en amont.

**Conclusion** : `cognitive_orchestrator` est aujourd'hui de la **télémétrie décorative**. Deux options honnêtes : (a) l'assumer comme tel et le renommer/fusionner ; (b) matérialiser réellement ses 4 décisions en transitions. **Ne pas laisser un nœud « décisionnel » sans décision** — c'est précisément ce qui rend le graphe illisible (on croit qu'une décision est prise là où rien ne se passe).

### P1-2. `validator` peut produire un état terminal que la topologie traite comme nominal

Branche `blocking_technical_id` (`nodes/validation.py:367-397`) : le validateur détecte qu'un champ `*_id` manque sans résolveur, produit un **message utilisateur final** et pose `status="COMPLETED"`, `response_strategy="CLARIFICATION"`, `missing_fields=[]`, `pending_interaction=None`.

Parcours réel de cette sortie :
1. `DomainRouter.decide` : le goal (ex. `STOCK_UPDATE_LEVEL`) n'appartient à aucune `RouteRule` ; `status="COMPLETED"` n'est **pas** dans `{ERROR, WAITING_INPUT}` → repli sur `make_route_after_validator`.
2. `route_after_validator` : pas de `CONFIRM_ACTION`, event ≠ UNKNOWN, `missing_fields` vide, status ≠ WAITING_INPUT → **« 4. FLUX NOMINAL » → `to_resolver`**.
3. `context_resolver` s'exécute donc **sur un tour déjà conclu** — avec de possibles appels MCP — et peut écraser le `final_response` du validateur.

Un état terminal doit être terminal dans la topologie, pas seulement dans l'intention du code.

### P1-3. Le garde-fou de plus haute priorité de `response_strategy` est adressé à la mauvaise adresse

`interpreter/strategy.py:42` (tout premier test de la fonction, avant même la sécurité) :

```python
if state.get("slot_enrichment_force_clarification"):
```

Mais l'écrivain, `services/domain/slot_enrichment.py:394`, écrit :

```python
payload["slot_enrichment_force_clarification"] = True    # ← dans transaction_payload
```

Deux adresses différentes. **Ce mécanisme de sécurité n'a jamais pu se déclencher.** (Et le déclarer comme canal ne suffirait pas : c'est une incohérence écrivain/lecteur, pas seulement un canal manquant.)

### P1-4. 9 clés d'état écrites par des nœuds n'atteignent jamais leur lecteur

Audit AST automatisé (script reproductible, `scratchpad/undeclared_keys_audit.py`) croisé avec les 108 canaux déclarés :

| Clé | Écrite par | Lue par | Effet réel |
|---|---|---|---|
| `sales_publish_draft` | `confirmation_gate` | `confirmation_gate`, `sales_confirmation`, `sales_execution_finalizer` | **P0-1** |
| `farm_creation_attempted` | `ensure_farm_node` | `ensure_farm_node` | Garde anti-double-résolution **morte** (toujours falsy) |
| `auto_farm_notice` | `ensure_farm_node`, `executor` | `rendering/success.py` | Message « J'ai configuré votre ferme par défaut » **jamais affiché** |
| `error_creating_farm` | `ensure_farm_node` | `rendering/success.py` | Avertissement d'échec de création **jamais affiché** |
| `error_message` / `technical_details` | `_safe_node` (tous les nœuds) | — | Motif de crash **perdu** (logs seulement) ; l'utilisateur reçoit un message générique |
| `chat_history` | `state_cleaner` | `workspace/checkpointer.py` | Troncature à 4 entrées **inopérante** |
| `blocked_user_query` | `input_normalizer` | — | Trace d'injection de prompt **perdue** |
| `menu_snapshot_id` | `ui_engine` (aussi dans `working_memory`) | `memory.py` (via `or`) | Bénin — la moitié `state` de l'`or` est morte, la moitié `working_memory` fonctionne |

Les 3 premières lignes forment un ensemble cohérent : **tout le mécanisme d'auto-provisioning de ferme est à moitié débranché**, ce qui explique P0-2 aussi bien que des symptômes utilisateur diffus (« l'agent a créé une ferme sans me le dire »).

### P1-5. `forced_role` : un `or` sur un champ qui ne peut jamais exister

`nodes/cognitive.py:89` et `nodes/semantic_disambiguation.py:80` lisent `state.get("forced_role") or state.get("user_role")`. Or `forced_role` n'est écrit qu'ici : `interpreter/routing.py:1204`, dans un **dict local** (`{**state, "forced_role": role_up}`) passé à une fonction synchrone — jamais un canal du graphe. La branche gauche de l'`or` est morte. (Même famille que la redondance `role`/`user_role` déjà purgée aujourd'hui.)

## P2 — Lisibilité et dette de représentation

- **P2-1. Deux edges conditionnels dont les libellés mentent.** `_route_after_resolver` renvoie `"to_strategy"` → mappé sur **`ui_engine`** ; `_route_after_executor` renvoie `"to_strategy"` → mappé sur **`procurement_execution_finalizer`**. Le `path_map` autorise ce découplage, mais ici il rend le graphe illisible : on croit lire une route vers `response_strategy`.
- **P2-2. `_route_after_executor` est un edge conditionnel à une seule issue** — sémantiquement un `add_edge`, avec le coût cognitif d'un branchement.
- **P2-3. Les finalizers chaînés en no-op.** `procurement_execution_finalizer → sales_execution_finalizer → response_strategy` : au plus un des deux agit par tour. C'est un compromis assumé et documenté, mais il ne passe pas à l'échelle (le 3ᵉ domaine transactionnel ajoutera un 3ᵉ no-op inconditionnel).
- **P2-4. Les machines à états des tunnels sont des chaînes dans `working_memory`** (`bid_phase`, `update_phase`, `gps_stage`, `winner_gps_stage`, `preorder_workflow.phase`). Conséquence directe : `cognitive_guard` doit connaître nominativement ces clés internes pour les nettoyer (`stale_wm_keys`) — une fuite d'implémentation des tunnels dans un nœud global.
- **P2-5. Aucun test d'architecture ne vérifie le contrat nœud↔edge↔canal.** C'est la cause racine commune de P0-1, P1-1, P1-3, P1-4 : rien ne pouvait les détecter.

---

# 4. MATRICE COMPLÈTE DES TRANSITIONS

Reconstruite depuis `core/graph_builder.py` (edges) + le corps réel de chaque nœud (issues). `⚠️` = issue réelle non représentée par la topologie.

| # | Nœud | Sorties réelles (états produits) | Condition de routage | Destination(s) réelle(s) | Terminal ? |
|---|---|---|---|---|---|
| 1 | `role_guard` | `{}` ou `{user_role}` | — (edge fixe) | `input_normalizer` | non |
| 2 | `input_normalizer` | nominal / `BLOCKED`+`PROFILE_UNAVAILABLE` / `PROMPT_INJECTION_DETECTED` | — (edge fixe) | `security_moderation` | non ⚠️ (2 issues bloquantes traversent quand même la modération) |
| 3 | `security_moderation` | `SAFE` / `ACCOUNT_BLOCKED` / `PROHIBITED_PRODUCT` / `SCAM_DETECTED` | `_route_after_security` (`security_status ∈ _SECURITY_BLOCKING` ou `BLOCKED`+`final_response`) | `input_interpreter` \| `response_strategy` | oui (branche bloquante) |
| 4 | `input_interpreter` | `NEW_TASK/ANSWER/CONFIRM/REJECT/SELECTION/UPDATE/UNKNOWN/OUT_OF_SCOPE` + `unknown_reason` | `FastPathPolicy.route` (`ANSWER\|SELECTION` × `ALL_BUYER_TUNNEL_GOALS`) | `cognitive_guard` \| `memory_update` | non |
| 5 | `cognitive_guard` | `action ∈ {continue, suspend_current_goal, recover_active_tunnel, abandon_tunnel_max_retries}` | — (edge fixe) | `cognitive_orchestrator` | non ⚠️ (4 décisions, 0 transition) |
| 6 | `cognitive_orchestrator` | `next_step ∈ {continue, respond, replan, clarify, continue_tunnel}`, `should_replan` | `_route_after_cognitive` (lit `is_onboarding`, **pas** la sortie du nœud) | `onboarding_node` \| `clarification_node` | non ⚠️ **P1-1** |
| 7 | `clarification_node` | `{}` / `CLARIFICATION`+`final_response` / repli technique | `_route_after_clarification` (`strategy ∈ {CLARIFICATION, RECOVERY}` + `final_response`) | `semantic_disambiguation` \| `response_strategy` | oui (branche clarification) |
| 8 | `semantic_disambiguation` | `{}` / `DISAMBIGUATION_PENDING`+menu | `_route_after_disambiguation` (goal + `SELECTION_MENU` + strategy) | `goal_planner` \| `response_strategy` | oui (branche menu) |
| 9 | `goal_planner` | goal verrouillé / suspendu / repris / annulé / `WAITING_INPUT` | `_route_after_planner` (`BUYER_CART_GOALS` → memory ; `WAITING_INPUT` → strategy) | `memory_update` \| `response_strategy` | oui (branche WAITING_INPUT) |
| 10 | `memory_update` | payload fusionné, sélections résolues, contextes purgés | — (edge fixe) | `validator` | non |
| 11 | `validator` | `WAITING_INPUT` / `PLANNING` / `PROCESSING` / **`COMPLETED`** (blocking_technical_id) | `DomainRouter.decide` puis repli `route_after_validator` | `cart_management` \| `negotiation_gate` \| `order_tracking_node` \| `context_resolver` \| `confirmation_gate` \| `response_strategy` | ⚠️ **P1-2** (`COMPLETED` → `to_resolver`) |
| 12 | `cart_management` | menu vendeur / menu palier / quantité / ajout / `ERROR` | — (edge fixe) | `ui_engine` | non |
| 13 | `negotiation_gate` | ouverture / offres / contre-offre / acceptation / `ERROR` | — (edge fixe) | `ui_engine` | non |
| 14 | `order_tracking_node` | liste / statut / annulation / étape GPS / `ERROR` | — (edge fixe) | `ui_engine` | non |
| 15 | `context_resolver` | `PLANNING` / `WAITING_INPUT` / `ERROR` / `COMPLETED` / menu | `_route_after_resolver` (`status ∈ {WAITING_INPUT, ERROR, COMPLETED}`) | `ui_engine` (libellé « to_strategy », **P2-1**) \| `ensure_farm_node` \| `confirmation_gate` | oui (3 statuts) |
| 16 | `ui_engine` | `ag_ui_component` + `available_mapping` + snapshot, ou `{}` | — (edge fixe) | `response_strategy` | non |
| 17 | `ensure_farm_node` | **6 issues** (voir P0-2) | **aucune** (edge fixe) | `confirmation_gate` | ⚠️ **P0-2** (3 issues `WAITING_INPUT` écrasées) |
| 18 | `confirmation_gate` | `EXECUTING` / `WAITING_CONFIRMATION` / `WAITING_INPUT` (draft) / abandon / `PLANNING` (reject doux) | `_route_after_confirmation` (`status == EXECUTING`) | `mcp_tool_executor` \| `response_strategy` | oui |
| 19 | `mcp_tool_executor` | `COMPLETED` / `ERROR` / `WAITING_INPUT` / `HUMAN_INTERVENTION` | `_route_after_executor` (**une seule issue**) | `procurement_execution_finalizer` (libellé « to_strategy », **P2-1/P2-2**) | non ⚠️ (`HUMAN_INTERVENTION` n'a aucune route dédiée) |
| 20 | `procurement_execution_finalizer` | transition de draft ou no-op | — (edge fixe) | `sales_execution_finalizer` | non |
| 21 | `sales_execution_finalizer` | transition de draft ou no-op | — (edge fixe) | `response_strategy` | non |
| 22 | `response_strategy` | 9 stratégies | — (edge fixe) | `state_cleaner` | non |
| 23 | `state_cleaner` | purges de fin de tour | — (edge fixe) | `final_response` | non |
| 24 | `final_response` | `final_response` + `ag_ui_component` | — (edge fixe) | `post_response_cleanup` | non |
| 25 | `post_response_cleanup` | resets EPHEMERAL | — (edge fixe) | `END` | **oui** |
| 26 | `onboarding_node` | `ONBOARDING` / `SUCCESS`+`COMPLETED` / `{}` | — (edge fixe) | `response_strategy` | non |

**Points structurels que la matrice révèle :**
- **Un seul état terminal réel** (`post_response_cleanup → END`). Tous les autres « terminaux » sont des convergences vers `response_strategy`. C'est un choix **sain** (un point de rendu unique) — à conserver.
- **3 nœuds décident sans pouvoir l'exprimer** : `ensure_farm_node` (P0-2), `cognitive_guard`/`cognitive_orchestrator` (P1-1), `validator` (P1-2).
- **1 sortie sans route dédiée** : `HUMAN_INTERVENTION` (`executor`) tombe dans le rendu générique.

---

# 5. LES 6 TYPES DE ROUTAGE — LA SÉPARATION EST-ELLE CORRECTE ?

| Type | Où il vit | Verdict |
|---|---|---|
| **A. Compréhension** | `input_interpreter` (+ `InterpreterResult`) | ✅ **Correct.** Point de conversion unique, `UNKNOWN` toujours motivé. Modèle à suivre. |
| **B. Cognitif** | `cognitive_guard` / `cognitive_orchestrator` | ❌ **Cassé.** Le `guard` décide réellement (interruption, abandon, retry) mais **sans transition** ; l'`orchestrator` produit des décisions **sans consommateur**. La couche existe sur le papier, pas dans le graphe. |
| **C. Conversationnel** | `clarification_node`, `semantic_disambiguation`, `goal_planner` | ✅ **Correct** — les 3 ont de vrais edges conditionnels et des court-circuits explicites vers le rendu. |
| **D. Métier** | `validator` → `DomainRouter` | ⚠️ **Presque.** Bonne séparation (readiness ≠ domaine), mais **deux routeurs en cascade** (`DomainRouter.decide` puis repli `route_after_validator`) avec des critères différents, dans deux fichiers. C'est là que P1-2 se cache. |
| **E. Transactionnel** | `confirmation_gate` → `executor` → finalizers | ✅ **Correct sur le principe** (autorisation ≠ exécution ≠ finalisation), ⚠️ dégradé par P2-2/P2-3 et par le fait que **les tunnels buyer/producer le contournent entièrement** (§10). |
| **F. Rendu** | `response_strategy` → `nodes/rendering/*` | ✅ **Excellent.** Point de convergence unique, dispatch par stratégie, `ResponsePlan` matérialise la décision avant le rendu. À ne pas toucher. |

**Défauts transversaux confirmés :**
- **Décisions enfouies dans les nœuds** : oui, 3 cas majeurs (§4).
- **Duplication de routeurs** : oui, un seul cas (D), mais c'est celui qui produit un bug réel.
- **État utilisé comme mécanisme de contrôle implicite** : oui — `working_memory.*_phase` (P2-4) et `is_onboarding` (qui court-circuite une couche entière depuis `input_normalizer`).
- **Conditions dispersées** : les critères de routage post-validateur vivent dans **3 fichiers** (`core/router.py`, `core/tunnel_manager.py` via les guards, `interpreter/routing.py`).
- **Edges trop intelligents** : non, aucun. Les fonctions de routage sont pures et lisent l'état — c'est correct.

---

# 6. `conditional_edges` VS `Command(goto=...)` — POUR **CETTE** ARCHITECTURE

## Ce que dit la documentation

- `add_conditional_edges` **garde le routage hors des nœuds**, dans une fonction dédiée qui décide d'après l'état ([docs](https://docs.langchain.com/oss/python/langgraph/use-graph-api)).
- `Command` sert quand un nœud doit **à la fois** mettre à jour l'état **et** décider de la suite : _« It can be useful to combine control flow (edges) and state updates (nodes) »_ — le cas d'usage explicite est « BOTH perform state updates AND decide which node to go to next in the SAME node » ([docs](https://docs.langchain.com/oss/python/langgraph/use-graph-api)).
- Avec `Command`, **les edges de routage disparaissent du graphe** : le contrôle vit dans le nœud ([how-to](https://langchain-ai.github.io/langgraphjs/how-tos/command/)).

## Le critère de décision pour Market Coach

Le bon critère n'est pas « moderne vs ancien », c'est : **la décision dépend-elle d'une donnée que seul le nœud possède au moment où il s'exécute ?**

| Cas | Décision dérivable de l'état après le nœud ? | Recommandation |
|---|---|---|
| `security_moderation`, `input_interpreter`, `clarification_node`, `semantic_disambiguation`, `goal_planner`, `validator`/`DomainRouter`, `confirmation_gate` | **Oui** — `security_status`, `interpreted_event`, `status`, `pending_interaction` sont tous des faits d'état lisibles | ✅ **Garder `conditional_edges`.** Le routage reste testable en isolation (fonction pure `state → str`), inspectable, et le graphe reste dessinable. |
| **`ensure_farm_node`** | **Non** — la décision (« 0, 1 ou N fermes ? ») dépend d'un appel MCP fait *dans* le nœud ; l'état ne porte le résultat qu'après coup, et l'écrasement par `confirmation_gate` détruit précisément cette information | ✅ **Candidat n°1 à `Command(goto=...)`** — ou, à défaut, un `conditional_edge` sur `status`. Le `Command` exprime mieux l'intention : « j'ai interrogé, je sais, je décide ». |
| `context_resolver` | Frontière — la décision dépend de ce que le résolveur vient d'apprendre, mais elle est **déjà** correctement exposée via `status` | 🟡 Conditionnel suffit. Ne pas changer sans raison. |
| `mcp_tool_executor` → finalizers | Non-décision (une seule issue) | ✅ Remplacer par `add_edge` (P2-2), ou introduire une vraie décision (§13, Archi B). |

**Recommandation nette :** **ne pas migrer massivement vers `Command`.** Dans cette architecture, `conditional_edges` est un **atout** : il rend chaque décision testable indépendamment du nœud (et vos tests exploitent déjà cela). `Command` doit rester **chirurgical** — là où l'edge fixe actuel détruit une décision (`ensure_farm_node`), et éventuellement pour les futurs subgraphs (`Command(graph=Command.PARENT)`).

**Contrainte documentée à connaître** si vous adoptez `Command` depuis un subgraph : _« you MUST define a reducer for the key you're updating in the parent graph state »_ ([docs](https://docs.langchain.com/oss/python/langgraph/use-graph-api)) — ce que votre architecture fait déjà systématiquement.

---

# 7. SOUS-GRAPHES — CE QUI LE MÉRITE VRAIMENT

## Ce que dit la documentation

- Deux modes : **schéma partagé** (subgraph compilé passé directement à `add_node`, lecture/écriture automatique sur les canaux du parent) ou **schéma différent** (invocation dans une fonction avec transformation entrée/sortie) ([docs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)).
- Persistance : par défaut les subgraphs **héritent du checkpointer parent** ; `checkpointer=True` pour accumuler l'état par thread ; `checkpointer=False` pour aucun overhead ([docs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)).
- Cas d'usage recommandés : systèmes multi-agents, **réutilisation d'un ensemble de nœuds**, développement distribué entre équipes ([docs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)).
- ⚠️ Les subgraphs par thread « ne supportent pas les appels d'outils parallèles » et exigent une isolation de namespace par nommage stable ([docs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)).

## Verdict par candidat

| Candidat | Le mérite-t-il ? | Raison |
|---|---|---|
| **`BuyerCartGraph`** (panier + paliers + sélection vendeur) | 🟡 **Oui, mais pas en premier** | C'est le tunnel le plus complexe (1320 lignes) avec une vraie machine à états interne. Un subgraph donnerait un namespace propre à `vendor_selection_context`/`tier_selection_context`. **Mais** le gain réel vient d'abord de typer la machine à états (§10), pas du subgraph. |
| **`ProcurementGraph` / `SalesPublishGraph` / `PreorderGraph`** | ❌ **Non** | Ces domaines ont **déjà** leur machine à états explicite et versionnée (les drafts). Les encapsuler en subgraph ajouterait une couche sans rien résoudre. |
| **`OrderTrackingGraph`** | ❌ **Non** | Essentiellement de la lecture + un mini-gate GPS partagé. Le coût d'interface dépasse le gain. |
| **`ConfirmationGraph`** | ❌ **Non, franchement** | La confirmation est déjà un point unique. En faire un subgraph disperserait le seul garde-fou HITL du système. |
| **`ProducerAuctionGraph`** (bid_phase) | 🟡 **Candidat n°2** | Même profil que le panier : machine à états en chaînes dans `working_memory`, dont `cognitive_guard` doit connaître les clés. |

**Conclusion §7 :** **au plus un subgraph pilote** (`BuyerCartGraph`), et **seulement après** P0/P1 + typage des phases. Faire des subgraphs maintenant encapsulerait les bugs au lieu de les corriger, et compliquerait la relation avec `WorkspaceCheckpointer` (budget 480 Ko, namespaces).

---

# 8. HITL : `confirmation_gate` + drafts VS `interrupt()`

## Ce que dit la documentation (points décisifs)

1. **Au resume, LangGraph redémarre le nœud entier depuis le début** — pas depuis la ligne de l'`interrupt` : _« the runtime restarts the entire node from the beginning—it does not resume from the exact line where interrupt was called »_ ([docs](https://docs.langchain.com/oss/python/langgraph/interrupts)).
2. **Les effets de bord avant un `interrupt` doivent être idempotents** : _« side effects called before interrupt should (ideally) be idempotent... it will be re-run multiple times when the node is resumed »_ ; la doc recommande explicitement `upsert` plutôt que `create` ([docs](https://docs.langchain.com/oss/python/langgraph/interrupts)).
3. **Correspondance des interrupts strictement par index** ; ne jamais sauter conditionnellement un interrupt, ne pas boucler avec une logique non déterministe ([docs](https://docs.langchain.com/oss/python/langgraph/interrupts)).
4. **Avec subgraph, parent ET subgraph redémarrent tous les deux depuis le début du nœud** ([docs](https://docs.langchain.com/oss/python/langgraph/interrupts)).
5. Requiert checkpointer + `thread_id` ; la reprise est un `graph.invoke(Command(resume=...), thread)` ([docs](https://docs.langchain.com/oss/python/langgraph/interrupts)).

## Confrontation avec la réalité WhatsApp de Market Coach

| Critère | `confirmation_gate` + drafts (actuel) | `interrupt()` + resume |
|---|---|---|
| **Nature de la réponse utilisateur** | Texte libre (« oui », « non plutôt 200 tonnes », « c'est quoi le prix du marché ? ») qui **doit** traverser tout le pipeline NLU (interpréteur → memory_update → correction de payload) avant qu'on sache ce que c'est | `Command(resume=<valeur>)` suppose une **valeur structurée** fournie par un client. Ici il faudrait faire tourner l'interprétation **avant** le resume (hors graphe) ou **dans** le nœud interrompu — ce qui ré-exécuterait l'appel LLM à chaque reprise (point 1) |
| **Effets de bord avant la pause** | Insertion Postgres du draft **avant** la question, protégée par CAS + version + clé d'idempotence | Le nœud rejouerait l'insertion à chaque resume → exige de tout rendre `upsert` (point 2). Le modèle draft le permettrait, mais on paierait le risque sans gagner de fonction |
| **Corrections en cours de confirmation** | Gérées nativement : `UPDATE` → nouvelle **version** de draft → nouveau `CONFIRM_ACTION.target` | Un interrupt ne modélise pas « la réponse n'est ni oui ni non mais une correction qui change l'objet à confirmer ». Il faudrait boucler des interrupts — explicitement déconseillé (point 3) |
| **Confirmation périmée / message retardé** | Résolu par `ConfirmationTarget(draft_id, version)` + TTL 600 s + comparaison de payload | À réimplémenter par-dessus l'interrupt |
| **Crash / redémarrage** | L'état canonique est en **Postgres** (draft), pas seulement dans le checkpoint | Dépend entièrement du checkpoint et du mode de durabilité |
| **Concurrence (2 messages rapprochés)** | CAS Postgres tranche | Deux resume concurrents sur le même thread = conflit de checkpoint |
| **Observabilité** | Chaque transition émet un événement transactionnel dédié | Boîte plus opaque |

## Recommandation §8 — argumentée

> **Ne PAS migrer le HITL vers `interrupt()`.** Le modèle actuel (draft versionné + `PendingInteraction.target` + tour suivant réinterprété) est **mieux adapté** à un canal asynchrone multi-tours où la « réponse » est du langage naturel qui doit lui-même être interprété, et où l'objet à confirmer peut **changer** pendant l'attente. `interrupt()` est conçu pour une pause **dans** une exécution dont la reprise fournit une valeur structurée — typiquement une UI d'approbation.

**Nuance utile** : `interrupt()` reste pertinent pour un cas précis que vous n'avez pas encore — une **approbation opérateur/admin** (ex. validation humaine interne d'une transaction à risque, hors conversation utilisateur). Là, la valeur de resume est structurée et le canal est synchrone. À garder en tête, pas à déployer maintenant.

**En revanche, à adopter dès maintenant** : le réglage `durability` de LangGraph 1.x (`"sync"` / `"async"` / `"exit"`). Pour un agent transactionnel WhatsApp, `"sync"` (persistance avant l'étape suivante) est le bon défaut ; `"exit"` (le plus rapide) ne permet **pas** la reprise après crash en cours d'exécution ([docs](https://docs.langchain.com/oss/python/langgraph/durable-execution)). À vérifier explicitement dans votre `compile()`/`ainvoke()`.

---

# 9. LE STATE — TROP CENTRALISÉ ?

**108 canaux déclarés**, classés `DURABLE`/`EPHEMERAL`/`DERIVED`. Verdict nuancé :

**Là où le state global est légitime et doit rester :**
- Identité/session (`user_phone`, `user_role`, `session_id`) — lu partout, une seule vérité.
- `pending_interaction`, `current_goal`, `status` — le cœur de la machine à états conversationnelle ; les distribuer serait une régression.
- `transaction_payload` pendant la **collecte** — c'est le brouillon partagé entre validator/memory/rendering.

**Là où il devient nuisible :**
1. **`working_memory` est un sac fourre-tout** (`merge_dict`, jamais typé) qui héberge : verrous de tunnel, caches de menus, phases de machines à états, compteurs, corrections récentes. C'est le **seul** endroit du système sans contrat. Les commentaires du code y font référence comme source de bugs répétés (clés jamais nettoyées, `merge_dict` qui ne supprime pas). → **Candidat n°1 à un typage explicite**, domaine par domaine.
2. **Trois registres à synchroniser à la main** pour qu'un champ survive : reducer `Annotated`, `BuyerContext`/`ProducerContext`, `state_profile.py`. Déjà 3 récidives connues (`vendor_selection_context`, `tier_selection_context`, et maintenant `sales_publish_draft`/P0-1). → **Doit devenir une vérification automatique** (test d'architecture, §16.9).
3. **Le couplage buyer/producer est faible et bien géré** (héritage `TypedDict` + résolution par goal). **Ce n'est pas un problème.** Ne pas séparer les états par domaine : le bénéfice serait faible et le coût (interfaces, mapping) élevé.
4. **`DomainContext`** (`domain/model.py`) est la bonne idée sous-exploitée : un snapshot immuable et **restreint** passé aux services. Mais il expose des champs qui n'existent pas dans l'état (`tenant`, `organization`, `timezone`, `locale`, `region`) — vestiges d'un template générique. → À **réduire au réel**.

---

# 10. LES TUNNELS BUYER — OPTION A, B, C OU D ?

`cart_management`, `negotiation_gate`, `order_tracking_node` court-circuitent `confirmation_gate` et `mcp_tool_executor` : ils appellent MCP eux-mêmes et gèrent leur propre confirmation.

**Réponse : ni A ni B pures — c'est D (parent + workflows spécialisés), mais mal outillé.**

- **Ce n'est pas de la duplication (B)** : ces tunnels font des choses que le pipeline générique **ne sait pas faire** (menus multi-étapes, sélection vendeur/palier, gate GPS partagé, négociation à plusieurs tours). Les leur faire passer de force dans `validator → confirmation_gate → executor` régresserait l'UX — c'est d'ailleurs exactement ce qu'a montré l'incident documenté sur `BUYER_ADD_TO_CART` (`required` réduit à `product` parce que le garde générique bloquait le routage vers `cart_management`).
- **C'est bien un workflow spécialisé (A/D)**, mais avec **deux dettes réelles** :
  1. **Aucun garde HITL générique ne s'applique.** `confirmation_gate` documente être « le point UNIQUE de confirmation humaine ; tout nouveau chemin qui le BYPASSE s'exécute sans validation humaine » — et 3 nœuds + 3 tunnels producteur le bypassent. Ce n'est pas faux (ils confirment en interne), mais **rien ne le garantit structurellement**. Un futur tunnel qui oublie sa confirmation écrira en base sans filet.
  2. **Leur machine à états est en chaînes dans `working_memory`** (P2-4), d'où la fuite d'implémentation dans `cognitive_guard`.

**Recommandation :** garder l'architecture D, mais **remonter les tunnels au niveau de rigueur des drafts** : une machine à états typée et versionnée par tunnel (comme `ProcurementDraft`), plutôt qu'un subgraph. Le subgraph deviendra pertinent *après*, et seulement pour le panier.

---

# 11. FAST PATH

`input_interpreter --[ANSWER|SELECTION × ALL_BUYER_TUNNEL_GOALS]--> memory_update`, contournant `cognitive_guard`, `cognitive_orchestrator`, `clarification_node`, `semantic_disambiguation`, `goal_planner`.

**Ce qu'on saute réellement**, nœud par nœud :

| Nœud sauté | Ce qu'on perd | Grave ? |
|---|---|---|
| `cognitive_guard` | **`retry_count`** (compteur d'échecs), détection d'interruption, `conversation_progress`, report d'entités stables | ⚠️ **Oui, partiellement** : dans un tunnel panier, un utilisateur qui répond de travers n'incrémente jamais le compteur d'abandon — le filet `abandon_tunnel_max_retries` **ne s'applique pas aux tunnels buyer** |
| `cognitive_orchestrator` | rien (P1-1) | non |
| `clarification_node` | pertinent seulement hors tunnel | non |
| `semantic_disambiguation` | ne se déclenche que sur `NEW_TASK`/`UNKNOWN` | non |
| `goal_planner` | **le verrouillage/déverrouillage de goal** | 🟡 compensé par `memory_update` (« FAILLE 1 — Sanctuarise the active business goal »), mais c'est une **deuxième implémentation** de la même règle |

**Verdict :** le fast-path est **bien placé** (le gain de latence est réel et l'événement ciblé est le bon), mais il **duplique une invariante** (sanctuarisation du goal) et **désactive une sécurité** (compteur d'abandon) sans que ce soit documenté comme un choix. → À conserver, avec deux corrections : extraire l'incrément de `retry_count` en amont du fast-path (ou l'assumer explicitement), et documenter l'invariante dupliquée.

---

# 12. MATRICE DES ÉTATS D'ERREUR ET DE REPRISE

| État | Où il apparaît | Producteur | Consommateur | Où va le graphe | Risque |
|---|---|---|---|---|---|
| `WAITING_INPUT` | partout | validator, resolvers, `ensure_farm_node`, executor | `_route_after_*`, `response_strategy` | `response_strategy` (sauf **`ensure_farm_node`** → écrasé) | 🔴 **P0-2** |
| `WAITING_CONFIRMATION` | `confirmation_gate` | confirmation_gate | `route_after_validator`, `post_response_cleanup` (`_keep_goal_channel`) | `response_strategy` puis tour suivant | 🟢 correct (TTL 600 s + target versionné) |
| `ERROR` | nœuds métier, executor, `_safe_node` | tous | `DomainRouter.decide`, `_route_after_resolver`, `response_strategy` | `response_strategy` → `render_error` | 🟡 le **motif** est perdu (P1-4 : `error_message` droppé) |
| `FAILED` | drafts (finalizers) | finalizers | `state_cleaner` (reset terminal) | `response_strategy` | 🟢 |
| `COMPLETED` | executor, onboarding, **validator** | multiples | `_route_after_resolver`, `state_cleaner` | ⚠️ depuis **validator** → `to_resolver` | 🔴 **P1-2** |
| `UNKNOWN` + `unknown_reason` | interpréteur | `InterpreterResult` | `clarification_node`, `rendering/ask.py`, télémétrie | `response_strategy`/clarification | 🟢 **exemplaire** (corrigé lors du chantier LLM Gateway) |
| `TECHNICAL_FAILURE` | `unknown_reason` | interpréteur (panne LLM) | `clarification_node`, `render_ask_missing_field` | repli déterministe honnête | 🟢 |
| `EXECUTION_UNKNOWN` | drafts | finalizers + services de réconciliation | réconciliation Celery | hors graphe (job périodique) | 🟢 bon design |
| `REJECT` | interpréteur | LLM | `confirmation_gate` (2 politiques : préservation ou purge), `response_strategy` | `response_strategy` | 🟢 |
| `INTERRUPTION` | `cognitive_guard` | guard | `response_strategy`, `render_interruption` | `response_strategy` | 🟡 **aucune transition** : le guard décide « suspend_current_goal » mais le graphe continue tout droit |
| `RECOVERY` | `cognitive_guard` | guard | `response_strategy`, `render_recovery` | `response_strategy` | 🟡 idem |
| `OUT_OF_SCOPE` | interpréteur | LLM | `response_strategy`, `clarification_node` | clarification | 🟢 |
| `HUMAN_INTERVENTION` | `mcp_tool_executor` | TaskHandler | **personne** | rendu générique | 🟡 état orphelin |
| `BLOCKED` / `PROFILE_UNAVAILABLE` | `input_normalizer` | normalizer | `_route_after_security` | traverse quand même `security_moderation` (~10 s d'appels MCP inutiles) | 🟡 |

**États « suspendus » identifiés :** `HUMAN_INTERVENTION` (aucun consommateur), `INTERRUPTION`/`RECOVERY` (décidés sans transition), `COMPLETED` issu du validateur (route nominale au lieu de terminale).

---

# 13. TROIS ARCHITECTURES

## Architecture A — évolution minimale

Conserver `StateGraph` + `conditional_edges`. Corriger uniquement P0/P1 :
déclarer `sales_publish_draft` ; conditionnel après `ensure_farm_node` ; rendre terminal le `COMPLETED` du validateur ; brancher ou supprimer `cognitive_orchestrator` ; corriger l'adresse de `slot_enrichment_force_clarification` ; déclarer (ou supprimer) les 9 canaux fantômes ; ajouter les tests d'architecture.

```
[topologie inchangée, 3 edges fixes → conditionnels]
```

- **+** Risque quasi nul, gain immédiat sur des pannes réelles, aucune formation d'équipe.
- **−** Ne traite pas la dette de fond : `working_memory` non typé, tunnels non contraints, finalizers empilés.

## Architecture B — hybride (recommandée)

A **+** :
1. `Command(goto=...)` **chirurgical** là où la décision appartient au nœud (`ensure_farm_node` d'abord).
2. **Machines à états typées** pour les tunnels (sur le modèle des drafts) → suppression des phases-chaînes de `working_memory` et de la fuite dans `cognitive_guard`.
3. **Contrat de sortie exhaustif par nœud** (`Literal[...]` sur chaque fonction de routage + test qui compare aux `path_map`).
4. **1 subgraph pilote** (`BuyerCartGraph`) — seulement après 1-3.
5. `durability="sync"` explicite ; HITL **inchangé** (pas d'`interrupt()`).

```
       ┌───────────── pipeline conversationnel (conditional_edges) ─────────────┐
START→ role_guard → normalizer → security → interpreter →[fast|cognitive]→ planner
       → memory → validator ──DomainRouter──┬─→ [BuyerCartGraph]  (subgraph pilote)
                                            ├─→ negotiation / order_tracking (FSM typées)
                                            └─→ context_resolver
                                                   │  Command(goto)
                                                   ├─→ ensure_farm ──┬─→ response_strategy (menu ferme)
                                                   │                 └─→ confirmation_gate
                                                   └─→ confirmation_gate → executor → finalizer(goal) →
       response_strategy → state_cleaner → final_response → post_cleanup → END
```

- **+** Corrige les causes racines, garde la lisibilité, migration incrémentale et testable.
- **−** Demande de la discipline (contrats de sortie) et une vraie revue du typage `working_memory`.

## Architecture C — refonte forte (supervisor + subgraphs par domaine + `interrupt()`)

`BuyerGraph`/`ProducerGraph` comme subgraphs complets, superviseur en parent, état découpé par domaine avec reducers au parent, HITL par `interrupt()`.

- **+** Séparation des domaines maximale, extensibilité théorique.
- **−** **Inadapté ici** : `interrupt()` ne convient pas au canal (§8) ; le découpage d'état casserait `PendingInteraction` (qui **doit** voir les contextes buyer pour dériver le tunnel panier) ; le `WorkspaceCheckpointer` (budget 480 Ko, namespaces) devrait être repensé ; migration à haut risque sur un système déjà en production avec 6 mois d'incidents patiemment corrigés.

## Score comparatif (1 = mauvais, 5 = excellent ; « Complexité » et « Coût de migration » : 5 = faible)

| Critère | A | B | C |
|---|---|---|---|
| Lisibilité du workflow | 3 | **5** | 3 |
| Robustesse / correction | 4 | **5** | 4 |
| Testabilité | 4 | **5** | 3 |
| Persistance | 4 | 4 | 2 |
| HITL | 4 | **5** | 2 |
| Recovery | 4 | **5** | 3 |
| Scalabilité (nouveau domaine) | 2 | **4** | 4 |
| Complexité (5 = simple) | **5** | 4 | 1 |
| Maintenabilité | 3 | **5** | 2 |
| Coût/risque de migration (5 = faible) | **5** | 3 | 1 |
| **Total** | **38** | **45** | **25** |

---

# 14. ARCHITECTURE RECOMMANDÉE ET RÈGLES DE ROUTAGE

## Recommandation : **Architecture B, livrée par phases, avec A comme phase 1 obligatoire**

**Réponse à votre question centrale** — « pourquoi cette architecture et pas une autre ? » :

> Parce que le workflow de Market Coach est **une machine à états conversationnelle à convergence unique**, pas un système multi-agent. Ses décisions sont, à 90 %, **dérivables de l'état** (`pending_interaction`, `status`, `interpreted_event`) — c'est exactement le domaine d'excellence de `conditional_edges`, qui garde ces décisions **hors des nœuds**, pures et testables une par une. `Command` et `interrupt()` sont supérieurs quand la décision ou la pause appartient **intrinsèquement** au nœud ; ici ce n'est vrai que pour un nœud (`ensure_farm_node`) et pour aucun HITL, parce que la réponse de l'utilisateur est du langage naturel qui doit repasser par tout le pipeline de compréhension. Le vrai déficit n'est pas le choix des primitives : c'est que **le contrat entre nœuds, edges et canaux d'état n'est vérifié par rien**.

## Règles de routage — qui décide quoi (version vérifiée, corrigée)

| Couche | Décide | Ne décide PAS | Correction vs aujourd'hui |
|---|---|---|---|
| `input_interpreter` | l'**événement** + l'**intention** + `unknown_reason` | le goal actif, la stratégie | ✅ conforme |
| `cognitive_guard` | **interruption / abandon / retry** | l'intention, le rendu | ⚠️ doit **exposer une transition** (aujourd'hui : décide sans router) |
| `cognitive_orchestrator` | — | — | ⚠️ **à brancher ou à supprimer** |
| `goal_planner` | **cycle de vie du goal** (verrou, suspension, reprise) | la complétude, le domaine | ✅ conforme (seul écrivain de `current_goal`) |
| `validator` | **readiness** (complétude + contrats) | le domaine, l'autorisation | ⚠️ doit pouvoir dire **« terminal »** (P1-2) |
| `DomainRouter` | **le domaine / le flow** | la readiness, l'exécution | ⚠️ un seul routeur, pas deux en cascade |
| Flow métier | **le contexte métier** + sa propre UX multi-étapes | l'autorisation d'écriture générique | 🟡 doit **prouver** qu'il confirme (§16.9) |
| `ensure_farm_node` | **la ressource ferme** + **sa propre suite** | la confirmation | 🔴 aujourd'hui il décide sans pouvoir router |
| `confirmation_gate` | **l'autorisation d'écriture** | quoi exécuter, comment rendre | ✅ conforme |
| `mcp_tool_executor` | **l'exécution** + la traduction d'erreur | l'autorisation, le rendu | ✅ conforme |
| Finalizers | **la transition terminale du draft** | le rendu | 🟡 devrait être choisi par goal, pas chaîné |
| `response_strategy` | **la représentation** | tout le reste | ✅ conforme, à préserver |

---

# 15. PLAN DE MIGRATION

**Phase 1 — P0 (jours, risque quasi nul, aucun changement de topologie)**
1. Déclarer `sales_publish_draft` (`core/state.py` + `state_profile.py`) → **teste d'abord par un test qui traverse le graphe compilé** (sinon on ne verra rien).
2. Décider du sort des 8 autres canaux fantômes : déclarer (`auto_farm_notice`, `error_creating_farm`, `farm_creation_attempted`, `error_message`) ou supprimer le code mort (`chat_history`, `blocked_user_query`, la moitié morte de `menu_snapshot_id`, `forced_role`).
3. Corriger l'adresse de `slot_enrichment_force_clarification` (state vs payload).

**Phase 2 — P1 topologie (jours, risque faible, tests avant/après)**
4. `ensure_farm_node` : edge fixe → décision explicite (`Command(goto=...)` ou conditionnel sur `status`).
5. `validator` : rendre terminal le cas `blocking_technical_id` (route vers `response_strategy`).
6. `cognitive_orchestrator` : brancher ses décisions **ou** le réduire à ce qu'il est (télémétrie) — décision de produit, pas technique.
7. `_route_after_executor` → `add_edge` ; renommer les libellés menteurs (P2-1).

**Phase 3 — dette de fond (semaines)**
8. Typage des machines à états de tunnel (modèle draft) ; suppression des phases-chaînes de `working_memory` et du `stale_wm_keys` de `cognitive_guard`.
9. `DomainContext` réduit aux champs réels.
10. Fast-path : traiter `retry_count` explicitement.

**Phase 4 — seulement si 1-3 sont livrés et stables**
11. Subgraph pilote `BuyerCartGraph`.
12. Revue de `durability` et du budget `WorkspaceCheckpointer`.

**Ce qu'il ne faut PAS faire** : migrer le HITL vers `interrupt()`, découper l'état par domaine, créer 6 subgraphs.

---

# 16. TESTS D'ARCHITECTURE À AJOUTER

Ces tests auraient détecté **tous** les P0/P1 ci-dessus. Ils sont la vraie livraison de cet audit.

1. **Contrat canaux ↔ écritures** : pour chaque nœud, exécuter le graphe **compilé** et vérifier qu'aucune clé retournée n'est silencieusement droppée (assertion sur `set(patch) - set(MarketAgentState.__annotations__)` == ∅). → aurait attrapé **P0-1** et **P1-4**.
2. **Cohérence des 3 registres** : tout champ de `MarketAgentState` doit avoir une `FieldSpec` dans `state_profile.py`, et réciproquement. → aurait attrapé la 3ᵉ récidive.
3. **Exhaustivité des sorties de routage** : pour chaque fonction `_route_after_*`, l'ensemble de ses `return` littéraux doit être **exactement** l'ensemble des clés du `path_map` déclaré dans `graph_builder.py`.
4. **Aucun edge fixe après un nœud qui produit `WAITING_INPUT`/`ERROR`** : test statique/dynamique listant les nœuds capables de poser un `status` bloquant et vérifiant qu'ils sont suivis d'une décision. → aurait attrapé **P0-2**.
5. **Terminalité** : tout patch contenant `final_response` + un `status` terminal doit atteindre `response_strategy` sans traverser un nœud à effets de bord. → aurait attrapé **P1-2**.
6. **Décisions consommées** : toute clé de décision produite (`should_replan`, `cognitive_decision.next_step`) doit avoir au moins un lecteur. → aurait attrapé **P1-1**.
7. **Écrivain/lecteur d'un même drapeau** : pour une liste de drapeaux de contrôle, vérifier que l'adresse d'écriture == l'adresse de lecture. → aurait attrapé **P1-3**.
8. **Pas de cul-de-sac** : parcours du graphe compilé — tout nœud atteint `END`.
9. **Tout chemin d'écriture passe par une confirmation** : pour chaque goal `action_type=WRITE`, prouver qu'aucun chemin n'atteint un appel MCP d'écriture sans soit `confirmation_gate`, soit une confirmation interne au tunnel déclarée explicitement dans un registre. → verrouille l'invariant que `confirmation_gate` revendique déjà en commentaire.
10. **Fast-path équivalent** : pour un même état d'entrée, le chemin fast-path et le chemin cognitif doivent produire le même `transaction_payload` (seule la latence diffère).
11. **Finalizers** : chaque draft en `EXECUTING` atteint un état terminal (`EXECUTED`/`FAILED`/`EXECUTION_UNKNOWN`) — jamais bloqué.
12. **Reprise** : sérialiser/désérialiser l'état à chaque frontière de tour et rejouer — le tour N+1 doit être identique avec et sans passage par le checkpoint (détecte les canaux non déclarés **et** les fuites de fin de tour).

---

## Sources

- [Interrupts — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Use the graph API (`add_conditional_edges`, `Command`, `Command.PARENT`) — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/use-graph-api)
- [Use subgraphs — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [Durable execution — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [How to combine control flow and state updates with `Command`](https://langchain-ai.github.io/langgraphjs/how-tos/command/)
- [`add_conditional_edges` — LangChain Reference](https://reference.langchain.com/python/langgraph/graph/state/StateGraph/add_conditional_edges)
- [Making it easier to build human-in-the-loop agents with `interrupt` — LangChain Blog](https://www.langchain.com/blog/making-it-easier-to-build-human-in-the-loop-agents-with-interrupt)
