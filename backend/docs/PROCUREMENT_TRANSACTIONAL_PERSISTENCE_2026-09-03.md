# Persistance transactionnelle PROCUREMENT_CREATE_REQUEST — 2026-09-03

Rapport de clôture du 4e mandat de la refonte transactionnelle du flux
`PROCUREMENT_CREATE_REQUEST` (WhatsApp, agent MarketCoach). Suite directe de
trois mandats précédents (deep refactor confirmation flow → architectural
hardening pass → clôture du pipeline d'exécution). Ce mandat traitait les
trois derniers problèmes structurels identifiés : (1) persistance non
transactionnelle du draft, (2) ambiguïté de l'exécution externe après crash,
(3) observabilité non corrélée.

Condition finale posée par le mandat, reprise ici verbatim comme critère de
lecture de ce rapport : *"Je ne veux pas que tu déclares ce travail 'terminé'
si `ProcurementDraft` reste uniquement un objet dans le state LangGraph sans
protection transactionnelle suffisante. Je ne veux pas non plus que tu
prétendes résoudre le exactly-once externe si MCP ne le permet pas."*

---

## A. Architecture finale

```
                        ┌─────────────────────────────┐
                        │   PostgreSQL (canonique)     │
                        │ marketplace.procurement_drafts│
                        │ PK draft_id, version INTEGER  │
                        └───────────┬──────────────────┘
                     insert v1      │  compare_and_swap (CAS)
           ┌─────────────────────┐ │ ┌──────────────────────────┐
           │ confirmation_gate   │─┘ │ procurement_confirmation  │
           │ (bootstrap v1)      │   │ (UPDATE/CONFIRM/REJECT)   │
           └─────────────────────┘   └────────────┬──────────────┘
                                                    │ CONFIRMED_READY_FOR_EXECUTION
                                                    ▼
                                     transaction_payload = draft.execution_payload()
                                     execution_authorized = True
                                                    │
                                                    ▼
                                     mcp_tool_executor (générique)
                                     idempotency_key = execution_key(draft)
                                                    │ execution_status / execution_result
                                                    ▼
                                     procurement_execution_finalizer
                                     (relit l'autoritatif, CAS EXECUTING→terminal)
                                                    │
                                                    ▼
                                     DomainOutcome → build_response_plan (pur)
                                     → apply_response_plan (mécanique) → Dispatcher
```

`ProcurementDraft` (LangGraph state) est désormais une **projection de
travail** : rafraîchie à chaque nœud avec ce que PostgreSQL a réellement
accepté, jamais relue comme autoritative pour décider. PostgreSQL
(`marketplace.procurement_drafts`) est la **source canonique**, protégée par
un compteur de version en CAS (`UPDATE ... WHERE version = :expected`).

3 points de mutation, tous CAS-protégés :
1. [confirmation_gate.py](../src/agriconnect/graphs/agents/market_coach/nodes/confirmation_gate.py) — `INSERT` du draft v1 (bootstrap).
2. [procurement_confirmation.py](../src/agriconnect/graphs/agents/market_coach/flows/buyer/procurement_confirmation.py) — `compare_and_swap` après chaque `UPDATE`/`CONFIRM`/`REJECT`.
3. [procurement_execution_finalizer.py](../src/agriconnect/graphs/agents/market_coach/flows/buyer/procurement_execution_finalizer.py) — `compare_and_swap` EXECUTING→EXECUTED/FAILED/EXECUTION_UNKNOWN.

---

## B. Persistance (schéma + CAS)

Module : [services/database/procurement_draft_store.py](../src/agriconnect/services/database/procurement_draft_store.py).

```sql
CREATE TABLE IF NOT EXISTS marketplace.procurement_drafts (
    draft_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_procurement_drafts_conversation
    ON marketplace.procurement_drafts (conversation_id);
```

**Décision explicite : snapshot versionné, PAS un event log.** Une seule
ligne par `draft_id`, `version` comme compteur de verrouillage optimiste.
Justification (détaillée dans la docstring du module) : `ProcurementDraft`
est déjà un modèle à instantané unique côté domaine — un event log
dupliquerait ce modèle plutôt que de le persister. Trade-off assumé : aucun
historique interrogeable des corrections intermédiaires en base (seul le
log applicatif `PROCUREMENT_TRACE`/Langfuse en garde trace) ; après un
crash, seul l'état final connu est relisible, jamais "à quoi ressemblait
v2". Jugé acceptable car la réconciliation (§E) porte sur l'ISSUE de
l'exécution, pas sur l'historique des corrections utilisateur.

Le CAS lui-même :
```sql
UPDATE marketplace.procurement_drafts
SET version = :new_version, status = :status, payload = :payload, updated_at = now()
WHERE draft_id = :draft_id AND version = :expected_version
```
`rowcount == 1` ⇒ cette écriture a gagné la course. `rowcount == 0` ⇒ soit un
autre écrivain a déjà avancé la version (VERSION_CONFLICT réel), soit la DB
est injoignable — **ces deux cas sont désormais distingués explicitement**
(voir §D) : le second n'est PLUS confondu avec le premier.

---

## C. Concurrence (tests SQL réels + résultats)

**Portée honnête** (choix approuvé explicitement par l'utilisateur face à
l'absence d'infrastructure Postgres de test dans ce dépôt — pas de
docker-compose, pas de fixture DB) : les tests de concurrence tournent
contre un **faux moteur qui reproduit fidèlement** la sémantique
`SELECT`/`INSERT ... ON CONFLICT DO NOTHING`/`UPDATE ... WHERE ... →
rowcount` des 3 requêtes réelles ci-dessus — jamais contre un vrai Postgres.
Ces tests prouvent que la **logique Python** autour du CAS est correcte sous
concurrence RÉELLE (vrais threads OS, pas seulement séquentielle) ; ils NE
PROUVENT PAS que le SQL lui-même est valide contre un moteur réel (types
JSONB, contrainte `PRIMARY KEY`, `ON CONFLICT` réel — validation CI contre
une vraie base : chantier séparé, non fait).

Fichier : [tests/architecture/test_procurement_draft_persistence.py](../tests/architecture/test_procurement_draft_persistence.py) (16 tests).

Résultat notable — **un vrai bug trouvé par ce test, pas seulement vérifié**
: `test_ten_threads_racing_the_same_expected_version_only_one_wins` (10
threads OS, même `expected_version`, `compare_and_swap` simultané) a
d'abord échoué : `with_status()` (transition de statut pure, ex.
CONFIRM/CANCEL/finalisation) ne faisait PAS avancer `version` — seul
`with_updates()` (changement de champ) le faisait. Conséquence réelle :
un `CONFIRM` puis un `UPDATE` concurrents, tous deux lus à `expected_version=N`,
pouvaient TOUS LES DEUX gagner leur `compare_and_swap` en séquence (le
premier ne changeant pas `version`, le second matchait encore le même
`WHERE version = N`) — un vrai lost-update, la protection CAS n'était donc
PAS complète. **Corrigé** : `with_status()` bump désormais systématiquement
la version (une ligne persistée qui change — statut ou payload — est une
nouvelle génération), et la double-transition interne
`DRAFT→CONFIRMED→EXECUTING` d'un CONFIRM est composée en **un seul**
incrément (`_confirm_to_executing()`), pas deux, pour rester "un événement
métier = une version". Cette correction a elle-même révélé un second bug de
précédence (§D).

