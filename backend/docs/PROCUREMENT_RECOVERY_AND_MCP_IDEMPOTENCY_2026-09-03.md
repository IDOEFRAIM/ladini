# Recovery/réconciliation PROCUREMENT + idempotence MCP réelle — 2026-09-03

Rapport de clôture de la phase "recovery / idempotence MCP / audit PREORDER"
— suite directe de [PROCUREMENT_TRANSACTIONAL_PERSISTENCE_2026-09-03.md](PROCUREMENT_TRANSACTIONAL_PERSISTENCE_2026-09-03.md),
qui avait fermé la persistance PostgreSQL + CAS. Cette phase traite les
trois sujets explicitement demandés, dans l'ordre : (1) réconciliation
`EXECUTING`/`EXECUTION_UNKNOWN`, (2) idempotence réelle côté MCP, (3) audit
(pas migration) du workflow PREORDER.

---

## A. Architecture de recovery

```
find_stale_executing_candidates()          [services/database/procurement_draft_store.py]
   (status='EXECUTING' AND updated_at < now() - settings.PROCUREMENT_EXECUTING_STALE_SECONDS)
        │
        ▼
reconcile_draft(draft)                      [services/reconciliation/procurement_reconciliation_service.py]
        │
        ▼
mcp_idempotency_store.peek(execution_key(draft), "create_auction")   [LECTURE SEULE]
        │
   ┌────┼──────────────────┬───────────────────────┐
   │ COMPLETED trouvé  │ FAILED trouvé        │ PENDING / absent      │
   ▼                   ▼                      ▼
EXTERNAL_EFFECT_FOUND  CONFIRMED_NO_EFFECT   AMBIGUOUS
   │                   │                      │
   ▼                   ▼                      ▼
finalize_after_execution()  (RÉUTILISE le même adaptateur que le flux normal)
   │
   ▼
compare_and_swap(expected_version=draft.version, ...)   [CAS — même primitif que partout ailleurs]
   │
   ▼
EXECUTED / FAILED / EXECUTION_UNKNOWN, persisté
```

Déclenché par `workers/crons/procurement_reconciliation.py` (Celery Beat,
cadence `settings.PROCUREMENT_RECONCILIATION_INTERVAL_SECONDS`, défaut 300s).
Vit entièrement HORS de `confirmation_gate`/`mcp_tool_executor`/
`response_strategy` (consigne explicite respectée — module séparé, jamais
appelé par le graphe LangGraph).

---

## B. Idempotence MCP — où la clé entre, où elle est persistée, comment la dédup fonctionne

**Trace complète de la clé** (mandat §2.1) :

```
execution_key(draft)                         [domain/procurement_draft.py]
   → idempotency_key= sur MCPToolProvider.execute()   [actions/tool_provider.py]
   → idempotency_key= sur MarketRuntime.call_db()     [utils.py]
   → idempotency_key= sur AgriMCPClient.call_tool()   [infrastructure/mcp/client.py — DÉJÀ acceptait ce param]
   → _idempotency_key dans les arguments JSON transmis au transport
   → AgriDBMCPServer.call_tool()                      [infrastructure/mcp/runtime.py]
       AVANT (jusqu'à cette phase) : sanitized_args.pop("_idempotency_key") — RETIRÉE, jamais utilisée.
       MAINTENANT : extraite, puis mcp_idempotency_store.claim(key, tool_name, hash(payload))
   → si CLAIMED : exécution réelle → complete()/fail() persiste le résultat
   → si REPLAY : résultat REJOUÉ, l'outil N'EST PAS ré-exécuté
   → si CONFLICT : RuntimeError("idempotency_conflict: ...") — jamais résolu silencieusement
   → si IN_PROGRESS : RuntimeError("idempotency_in_progress: ... (temporary)") — retry transitoire existant de l'exécuteur (2 tentatives, backoff)
```

**Table de dédup RÉELLE** (`marketplace.mcp_idempotency_records`,
`services/database/mcp_idempotency_store.py`) :

```sql
CREATE TABLE marketplace.mcp_idempotency_records (
    idempotency_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,              -- PENDING | COMPLETED | FAILED
    external_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (idempotency_key, tool_name)
)
```

