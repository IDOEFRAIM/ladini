# Revue de validation du bloc refondu — Market Coach (2026-09-08)

Suite au chantier "refonte des responsabilités des nœuds d'entrée"
(`NODE_RESPONSIBILITIES_REFACTOR_2026-09-08.md`), revue de validation
approfondie sur le PÉRIMÈTRE STRICT : `session_bootstrap`,
`security_moderation`, le router immédiatement après, `input_interpreter`
et ses helpers de prompt, `cognitive_guard`, `semantic_disambiguation`,
les edges LangGraph entre ces nœuds, les nouveaux champs d'état
(`disambiguation_candidate`, `security_decision`, `input_truncated`), et
leurs tests. **Aucun nœud non audité n'a été refondu** — deux corrections
mineures dans `services/onboarding.py` (helper directement nécessaire à
la pureté de `session_bootstrap`) sont documentées section B.

## Réponse aux 4 questions du mandat

**1. `session_bootstrap` est-il réellement cohérent et minimal ?** OUI,
après 2 corrections trouvées et appliquées par cette revue (§A).
**2. `security_moderation` est-il réellement la frontière de confiance ?**
OUI — Invariants A/C/D/E déjà satisfaits ; Invariant B PROUVÉ (pas
seulement supposé) par un nouveau test au niveau du graphe compilé ; un
gain structurel (`security_decision`) a été ajouté pour rendre le routeur
véritablement trivial à un seul champ.
**3. `input_interpreter` est-il réellement double-rôle partout ?** OUI —
filtrage résiduel trouvé et corrigé dans la docstring de module (stale,
contredisait le code) ; fast-paths prouvés structurellement role-agnostic
(la fonction ne reçoit même pas `role` en paramètre) ; contrat
`InterpreterResult` vérifié déjà robuste (point de passage unique
préexistant, confirmé).
**4. `cognitive_guard` est-il réellement l'unique propriétaire de la
décision conversationnelle ?** PARTIELLEMENT — il possède la source
unique du CANDIDAT (`disambiguation_candidate`), mais la décision finale
"faut-il désambiguïser" reste dans `semantic_disambiguation` (dette
documentée, pas un bug — pas de duplication). Un vrai mort-code
(`_should_trigger_disambiguation`) trouvé et supprimé au passage.

---

## A. Verdict par composant

### `session_bootstrap` — SAIN (après correction)

2 écarts trouvés et corrigés par cette revue :
- **`preload_farms` retiré** — ce n'était PAS un contexte "minimum
  universel" (mandat de revue §2) : un acheteur n'en a jamais besoin.
  Vérifié SANS risque de régression : son unique consommateur
  (`flows/producer/farm_logic.py::ensure_farm_node`) a déjà son propre
  repli complet vers `FarmGateway.list_farms()` avec persistance dans le
  même champ DURABLE.
- **`working_memory.active_tunnel_label` retiré** — code MORT (recherche
  exhaustive : zéro lecteur dans tout `src/agriconnect`), même classe de
  bug que `working_memory.turn_count` déjà purgé lors du chantier
  précédent.

1 écart trouvé et corrigé dans un HELPER appelé par ce nœud
(`services/onboarding.py::resolve_onboarding_state`, pas le nœud
lui-même) :
- Écrivait `interpreted_event`/`detected_intent`/`interpreter_confidence`
  — le contrat de sortie exclusif d'`input_interpreter`. Preuve de
  redondance totale (pas juste mal placé) : `input_interpreter::
  _emit_onboarding` réécrit INCONDITIONNELLEMENT ces 3 mêmes valeurs sur
  les 3 chemins onboarding (no_llm/llm_crash/llm_success), donc la
  pré-écriture de `session_bootstrap` était toujours écrasée avant la fin
  du tour. Retiré sans risque.

1 KEEP jugé délibérément :
- `_profile_unavailable_patch()` écrit `response_strategy`/
  `final_response`/`ag_ui_component` — en apparence une "réponse métier"
  interdite à ce nœud. Jugement retenu : c'est une réponse à un échec
  TECHNIQUE du service profil ("erreur technique du service profil",
  explicitement listé comme responsabilité légitime), pas une décision
  métier — aucun autre propriétaire sûr n'existe dans le périmètre pour
  ce cas précis.