Autres résultats de concurrence, tous verts :
- `test_update_then_confirm_racing_through_the_real_node_yields_one_valid_transition`
  — course réelle à travers `resolve_procurement_confirmation` (pas les
  fonctions du store isolées) : ligne finale toujours cohérente (soit
  EXECUTING, soit DRAFT v2 — jamais un mélange).
- `TestUpdateConfirmRace` (`test_procurement_execution_pipeline_closure.py`,
  10 threads, 5 CONFIRM / 5 UPDATE) — au plus 1 exécution quel que soit
  l'ordre d'arrivée réel.

---

## D. Exécution (chemin exact vers MCP)

```
apply_domain_action (CONFIRM) → CONFIRMED_READY_FOR_EXECUTION
    → transaction_payload = draft.execution_payload()
    → execution_authorized = True
    → mcp_tool_executor (nodes/executor.py, générique, inchangé structurellement)
        → idempotency_key = execution_key(draft)   [NOUVEAU, §E]
        → MCPToolProvider.execute(tool_name, args, idempotency_key=...)
        → MarketRuntime.call_db(tool_name, idempotency_key=..., **args)
        → AgriMCPClient.call_tool(tool_name, args, idempotency_key=...)
    → execution_status / execution_result (bruts) dans le state
    → procurement_execution_finalizer (relit l'autoritatif, adapt_mcp_result, CAS)
```

