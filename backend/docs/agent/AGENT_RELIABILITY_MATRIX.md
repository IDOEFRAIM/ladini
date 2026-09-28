# Agent Reliability Matrix

Statut : audit systémique, 2026-09-28. Périmètre : les 27 intents `action_type:
"WRITE"` de `interpreter/intent.py::INTENT_CONFIG` (inventaire complet, vérifié
par grep). Compagnon de `TRANSACTION_STATE_LIFECYCLE.md` (qui documente le
modèle de state en détail) et `AGENT_PRODUCTION_READINESS.md` (le gate global).

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
| SALES_UPDATE_PRODUCT | ⚠️ | ⚠️ (pas de mini-flow `working_memory` connu, non vérifié exhaustivement) | ✅ (service layer) | ❌ (pas de draft, pas de clé stable avant le fallback message_sid de cette session — **⚠️ après**: couvert par le fallback générique, jamais testé spécifiquement pour cet intent) | |
| SALES_UNPUBLISH_PRODUCT | ⚠️ | — | ✅ | ⚠️ (idem) | |
| PRODUCER_CANCEL_ORDER | ⚠️ | — | ✅ (`get_producer_profile`+`not_owner`, testé) | ⚠️ (idem) | |
| PRODUCER_CONFIRM_ORDER | ⚠️ | — | ✅ | ⚠️ (idem) | |
| PRODUCER_CONFIRM_DELIVERY_OTP | ⚠️ | — | ✅ (code déterministe) | ⚠️ (idem) | |
| PRODUCER_CONFIRM_DELIVERY_PAYMENT | ⚠️ | — | ✅ (`get_producer_profile`+`not_owner`, testé) | ⚠️ (idem) | **seul goal de ce groupe sur le chemin de confirmation GÉNÉRIQUE, pas own-flow — cf. P1 confirmation §ci-dessous** |
| PROCUREMENT_UPDATE_REQUEST | ⚠️ | — | ⚠️ (`tool_name` "symbolique", non vérifié) | ⚠️ (idem) | |
| BUYER_ADD_TO_CART | ✅ (candidate lists reconstruites à neuf, `domain/selection_actions.py`) | ✅ | — (panier propre à l'acheteur) | — (accumulation, pas une action ponctuelle) | |
| BUYER_CREATE_PREORDER / BUYER_CANCEL_ORDER / BUYER_NEGOTIATE_PRICE | ⚠️ | ⚠️ | ✅ | ⚠️ (idem fallback générique) | |
| UPDATE_RECURRING_NEED | ⚠️ | — | ✅ (via draft CAS parent) | ✅ (draft version) | |

## Intents génériques (transaction_payload brut, `confirmation_gate` générique)

| Intent | Happy path | Confirmation intégrité | Idempotent | Notes |
|---|---|---|---|---|
| STOCK_REGISTER_HARVEST | ✅ | ❌ **P1** (résumé construit depuis `transaction_payload` brut, pas de draft — voir §Confirmation) | ⚠️ (fallback message_sid de cette session, non testé spécifiquement) | |
| SALES_RECORD_DIRECT | ✅ | ❌ **P1** | ⚠️ | |
| PRODUCTION_DECLARE_FUTURE / PRODUCTION_UPDATE_FUTURE | ✅ | ❌ **P1** | ⚠️ | |
| FINANCE_LOG_EXPENSE | ✅ | ❌ **P1** | ⚠️ | |
| FARM_CREATE / FARM_UPDATE | ✅ | ❌ **P1** (impact limité — champs administratifs, pas d'argent direct) | ⚠️ | |
| PROFILE_SET_GEO / PROFILE_SET_PREFS | ✅ | — (pas d'argent en jeu) | ⚠️ | |

## Sections transverses

| Dimension | État | Preuve |
|---|---|---|
| Webhook duplicate delivery | ✅ (3 couches : `msg:{SID}`, `task_claim`/`task_done`, `resp:{event_id}`) | `test_process_agent_task_message_dedup.py`, `test_response_dispatch_idempotency.py` |
| Webhook enqueue failure (Celery `.delay()` échoue après le claim) | ❌ **P1**, non fixé cette session | aucun test — perte de message silencieuse, 200 renvoyé quand même |
| Concurrency (verrou par conversation) | ✅ mécanisme, ⚠️ résiduel en mode dégradé | `test_conversation_lock.py` — le fallback "dégradé" en cas de timeout retire la sérialisation, sans backstop pour les ~15 goals sans draft |
| Retry transitoire MCP après timeout | ✅ (corrigé cette session pour les goals sans draft — fallback `message_sid`) | `test_execution_idempotency_key.py::TestMessageSidFallbackForNonDraftGoals` |
| Erreur technique mi-tour (`_sync_workspace` non appelé) | ❌ **P1**, non fixé cette session | aucun test — `ws.active_goal` peut ressusciter périmé au tour suivant |
| PendingInteraction (TTL, kind unique, cart tunnel) | ✅ | `test_pending_interaction_lifecycle.py` (préexistant) |
| Draft registry (4 types, purge conditionnelle) | ✅ (SALES_PUBLISH_PRODUCT), ⚠️ (les 3 autres, mécanisme générique partagé, pas de test dédié par type) | `test_sales_publish_cross_flow_state_leak.py` (cette session), `test_recurring_need_state_leak.py` (préexistant) |
| Candidate selection (cart/tier) | ✅ | `domain/selection_actions.py` reconstruit à neuf chaque tour |
| Candidate selection (bid/auction, hors tunnel panier) | ✅ (corrigé cette session — `bid_phase`/`pending_bid_*` purgés sur transition réelle de but) | `TestMiniFlowKeysArePurgedOnRealGoalTransitions` |
| Same-intent/new-action (SALES_PUBLISH_PRODUCT) | ✅ | `test_sales_publish_cross_flow_state_leak.py` |
| Same-intent/new-action (bid/auction) | ✅ (corrigé cette session) | `TestMiniFlowKeysArePurgedOnRealGoalTransitions` |
| Confirmation construction (draft goals) | ✅ | `check_confirmation_target_invariant`, appelé sur le hot path |
| Confirmation construction (8+ goals génériques) | ❌ **P1**, non fixé cette session | pas de draft, `transaction_payload` brut — voir matrice ci-dessus |
| LLM output trust (schémas stricts) | ✅ (3 contrats sur 4) ⚠️ (ACTIVE_SLOT, `extra` non forbid) | spot-check cette session, pas de test dédié au gap ACTIVE_SLOT |
| Hallucinated entity IDs | ⚠️ | pas de chemin conversationnel démontré aujourd'hui, mais pas de seconde ligne de défense au niveau ACTIVE_SLOT |
| Domain invariants (qty>0, price>0, stock non négatif) | ✅ | `positive_float`, `remove_stock` (`insufficient_stock`) |
| Authorization (ownership DB-layer) | ✅ (corrigé cette session pour `select_winning_bid`), ✅ ailleurs | `test_order_mutations_require_ownership.py` (étendu cette session) |
| MCP tool contract hygiene | ✅ (spot-check 4 tools critiques) | — |
| Business events / outbox | ✅ (atomique, même transaction DB) | `domain/analytics/emitter.py` |
| Response/write mismatch | ✅ (succès strictement gated sur `is_success_response`) | — |
| Dual-role buyer/producer | ✅ (design role-agnostic, ownership poussé au DB-layer) | `nodes/role_guard.py` |
| Number/unit parsing | ✅ (corrigé cette session — ambiguïté point=milliers) | `test_dot_thousands_separator_ambiguity.py` |
| Ambiguous short replies | ✅ (déjà géré par `PendingInteraction`/candidate lists fraîches) | préexistant |
| Legacy interpreter fallback (exposition d'IDs techniques) | ⚠️ **résiduel, non fixé** | mécanisme de secours intentionnel (`MARKET_COACH_NEW_TASK_V2_ENABLED`), jamais désactivé unilatéralement cette session — voir Production Readiness |

## Corrections apportées cette session (résumé)

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

## Ce qui reste ❌ (documenté, non fixé cette session — voir Production Readiness pour la justification)

- Confirmation construite depuis `transaction_payload` brut pour ~8 goals
  génériques (P1).
- Webhook enqueue failure → perte de message silencieuse (P1).
- `_sync_workspace` non appelé sur erreur technique mi-tour → `ws.active_goal`
  périmé peut ressusciter (P1).
- ACTIVE_SLOT contract sans `extra="forbid"` (P2, aucun exploit démontré,
  mais seule ligne de défense pour ce chemin).
- Fallback legacy de l'interpréteur unifié (exposition d'IDs techniques dans
  le prompt) — mécanisme de secours intentionnel, décision produit requise
  avant toute suppression (P2/P3 selon l'usage réel en production).
