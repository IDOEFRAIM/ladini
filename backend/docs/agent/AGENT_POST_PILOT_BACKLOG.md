# Agent — Post-Pilot Hardening Backlog

Statut : clôture du chantier reliability hardening, 2026-09-28. Compagnon de
`AGENT_PRODUCTION_READINESS.md` et `AGENT_PILOT_RUNBOOK.md`.

Ce document contient trois choses : (1) le backlog explicite de ce qui n'est
PAS fait et pourquoi, (2) les conditions mesurables pour passer du pilote
restreint au déploiement non restreint, (3) le registre de toutes les issues
connues, chacune dans une seule catégorie.

## 1. POST-PILOT HARDENING BACKLOG

### B1 — P1 futur : DURABLE INBOUND INBOX / DEAD-LETTER

**Pourquoi.** L'invariant « webhook accepté ⇒ message traité ou explicitement
mis en dead-letter » n'est prouvé qu'à moitié. Aujourd'hui (I6) un webhook ne
répond 200 que si l'enqueue Celery a réussi ; sur échec il répond 503 et
relâche son claim, et le **provider** redélivre. Mais si la panne du broker
dure plus longtemps que la fenêtre de redélivrance du provider, le message est
perdu **sans trace chez nous** : il n'existe aucun enregistrement durable d'un
message reçu, donc rien à rejouer ni à compter.

**Architecture cible.**

```
provider webhook
  -> persist inbound_message (état RECEIVED)      <- dans la même transaction
  -> commit
  -> 200 au provider                              <- ACK seulement APRÈS commit
  -> dispatcher (poll ou outbox) : RECEIVED -> QUEUED (enqueue Celery)
  -> worker : QUEUED -> PROCESSING -> PROCESSED
  -> échec : PROCESSING -> FAILED (retry borné) -> DEAD
```

États : `RECEIVED`, `QUEUED`, `PROCESSING`, `PROCESSED`, `FAILED`, `DEAD`.
Clé d'unicité : `(provider, message_id)` — ce qui remplace le claim Redis
`msg:{id}` comme dédup **durable** (le claim Redis reste utile comme
accélérateur). `DEAD` doit déclencher une alerte et être rejouable à la main.
Le sous-système existant d'outbox (`workers/`, motif Outbox des solicitations
proactives) est le point de départ le plus naturel pour le dispatcher.

**Pourquoi PAS maintenant.** Ce backend ne peut pas créer cette table :
`schema_contract/` est un miroir en lecture seule généré depuis le dépôt
frontend Drizzle, et `tests/schema/test_schema_contract_static.py::
test_no_runtime_ddl_in_backend_source` interdit toute DDL runtime. La réaliser
exige donc une migration Drizzle **côté frontend**, la resynchronisation du
contrat, puis le code backend — un chantier cross-repo, pas un correctif de
clôture. La forcer ici aurait soit cassé le garde-fou, soit créé une table
fantôme jamais reconnue par la vraie base.

**Impact.** Ferme le seul risque résiduel qui distingue « pilote restreint »
de « déploiement non restreint » (message reçu mais jamais traité, sans trace).
Donne aussi la base de `inbound_recovered_messages` (métrique aujourd'hui
impossible faute de signal de redélivrance).

**Priorité.** P1 — **bloquant pour le passage non restreint**, non bloquant
pour le pilote (compensé par G1–G7 du runbook).

**Condition de déclenchement avant large-scale.** Doit être **implémenté et
testé** (dont un exercice : broker coupé plus longtemps que la fenêtre de retry
du provider, puis vérification que tous les messages sont retrouvés en
`RECEIVED`/`QUEUED` et traités) **avant** de retirer la garde G2 (volume faible)
ou d'ouvrir à des utilisateurs non connus. Déclencheur d'anticipation : dès que
le premier `inbound_enqueue_failures > 0` en production est suivi d'un message
non retrouvé (cas 4 du runbook), cette priorité passe à « immédiat ».

### B2 — P1 futur : métriques worker visibles

Les compteurs `workspace_reconciliation_failures` et `transaction_retry_count`
sont incrémentés dans le worker Celery, qui n'expose pas `/metrics` (gap connu,
`infra/alloy/README.md` : mode multiprocess de `prometheus_client` ou serveur
de métriques par process). Les règles d'alerte correspondantes existent mais
n'ont d'effet que si le push OTLP transporte ces métriques. **À faire** :
rendre les métriques worker scrapables (ou vérifier/valider le chemin OTLP) et
prouver qu'une alerte se déclenche. Bloquant pour le non restreint : à volume
non restreint on ne peut pas dépendre d'une lecture quotidienne des logs.

### B3 — P2 : idempotence des goals génériques sans draft