**Bug de précédence trouvé et corrigé pendant ce mandat** (conséquence
directe du bump de version ci-dessus) : `apply_domain_action`'s branche
CONFIRM vérifiait la correspondance `ConfirmationTarget` **avant** la
politique de retry par statut. Une fois `CONFIRM` bumpant réellement la
version, un retry Celery légitime (même `ConfirmationTarget` que celui qui a
fait passer le draft en EXECUTING) se voyait répondre `STALE_TARGET` au lieu
de `ALREADY_EXECUTING` — un faux rejet, découvert par
`TestE_CrashAfterClaimLeavesAnHonestState`/`TestConfirmIsIdempotent` qui
échouaient après le fix de version. **Corrigé** : la politique de retry par
statut (EXECUTED/FAILED/EXECUTING/EXECUTION_UNKNOWN/CANCELLED) est
maintenant évaluée AVANT la correspondance de version — `STALE_TARGET` n'a
de sens QUE tant que le draft est encore `DRAFT` (édition concurrente),
jamais une fois sorti de cet état.

Séparation stricte préservée : le dispatcher/exécuteur générique
(`mcp_tool_executor`) ne connaît AUCUN état métier procurement — il lit
`state["procurement_draft"]` uniquement pour dériver une clé (générique, no-op
pour les 15+ autres goals qui ne posent pas ce champ), jamais pour décider
quoi que ce soit.

---

## E. Idempotence (garantie exacte + limites)

**Trois mécanismes distincts, complémentaires, jamais confondus :**

| Mécanisme | Portée | Protège contre | Ce qu'il NE fait PAS |
|---|---|---|---|
| `claim_once` (Redis, `core/idempotency.py`) | Locale, TTL 3600s | Deux CONFIRM concurrents dans CE process/cette flotte de workers | Ne survit pas à un flush Redis ; pas de garantie externe |
| `execution_key` / `idempotency_key` MCP | Client → transport | Corrélation (logs `MCP_CALL_AUDIT`, Langfuse) + retry interne `AgriMCPClient` (rejoue la MÊME clé après coupure réseau, jamais une nouvelle par tentative) | **Audité : `AgriDBMCPServer.call_tool` retire `_idempotency_key` AVANT dispatch — `create_auction` n'a AUCUNE déduplication côté serveur.** Deux appels avec la même clé mais deux connexions TCP distinctes créent DEUX auctions. |
| `version`/CAS (PostgreSQL) | Business state | Deux écrivains faisant avancer le MÊME draft simultanément (race UPDATE/CONFIRM) | Ne protège pas l'effet EXTERNE (`create_auction`), seulement la ligne `procurement_drafts` |

**Garantie honnête, non maquillée** : ce mandat obtient de l'**at-most-once
best-effort côté DB business-state** (grâce au CAS) + une **corrélation
client-side réelle** de l'exécution MCP. Il n'obtient **PAS** l'exactly-once
externe — `create_auction` reste appelable deux fois avec la même
`idempotency_key` si les deux appels traversent des connexions/process
distincts après un crash. C'est pourquoi `EXECUTION_UNKNOWN` +
réconciliation (jamais un retry aveugle) existe : c'est le palliatif à
l'absence de dédup serveur, pas une solution qui la remplace.

---

## F. Recovery (crash avant/après MCP, timeout, redémarrage)

