# Agent Reliability Matrix

Statut : audit systémique, 2026-09-28 (Phase 1) — mis à jour 2026-09-28 (Phase 2,
"AGENT RELIABILITY HARDENING — PHASE 2", fermeture des 3 P1 restants). Périmètre :
les 27 intents `action_type: "WRITE"` de `interpreter/intent.py::INTENT_CONFIG`
(inventaire complet, vérifié par grep). Compagnon de `TRANSACTION_STATE_LIFECYCLE.md`
(qui documente le modèle de state en détail) et `AGENT_PRODUCTION_READINESS.md`
(le gate global).

Légende :
- ✅ couvert et prouvé (un test réel existe et passe)
- ⚠️ partiel (protégé pour partie, ou protégé mais pas par un test dédié)
- ❌ trou confirmé (reproductible, non testé)
- — non applicable à cet intent

`Draft` = draft versionné (`core/draft_registry.py`, identité d'instance CAS
`draft_id`+`version`). `Idempotent` = un retry/redelivery du même message ne
peut pas ré-exécuter l'action deux fois. `Same-intent/new-action` = deux
tentatives distinctes du MÊME intent ne se contaminent jamais.

## Intents à draft versionné (protection maximale)

| Intent | Happy path | Missing info | Correction | Same-intent/new-action | Intent switch | Cancel | Idempotent | Reconciliation |
|---|---|---|---|---|---|---|---|---|
| SALES_PUBLISH_PRODUCT | ✅ | ✅ | ✅ | ✅ (corrigé cette session) | ✅ | ✅ | ✅ | ✅ |
| PROCUREMENT_CREATE_REQUEST | ✅ | ✅ | ✅ | ⚠️ (même mécanisme générique que sales, pas de test dédié) | ✅ | ✅ | ✅ | ✅ |
| BUYER_PREORDER_INIT / BUYER_PREORDER_CONFIRM | ✅ | ✅ | ✅ | ⚠️ (fenêtre pré-draft ~nulle, risque structurel faible, pas de test dédié) | ✅ | ✅ | ✅ | ✅ |
| CREATE_RECURRING_NEED | ✅ | ✅ | ✅ (garde `decide_active_draft_reply` dédiée) | ✅ | ✅ | ✅ | ✅ | ✅ |

Les 4 vont par ailleurs à travers le même correctif générique de cette session
(bid/update/gps mini-flow purge — sans objet pour ces 4, qui n'ont pas de mini
machine `working_memory` propre).

## Intents "own-flow" (leur propre mini-tunnel, hors draft registry)

