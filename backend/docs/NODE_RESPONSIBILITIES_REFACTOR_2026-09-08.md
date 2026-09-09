# Refonte des responsabilités des nœuds d'entrée — Market Coach (2026-09-08)

Mandat : refondre la **philosophie, les responsabilités et les transitions** des nœuds `normalize_role` / `role_guard` / `input_normalizer` / `security_moderation` / `input_interpreter` / `cognitive_guard` / `cognitive_orchestrator` / `clarification_node` / `semantic_disambiguation` / `confirmation_gate`, sans toucher aux nœuds non audités (`goal_planner`, `memory_update`, `validator`, `domain_router`, `context_resolver`, les resolvers métier, les tunnels buyer/producer, les executors, les finalizers, `response_strategy`, `state_cleaner`, `final_response`, `post_response_cleanup`).

Principe directeur appliqué : **un nœud = une responsabilité claire, une décision = un seul propriétaire, une donnée = une seule source canonique.**

---

## A. Résumé architectural — ancien rôle / nouveau rôle par nœud

### `role_guard` → **supprimé** (nœud LangGraph)

| | |
|---|---|
| Ancien rôle | Nœud d'entrée écrivant `user_role` par défaut (compile-time `role`, tombait toujours sur `"PRODUCER"` faute d'info) |
| Nouveau rôle | N'existe plus comme nœud. Sa responsabilité résiduelle (rôle par défaut) est reprise par `session_bootstrap`, avec `normalize_role` corrigé (voir ci-dessous). |
| Retiré | Le nœud lui-même, son edge depuis `START`. |
| Conservé | Le fichier `nodes/role_guard.py`/`make_role_guard` existe toujours (non supprimé — un harnais de test d'evals, `tests/evals/runners/harness.py`, l'importe directement avec un flag `with_role_guard` optionnel) mais n'est plus câblé dans le graphe compilé. |

`graphs/roles.py::normalize_role` — comportement corrigé :

```python
BUYER / ACHETEUR / ACHETEUSE     -> "BUYER"
PRODUCER / PRODUCTEUR / PRODUCTRICE -> "PRODUCER"
tout le reste (y compris None, "") -> "UNKNOWN"   # avant : "PRODUCER" silencieux
```

### `input_normalizer` → **purifié**

| | |
|---|---|
| Ancien rôle | Normalisation de texte + détection/neutralisation de prompt injection (remplaçait le texte par un faux prompt système) + chargement profil DB/MCP + préchargement fermes + bascule onboarding + bookkeeping de tunnel |
| Nouveau rôle | Question UNIQUE : quelle est l'entrée utilisateur canonique de ce tour ? Durcissement du texte (caractères de contrôle, taille avec `input_truncated` explicite), Unicode/whitespace, `turn_count`, timestamp, transcription audio, reset des champs formulaire legacy (compat checkpoint). |
| Retiré | Détection d'injection (→ `security_moderation`), remplacement du texte par `_SYSTEM_OVERRIDE_PROMPT` (interdiction explicite, supprimé sans repli), `load_user_profile`/`preload_farms`/`resolve_onboarding_state` (→ `session_bootstrap`), bookkeeping `working_memory.active_tunnel_label` (→ `session_bootstrap`). |
| Conservé | Récupération raw/normalized text, taille explicite, transcription audio, `turn_count`, timestamp. |

### `session_bootstrap` → **nouveau nœud technique** (pas une "philosophie" de dialogue)

Regroupe la responsabilité résiduelle de `role_guard` (rôle par défaut) et le chargement profil/fermes/onboarding sorti d'`input_normalizer`. Justification et dette assumée détaillées dans sa propre docstring (`nodes/session_bootstrap.py`) — voir section E ci-dessous.

### `security_moderation` → **propriétaire unique de la décision de sécurité**