| Scénario | Comportement garanti |
|---|---|
| Crash APRÈS claim, AVANT l'appel MCP | Draft persisté `EXECUTING` (CAS avant tout appel MCP, §A). Un retry Celery relit l'AUTORITATIF, statut `EXECUTING` → `ALREADY_EXECUTING`, jamais un second `create_auction`. |
| MCP a réussi, crash AVANT finalisation | Draft reste `EXECUTING` en base — **PAS une fausse `EXECUTED`**. Un retry ultérieur reste `ALREADY_EXECUTING` (réponse sûre identique, qu'un autre worker traite réellement ou qu'un crash passé soit en cause — mandat §12). |
| Timeout MCP (ni succès ni erreur net) | `adapt_mcp_result` ne devine JAMAIS : tout ce qui n'est pas clairement `COMPLETED`/`ERROR` → `ambiguous=True` → `finalize_after_execution` → `EXECUTION_UNKNOWN`. Un CONFIRM ultérieur exige réconciliation (`RECONCILIATION_REQUIRED`), jamais un nouveau `create_auction`. |
| Redémarrage worker | Testé (`TestCrashAndRetryVariants`, `TestExecutionFinalizerNode`) — le finaliseur relit `procurement_draft_store.load()` (autoritatif) avant de finaliser, pas le cache LangGraph potentiellement périmé du worker mort. |
| `EXECUTING` bloqué indéfiniment | **Détecté, pas seulement documenté** : `procurement_draft_store.find_stale_executing(older_than_seconds=...)` (requête réelle sur `updated_at`, voir §G) retourne les drafts `EXECUTING` figés depuis plus de N secondes. **Non câblé en cron cette session** (§I — dette assumée et documentée, pas silencieuse) : la fonction existe et est testée, un job Celery Beat périodique qui l'appelle et applique `EXECUTION_UNKNOWN` reste à écrire. |

---

## G. Observabilité (tracing réel + Langfuse)

**Ce qui existait avant ce mandat** : `logger.info("PROCUREMENT_TRACE"...)` —
explicitement **PAS** de l'instrumentation Langfuse, quoi qu'en dise le nom.

**Ce qui a été ajouté** : `core/telemetry.py::record_procurement_transaction_event`
— un événement Langfuse RÉEL par appel à `apply_domain_action`/
`finalize_after_execution`, suivant EXACTEMENT le précédent déjà établi
(`record_state_transition`, mandat §35 d'un chantier antérieur) : même
client `_langfuse_client`, même `_ensure_langfuse_trace(trace_id)`, même
discipline défensive (`except Exception` → jamais un tour cassé). Champs
capturés : `event_id`, `conversation_id`, `message_id`, `draft_id`,
`draft_version_before/after`, `pending_kind`, `confirmation_target`,
`interpreter_event`, `domain_action`, `state_before/after`,
`execution_key`, `external_request_id`, `mcp_status`, `execution_result`,
`outcome`, `error` — la liste exacte demandée. Câblé aux 3 points de
mutation (bootstrap, confirmation, finalisation). Le `logger.info` existant
est **conservé** (utile au grep local) mais explicitement documenté comme
n'étant PAS l'instrumentation exigée.

**Exemple concret** — scénario "2 tonnes 250 kg → 1 tonne 125 kg → okay" :

1. `bootstrap_draft_v1` : `draft_version_before=None`, `draft_version_after=1`, `pending_kind=CONFIRM_ACTION`, `state_after=DRAFT`.
2. `UpdateProcurementDraft` : `draft_version_before=1`, `draft_version_after=2`, `domain_action=UpdateProcurementDraft`, `state_before=DRAFT`, `state_after=DRAFT`.
3. `UpdateProcurementDraft` : `draft_version_before=2`, `draft_version_after=3`, mêmes états.
4. `ConfirmProcurementDraft` : `draft_version_before=3`, `draft_version_after=4` (CONFIRM bump désormais, §C), `state_before=DRAFT`, `state_after=EXECUTING`, `execution_key=procurement:<id>:3`, `outcome=CONFIRMED_READY_FOR_EXECUTION`.
5. `finalize_after_execution` : `draft_version_before=4`, `draft_version_after=5`, `state_before=EXECUTING`, `state_after=EXECUTED`, `mcp_status=COMPLETED`, `external_request_id=<auction_id>`, `outcome=PROCUREMENT_EXECUTED`.

Les 5 événements partagent le même `trace_id` (contexte de tour courant,
`_current_trace_id`) — corrélation de bout en bout dans une seule Trace
Langfuse, exactement ce que le mandat demandait.

**Limite honnête** : `message_id` est lu depuis `state.get("message_sid")`,
un champ qui **n'existe pas encore** dans `MarketAgentState` (vérifié —
absent de `core/state.py`) ; il vaudra `None` tant qu'aucun nœud amont ne le
pose. Non fabriqué, non simulé — simplement pas encore câblé en amont
(hors périmètre de ce mandat, qui portait sur la persistance/exécution).