~15 goals WRITE sans draft n'ont pour idempotence que le fallback
`message_sid` : il neutralise retry et redélivrance du **même** message, pas
deux messages WhatsApp **distincts** confirmant deux fois sous verrou de
conversation dégradé. Pas de record de réconciliation non plus si un crash
survient en milieu d'écriture. Options (aucune n'est un correctif de clôture) :
draft versionné par goal à risque (`SALES_RECORD_DIRECT`,
`FINANCE_LOG_EXPENSE`, `STOCK_REGISTER_HARVEST` en premier : ce sont ceux qui
portent de l'argent ou du stock), ou clé d'idempotence métier stable.
Décision guidée par les **doublons réellement observés** pendant le pilote.

### B4 — P2 : contrat `ACTIVE_SLOT` sans `extra="forbid"`

`ActiveSlotDecision.extracted_entities` est le seul des 4 contrats micro-prompt
qui n'interdit pas structurellement un champ `_id`. Aucun exploit
conversationnel démontré. Fix : énumérer les champs autorisés + `extra="forbid"`
avec un test de non-régression sur les extractions existantes.

### B5 — P2 : fallback « interpréteur unifié legacy »

Toujours câblé (`interpreter/routing.py`, atteint sur exception ou
`MARKET_COACH_NEW_TASK_V2_ENABLED` désactivé) ; son prompt expose des ids
techniques. C'est un filet de sécurité volontaire : sa suppression est une
**décision produit**, à prendre avec la fréquence réelle de repli
(`ladini_legacy_fallback_total`) observée en pilote.

### B6 — P3 : dette de test et d'outillage

- `RecurringNeedDraft` sans `check_confirmation_target_invariant` (filet
  redondant : la protection primaire `STALE_TARGET` fonctionne).
- Pas de test cross-flow-leak dédié pour `procurement_draft`/`preorder_draft`.
- Pas de fichier « golden E2E » unique ; pas de Hypothesis ; pas de test
  persist → restart → continue contre un Redis/Postgres réels en CI.
- `inbound_recovered_messages` et `transaction_idempotency_hits` non
  instrumentés (pas de signal fiable ; le premier dépend de B1, le second vit
  à la frontière du serveur MCP).
- Erreurs Ruff préexistantes (711 sur `src` + `tests`) et erreurs mypy
  préexistantes dans les fichiers touchés : non introduites par ce chantier.
- Environnement de test Windows : deux gardes architecturales
  (`test_turn_policy_classification.py`, 2 tests) comparent des chemins avec
  `/` et échouent sur Windows (`core\turn_trace.py`) ; un test lance un vrai
  worker Celery dont la sortie cp1252 empoisonne la capture pytest sans
  `PYTHONUTF8=1`. Aucun n'est un défaut de production.
- Isolation de test : `test_process_agent_task_message_dedup.py` suppose un
  Redis absent ou vierge (clés `resp:{event_id}` fixes, TTL 1 h) ; contre un
  Redis local persistant, un second run dans l'heure échoue à tort.

## 2. Critères pour passer de RESTRICTED à UNRESTRICTED

Aucun nombre de semaines n'est fixé : **aucune donnée ne le justifie** (il
n'existe pas de baseline de trafic ni de taux d'incident en production).
La durée est une conséquence du temps nécessaire pour satisfaire les critères
ci-dessous, pas un critère elle-même. Chaque critère a une preuve vérifiable.

| # | Critère | Preuve |
|---|---|---|
| U1 | Durable inbox / dead-letter (B1) implémenté **et** exercé (panne broker plus longue que la fenêtre de retry du provider) | tests + compte rendu d'exercice |
| U2 | Métriques worker visibles et alertes prouvées (B2) : une règle de `ladini-agent-reliability` s'est déclenchée lors d'un test | capture + horodatage |
| U3 | **Zéro violation d'autorisation** : aucun tour `error_category='SECURITY'` inexpliqué, aucune écriture cross-acteur | revue de `agent_turns` + `MCP_EXEC_AUDIT` sur toute la période |
| U4 | **Aucune double écriture** attribuable à un retry ou une redélivrance (même `message_sid`) ; les doublons « deux messages distincts » sont comptés et B3 est tranché sur leur nombre réel | requêtes du runbook cas 3/5 |
| U5 | **Idempotence observée en réel** : au moins un cas réel `task_retries > 0` avec exactement une écriture métier vérifiée | `agent_turns` + `mcp_idempotency_records` |
| U6 | **Retry webhook observé sans perte** : au moins une redélivrance provider réelle (ou exercice Test C du runbook) avec exactement une réponse | logs + `agent_turns` |
| U7 | **Runbook testé** : les 7 cas ont été parcourus au moins une fois (incident réel ou exercice sur table), corrections apportées au runbook | journal de révision |
| U8 | **Monitoring stable** : seuils recalibrés sur une baseline observée (les seuils actuels sont des valeurs initiales), pas d'alerte bruyante ignorée | historique d'alertes |
| U9 | **Volume et diversité significatifs** : les intents marqués ⚠️ dans `AGENT_RELIABILITY_MATRIX.md` ont chacun au moins une exécution réelle observée ; le seuil de volume est fixé par l'équipe **à partir de la baseline pilote**, pas avant | matrice mise à jour |
| U10 | **Aucune nouvelle classe P0/P1 ouverte** (aucune issue P0/P1 sans correctif ou sans garde opérationnelle explicite) | registre §3 |

