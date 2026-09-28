# Agent Production Readiness — Transaction Core

Statut : audit systémique, 2026-09-28 (Phase 1) — mis à jour 2026-09-28
(Phase 2, "AGENT RELIABILITY HARDENING — PHASE 2"). Compagnon de
`AGENT_RELIABILITY_MATRIX.md` (détail par intent) et
`TRANSACTION_STATE_LIFECYCLE.md` (détail du modèle de state). Méthode
inchangée entre les 2 phases : reproduire AVANT de corriger (un test qui
échoue sur le code pré-correctif, vérifié en le stashant, avant tout fix),
jamais une correction "à l'aveugle".

## Verdict

**AGENT TRANSACTION CORE READY FOR RESTRICTED PILOT — PAS ENCORE READY POUR
UN DÉPLOIEMENT LARGE/HAUT-VOLUME SANS CONTRÔLE COMPENSATOIRE.**

Les 3 P1 identifiés en Phase 1 (§"P1 trouvés, NON corrigés" de la version
précédente de ce document) sont **fermés** en Phase 2, chacun avec repro
AVANT correctif (vérifié en stashant le fix et en confirmant l'échec exact
attendu, puis en le restaurant et en confirmant le passage au vert) :

1. **Confirmation construite depuis `transaction_payload` brut** (10 goals
   génériques, inventaire exact — voir Matrix) → **fermé**. `confirmation_gate`
   certifie désormais le snapshot GELÉ montré à l'utilisateur
   (`confirmation_summary_payload`, déjà existant dans `core/state.py`,
   réutilisé plutôt qu'une nouvelle classe `CertifiedCommand`), jamais un
   `transaction_payload` live qui aurait pu diverger entre la confirmation et
   l'exécution.
2. **Échec d'enqueue Celery après le claim webhook → perte de message
   silencieuse** → **fermé pour la fenêtre normale**, ⚠️ **résiduel documenté
   pour une panne SOUTENUE** (voir §Résiduels ci-dessous — contrainte
   d'architecture réelle, pas un oubli).
3. **`_sync_workspace` non appelé sur erreur technique mi-tour → `ws.active_goal`
   périmé peut ressusciter** → **fermé**. `_reconcile_workspace_after_failure`
   relit le checkpoint réel sur les 3 branches d'échec de `Orchestrator.handle`.

Les 3 invariants que la mission Phase 2 exige comme preuve de complétude :

1. **CE QUE L'UTILISATEUR A CONFIRMÉ == CE QUE LE MÉTIER EXÉCUTE.** ✅ Prouvé
   (`test_confirmation_gate_certified_command.py`, A1-A8, reproduit puis fixé).
2. **WEBHOOK ACCEPTÉ == LE MESSAGE SERA ÉVENTUELLEMENT TRAITÉ OU EXPLICITEMENT
   MIS EN DEAD-LETTER.** ⚠️ **Partiellement prouvé.** La première moitié est
   maintenant VRAIE et testée : un 200 (accepté) n'est renvoyé QUE si l'enqueue
   Celery a RÉELLEMENT réussi — avant ce correctif, un 200 pouvait mentir
   (message perdu). Sur échec, 503 + claim relâché ⇒ le provider (Twilio/Meta)
   redélivre, et cette redélivraison est maintenant traitée comme fraîche
   (`test_twilio_webhook_enqueue_failure_recovery.py`,
   `test_whatsapp_webhook_enqueue_failure_recovery.py`). La seconde moitié
   ("ou explicitement dead-letter") n'est **pas** construite : aucun mécanisme
   ne trace explicitement un message dont TOUTES les tentatives de
   redélivraison du provider sont épuisées. Voir §Résiduels pour la raison
   exacte (contrainte de schéma) et l'option de contournement.
3. **APRÈS UN ÉCHEC DE TOUR, L'ÉTAT PERSISTÉ NE PEUT PAS RESSUSCITER UN GOAL
   INVALIDE.** ✅ Prouvé (`test_orchestrator_workspace_reconciliation_on_error.py`,
   reproduit puis fixé, y compris la non-régression "un goal encore
   légitimement actif doit survivre").

Verdict détaillé : l'architecture était déjà solide (Phase 1 le confirmait :
drafts versionnés + CAS, `PendingInteraction` avec TTL, verrou Redis par
conversation, dédup webhook 3 couches, outbox transactionnel, invariants
métier au niveau service) ; les 3 P1 bloquants sont maintenant fermés avec
tests reproductibles. Le seul résiduel qui empêche un verdict READY
inconditionnel est la moitié "dead-letter" de l'invariant 2 — un gap ÉTROIT,
compris, et surveillable (nouveaux compteurs `inbound_enqueue_failures`/
`duplicate_inbound_messages`), pas un trou béant comme au début de cette
session.

Un pilote **restreint** (producteurs pilotes connus, volumes faibles, support
humain disponible) est désormais raisonnable SANS réserve particulière au-delà
de ce qui existait déjà. Un déploiement large/haut-volume reste conditionné à
la fermeture du résiduel dead-letter (§Résiduels) OU à un contrôle
compensatoire opérationnel (alerting + runbook, détaillé ci-dessous).

## Gates

| Gate | Statut | Preuve |
|---|---|---|
| STATE (lifecycle transactionnel) | **PASS** | `TRANSACTION_STATE_LIFECYCLE.md`, `AGENT_RELIABILITY_MATRIX.md` |
| DRAFT (registre versionné, 4 types) | **PASS** (SALES_PUBLISH_PRODUCT prouvé cette session ; les 3 autres partagent le même mécanisme générique, non testés individuellement) | `test_sales_publish_cross_flow_state_leak.py`, `test_recurring_need_state_leak.py` |
| PENDING (PendingInteraction) | **PASS** | suite préexistante (`test_pending_interaction_lifecycle.py`) |
| VALIDATION (invariants domaine) | **PASS** | `positive_float`, `remove_stock` (stock non négatif), `place_bid`/`select_winning_bid` (statuts) |
| IDEMPOTENCE | **PARTIAL** | 4 drafts + `select_winning_bid` : PASS prouvé. ~15 goals génériques : fallback `message_sid` (Phase 1) couvre retry/redelivery du MÊME message ; ne couvre toujours pas deux messages WhatsApp DISTINCTS confirmant deux fois sous verrou dégradé (résiduel P2, hors scope des 3 P1 fermés) |
| AUTHORIZATION | **PASS** (`select_winning_bid` Phase 1 ; confirmé de nouveau Phase 2 — le gel du payload en Phase 2 ne contourne AUCUN check d'ownership existant, il les protège en plus contre un id substitué post-confirmation) | `test_order_mutations_require_ownership.py`, `TestA3A4HallucinatedOrForeignEntityIdNeverSurvivesToExecution` |
| CONCURRENCY | **PARTIAL** | inchangé Phase 2 — verrou par conversation solide et testé ; son propre mode dégradé documenté (`test_conversation_lock.py`) retire la sérialisation SANS backstop pour les goals sans draft |
| RETRY | **PASS** (Phase 2 ferme le dernier trou) | retry transitoire MCP idempotent pour tous les goals (Phase 1, fallback message_sid) ; échec d'enqueue Celery après claim webhook maintenant récupérable (Phase 2, release-claim + 503) — voir §Résiduels pour la nuance dead-letter |
| ERROR RECOVERY | **PASS** (Phase 2 ferme le dernier trou) | chemin succès disciplié et testé ; chemin erreur technique mi-tour réconcilie maintenant `ws.active_goal` sur les 3 branches d'échec, testé (reproduit AVANT correctif) |
| CLEANUP | **PASS** (Phase 1) | terminal COMPLETED purge les 4 drafts ; abandon max-retries idem ; mini-flows bid/update/GPS purgés sur transition réelle de but |
| OBSERVABILITY | **PASS** (préexistant + étendu Phase 2) | logs structurés `MCP_EXEC_AUDIT`, `[GoalPlanner ...]`, PII masqué (`_mask_pii_args`) ; + `inbound_queued`/`flow_certified`/`workspace_reconciled` et 4 compteurs Prometheus/OTel (Phase 2) |
| E2E | **PARTIAL** | suite existante large (unit/integration/architecture/evals/chaos) ; pas de suite "golden" UNIQUE et nommée construite — les 5 scénarios chaos demandés en Phase 2 sont chacun couverts par une combinaison de tests existants + nouveaux (voir §Chaos Phase 2), pas rassemblés en un seul fichier |

## P0 trouvés et corrigés cette session

1. **`select_winning_bid` sans contrôle de propriété acheteur.** Clôturait
   une enchère et créait une `Order` sans jamais vérifier que l'appelant
   est le VRAI acheteur propriétaire — contrairement à `cancel_auction`,
   MÊME fichier, qui le fait. Corrigé : garde ajouté (résolution du
   propriétaire réel via `BuyerProfile`/`User`, rejet `not_owner`). `phone`
   réel threadé dans `flows/buyer/negotiation.py::_handle_viewing_offers`
   (seul call site qui ne le passait pas). Verrouillé dans
   `test_order_mutations_require_ownership.py`.
2. **Boucle de retry transitoire MCP garantissant une écriture en double**
   pour ~15 goals WRITE sans draft (`SALES_PLACE_BID`,
   `STOCK_REGISTER_HARVEST`, confirmations de commande...) : chaque
   tentative de retry après timeout recevait une clé d'idempotence
   ALÉATOIRE (`None` → UUID côté client), rendant le serveur MCP incapable
   de reconnaître un rejeu. Corrigé : fallback `message_sid` (identifiant
   stable de l'événement WhatsApp entrant) — dédoublonne toute répétition
   de l'exécution d'un même tool pour un même message, sans jamais
   confondre deux messages distincts.
3. **Mini-flows `working_memory` (bid/update/GPS) jamais purgés sur une
   transition réelle de but** — même classe que l'incident
   SALES_PUBLISH_PRODUCT qui a déclenché cet audit, mais pour
   `bid_phase`/`pending_bid_auction`/`pending_bid_price`/`update_phase`/
   `winner_gps_stage`. Corrigé : ces clés sont maintenant purgées partout
   où `_purge_transaction_state()` l'est déjà (RULE 0bis/1/1quater/4/4bis/5).

## P1 trouvés et corrigés cette session

4. **"500.000" FCFA lu silencieusement comme 500.0** — sous-évaluation x1000
   d'un prix/quantité écrit avec le point comme séparateur de milliers
   (convention francophone courante). Corrigé : rejet explicite de
   l'ambiguïté (le flow redemande) plutôt qu'une valeur devinée.

## P1 fermés en Phase 2 (les 3 blockers explicites de la Phase 1)

Chacun suit la même méthode : repro AVANT correctif (le nouveau test,
exécuté sur le code stashé/pré-correctif, échoue EXACTEMENT comme prédit),
puis correctif minimal, puis le même test passe, puis gates complets
(suite, ruff, mypy) sans nouvelle régression.

5. **Confirmation construite depuis `transaction_payload` brut — 10 goals
   génériques exacts** (`STOCK_REGISTER_HARVEST`, `SALES_RECORD_DIRECT`,
   `SALES_UNPUBLISH_PRODUCT`, `PRODUCER_CONFIRM_DELIVERY_PAYMENT`,
   `PRODUCTION_DECLARE_FUTURE`, `FINANCE_LOG_EXPENSE`, `FARM_CREATE/UPDATE`,
   `PROFILE_SET_GEO/PREFS` — 2 de plus que l'estimation Phase 1 "~8", qui
   avait mal classé `SALES_UNPUBLISH_PRODUCT`/`PRODUCER_CONFIRM_DELIVERY_PAYMENT`
   comme "own-flow"). **Fermé** : `nodes/confirmation_gate.py`, branche
   CONFIRM générique, vérifie `confirmation_summary_goal`/
   `confirmation_summary_payload` (le snapshot GELÉ, déjà dans `core/state.py`,
   déjà utilisé par `nodes/rendering/confirm.py` comme garde anti-péremption
   À L'AFFICHAGE — ce correctif étend simplement son autorité au point
   d'EXÉCUTION) avant de certifier, puis remplace `transaction_payload` par
   ce snapshot (`{"__reset__": True, **data}`, jamais une fusion) avant
   d'autoriser l'exécution. Décision délibérée : PAS de nouvelle classe
   `CertifiedCommand` — le mécanisme existant suffisait, cohérent avec le
   mandat "pas de gros refactor" et "n'introduire un CertifiedCommand que si
   justifié". Testé : `tests/nodes/test_confirmation_gate_certified_command.py`
   (A1 inventaire exact verrouillé, A2 contamination cross-flow neutralisée,
   A3/A4 id substitué post-confirmation neutralisé, A5 golden test du garde
   amont existant, A6 double CONFIRM, A7 stabilité de la clé d'idempotence,
   A8 nouveau but invalide une confirmation périmée). A3/A4 : la vérification
   RÉELLE d'existence/propriété reste à la couche domaine
   (`services/database/producer.py::declare_future_production`, vérifié par
   lecture directe, pas par un test DB simulé — dette de test résiduelle,
   pas un bug, voir §Résiduels).
6. **Échec d'enqueue Celery après le claim webhook → perte de message
   silencieuse.** **Fermé pour la fenêtre normale.** `twilio_webhook.py`/
   `whatsapp_webhook.py` capturent le claim `msg:{id}` AVANT
   `process_agent_task.delay()` ; sur échec d'enqueue (ou pause maintenance),
   le claim est maintenant explicitement relâché AVANT de renvoyer 503 — le
   provider (Twilio/Meta) redélivre, et cette redélivraison est traitée comme
   un message NEUF plutôt que droppée par le dédoublonnage (bug confirmé y
   compris sur la branche pause maintenance préexistante, dont la promesse
   "le message n'est pas perdu" était en réalité fausse pour la même raison).
   Testé : `test_twilio_webhook_enqueue_failure_recovery.py`,
   `test_whatsapp_webhook_enqueue_failure_recovery.py` (5 scénarios chacun :
   échec broker, timeout, redélivraison réussie, non-régression succès garde
   le claim, pause maintenance). Un vrai inbox transactionnel PostgreSQL
   (RECEIVED/QUEUED/PROCESSING/PROCESSED/FAILED) a été considéré et REJETÉ
   pour cette session précise — voir §Résiduels pour la raison exacte (pas
   un choix de confort).
7. **`_sync_workspace` non appelé sur erreur technique mi-tour →
   `ws.active_goal` potentiellement périmé au tour suivant.** **Fermé.**
   `orchestrator.py::_reconcile_workspace_after_failure`, appelé sur les 3
   branches d'échec de `Orchestrator.handle` (`AgentCircuitBreaker`,
   `asyncio.TimeoutError`, `Exception` générique) AVANT `_flush_workspace` :
   relit le checkpoint LangGraph réel (déjà attaché, même source que
   `_load_before_state`) et réutilise `_sync_workspace` (inchangé, jusque-là
   réservé au chemin succès) pour dériver `ws.active_goal` de ce que les
   nœuds qui ont RÉELLEMENT terminé avant le crash ont produit — au lieu de
   laisser `ws.active_goal` figé à sa valeur pré-tour, que `_run_market`
   réinjecte comme entrée explicite du graphe au tour suivant. Best-effort
   par construction (une erreur ici dégrade vers "valeur pré-tour conservée",
   jamais une 2e exception). Testé :
   `test_orchestrator_workspace_reconciliation_on_error.py` (timeout et
   exception générique, plus la non-régression "un goal encore légitimement
   actif ne doit jamais être effacé aveuglément", plus la dégradation
   gracieuse si la réconciliation elle-même échoue).

## P2 documentés, non fixés (dette explicite, pas urgente)

8. **`ActiveSlotDecision.extracted_entities` sans `extra="forbid"`** — seul
   des 4 contrats micro-prompt à ne pas interdire structurellement un champ
   `_id`. Aucun exploit conversationnel démontré aujourd'hui (les
   consommateurs réels résolvent via des listes candidates fraîches), mais
   c'est la seule ligne de défense sur ce chemin — un futur bug de liste de
   candidats périmée y trouverait une porte ouverte. Fix recommandé :
   enumérer les champs autorisés + `extra="forbid"`, avec un test de
   non-régression sur les extractions existantes avant de l'activer.
9. **Fallback "interpréteur unifié legacy" encore câblé dans le graphe
   compilé** (`interpreter/routing.py`, atteint sur exception/désactivation
   de `MARKET_COACH_NEW_TASK_V2_ENABLED`) — construit un prompt exposant des
   IDs techniques (`producer_id`/`pricing_tier_id`), exactement le pattern
   que le micro-prompt moderne a été conçu pour éliminer. Rollback
   volontaire (`_use_new_task_v2`), pas du code mort — sa suppression est
   une décision produit (perdre le filet de sécurité en cas de régression
   du chemin moderne), pas un correctif technique unilatéral.
10. **Pas de reconciliation record pour les ~15 goals sans draft** — un
    crash mi-write pour `STOCK_REGISTER_HARVEST`/paiement ne laisse aucun
    artefact à réconcilier (contrairement aux 4 drafts, qui ont chacun leur
    service + cron de reconciliation testés). Infrastructure manquante,
    pas un bug — ajouter un draft à chacun de ces 15 goals serait le
    "gros refactor" que ce mandat exclut explicitement.
11. **Pas de test DB-layer dédié pour l'ownership check de
    `declare_future_production`** (`services/database/producer.py`, farm
    inexistante → `ValueError` ; farm d'un autre producteur → `ValueError`) —
    vérifié par lecture directe du code cette session (Phase 2, pour A3/A4),
    jamais exécuté avec une session DB simulée/réelle. Le code EST correct
    (lu ligne par ligne), mais n'a pas de filet automatisé qui empêcherait
    une régression future. Fix recommandé : un test unitaire avec double de
    session AsyncSession (mirroring `test_select_winning_bid_state_guards.py`),
    pas urgent (comportement déjà vérifié, pas un bug ouvert).
12. **`inbound_recovered_messages`/`transaction_idempotency_hits` non
    instrumentés** (2 des 6 métriques listées par la mission Phase 2 sur 4
    ajoutées). Le premier a besoin d'un signal de tentative-de-redélivraison
    (le provider ne l'expose pas facilement sans persistance locale — voir
    §Résiduels) ; le second vit à la frontière du serveur MCP (hors de ce
    backend). Documenté plutôt que fabriqué avec une heuristique non fiable.

## Résiduel P1-B en détail — pourquoi pas d'inbox transactionnel durable

La mission Phase 2 demandait d'évaluer un pattern Transactional Inbox
(états RECEIVED/QUEUED/PROCESSING/PROCESSED/FAILED, table Postgres dédiée)
pour fermer ENTIÈREMENT l'invariant 2 (webhook accepté ⇒ traité ou
explicitement dead-letter). Ce pattern a été sérieusement considéré, PUIS
écarté pour cette session précise — pas par confort, mais parce que ce
backend ne peut PAS créer de nouvelle table :

- `schema_contract/` (migrations SQL + `drizzle_snapshot.json`) est un
  MIROIR EN LECTURE SEULE généré depuis un dépôt frontend Drizzle séparé
  (`tests/schema/test_schema_contract_static.py::test_contract_copy_is_in_sync_with_frontend_repo`,
  `sync_contract.py`) — ce dépôt-ci n'est pas la source de vérité du schéma.
- `tests/schema/test_schema_contract_static.py::test_no_runtime_ddl_in_backend_source`
  interdit explicitement toute DDL (`CREATE TABLE`/`Base.metadata.create_all`)
  émise par ce backend au runtime — vérifié par une analyse AST de TOUT le
  code source, une seule exception sanctionnée (le bootstrap du runner de
  migrations lui-même).

Ajouter une table `inbound_message_inbox` depuis cette session aurait donc
soit fait échouer ce test de garde-fou, soit créé une table fantôme jamais
reconnue par la vraie base de données (le schéma réel vient exclusivement
des migrations Drizzle du dépôt frontend). Le correctif réellement livré
(release-claim + 503, réutilisant le mécanisme de redélivraison DÉJÀ
implicitement supposé par le code préexistant — voir `core/maintenance.py`)
ferme la fenêtre normale (panne transitoire, quelques minutes à quelques
heures selon la politique de retry du provider) sans construire d'infra que
ce dépôt ne peut pas posséder proprement.

**Contournement recommandé pour un déploiement haut-volume** : alerting sur
le nouveau compteur `inbound_enqueue_failures` (Prometheus/OTel, voir
Matrix) + un runbook manuel ("si ce compteur reste > 0 pendant N minutes,
vérifier le broker et rejouer manuellement les MessageSid loggés par
`TWILIO_WEBHOOK_ENQUEUE_FAILED`/`WHATSAPP_WEBHOOK_ENQUEUE_FAILED`"). Une
vraie fermeture complète de l'invariant 2 nécessite soit une migration
Drizzle côté dépôt frontend (hors de portée de cette session), soit un
changement de politique (dépendre du provider comme SEULE source de
redélivrance, ce qui est déjà standard chez Twilio/Meta pour ce type
d'intégration webhook).

## Chaos scenarios Phase 2 (mission §Partie E)

Les 5 scénarios demandés, chacun prouvé par une combinaison de tests
(certains nouveaux Phase 2, certains déjà existants et re-vérifiés au
§Part D) plutôt que rassemblés dans un unique nouveau fichier "golden chaos" :

1. **Publish + info manquante + panne broker + retry → exactement une
   écriture.** `test_twilio_webhook_enqueue_failure_recovery.py::
   test_a_redelivery_after_enqueue_failure_is_treated_as_a_fresh_message`
   (la redélivraison atteint réellement Celery une 2e fois) COMPOSÉ avec
   `test_process_agent_task_message_dedup.py` (préexistant — deux livraisons
   Celery pour le même `message_sid` n'exécutent le pipeline métier qu'UNE
   fois) et `test_execution_idempotency_key.py` (préexistant — la clé MCP
   reste stable across retries).
2. **Bid A abandonnée → bid B → pas de prix/id périmé.**
   `TestMiniFlowKeysArePurgedOnRealGoalTransitions` (Phase 1, re-vérifié
   Part D — 139 tests golden, tous verts).
3. **Action destructrice confirmée → état brut muté → "OK" → SEULEMENT la
   commande certifiée d'origine.** `TestA2ConfirmedPayloadSurvivesLiveMutation`
   (Phase 2, nouveau — reproduit exactement ce scénario, y compris le cas
   littéral "contamination avec 461000/boeufs" qui a déclenché l'incident
   Phase 0 initial).
4. **Erreur technique mi-tour → reload → nouveau but sans rapport → pas de
   `active_goal` périmé.** `test_orchestrator_workspace_reconciliation_on_error.py`
   (Phase 2, nouveau).
5. **Webhook dupliqué concurrent → exactement un traitement logique.**
   `test_process_agent_task_message_dedup.py` (préexistant) COMPOSÉ avec
   `TestMaintenancePauseAlsoReleasesTheClaim`/les tests de dédoublonnage
   ajoutés Phase 2 (le claim release ne réintroduit PAS de double
   traitement — un succès garde le claim, seul un échec le relâche).

## Security / authorization regression sweep Phase 2 (mission §Partie F)

Pas un ré-audit complet — un balayage ciblé confirmant qu'aucun des 3
correctifs P1-A/B/C n'introduit de bypass. `poetry run pytest tests/architecture/`
(52 fichiers, toutes les gardes d'autorisation/architecture existantes,
dont `test_order_mutations_require_ownership.py`,
`test_unknown_never_executes_confirmation.py`,
`test_write_capability_reachability.py`) : **vert avant ET après** les 3
correctifs. `TestA3A4HallucinatedOrForeignEntityIdNeverSurvivesToExecution`
(Phase 2) confirme explicitement que le gel du payload (P1-A) AJOUTE une
ligne de défense contre un id substitué post-confirmation, sans jamais
retirer ou contourner un check d'ownership existant.

## P3 — dette documentée uniquement

- `RecurringNeedDraft` sans `check_confirmation_target_invariant` (les 3
  autres drafts l'ont) — filet redondant manquant, pas une brèche (la
  protection primaire `STALE_TARGET` fonctionne).
- Pas de test cross-flow-leak dédié pour `procurement_draft`/`preorder_draft`
  (le mécanisme générique les protège, mais rien ne verrouille spécifiquement
  leur comportement si `_draft_state_key_for_goal`/le registre régresse).
- `available_mapping`/`negotiation_context` nettoyage déjà correct mais pas
  documenté comme tel avant cet audit.
- 8 erreurs ruff pré-existantes (tri d'imports, imports/variables inutilisés)
  dans des fichiers touchés cette session, non corrigées (hors-diff, sans
  rapport avec les correctifs).

## Zones encore INSUFFISAMMENT prouvées (mission §49, mis à jour Phase 2)

- **Concurrency réelle** : inchangé — le mode dégradé du verrou est testé
  isolément (`test_conversation_lock.py`), mais aucun test ne prouve le
  comportement downstream (deux confirmations concurrentes réellement
  exécutées deux fois) pour un goal sans draft. Hors scope des 3 P1 fermés
  Phase 2 (P2 résiduel).
- **Redis/checkpointer réel** : inchangé — aucun nouveau test
  persist→restart→continue spécifique aux correctifs Phase 1. Les
  correctifs Phase 2 (P1-C notamment) SONT testés contre un checkpointer
  simulé qui reflète fidèlement le contrat réel (`aget_tuple`/
  `channel_values`), mais pas contre un Redis/Postgres réel démarré en CI.
- **Golden E2E (mission §48, 12 scénarios)** : inchangé — suite existante
  dispersée, pas de fichier "golden" unique.
- **Dead-letter explicite pour l'invariant 2** : voir §Résiduel P1-B en
  détail ci-dessus — le seul des 3 invariants Phase 2 qui n'est
  qu'À MOITIÉ prouvé, pour une raison d'architecture précise, pas un oubli.
- **Property-based testing (Hypothesis)** — toujours pas introduit.

## Gates exécutés

- Suite backend complète (`poetry run pytest tests`) : **verte** avant ET
  après CHAQUE correctif des 2 phases (vérifié par comparaison au baseline
  avant chaque changement risqué, y compris en stashant temporairement
  chaque fix Phase 2 pour confirmer la repro AVANT de le restaurer).
- Ruff sur tous les fichiers touchés (Phase 1 + Phase 2) : propre (0
  nouvelle erreur — les erreurs pré-existantes documentées, vérifiées par
  diff au baseline avant chaque modification).
- mypy sur tous les fichiers touchés (Phase 1 + Phase 2) : 0 nouvelle
  erreur (vérifié fichier par fichier contre le baseline avant
  modification — confirmé pour `orchestrator.py`, `twilio_webhook.py`,
  `whatsapp_webhook.py`, `confirmation_gate.py`, `telemetry.py`, `tasks.py`).
- Aucune migration DB nécessaire pour les correctifs de cette session (voir
  §Résiduel P1-B pour pourquoi une migration AURAIT été nécessaire pour
  fermer complètement l'invariant 2, et pourquoi elle n'a pas été tentée).

## Recommandation de séquencement (mission §Partie J — PR strategy)

Réitéré de la Phase 1, toujours vrai : les correctifs (Phase 1 ET Phase 2)
touchent des couches différentes mais ont été développés et testés comme un
tout cohérent sur la MÊME branche — contrainte de session non négociable
(un seul dépôt/branche désignés, `claude/laughing-dirac-tzipit`, aucune
stratégie multi-branches/multi-PR possible depuis cette session ; **aucune
PR n'a été créée ni mergée automatiquement**, conformément au mandat). Pour
un futur découpage en PRs séparées si le processus de revue l'exige :

**Phase 1** (rappel, inchangé) :
- PR 1 — mini-flow state purge (goal_planner.py + tests) : autonome.
- PR 2 — idempotency fallback (executor.py + tests) : autonome.
- PR 3 — select_winning_bid ownership (auction.py + negotiation.py + tests) :
  autonome, prioritaire (sécurité).
- PR 4 — number parsing (quantity_unit.py + tests) : autonome.

**Phase 2** (nouveau) :
- PR A — CertifiedCommand / confirmation_gate.py + tests A1-A8 : autonome,
  ne dépend d'aucune autre PR de cette liste.
- PR B — webhook enqueue-failure recovery (twilio_webhook.py +
  whatsapp_webhook.py + tests) : autonome.
- PR C — workspace reconciliation on failure (orchestrator.py + tests) :
  autonome.
- PR G/H — observability (telemetry.py, logs dans les 3 fichiers ci-dessus) :
  techniquement scindable en 3 (un ajout de log/compteur par PR A/B/C), mais
  regroupée ici par commodité — aucune dépendance fonctionnelle sur A/B/C,
  peut suivre chacune ou être mergée séparément sans risque.

Chaque PR (Phase 1 et Phase 2) reste cohérente isolément (testée, gate vert
indépendamment) si un découpage est nécessaire — vérifié en Phase 2 en
committant chaque correctif séparément sur la branche unique (4 commits
distincts : P1-C, P1-B, P1-A, observabilité) plutôt qu'un unique commit
fourre-tout, précisément pour que l'historique reste re-découpable a
posteriori sans réécrire quoi que ce soit.