| Intent | Happy path | Same-intent/new-action | Ownership | Idempotent | Notes |
|---|---|---|---|---|---|
| SALES_PLACE_BID | ✅ | ✅ (corrigé cette session — `bid_phase`/`pending_bid_*`) | — (dépose une offre sur SA propre auction) | ✅ (corrigé cette session — message_sid fallback) | |
| ACCEPT_BID / SELECT_WINNING_BID (`select_winning_bid`) | ✅ | ✅ | ✅ (corrigé cette session — gap réel confirmé, cf. §ci-dessous) | ✅ (bid_id, préexistant) | 2 call sites (`order_tracking.py`, `negotiation.py`) — les deux passent désormais un `phone` réel |
| SALES_UPDATE_PRODUCT | ⚠️ | ⚠️ (pas de mini-flow `working_memory` connu, non vérifié exhaustivement) | ✅ (service layer) | ❌ (pas de draft, pas de clé stable avant le fallback message_sid de cette session — **⚠️ après**: couvert par le fallback générique, jamais testé spécifiquement pour cet intent) | CONFIRM/REJECT lit `working_memory["update_pending"]` (même valeur que le récap affiché), jamais `transaction_payload` — vérifié Phase 2 |
| PRODUCTION_UPDATE_FUTURE | ⚠️ | ⚠️ | ✅ (`services/database/producer.py`, ownership+lock) | ⚠️ (idem) | **ajouté Phase 2** — retiré à tort du tableau générique en Phase 1 ; own-flow réel (`producer_update` tunnel), même pattern self-consistent que SALES_UPDATE_PRODUCT |
| PRODUCER_CANCEL_ORDER | ⚠️ | — | ✅ (`get_producer_profile`+`not_owner`, testé) | ⚠️ (idem) | |
| PRODUCER_CONFIRM_ORDER | ⚠️ | — | ✅ | ⚠️ (idem) | |
| PRODUCER_CONFIRM_DELIVERY_OTP | ⚠️ | — | ✅ (code déterministe) | ⚠️ (idem) | |
| PROCUREMENT_UPDATE_REQUEST | ⚠️ | — | ⚠️ (`tool_name` "symbolique", non vérifié) | ⚠️ (idem) | |
| BUYER_ADD_TO_CART | ✅ (candidate lists reconstruites à neuf, `domain/selection_actions.py`) | ✅ | — (panier propre à l'acheteur) | — (accumulation, pas une action ponctuelle) | |
| BUYER_CREATE_PREORDER / BUYER_CANCEL_ORDER / BUYER_NEGOTIATE_PRICE | ⚠️ | ⚠️ | ✅ | ⚠️ (idem fallback générique) | |
| UPDATE_RECURRING_NEED | ⚠️ | — | ✅ (via draft CAS parent) | ✅ (draft version) | |

## Intents génériques (transaction_payload brut, `confirmation_gate` générique)

Inventaire EXACT (Phase 2, revérifié par 2 traces indépendantes — routeur
`core/router.py::DomainRouter.build()` + `interpreter/intent.py::
_TUNNEL_ASSIGNMENTS` d'un côté, lecture directe de chaque flow file de
l'autre) : **10 goals**, pas "~8" (l'estimation initiale omettait
`SALES_UNPUBLISH_PRODUCT` et `PRODUCER_CONFIRM_DELIVERY_PAYMENT`, tous deux
mal classés "own-flow" en Phase 1).

| Intent | Happy path | Confirmation intégrité | TYPE | Idempotent | Notes |
|---|---|---|---|---|---|
| STOCK_REGISTER_HARVEST | ✅ | ✅ (Phase 2 — voir §Confirmation) | TYPE2 (quantity = ledger stock) | ⚠️ (fallback message_sid, non testé spécifiquement) | |
| SALES_RECORD_DIRECT | ✅ | ✅ (Phase 2) | TYPE2 (price = montant vente — risque le plus élevé du groupe) | ⚠️ | |
| SALES_UNPUBLISH_PRODUCT | ✅ | ✅ (Phase 2) | TYPE1 (id nu) | ⚠️ | corrige le classement "own-flow" erroné de la Phase 1 |
| PRODUCER_CONFIRM_DELIVERY_PAYMENT | ✅ | ✅ (Phase 2) | TYPE1 (id nu, + ownership DB-layer) | ⚠️ | idem — voir table own-flow ci-dessus |
| PRODUCTION_DECLARE_FUTURE | ✅ | ✅ (Phase 2) | TYPE2 (price/quantity/date — surface de champs la plus large) | ⚠️ | ownership `farm_id` vérifiée en DB (`services/database/producer.py::declare_future_production`) et **verrouillée par `test_declare_future_production_ownership.py`** (clôture) |
| PRODUCTION_UPDATE_FUTURE | — | — | — | — | **retiré de ce groupe (Phase 2)** : own-flow réel (`producer_update` tunnel), jamais `confirmation_gate` générique — erreur de classement Phase 1 corrigée |
| FINANCE_LOG_EXPENSE | ✅ | ✅ (Phase 2) | TYPE2 (price = montant dépense) | ⚠️ | |
| FARM_CREATE / FARM_UPDATE | ✅ | ✅ (Phase 2, impact limité — champs administratifs) | TYPE1 | ⚠️ | |
| PROFILE_SET_GEO / PROFILE_SET_PREFS | ✅ | ✅ (Phase 2, pas d'argent en jeu) | TYPE1 | ⚠️ | |

## Sections transverses

| Dimension | État | Preuve |
|---|---|---|
| Webhook duplicate delivery | ✅ (3 couches : `msg:{SID}`, `task_claim`/`task_done`, `resp:{event_id}`) | `test_process_agent_task_message_dedup.py`, `test_response_dispatch_idempotency.py` |
| Webhook enqueue failure (Celery `.delay()` échoue après le claim) | ✅ **fermé Phase 2** — release-claim + 503 (redélivraison provider), ⚠️ résiduel : pas de dead-letter durable (voir Production Readiness) | `test_twilio_webhook_enqueue_failure_recovery.py`, `test_whatsapp_webhook_enqueue_failure_recovery.py` |
| Concurrency (verrou par conversation) | ✅ mécanisme, ⚠️ résiduel en mode dégradé | `test_conversation_lock.py` — le fallback "dégradé" en cas de timeout retire la sérialisation, sans backstop pour les ~15 goals sans draft |
| Retry transitoire MCP après timeout | ✅ (corrigé cette session pour les goals sans draft — fallback `message_sid`) | `test_execution_idempotency_key.py::TestMessageSidFallbackForNonDraftGoals` |
| Erreur technique mi-tour (`_sync_workspace` non appelé) | ✅ **fermé Phase 2** — `_reconcile_workspace_after_failure`, appelé sur les 3 branches d'échec | `test_orchestrator_workspace_reconciliation_on_error.py` |
| PendingInteraction (TTL, kind unique, cart tunnel) | ✅ | `test_pending_interaction_lifecycle.py` (préexistant) |
| Draft registry (4 types, purge conditionnelle) | ✅ (SALES_PUBLISH_PRODUCT), ⚠️ (les 3 autres, mécanisme générique partagé, pas de test dédié par type) | `test_sales_publish_cross_flow_state_leak.py` (cette session), `test_recurring_need_state_leak.py` (préexistant) |
| Candidate selection (cart/tier) | ✅ | `domain/selection_actions.py` reconstruit à neuf chaque tour |
| Candidate selection (bid/auction, hors tunnel panier) | ✅ (corrigé cette session — `bid_phase`/`pending_bid_*` purgés sur transition réelle de but) | `TestMiniFlowKeysArePurgedOnRealGoalTransitions` |
| Same-intent/new-action (SALES_PUBLISH_PRODUCT) | ✅ | `test_sales_publish_cross_flow_state_leak.py` |
| Same-intent/new-action (bid/auction) | ✅ (corrigé cette session) | `TestMiniFlowKeysArePurgedOnRealGoalTransitions` |
| Confirmation construction (draft goals) | ✅ | `check_confirmation_target_invariant`, appelé sur le hot path |
| Confirmation construction (10 goals génériques) | ✅ **fermé Phase 2** — `confirmation_gate` certifie désormais le snapshot GELÉ (`confirmation_summary_payload`), jamais `transaction_payload` live | `test_confirmation_gate_certified_command.py` (A1-A8) |
| LLM output trust (schémas stricts) | ✅ (3 contrats sur 4) ⚠️ (ACTIVE_SLOT, `extra` non forbid) | spot-check cette session, pas de test dédié au gap ACTIVE_SLOT |
| Hallucinated entity IDs (post-confirmation) | ✅ **fermé Phase 2** pour les 10 goals génériques — un id substitué APRÈS la levée de la confirmation ne survit jamais au gel | `TestA3A4HallucinatedOrForeignEntityIdNeverSurvivesToExecution` |
| Hallucinated entity IDs (pré-confirmation, ACTIVE_SLOT) | ⚠️ résiduel, hors scope Phase 2 | pas de chemin conversationnel démontré, mais pas de seconde ligne de défense au niveau ACTIVE_SLOT — P2, cf. Production Readiness |
| Domain invariants (qty>0, price>0, stock non négatif) | ✅ | `positive_float`, `remove_stock` (`insufficient_stock`) |
| Authorization (ownership DB-layer) | ✅ (corrigé cette session pour `select_winning_bid`), ✅ ailleurs | `test_order_mutations_require_ownership.py` (étendu cette session) |
| MCP tool contract hygiene | ✅ (spot-check 4 tools critiques) | — |
| Business events / outbox | ✅ (atomique, même transaction DB) | `domain/analytics/emitter.py` |
| Response/write mismatch | ✅ (succès strictement gated sur `is_success_response`) | — |
| Dual-role buyer/producer | ✅ (design role-agnostic, ownership poussé au DB-layer) | `nodes/role_guard.py` |
| Number/unit parsing | ✅ (corrigé cette session — ambiguïté point=milliers) | `test_dot_thousands_separator_ambiguity.py` |
| Ambiguous short replies | ✅ (déjà géré par `PendingInteraction`/candidate lists fraîches) | préexistant |
| Legacy interpreter fallback (exposition d'IDs techniques) | ⚠️ **résiduel, non fixé** | mécanisme de secours intentionnel (`MARKET_COACH_NEW_TASK_V2_ENABLED`), jamais désactivé unilatéralement cette session — voir Production Readiness |

## Corrections apportées Phase 1 (résumé)

1. `interpreter/goal_planner.py` RULE 1quater — purge conditionnelle sur
   same-intent/new-action (déjà couvert par la mission précédente, généralisé
   ici aux mini-flows bid/update/GPS).
2. `nodes/executor.py::_derive_execution_idempotency_key` — fallback
   `message_sid` pour les ~15 goals WRITE sans draft.
3. `services/database/auction.py::select_winning_bid` — garde de propriété
   acheteur ajouté (gap réel, absent alors que son voisin `cancel_auction`
   l'avait déjà).
4. `flows/buyer/negotiation.py::_handle_viewing_offers` — `phone` réel
   threadé (prérequis du correctif #3 pour ce call site).
5. `domain/quantity_unit.py::_parse_number` — rejet de l'ambiguïté
   point-milliers au lieu d'une sous-évaluation silencieuse x1000.

## Corrections apportées Phase 2 (résumé) — ferme les 3 P1 restants

6. `nodes/confirmation_gate.py` — branche CONFIRM générique : vérifie
   `confirmation_summary_goal`/`confirmation_summary_payload` (snapshot gelé,
   déjà existant, réutilisé — pas de nouvelle classe) avant de certifier, puis
   remplace `transaction_payload` par ce snapshot (`{"__reset__": True,
   **data}`, jamais une fusion) avant d'autoriser l'exécution. Ferme
   l'inventaire exact des 10 goals génériques (voir tableau ci-dessus).
7. `api/routes/twilio_webhook.py` / `whatsapp_webhook.py` — un échec
   d'enqueue Celery (ou la pause maintenance) relâche désormais le claim
   `msg:{id}` posé en tête de webhook AVANT de renvoyer 503, au lieu de
   laisser ce claim bloquer silencieusement toute redélivraison provider
   pendant 1h.
8. `orchestrator/orchestrator.py::_reconcile_workspace_after_failure` —
   appelé sur les 3 branches d'échec (`AgentCircuitBreaker`, `TimeoutError`,
   `Exception`), relit le checkpoint réel et réconcilie `ws.active_goal` au
   lieu de le laisser figé à sa valeur pré-tour.
9. Observabilité : logs `inbound_queued`/`flow_certified`/`workspace_reconciled`
   (corrélation seulement, jamais de contenu métier) + 4 compteurs
   Prometheus/OTel (`inbound_enqueue_failures`, `duplicate_inbound_messages`,
   `workspace_reconciliation_failures`, `transaction_retry_count`).

## Ce qui reste ❌ / ⚠️ (voir Production Readiness pour le détail et la justification)

- **Résiduel P1-B** : pas de dead-letter DURABLE pour un message dont
  l'enqueue échoue de façon SOUTENUE (au-delà du budget de redélivraison du
  provider) — bloqué par une contrainte d'architecture réelle (`schema_contract/`
  est un miroir en LECTURE SEULE généré depuis un dépôt frontend Drizzle
  séparé ; `test_no_runtime_ddl_in_backend_source` interdit toute DDL runtime
  dans ce backend), pas un oubli. Voir Production Readiness pour le détail et
  l'option de contournement (alerting sur `inbound_enqueue_failures`).
- ACTIVE_SLOT contract sans `extra="forbid"` (P2, aucun exploit démontré,
  mais seule ligne de défense pour ce chemin) — hors scope Phase 2.
- Fallback legacy de l'interpréteur unifié (exposition d'IDs techniques dans
  le prompt) — mécanisme de secours intentionnel, décision produit requise
  avant toute suppression (P2/P3 selon l'usage réel en production).
- ~~Pas de test DB-layer dédié pour `declare_future_production`'s ownership
  check~~ — **fermé à la clôture** : `tests/unit/test_declare_future_production_ownership.py`
  (doublure de session, pas de Postgres réel).

**Clôture (2026-09-28)** : l'état définitif de chaque issue (CLOSED /
ACCEPTED_FOR_RESTRICTED_PILOT / BLOCKER_FOR_UNRESTRICTED / DEFERRED) est dans
`AGENT_POST_PILOT_BACKLOG.md` §3. Les ⚠️ ci-dessus qui portent sur
l'idempotence des goals génériques sont **acceptés pour le pilote restreint**
(garde : `AGENT_PILOT_RUNBOOK.md` G2/G6) et leur décision de sortie dépend des
doublons réellement observés (critère U4).