Critère d'**échec** du pilote (retour à zéro) : une violation d'autorisation
confirmée (U3), ou une double écriture sur un même `message_sid` (U4).

## 3. Registre des issues connues

Une issue, une catégorie. `BLOCKER_FOR_UNRESTRICTED` ne veut pas dire « bloque
le pilote » : le pilote est accepté avec la garde opérationnelle indiquée.

### CLOSED

| Issue | Sévérité | Preuve |
|---|---|---|
| Slots transactionnels périmés (461000/`UNITE`) réutilisés par un nouveau `SALES_PUBLISH_PRODUCT` | P0 | `test_sales_publish_cross_flow_state_leak.py` |
| Mini-flows bid/update/GPS jamais purgés sur transition de but | P0 | `test_goal_planner_state_machine.py::TestMiniFlowKeysArePurgedOnRealGoalTransitions` |
| Retry MCP sur goals sans draft = écriture en double (clé d'idempotence aléatoire) | P0 | `test_execution_idempotency_key.py` |
| `select_winning_bid` sans contrôle de propriété acheteur | P0 | `test_order_mutations_require_ownership.py`, `test_select_winning_bid_state_guards.py` |
| « 500.000 » FCFA lu comme 500.0 | P1 | `test_dot_thousands_separator_ambiguity.py` |
| Confirmation construite depuis `transaction_payload` live (10 goals) | P1 | `test_confirmation_gate_certified_command.py` |
| Échec d'enqueue après claim webhook = perte silencieuse (fenêtre normale) | P1 | `test_twilio_/test_whatsapp_webhook_enqueue_failure_recovery.py` |
| `ws.active_goal` périmé après erreur mi-tour | P1 | `test_orchestrator_workspace_reconciliation_on_error.py` |
| Pas de test DB-layer d'ownership pour `declare_future_production` | P2 | `test_declare_future_production_ownership.py` (clôture) |
| Échec de réconciliation workspace journalisé en DEBUG seulement | P2 | log `WORKSPACE_RECONCILIATION_FAILED` en WARNING (clôture) |
| Pas de règles d'alerte pour les compteurs reliability | P2 | groupe `ladini-agent-reliability` (clôture ; voir U2 pour la preuve d'effet) |
| Pas de test verrouillant le scénario d'acceptation « boeufs » (unité, pas de confirmation prématurée) | P2 | test ajouté à la clôture |

### ACCEPTED_FOR_RESTRICTED_PILOT

| Issue | Garde opérationnelle | Décision de sortie |
|---|---|---|
| Idempotence des ~15 goals génériques : deux messages **distincts** peuvent confirmer deux fois sous verrou dégradé (B3) | G2, G6, revue quotidienne des doublons | U4 (nombre réel de doublons) |
| Pas de record de réconciliation pour un crash en milieu d'écriture sur ces goals | G6 (correction manuelle) | B3 |
| Concurrence réelle non prouvée bout en bout (deux confirmations concurrentes en mode dégradé du verrou) | G2 | U4 |
| Seuils d'alerte initiaux sans baseline | recalibrage semaine 1 du pilote | U8 |
| Redis/checkpointer réel non exercé en CI (checkpointer simulé fidèle au contrat) | Test C + observation | U5, U6 |
| Politique de retry du provider supposée, non vérifiée dans cet environnement | checklist L5 | U6 |

### BLOCKER_FOR_UNRESTRICTED

| Issue | Garde pendant le pilote | Correctif |
|---|---|---|
| Pas de durable inbound inbox / dead-letter : panne broker > fenêtre de retry provider = message perdu sans trace | G1–G7, alerte `ladini-inbound-enqueue-failure`, runbook cas 1 et 4 | B1 → U1 |
| Métriques worker non scrapables ; alertes correspondantes sans effet garanti | logs `WORKSPACE_RECONCILIATION_FAILED`, `agent_turns.task_retries` | B2 → U2 |

### DEFERRED_P2/P3

| Issue | Sév. | Voir |
|---|---|---|
| `ActiveSlotDecision.extracted_entities` sans `extra="forbid"` | P2 | B4 |
| Fallback interpréteur legacy exposant des ids techniques | P2 | B5 |
| `RecurringNeedDraft` sans `check_confirmation_target_invariant` | P3 | B6 |
| Pas de test cross-flow-leak dédié procurement/preorder | P3 | B6 |
| `inbound_recovered_messages` / `transaction_idempotency_hits` non instrumentés | P3 | B6 (dépend de B1) |
| Pas de golden E2E unique, pas de Hypothesis, pas de test Redis/Postgres réel en CI | P3 | B6 |
| Erreurs Ruff/mypy préexistantes | P3 | B6 |
| Faux échecs de tests sur Windows (séparateurs de chemin, encodage cp1252) | P3 | B6 |
| `test_process_agent_task_message_dedup.py` dépend d'un Redis absent ou vierge | P3 | B6 |