| | |
|---|---|
| Ancien rôle | Compte bloqué/banni, produits interdits, scam — mais PAS l'injection de prompt (gérée, mal, ailleurs) |
| Nouveau rôle | Question UNIQUE : cette entrée peut-elle continuer vers l'interprétation métier ? Ajoute la détection d'injection de contexte comme SIGNAL déterministe (jamais un remplacement du texte). Vocabulaire fermé interne `SecurityDecision` (ALLOW/BLOCK/RESTRICT, RESTRICT réservé pour un futur usage) — journalisé, ne pilote pas le routage (qui reste sur `status`/`security_status`, inchangé). |
| Bug corrigé | Le blocage sur injection ne routait PAS correctement avant ce chantier — `nodes/routing.py::_SECURITY_BLOCKING` ne contenait pas `"PROMPT_INJECTION_DETECTED"` malgré un commentaire de `core/state.py` prétendant déjà le contraire. Le texte n'est plus jamais modifié par ce nœud. |

### `input_interpreter` → **filtrage structurel par rôle retiré**

| | |
|---|---|
| Ancien rôle | Moteur de compréhension + restriction du catalogue d'intentions accessibles selon le rôle compile-time du graphe (`allowed_intents_for_role`), à la fois dans le prompt LLM et dans un clamp post-LLM |
| Nouveau rôle | Un utilisateur peut déclencher une intention BUYER ou PRODUCER quel que soit son rôle de profil — `user_role` reste lisible comme contexte SECONDAIRE (`cart_pending`, repli dégradé, logs). |
| Retiré | Le filtrage du catalogue dans `_build_dynamic_interpreter_prompt` (le commentaire existant prétendait déjà "aucun effet", c'était faux) ; le clamp `raw_intent not in allowed_intents_for_role(role_up)`. |
| Conservé | La garde anti-hallucination — remplacée par une vérification contre le catalogue COMPLET (`raw_intent not in INTENT_CONFIG`), pas contre un sous-ensemble par rôle. Tout le reste (bypass interactif, fast-path, LLM Gateway, `InterpreterResult`, anti-hallucination d'unités, quantité composée, `pricing_tiers`, `TECHNICAL_FAILURE`) inchangé. |

### `cognitive_guard` → **devient la policy conversationnelle** (nom conservé, philosophie renommée)

| | |
|---|---|
| Ancien rôle | Interruption de tunnel (SANS garde de confiance), récupération/abandon UNKNOWN, carry-forward d'entités, progression UI, calcul indépendant des candidats de désambiguïsation, reset détaillé des mini-machines à états transactionnelles |
| Nouveau rôle | Étant donné `InterpreterResult` et l'état conversationnel, quelle est la relation entre ce nouvel événement et la conversation active ? Calcule désormais le candidat de désambiguïsation UNE fois (`disambiguation_candidate`), consulté par `clarification_node`/`semantic_disambiguation`. |
| Bug corrigé | L'interruption d'un tunnel actif sur `NEW_TASK` + intention différente ne vérifiait AUCUNE confiance — une intention concurrente à confiance quasi nulle cassait quand même un tunnel fiable. Seuil `_DISAMBIGUATION_CONFIDENCE_THRESHOLD` (0.85, cohérence avec le reste de l'interpréteur/policy) ajouté. |
| Retiré (encapsulé, pas supprimé) | Le detail des resets de `BidWorkflowState`/`ProducerUpdateWorkflowState`/`WinnerGpsWorkflowState`/`preorder_workflow`/`transaction_payload`/`selected_tool`/`execution_result` — extrait vers `core/conversation_reset.py::reset_abandoned_conversation_context` (comportement identique, propriété/lisibilité déplacées). |
| Conservé | Toutes les règles réelles (interruption, recovery, retry, reset, exception GPS native, carry-forward, progression UI/proactive_hint — ces deux derniers restent dans `cognitive_guard`, aucun propriétaire sûr identifié ailleurs dans le périmètre audité). |

### `cognitive_orchestrator` → **supprimé** (nœud ET fonction)

Sa classification `phase`/`next_step`/`reason`/`loop` ne pilotait AUCUNE transition réelle du graphe — déjà documenté dans le code avant ce chantier (`P1-1 audit architectural`), jamais corrigé. Le seul routage qui en dépendait topologiquement (`is_onboarding` → `onboarding_node`) est rattaché directement après `cognitive_guard`. Une trace de diagnostic (log `logger.debug`) résume désormais `event`/`intent`/`goal`/`confidence`/`action` à la fin de `cognitive_guard`, remplaçant la pseudo-boucle décorative.

### `semantic_disambiguation` → **exécuteur, plus décideur concurrent**

| | |
|---|---|
| Ancien rôle | Recalculait indépendamment (LUI-MÊME) si une désambiguïsation était nécessaire (2e — voire 3e — calcul du même signal dans le graphe) |
| Nouveau rôle | Consulte en priorité `state["disambiguation_candidate"]` (calculé une fois par `cognitive_guard`) ; ne recalcule en local qu'en repli défensif. |
| Bug corrigé | `confidence = state.get("interpreter_confidence") or 1.0` — une confiance ABSENTE devenait MAXIMALE (l'inverse d'un repli sûr). Corrigé en `or 0.0`. |
| Conservé | Hint lexical le plus spécifique, menus pédagogiques, `PendingInteraction.SELECTION_MENU`, mapping des choix, stash des entités extraites, `DISAMBIGUATION_PENDING` (dette identifiée, non touchée — dépendances non auditées). |

### `clarification_node` → **clarification seulement**

| | |
|---|---|
| Ancien rôle | Recalculait lui aussi `_detect_disambiguation_candidates` pour décider s'il devait s'effacer ; supposait un utilisateur "producteur" par défaut dans le prompt LLM |
| Nouveau rôle | Consulte `state["disambiguation_candidate"]` au lieu de recalculer. La gestion GPS hors-tunnel (responsabilité de domaine mal placée, flows GPS non audités) est encapsulée dans un helper nommé `_render_out_of_tunnel_location_outcome`, marqué transitoire. |
| Corrigé | Suppression de la présomption "un producteur" par défaut (double rôle : `user_name or 'vous'`, `user_role or 'non précisé'`). |
| Conservé | `TECHNICAL_FAILURE` (pas de second appel Gateway), fallback déterministe, réponse LLM courte adaptée WhatsApp. |

### `confirmation_gate` → **non modifié**

Mandat explicite : préserver, ses voisins transactionnels (`draft_id`/`draft_version`, executors, finalizers) n'ont pas été audités dans ce chantier. Aucun changement.

---

## B. Graphe avant/après (périmètre modifié uniquement)

**Avant :**
```
START → role_guard → input_normalizer → security_moderation
       → input_interpreter → cognitive_guard → cognitive_orchestrator
       → clarification_node → semantic_disambiguation → goal_planner → ...
```

**Après :**
```
START → session_bootstrap → input_normalizer → security_moderation
       → input_interpreter → cognitive_guard
       → clarification_node → semantic_disambiguation → goal_planner → ...
```

Le reste du graphe (`goal_planner` → ... → `END`) est topologiquement inchangé.

---

## C. Fichiers modifiés — liste exacte et justification

**Créés :**
- `src/agriconnect/graphs/agents/market_coach/nodes/session_bootstrap.py` — nouveau nœud technique (rôle par défaut + profil/onboarding/fermes + bookkeeping tunnel), extrait d'`input_normalizer`/`role_guard`.
- `src/agriconnect/graphs/agents/market_coach/core/conversation_reset.py` — helper `reset_abandoned_conversation_context`, extrait de `cognitive_guard`.
- `tests/nodes/test_session_bootstrap.py` — couverture du nouveau nœud (reprend, à l'identique, les tests qui vivaient dans `test_input_normalizer.py` avant le déplacement).
- `tests/unit/test_normalize_role.py` — contrat `normalize_role` exigé par le mandat.

**Modifiés :**
- `src/agriconnect/graphs/roles.py` — `normalize_role` ne retourne plus jamais "PRODUCER" par défaut.
- `src/agriconnect/graphs/agents/market_coach/core/graph_builder.py` — retrait `role_guard`/`cognitive_orchestrator`, ajout `session_bootstrap`, rewiring de `_route_after_cognitive`.
- `src/agriconnect/graphs/agents/market_coach/core/state.py` — nouveaux champs `input_truncated`, `disambiguation_candidate` ; `security_status` Literal étendu.
- `src/agriconnect/graphs/agents/market_coach/core/state_profile.py` — déclaration EPHEMERAL des deux nouveaux champs.
- `src/agriconnect/graphs/agents/market_coach/nodes/input_normalizer.py` — réécrit, purifié.
- `src/agriconnect/graphs/agents/market_coach/nodes/security_moderation.py` — détection d'injection ajoutée, `SecurityDecision`, wrapper de log.
- `src/agriconnect/graphs/agents/market_coach/nodes/cognitive.py` — seuil de confiance sur l'interruption, `disambiguation_candidate`, extraction du reset d'abandon, suppression de `cognitive_orchestrator`.
- `src/agriconnect/graphs/agents/market_coach/nodes/clarification.py` — consultation du candidat précalculé, encapsulation GPS, suppression du biais "producteur".
- `src/agriconnect/graphs/agents/market_coach/nodes/semantic_disambiguation.py` — correctif `confidence or 0.0`, consultation du candidat précalculé.
- `src/agriconnect/graphs/agents/market_coach/nodes/routing.py` — `"PROMPT_INJECTION_DETECTED"` ajouté à `_SECURITY_BLOCKING`.
- `src/agriconnect/graphs/agents/market_coach/interpreter/routing.py` — retrait du filtrage par rôle (prompt + clamp), garde anti-hallucination reformulée contre le catalogue complet.
- `tests/nodes/test_cognitive_guard_and_orchestrator.py` — retrait de `TestCognitiveOrchestrator` (code supprimé), tests de confiance ajoutés, tests `disambiguation_candidate` ajoutés.
- `tests/nodes/test_input_normalizer.py` — réécrit pour le nœud purifié.
- `tests/nodes/test_security_moderation.py` — tests d'injection ajoutés (déplacés depuis `test_input_normalizer.py`).
- `tests/chaos/test_role_isolation.py` — contrat `normalize_role` mis à jour (`UNKNOWN` accepté).
- `tests/integration/test_end_to_end.py` — imports mis à jour vers les nouveaux emplacements.
- `docs/MARKET_COACH_ARCHITECTURE_MAP_2026-09-08.md` — addendum §17.

**Non modifiés (mandat explicite) :** `nodes/confirmation_gate.py`, `nodes/role_guard.py` (fichier conservé, non câblé), `goal_planner`, `memory_update`, `validator`, `domain_router`, `context_resolver`, resolvers métier, tunnels, executors, finalizers, `response_strategy`, `state_cleaner`, `final_response`, `post_response_cleanup`.

---

## D. Tests

**Nouveaux tests :**
- `tests/unit/test_normalize_role.py` (9 tests) — contrat complet mandaté.
- `tests/nodes/test_session_bootstrap.py` (15 tests) — rôle par défaut + reprise intégrale de la couverture profil/onboarding/tunnel.
- `TestDisambiguationCandidatePrecomputed` dans `test_cognitive_guard_and_orchestrator.py` (2 tests) — source unique du candidat.
- `TestCognitiveGuardInterruption` étendue (2 tests) — confiance haute interrompt, confiance basse n'interrompt pas.
- `TestDetectContextInjection` + `TestSecurityModerationNodeInjectionDecision` dans `test_security_moderation.py` (12 tests) — détection déplacée + non-régression du routage.
- `TestPurityContract` dans `test_input_normalizer.py` (5 tests) — contrat architectural : plus d'écriture `detected_intent`/`current_goal`/`execution_authorized`, plus d'appel profil, plus de remplacement du texte.

**Tests modifiés (comportement intentionnellement changé, pas une régression) :**
- `test_cognitive_guard_and_orchestrator.py` — `TestCognitiveOrchestrator` retirée (code supprimé) ; `test_new_task_with_different_intent_suspends_the_current_goal` scindé en deux (haute confiance interrompt / basse confiance n'interrompt pas).
- `tests/chaos/test_role_isolation.py::test_unknown_role_collapses_deterministically` — accepte désormais `"UNKNOWN"`.
- `tests/integration/test_end_to_end.py` — 3 tests réimportent depuis les nouveaux emplacements (`security_moderation` pour l'injection ; `_harden_text` retourne un tuple).
- `tests/interpreter/test_extraction_and_llm_primacy.py` — aucune modification de test nécessaire, corrigé côté production (garde anti-hallucination reformulée pour ne PAS régresser ce test).

**Suites exécutées :** `tests/architecture`, `tests/unit`, `tests/nodes`, `tests/interpreter`, `tests/chaos`, `tests/integration` — voir résultat détaillé en fin de ce document (section « Résultat final »).

---

## E. Dette volontairement laissée (nœuds voisins non audités)

- **`session_bootstrap` reste un nœud technique intermédiaire**, pas conforme à la lettre de la topologie cible du mandat (`START → input_normalizer` directement, sans intermédiaire). Choix assumé : le chargement du profil est fonctionnellement LIVE pour les utilisateurs BUYER (`orchestrator.py` ne le précharge que pour les PRODUCER en amont, comme effet de bord de sa propre vérification de rôle) — le déplacer entièrement dans `orchestrator.py` (non audité) ou le supprimer aurait cassé un chemin de production réel. Documenté explicitement dans la docstring du module.
- **`_render_out_of_tunnel_location_outcome` (GPS) reste dans `clarification_node`** — responsabilité de domaine mal placée, mais les flows GPS (`flows/buyer/gps_delivery_gate.py`, `core/location.py`) n'ont pas été audités dans ce chantier. Encapsulé dans un helper nommé, marqué transitoire.
- **`DISAMBIGUATION_PENDING` (pseudo-goal) et le stash dans `transaction_payload`** — non touchés, `goal_planner`/`memory_update` en dépendent et n'ont pas été audités.
- **`cognitive_guard` reste propriétaire de la progression UI (`conversation_progress`/`proactive_hint`) et du carry-forward d'entités** — aucun propriétaire sûr identifié ailleurs dans le périmètre audité ; non déplacé (mandat : ne pas inventer une architecture sur des nœuds non inspectés).
- **`confirmation_gate`** : dette identifiée mais non migrée (booléens `is_certified`/`execution_authorized` comme preuve d'autorisation faible ; persistance canonique PostgreSQL vs cache LangGraph) — mandat explicite de ne pas y toucher tant que l'executor n'a pas été audité.
- **`interpreter/routing.py::cart_pending`/`_degraded_fallback`** continuent de lire le rôle COMPILE-TIME (`role_up`, figé au moment de la construction du graphe par `GraphFactory`) plutôt que `state["user_role"]` (rôle courant de la conversation). Usage SECONDAIRE toléré par le mandat (ne filtre structurellement rien), mais reste une incohérence mineure avec la philosophie double-rôle — non corrigée dans ce chantier (touche `core/router.py`/`GraphFactory`, hors périmètre strict).
- **Deux graphes compilés distincts subsistent** (`GraphFactory` cache un graphe par rôle PRODUCER/BUYER) — devenu vestigial maintenant que le filtrage structurel d'intentions a été retiré, mais l'infrastructure elle-même (compile-time role → `make_input_interpreter`/`get_domain_router`) n'a pas été démantelée (touche `core/router.py`, `graphs/factory.py`, `orchestrator.py` — hors périmètre strict).

Ce chantier ne prétend PAS avoir résolu l'architecture globale de Market Coach — seuls les 10 nœuds explicitement listés dans le mandat ont été refondus.

---

## Résultat final — suite complète

`python -m pytest tests/ --ignore=tests/evals -q` (hors harnais d'évaluation LLM, non déterministe) :

- **6 échecs**, tous vérifiés **pré-existants** — identiques avant/après ce chantier (comparaison directe via `git stash` sur le code source : les 6 mêmes tests échouent exactement de la même façon sur le code AVANT toute modification de cette session). Aucun rapport avec les nœuds refondus :
  - `tests/architecture/test_order_mutations_require_ownership.py` (2) — assertions sur des mentions dans un commentaire Markdown de `infrastructure/mcp/exposure.py`, sans rapport avec Market Coach.
  - `tests/unit/test_create_auction_catalog_gate.py` (4) — `BusinessRuleException: Date limite invalide.`, un problème de date codée en dur dans les fixtures du test, déjà connu (voir `[[precommande-architecture-consolidation-2026-08]]`).
- **Zéro nouvelle régression** introduite par ce chantier sur `tests/architecture`, `tests/unit`, `tests/nodes`, `tests/interpreter`, `tests/chaos`, `tests/integration`.
- Graphe compilé avec succès pour les deux rôles (`build_graph(role="PRODUCER")` / `build_graph(role="BUYER")`) après suppression de `role_guard`/`cognitive_orchestrator` et ajout de `session_bootstrap`.