**Sémantique d'une requête répétée** (mandat §2.4, implémentée) : même clé +
même hash de payload → `REPLAY`, résultat existant renvoyé, aucune
ré-exécution. Même clé + hash DIFFÉRENT → `CONFLICT`, exception explicite,
jamais un résultat silencieux (les deux transactions gardent leur intégrité,
l'utilisateur doit recommencer proprement). Un `FAILED` confirmé (aucun
effet externe produit) peut être retenté sous la MÊME clé — testé
(`test_a_confirmed_failure_can_be_retried_under_the_same_key`).

**Ce n'est PAS une abstraction de façade** (mandat §2.2) : le mécanisme est
RÉELLEMENT branché au chokepoint unique `AgriDBMCPServer.call_tool` (tous
les outils MCP y passent), fonctionne pour N'IMPORTE QUEL outil qui pose
`_idempotency_key` (pas seulement `create_auction`), et est prouvé sous
concurrence RÉELLE de threads OS (`TestRealThreadConcurrency`, 10 threads,
exactement 1 `CLAIMED`).

---

## C. Sémantique de crash

| Scénario | Comportement |
|---|---|
| Crash AVANT `AgriDBMCPServer.call_tool` (jamais atteint) | Aucune ligne dans `mcp_idempotency_records`. `peek()` → `None` → réconciliation `AMBIGUOUS` → `EXECUTION_UNKNOWN`, jamais deviné. |
| Crash APRÈS `claim()` (`CLAIMED`), AVANT que `create_auction` ne retourne | Ligne reste `PENDING`. `peek()` → `PENDING` → réconciliation `AMBIGUOUS` → `EXECUTION_UNKNOWN`. **Limite honnête** (documentée dans le module) : ce cas est indiscernable de "une autre requête légitime est en cours" — ni cette table, ni la réconciliation, ne peuvent le résoudre seules ; nécessite un lookup par référence externe stable sur `Auction`, qui **n'existe pas** (aucune colonne `idempotency_key`/`client_request_id`, vérifié sur `domain/orders/models.py::Auction`). |
| `create_auction` a réussi, crash AVANT `complete()` | Même cas que ci-dessus (`PENDING`) — l'auction existe réellement mais ni le domaine ni cette table ne le savent tant que personne ne relit `Order`/`Auction` directement (hors périmètre de cette table, qui n'a pas de lookup métier). |
| `create_auction` a réussi, `complete()` a réussi, crash APRÈS (avant que `procurement_execution_finalizer.py` ne finalise le draft) | `peek()` → `COMPLETED` → réconciliation `EXTERNAL_EFFECT_FOUND` → `EXECUTED`. **Cas RÉSOLU automatiquement**, testé (`test_a_completed_mcp_record_is_found_and_finalizes_to_executed`). |
| `create_auction` a levé une exception métier AVANT toute écriture (ex: profil acheteur manquant) | `fail()` appelé → `FAILED`. Un retry ultérieur sous la même clé est légitime (`claim()` réouvre). **Limite documentée dans le code** : cette hypothèse ("exception = aucun effet") est correcte pour les handlers de ce dépôt qui valident avant d'écrire (vérifié pour `create_auction`), pas une garantie universelle pour tout futur handler. |
| Timeout MCP | `adapt_mcp_result(None, None)` → `ambiguous=True` → jamais un faux `FAILED`. |
| Redémarrage worker | `reconcile_draft` relit toujours l'état AUTORITATIF (peek + CAS), jamais un cache local — un worker B redémarré reconcilie de façon identique à celui qui a crashé. |
| `EXECUTING` bloqué indéfiniment | Détecté (`find_stale_executing`), traité par le cron `procurement-reconciliation` (Beat, cadence configurable). |

---

## D. Persistance — schéma final et CAS

Inchangé depuis le rapport précédent pour `procurement_drafts` (voir ce
rapport, section B), **complété** par `mcp_idempotency_records` (section B
ci-dessus) et par une requête de staleness :

```sql
SELECT ... FROM marketplace.procurement_drafts
WHERE status = 'EXECUTING' AND updated_at < now() - (:older_than_seconds || ' seconds')::interval
```