---

## H. Code supprimé / remplacé

- `ALREADY_CONFIRMED` (kind unique) → remplacé par 4 kinds distincts (`ALREADY_EXECUTED`, `ALREADY_FAILED`, `ALREADY_EXECUTING`, `RECONCILIATION_REQUIRED`) — mandats précédents.
- `finalize_after_execution(draft, succeeded: bool)` → `finalize_after_execution(draft, ProcurementExecutionResult)` — découplage du format MCP.
- Deux appels `with_status()` séquentiels pour CONFIRM → `_confirm_to_executing()` (un seul incrément de version, ce mandat).
- Ordre `target.matches()` avant/après la politique de retry par statut inversé (ce mandat, §D).
- `logger.info("PROCUREMENT_TRACE"...)` seul → conservé + `record_procurement_transaction_event` réel ajouté à côté (pas un remplacement pur, un ajout honnête).

Aucune couche n'a été ajoutée sans remplacer explicitement ce qu'elle
supersédait (interdiction du mandat §21, respectée) : `transaction_payload`
n'est JAMAIS une 3e vérité — il est produit une seule fois, au dernier
moment, via `draft.execution_payload()` dans `apply_response_plan`.

---

## I. Tests

- **Unitaires/architecture** : 16 nouveaux tests dans `test_procurement_draft_persistence.py` (sémantique CAS, concurrence réelle par threads, bout-en-bout via `resolve_procurement_confirmation`, mode dégradé, détection de staleness).
- **Régression** : suite complète (`tests/`) — **zéro nouvelle régression**, les 4 échecs restants (`test_create_auction_catalog_gate.py`) sont pré-existants, confirmés identiques à chaque exécution complète depuis le début de cette session (indépendants de ce mandat, une date codée en dur).
- **Corrections en cours de route** (détail §C/§D) : 2 bugs RÉELS trouvés par les nouveaux tests de concurrence (version non bumpée par `with_status`, précédence `STALE_TARGET` vs retry-par-statut) — corrigés, pas contournés côté test.
- **Flakiness pré-existante notée, non corrigée** : `tests/chaos/test_event_loop_nonblocking.py::test_parallel_llm_calls_overlap_not_serialize` a échoué UNE fois sous charge de la suite complète (contention liée aux nombreuses tentatives de connexion DB réelle qu'introduit ce mandat) puis repassé systématiquement en isolation — timing-sensible, pas une régression logique. Non traité (mitigation = mocker `get_sessionmaker` globalement pour la suite — hors périmètre de ce mandat, signalé pour un futur nettoyage).

---

## J. Audit suivant — plan de migration structuré (mandat §19)

Tableau comparatif par workflow, mêmes 7 dimensions que le contrat
PROCUREMENT (déjà migré) :

| Workflow | État canonique aujourd'hui | Payload mutable | Cible de confirmation | Versionnement | Response plan | Idempotence | Persistance |
|---|---|---|---|---|---|---|---|
| **PROCUREMENT_CREATE_REQUEST** | `ProcurementDraft` (dataclass figée) | Non — `with_updates` produit un nouvel objet | `ConfirmationTarget(draft_id, version)` | Oui — compteur `version`, CAS réel | `ProcurementResponsePlan`, pur | `claim_once` + CAS + `idempotency_key` client | **PostgreSQL, CAS réel** ✅ |
| **BUYER_PREORDER_INIT/CONFIRM** | `transaction_payload` (dict mutable, `merge_dict`) | Oui — mutation en place | `waiting_for_confirmation: bool` (pas de version) | Non | Construit ad hoc dans plusieurs nœuds | `claim_response_item` (dispatch uniquement, pas la décision métier) | LangGraph checkpoint seul |
| **SALES_PUBLISH_PRODUCT / SALES_UPDATE** | idem preorder | Oui | idem | Non | idem | Aucune au niveau décision | Checkpoint seul |
| **STOCK_ADJUST / STOCK_REMOVE** | idem | Oui | idem | Non | idem | Aucune | Checkpoint seul |
| **CART_* (add/view/checkout)** | idem, + `tier_selection_context`/`vendor_selection_context` séparés | Oui | idem | Non | idem | Aucune | Checkpoint seul |

**Ordre de migration recommandé** (repris et confirmé du rapport
précédent) :

1. **`BUYER_PREORDER_INIT`/`BUYER_PREORDER_CONFIRM` — prochain candidat,
   risque le plus élevé.** Manipule un engagement financier (réservation de
   préproduction) sans stock débité immédiatement ; sujet à la MÊME classe
   de bug que celui corrigé pour PROCUREMENT (récap figé, confirmation sur
   état périmé) — voir [[future-production-preorder-loop]] déjà en mémoire :
   "UX wiring encore TODO". Bénéficierait directement du même patron
   (`PreorderDraft` versionné, `ConfirmationTarget`, table
   `marketplace.preorder_drafts` avec le même schéma CAS).
2. **SALES_PUBLISH_PRODUCT / SALES_UPDATE** — risque moyen (pas d'argent
   engagé avant confirmation humaine, mais mutation catalogue visible
   publiquement immédiatement après confirmation).
3. **STOCK_ADJUST / STOCK_REMOVE** — risque moyen-bas (réversible via un
   mouvement de stock correctif), mais partage le même `transaction_payload`
   mutable et mériterait le même traitement pour cohérence de code plutôt
   que pour un incident connu.
4. **CART_*** — risque le plus bas dans l'urgence (pas d'écriture externe
   avant `PROCUREMENT_CREATE_REQUEST`/checkout), mais porte la dette la plus
   ancienne (`tier_selection_context`/`vendor_selection_context` comme
   troisième état parallèle) — bénéficierait de la même discipline
   `PendingInteraction` déjà partiellement posée ailleurs dans ce repo (voir
   `core/pending_interaction.py`, déjà générique et RÉUTILISABLE tel quel
   pour ces 4 workflows, contrairement à `ProcurementDraft` qui est
   spécifique au métier procurement).