Dette assumée (inchangée depuis le chantier précédent, documentée à
nouveau) : ce nœud reste un intermédiaire technique entre `START` et
`input_normalizer`, ne correspondant pas à la lettre de la topologie
cible du mandat original (`START → input_normalizer` direct) — parce que
le chargement du profil est réellement nécessaire en production pour les
utilisateurs BUYER (`orchestrator.py` ne le précharge que pour la
vérification de rôle PRODUCER, effet de bord, pas pour BUYER).

**Performance** — vérifié : le profil n'est PAS rechargé si
`user_context_loaded=True` (déjà le cas avant cette revue, confirmé par
lecture + tests `test_already_loaded_context_skips_profile_reload`).

### `security_moderation` — SAIN (renforcé)

Les 5 invariants du mandat, vérifiés :
- **A** (texte jamais modifié) : SATISFAIT, déjà vrai, verrouillé par
  `test_injection_never_touches_normalized_or_translated_text`.
- **B** (entrée bloquée n'atteint jamais `input_interpreter`) : PROUVÉ
  au niveau du graphe COMPILÉ (nouveau fichier
  `tests/architecture/test_security_boundary_compiled_graph.py` — un
  sous-graphe réel `session_bootstrap → input_normalizer →
  security_moderation → {input_interpreter spy, response_strategy spy}`
  avec le VRAI `_route_after_security`). Avant cette revue, seule
  l'unitaire de `_route_after_security()` existait — le mandat demandait
  explicitement plus que cela.
- **C** (routeur trivial) : SATISFAIT structurellement (aucune regex/
  classification dans `_route_after_security`), mais RENFORCÉ : un
  nouveau champ `security_decision` (`ALLOW`/`BLOCK`/`RESTRICT`, écrit
  par CHAQUE exécution de `security_moderation`) est désormais le SEUL
  champ que le routeur a besoin de lire — élimine la double-lecture
  `security_status in _SECURITY_BLOCKING OR status=="BLOCKED"` comme
  signal primaire (conservée en repli défensif). **Bug trouvé et corrigé
  pendant l'implémentation** : le calcul initial de la décision ne
  regardait que le patch fraîchement retourné, pas l'état effectif —
  le chemin "blocage déjà décidé en amont" (`_security_moderation_impl`,
  1er early-return) ne réaffirme délibérément pas `status` dans son
  propre patch (déjà `BLOCKED` via le reducer du nœud précédent) ; une
  lecture naïve du patch seul aurait classé ce cas en `ALLOW` à tort.
  Corrigé (`_decision_for` regarde `patch.get("status", state.get(
  "status"))`), verrouillé par un test dédié.
- **D** (regex = signaux, pas la seule définition) : SATISFAIT — 4
  patterns anglais seulement, PAS étendus (mandat : "ne pas créer une
  énorme liste multilingue"), agissent en parallèle de 3 autres gates
  indépendants (compte, produits interdits, scam) — défense en
  profondeur, pas un système fragile mono-signal. Limitation
  intentionnellement documentée : patterns anglais uniquement (les
  utilisateurs écrivent majoritairement en français/langues locales) —
  hors périmètre de cette revue (extension = décision de contenu, pas un
  bug de routage).
- **E** (sécurité conversationnelle ≠ autorisation métier) : SATISFAIT —
  aucune logique d'ownership de commande/paiement/OTP dans ce fichier ;
  vérifié par lecture exhaustive.

### `input_interpreter` — SAIN (après correction de documentation)

- Filtrage structurel par rôle : CONFIRMÉ absent du code (déjà corrigé
  lors du chantier précédent) — MAIS le **docstring de MODULE** (en tête
  de `interpreter/routing.py`) était resté STALE, prétendant encore
  "filtré par rôle (PRODUCER/BUYER)" — corrigé.
- Preuve directe ajoutée (mandat, "exemple obligatoire") :
  `test_dual_role_allows_buying_intent_for_a_producer` (nouveau,
  symétrique du test préexistant côté BUYER→SALES) +
  `test_interpreter_prompt_catalog_is_role_independent` (preuve statique :
  le prompt PRODUCER et le prompt BUYER sont l'exact même texte).
- Fast-paths (`_interpret_fast_path`) : preuve STRUCTURELLE de
  role-agnosticisme — la fonction ne reçoit même pas `role` en paramètre
  (vérifié par lecture de sa signature + grep exhaustif de son corps :
  zéro occurrence de "role").
- Contrat `InterpreterResult` : déjà robuste (préexistant, chantier
  2026-09-02) — `make_input_interpreter` retourne un wrapper qui fait
  passer TOUT résultat de `_input_interpreter_impl` (~15 points de sortie
  internes) par `InterpreterResult.from_legacy_dict(raw).to_state_patch()`,
  vérifié par lecture directe du code (pas supposé) — un `UNKNOWN` ne
  peut structurellement pas sortir sans `unknown_reason`. Couvert par
  `tests/architecture/test_interpreter_result_contract.py` (préexistant,
  toujours vert). `interpretation_source` dédié n'existe pas comme champ
  séparé, mais `raw_analysis.path` en joue déjà le rôle et est peuplé sur
  les 16 points de sortie vérifiés (grep exhaustif) — pas un gap.
- Dette documentée (inchangée) : `cart_pending`/`_degraded_fallback`
  lisent encore le rôle COMPILE-TIME (`role_up`), pas `state["user_role"]`
  — usage SECONDAIRE toléré par le mandat (ne filtre rien
  structurellement), non corrigé (touche `core/router.py`/`GraphFactory`,
  hors périmètre strict).

### `cognitive_guard` — SAIN (bug + code mort corrigés)

- Interruption sans garde de confiance : CORRIGÉE lors du chantier
  précédent (seuil `_DISAMBIGUATION_CONFIDENCE_THRESHOLD`) — cette revue
  a RENFORCÉ la preuve : les tests "scénario C/D" du mandat (même goal,
  confiance haute → pas de fausse interruption ; UNKNOWN inconnu, confiance
  haute → pas de fausse interruption) fixaient auparavant
  `interpreter_confidence` à sa valeur par défaut (0.0 dans `make_state`)
  sans le savoir — ils prouvaient donc accidentellement le seuil de
  confiance plutôt que la condition "même intention"/"intention inconnue"
  qu'ils prétendaient tester. Corrigés pour fixer une confiance HAUTE
  explicite, isolant la vraie variable testée.
- **Code mort trouvé et supprimé** : `_should_trigger_disambiguation`
  (helper local, comparait `competition`/`confidence`) n'avait AUCUN
  appelant en production — seul `cognitive_orchestrator` (déjà mort)
  l'appelait. Même classe de bug que `should_replan`, déjà purgé.
  Supprimé + verrouillé par un test AST négatif
  (`TestShouldTriggerDisambiguationHasNoLiveCodeAnywhere`).
- Documentation stale trouvée et corrigée : `core/state.py` référençait
  encore "la docstring de `nodes/cognitive.py::cognitive_orchestrator`"
  pour expliquer `should_replan` — pointait vers une fonction qui n'existe
  plus. Corrigé.
- `disambiguation_candidate` : voir §C.
- Reste propriétaire de la progression UI (`conversation_progress`/
  `proactive_hint`) et du carry-forward d'entités — dette documentée,
  inchangée (mandat précédent, §7 : aucun autre propriétaire sûr dans le
  périmètre audité).

### `semantic_disambiguation` — SAIN

- Bug `or 1.0` : CORRIGÉ lors du chantier précédent (`or 0.0`) — cette
  revue a fait une recherche `or 1.0` sur les champs de confiance dans
  TOUT le package `market_coach` : **zéro occurrence**, confirmant
  l'éradication complète, pas seulement locale.
- Consulte `disambiguation_candidate` en priorité, garde un repli
  défensif local (`_detect_disambiguation_candidates`) — conforme au
  mandat ("garde-fou, pas une seconde policy complète").
- Ne recalcule PAS la décision "faut-il désambiguïser" depuis
  `cognitive_guard` — reste le seul propriétaire de cette décision
  précise (dette documentée §11 du mandat, acceptée).

### `clarification_node` — SAIN

- `_detect_disambiguation_candidates` retiré (import ET appel) —
  consulte `state["disambiguation_candidate"]` à la place. Verrouillé par
  `test_module_no_longer_imports_detect_disambiguation_candidates` +
  `test_precomputed_candidate_defers_to_semantic_disambiguation`.
- `TECHNICAL_FAILURE` → 0 second appel Gateway : déjà prouvé par un test
  préexistant (`test_technical_failure_never_calls_the_llm_and_returns_
  an_honest_degraded_message`, compteur `rt.llm.calls == 0`), re-vérifié
  vert après cette revue.
- Biais "un producteur" : CORRIGÉ lors du chantier précédent.

---

## B. Bugs trouvés (classés par catégorie)

**Bugs de state contract (cross-node ownership violation) :**
1. `services/onboarding.py::resolve_onboarding_state` écrivait
   `interpreted_event`/`detected_intent`/`interpreter_confidence` —
   contrat de sortie exclusif d'`input_interpreter`. Redondant à 100%
   (toujours réécrit ensuite), retiré.

**Bugs de routing (risque latent, pas encore exploité) :**
2. `security_moderation::_decision_for` (introduit par cette revue elle-
   même, corrigé avant merge) — un calcul naïf sur le patch seul aurait
   mal classé le cas "blocage déjà décidé en amont" en `ALLOW`.

**Duplications de policy / code mort :**
3. `nodes/cognitive.py::_should_trigger_disambiguation` — zéro appelant
   en production, supprimé.

**Documentation stale (pas un bug fonctionnel, mais trompeur) :**
4. Docstring de module `interpreter/routing.py` prétendait encore un
   filtrage par rôle retiré depuis le chantier précédent.
5. `core/state.py` référençait une docstring de fonction déjà supprimée
   (`cognitive_orchestrator`).

**Problèmes de performance (God-node drift) :**
6. `session_bootstrap` préchargeait les fermes (`preload_farms`) pour
   TOUS les utilisateurs, y compris les acheteurs qui n'en ont jamais
   besoin — retiré, le consommateur réel (`ensure_farm_node`) a déjà son
   propre repli paresseux.
7. `session_bootstrap` écrivait `working_memory.active_tunnel_label`,
   un champ sans aucun lecteur (travail perdu à chaque tour).

Aucun bug fonctionnel utilisateur-visible n'a été trouvé dans ce
périmètre (contrairement au chantier précédent) — cette revue a surtout
mis au jour de la dette de contrat (state ownership) et du code mort.

---

## C. Modifications réellement faites

**Modifiés :**
- `src/agriconnect/graphs/agents/market_coach/nodes/session_bootstrap.py`
  — retrait `preload_farms` + `active_tunnel_label`, docstring mise à jour.
- `src/agriconnect/graphs/agents/market_coach/services/onboarding.py` —
  retrait de l'écriture `interpreted_event`/`detected_intent`/
  `interpreter_confidence`.
- `src/agriconnect/graphs/agents/market_coach/nodes/security_moderation.py`
  — nouveau champ `security_decision` (calculé sur l'état effectif, pas
  seulement le patch).
- `src/agriconnect/graphs/agents/market_coach/nodes/routing.py` —
  `_route_after_security` lit `security_decision` en priorité.
- `src/agriconnect/graphs/agents/market_coach/core/state.py` — nouveaux
  champs `security_decision` ; commentaire stale corrigé.
- `src/agriconnect/graphs/agents/market_coach/core/state_profile.py` —
  déclaration EPHEMERAL de `security_decision`.
- `src/agriconnect/graphs/agents/market_coach/nodes/cognitive.py` —
  suppression de `_should_trigger_disambiguation` (code mort) + docstring
  explicite sur la propriété de la décision DISAMBIGUATE.
- `src/agriconnect/graphs/agents/market_coach/interpreter/routing.py` —
  docstring de module corrigée (stale).

**Créés :**
- `tests/architecture/test_security_boundary_compiled_graph.py` —
  Invariant B prouvé sur le graphe compilé réel.
- `tests/architecture/test_entry_block_topology.py` — topologie compilée
  réelle verrouillée (arêtes exactes, `role_guard`/`cognitive_orchestrator`
  absents, `session_bootstrap` bien positionné), pour les 2 variantes de
  rôle.

**Tests modifiés :**
- `tests/nodes/test_session_bootstrap.py` — retrait des tests farm-
  preload/tunnel-label (comportement supprimé), ajout de tests de
  non-régression explicites (`TestNoFarmPreloadDrift`,
  `TestNoDeadTunnelBookkeeping`, `TestPurityContract`).
- `tests/nodes/test_security_moderation.py` — `test_already_blocked_
  upstream_is_respected` mis à jour (assertion exacte devenue partielle +
  `security_decision`) ; nouvelle classe `TestSecurityDecisionField` (6
  tests, un par chemin BLOCK/ALLOW).
- `tests/nodes/test_cognitive_guard_and_orchestrator.py` — scénarios C/D
  du mandat corrigés (confiance haute explicite au lieu du défaut 0.0
  accidentel) ; `TestShouldTriggerDisambiguation` retirée (code
  supprimé), remplacée par une preuve négative.
- `tests/architecture/test_cognitive_decisions_are_consumed_or_removed.py`
  — 2 nouvelles classes de preuve négative AST (`cognitive_orchestrator`,
  `_should_trigger_disambiguation` absents de tout code source).
- `tests/unit/test_clarification_node.py` — nouvelle classe
  `TestClarificationNodeDoesNotRecomputeDisambiguationPolicy`.
- `tests/interpreter/test_extraction_and_llm_primacy.py` — test double-
  rôle symétrique ajouté (`test_dual_role_allows_buying_intent_for_a_
  producer`) + preuve statique du prompt (`test_interpreter_prompt_
  catalog_is_role_independent`).

---

## D. Topologie réelle du graphe compilé après correction

Introspectée directement (`graph.get_graph()`), pas relue depuis les
sources — identique pour les 2 variantes de rôle (PRODUCER/BUYER) :

```
START
  → session_bootstrap
  → input_normalizer
  → security_moderation
       to_interpreter → input_interpreter
       to_strategy    → response_strategy

input_interpreter
       to_cognitive    → cognitive_guard
       to_memory_fast  → memory_update      (FastPathPolicy, bypass zéro-token)

cognitive_guard
       to_onboarding    → onboarding_node
       to_clarification → clarification_node

clarification_node
       to_disambiguation → semantic_disambiguation
       to_strategy       → response_strategy

semantic_disambiguation
       to_planner  → goal_planner     [1er nœud NON audité]
       to_strategy → response_strategy
```

`role_guard` et `cognitive_orchestrator` : absents des nœuds du graphe
compilé (vérifié par introspection, verrouillé par
`tests/architecture/test_entry_block_topology.py`).

---

## E. Tests — résultats exacts

Suite complète (`python -m pytest tests/ --ignore=tests/evals -q`),
après toutes les corrections de cette revue :

- **6 échecs**, tous identiques à la baseline pré-existante déjà
  documentée dans le rapport du chantier précédent (`test_order_
  mutations_require_ownership.py` ×2, `test_create_auction_catalog_
  gate.py` ×4 — dates codées en dur, sans rapport avec ce périmètre).
- **Zéro nouvelle régression.**
- Nouveaux/modifiés dans ce périmètre : tous verts (voir détail des
  fichiers ci-dessus — chaque suite a été exécutée individuellement puis
  la suite complète a été rejouée une dernière fois après tous les
  correctifs).

---

## F. Dette restante (non résolue, documentée seulement)

Cette revue N'A PAS validé les nœuds situés après `goal_planner` — ils
restent hors périmètre, comme demandé. Dette identifiée mais non
touchée, dans le périmètre audité :

- `cart_pending`/`_degraded_fallback` (`interpreter/routing.py`) lisent
  le rôle compile-time plutôt que `state["user_role"]` — usage secondaire
  toléré, pas une violation structurelle du double-rôle.
- Deux graphes compilés distincts subsistent (`GraphFactory`, un par
  rôle) — devenus vestigiaux depuis le retrait du filtrage par rôle,
  mais l'infrastructure elle-même n'a pas été démantelée (touche
  `core/router.py`/`orchestrator.py`, hors périmètre strict).
- `session_bootstrap` reste un nœud technique intermédiaire non prévu
  par la lettre de la topologie cible originale — dette assumée et
  re-documentée cette fois avec la preuve précise de pourquoi (profil
  BUYER non préchargé par `orchestrator.py`).
- `semantic_disambiguation` reste le seul décideur de "faut-il
  désambiguïser" — `cognitive_guard` ne fournit qu'un candidat, pas une
  décision finale. Centraliser complètement exigerait de réconcilier
  deux heuristiques de confiance différentes (travail de conception, pas
  un correctif d'écart).
- Patterns d'injection anglais uniquement (`_CONTEXT_INJECTION_PATTERNS`)
  — limitation de couverture linguistique, pas un bug de routage,
  volontairement non étendue (mandat : pas de grosse liste multilingue).

Cette revue ne prétend PAS avoir validé `goal_planner`, `memory_update`,
`validator`, `domain_router`, `context_resolver`, les resolvers métier,
les tunnels, les executors, les finalizers, `response_strategy`,
`state_cleaner`, `final_response`, ni `post_response_cleanup`.