Décision explicite (documentée dans le code) : **pas de nouveau champ
`started_at`** — `updated_at` sert cette fonction pour une ligne
actuellement `EXECUTING` (aucune transition n'a eu lieu depuis qu'elle y
est entrée, par construction de la machine à état) ; ajouter un champ
séparé aurait recréé une 3e vérité pour représenter la même notion.

---

## E. Concurrence — tests réels et résultats

**Deux bugs RÉELS trouvés** (pas seulement vérifiés) par les tests de
concurrence de CETTE phase, tous deux CORRIGÉS :

1. **`with_status()` ne bumpait pas `version`** — un CONFIRM (transition de
   statut pure, sans changement de champ) laissait la colonne `version`
   inchangée en base ; un écrivain concurrent lisant la MÊME version pouvait
   donc gagner son CAS APRÈS le premier, écrasant la transition (lost
   update réel). Trouvé par
   `test_ten_threads_racing_the_same_expected_version_only_one_wins`.
   Corrigé : `with_status()` bump systématiquement ; le double-hop interne
   `DRAFT→CONFIRMED→EXECUTING` d'un CONFIRM est composé en UN SEUL
   incrément (`_confirm_to_executing()`), pas deux.
2. **Précédence `STALE_TARGET` vs politique de retry par statut** — la
   correction du bug 1 a exposé qu'`apply_domain_action` vérifiait la
   correspondance `ConfirmationTarget` AVANT la politique de retry par
   statut : un retry légitime (même target qui a déjà réussi) se voyait
   répondre `STALE_TARGET` au lieu de `ALREADY_EXECUTING`. Corrigé : la
   politique de retry par statut est évaluée EN PREMIER ; la correspondance
   de version ne s'applique QUE tant que le draft est encore `DRAFT`.

**Tests de concurrence de cette phase** (fichiers neufs) :
- `tests/unit/test_mcp_idempotency.py::TestRealThreadConcurrency` — 10
  threads OS, même clé/hash, exactement 1 `CLAIMED`.
- `tests/architecture/test_procurement_reconciliation_service.py::
  TestReconciliationConcurrency` — 5 threads réconciliant le MÊME draft
  simultanément, exactement 1 finalisation persistée.
- `tests/chaos/test_procurement_recovery_chaos.py::
  TestConfirmAndReconciliationRaceConcurrently` — un CONFIRM dupliqué ET
  un réconciliateur ciblant le MÊME draft en même temps, résultat cohérent
  garanti par le même CAS.

**Portée honnête inchangée** : faux moteur SQL fidèle (reproduit
`INSERT...ON CONFLICT`/`UPDATE...WHERE...→rowcount`), jamais un vrai
Postgres (toujours aucune infrastructure de test DB réelle dans ce dépôt).

---

## F. Réconciliation — détection, worker, récupération

- **Détection** : `find_stale_executing_candidates()`, seuil configurable
  (`settings.PROCUREMENT_EXECUTING_STALE_SECONDS`, défaut 900s) — pas de
  valeur codée en dur dans la logique.
- **Worker** : `workers/crons/procurement_reconciliation.py`, Celery Beat,
  cadence `settings.PROCUREMENT_RECONCILIATION_INTERVAL_SECONDS` (défaut
  300s). Idempotent par construction (réutilise le CAS existant, aucun
  verrou ad hoc).
- **Récupération** : 3 issues (EXTERNAL_EFFECT_FOUND/CONFIRMED_NO_EFFECT/
  AMBIGUOUS), jamais un 4e chemin caché.
- **Limite structurelle explicite** : `reconcile_draft` ne rappelle JAMAIS
  `create_auction` lui-même (vérifié par un test qui inspecte le code
  source du module et échoue si `mcp_tool_executor`/`MCPToolProvider`/
  `call_db`/`AgriMCPClient` y apparaissent). Un VRAI retry automatique de
  l'écriture nécessiterait de ré-entrer le graphe LangGraph comme un tour
  synthétique (pattern déjà utilisé ailleurs dans ce dépôt pour les
  sollicitations proactives) — **délibérément hors périmètre de cette
  phase**, voir section J.