**Ce qui est réutilisable tel quel pour ces 4 migrations** (pas à
reconstruire) : `core/idempotency.py::claim_once` (primitif partagé),
`services/database/procurement_draft_store.py` comme GABARIT direct (même
schéma DDL, même fonctions `load`/`insert`/`compare_and_swap`/
`find_stale_executing`, il suffit de dupliquer le module avec un nom de
table différent — ou de le généraliser en un store paramétré par
`(table_name, dataclass_type)` si 2+ workflows migrent réellement, pour
éviter la duplication qu'un simple copier-coller introduirait), et
`core/telemetry.py::record_procurement_transaction_event` (renommable en
`record_draft_transaction_event` générique le jour où un 2e workflow
l'utilise).

---

## Ce qui N'EST PAS fait (honnêteté explicite, condition finale du mandat)

- **`ProcurementDraft` n'est PLUS "uniquement un objet dans le state
  LangGraph"** — condition du mandat satisfaite : PostgreSQL est la source
  canonique aux 3 points de mutation, avec CAS réel testé sous concurrence
  de threads réels (portée : faux moteur fidèle, pas un vrai Postgres — non
  masqué, voir §C).
- **L'exactly-once externe N'EST PAS résolu** — condition du mandat
  également respectée dans le sens inverse : ce rapport ne le prétend à
  aucun moment. `create_auction` reste sans déduplication serveur ; la
  garantie obtenue est at-most-once côté état métier (DB) + corrélation
  client, avec `EXECUTION_UNKNOWN`/réconciliation comme palliatif honnête.
- Le job de réconciliation périodique (Celery Beat) qui consommerait
  `find_stale_executing` n'est **pas câblé** — la requête existe et est
  testée, son appelant cron n'existe pas.
- `message_id` dans la télémétrie est `None` en pratique tant que
  `state["message_sid"]` n'est pas posé par un nœud amont (webhook/interpreter)
  — champ prévu, pas encore alimenté.
- La migration PREORDER/SALES/STOCK/CART reste un PLAN (§J), aucune ligne
  de code de ces 4 workflows n'a été touchée ce mandat, conformément à la
  consigne "analyser, pas migrer" des mandats précédents, reconduite ici.