- **`EXECUTION_UNKNOWN` reste terminal** (hérité d'un mandat antérieur,
  "nécessite une réconciliation HUMAINE") : si l'effet externe apparaît
  APRÈS qu'un draft soit passé `EXECUTION_UNKNOWN`, ce mécanisme ne le
  découvrira JAMAIS automatiquement — verrouillé explicitement par
  `test_once_execution_unknown_a_later_reconciliation_pass_is_a_safe_no_op`,
  pas une découverte accidentelle mais une limite assumée et testée.

---

## G. PREORDER — audit (pas de migration, conformément à la consigne)

### Architecture actuelle (`flows/buyer/preorder.py`, 684 lignes)

Sources de vérité PARALLÈLES identifiées (même classe de problème que
PROCUREMENT avant sa refonte) :
- `preorder_workflow` (dict mutable dans le state — `phase`, `preorder_id`,
  `total_amount`, `gps_stage`...) : AUCUN versionnement, AUCUN CAS.
- `transaction_payload["resolved_id"]` : pilote le branchement (CANCEL/
  ADD_MORE/CONFIRM) — même anti-pattern que l'ancien PROCUREMENT.
- `active_cart` : liste d'items, synchronisée à la main avec
  `preorder_workflow`, pas de garantie structurelle de cohérence.
- `pending_interaction` : **déjà** sur le mécanisme canonique
  (`set_pending_interaction`/`InteractionKind` — CONFIRM_ACTION,
  PROVIDE_LOCATION, ENTER_FIELD) — RÉUTILISABLE tel quel, rien à migrer ici.

### Différence IMPORTANTE avec PROCUREMENT (trouvaille de cet audit)

`confirm_preorder_draft` (`services/database/buyer.py:1704`) verrouille la
ligne `Order` (`SELECT ... FOR UPDATE`) et vérifie
`order.status != "DRAFT"` → lève `BusinessRuleException(reason="not_draft")`
si déjà confirmée. **`create_auction` n'a jamais eu d'équivalent** — c'est
exactement l'absence de ce type de garde qui a motivé toute la refonte
PROCUREMENT. PREORDER a donc DÉJÀ une protection serveur réelle contre la
double confirmation (débit stock, statut) — le risque financier de double
exécution y est structurellement plus bas. `create_preorder_draft` (côté
CRÉATION du brouillon), en revanche, n'a AUCUNE protection — un retry crée
une nouvelle ligne `Order(status=DRAFT)` à chaque fois (risque bas :
clutter, pas d'argent engagé).

### Décision architecturale (mandat §8.3) : PAS de `BaseTransactionalDraft`

Examiné et REJETÉ pour cette phase : une abstraction commune
`ProcurementDraft`/`PreorderDraft` obligerait `ProcurementDraft` (déjà
stabilisé, testé, en production dans cette refonte) à absorber des
concepts propres à PREORDER (`items: List[...]`, `delivery_zone_id`,
`gps_stage`) qui n'ont pas de sens pour un appel d'offres — ou inversement
forcerait PREORDER à adopter des champs procurement-spécifiques. Le
couplage introduit dépasserait la duplication qu'il évite. **Ce qui EST
réellement mutualisable, et le sera SANS nouvelle abstraction** :
`PendingInteraction`/`ConfirmationTarget` (déjà génériques),
`core/idempotency.py::claim_once` (déjà générique),
`services/database/procurement_draft_store.py` comme GABARIT direct à
dupliquer (même schéma, mêmes 4 fonctions, `table_name` différent — pas un
paramétrage générique tant qu'un 2e workflow ne le justifie pas
concrètement), `record_procurement_transaction_event`/
`record_procurement_reconciliation_event` (renommables en génériques le
jour où PREORDER les utilise réellement).

### Ce qui N'A PAS été fait cette phase (limite explicite, pas cachée)

Aucune ligne de `flows/buyer/preorder.py` n'a été modifiée. Ni
`PreorderDraft`, ni table `preorder_drafts`, ni `ConfirmationTarget`
PREORDER n'existent. La consigne du mandat ("Une fois PROCUREMENT
complètement fermé, commence UNIQUEMENT la migration PREORDER") supposait
une migration complète dans cette même session ; la charge de travail
équivalente (persistance CAS + machine à état + idempotence + réconciliation
+ tests de concurrence réels) est celle des QUATRE mandats précédents
combinés pour PROCUREMENT. Livrer une version partielle/pressée de ce
niveau de garantie transactionnelle contredirait directement la règle
absolue du mandat lui-même ("ne déclare pas transactionnel si le contrôle
de version n'est pas réellement persisté et atomique"). Cette section G
EST l'audit complet demandé (§8.1) et la décision d'architecture (§8.3) ;
la construction de `PreorderDraft` (§8.2/8.4-8.7) reste le prochain
chantier, scopé et prêt à démarrer.

---

## H. Code supprimé / remplacé

- `sanitized_args.pop("_idempotency_key", None)` sans usage →
  **remplacé** par le cycle complet `claim()`/`complete()`/`fail()` — la
  clé n'est plus retirée sans effet, elle protège réellement.
- Deux appels `with_status()` séquentiels pour CONFIRM → `_confirm_to_executing()`.
- Ordre `target.matches()`/politique de retry par statut inversé dans
  `apply_domain_action`.
- Aucune nouvelle couche sans remplacer une ancienne : `mcp_idempotency_store`
  ne s'ajoute PAS à côté de `execution_key()` — elle EST ce que
  `execution_key()` attendait depuis sa création (sa propre docstring le
  documentait déjà comme un manque à combler).

---

## I. Tests (unit / architecture / DB simulée / concurrency / chaos)

| Fichier | Tests | Portée |
|---|---|---|
| `tests/unit/test_mcp_idempotency.py` | 17 | Sémantique `claim`/`complete`/`fail`, concurrence réelle (10 threads), câblage `AgriDBMCPServer.call_tool` (CLAIMED/REPLAY/CONFLICT/IN_PROGRESS/UNAVAILABLE/no-op) |
| `tests/architecture/test_procurement_reconciliation_service.py` | 9 | 3 issues de réconciliation, idempotence, concurrence réelle (5 threads), preuve structurelle "jamais d'appel MCP" |
| `tests/chaos/test_procurement_recovery_chaos.py` | 6 | Résolution en un passage, limite `EXECUTION_UNKNOWN` terminal, DB/store indisponibles, Redis indisponible protégé par CAS, CONFIRM+réconciliation concurrents |
| `tests/architecture/test_procurement_draft_persistence.py` | +4 (staleness) | `find_stale_executing` : détection, non-détection (frais/terminal), DB indisponible |

Régression complète (`pytest tests -q`) : **zéro nouvelle régression**,
mêmes 4 échecs pré-existants (`test_create_auction_catalog_gate.py`,
date codée en dur, sans rapport).

---

## J. Limites restantes (réelles, non maquillées)

1. **Exactly-once externe toujours PAS garanti** pour `create_auction` — la
   fenêtre "PENDING sans lookup métier possible" (section C) reste ouverte
   tant qu'`Auction` n'a pas de colonne de référence externe stable.
2. **Retry automatique de l'écriture non implémenté** — `reconcile_draft`
   classifie et finalise, ne relance jamais `create_auction`. Nécessiterait
   une ré-entrée synthétique du graphe (pattern Outbox déjà présent
   ailleurs dans ce dépôt pour d'autres besoins).
3. **`EXECUTION_UNKNOWN` est un cul-de-sac définitif** dans la machine à
   état actuelle — une preuve tardive de succès externe n'y change rien
   automatiquement.
4. **`attempt` (champ d'observabilité demandé) n'est pas un compteur
   persisté cross-tick** — paramètre optionnel de `reconcile_draft`,
   défaut 1, jamais lu/écrit en base.
5. **PREORDER n'a reçu qu'un audit**, aucune migration de code (section G).
6. **Le worker `procurement_reconciliation.py` n'a pas de test Celery
   dédié** (il délègue entièrement à `reconciliation_service`, déjà testé
   à 9 tests — le test du wrapper `@celery_app.task` lui-même suivrait le
   même schéma que les crons existants, non dupliqué ici par manque de
   valeur ajoutée réelle).
