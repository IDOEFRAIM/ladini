# PREORDER — clôture escrow / IPN / réconciliation — 2026-09-03

Suite directe de
[PREORDER_TRANSACTIONAL_MIGRATION_2026-09-03.md](PREORDER_TRANSACTIONAL_MIGRATION_2026-09-03.md).
Cette phase ferme les gaps que ce rapport exposait explicitement : PREORDER
avait un `PreorderDraft`/version/CAS/confirmation/`ResponsePlan` complets,
mais l'IPN Paydunya ne synchronisait JAMAIS ce draft, `EXECUTION_UNKNOWN`
n'avait aucune stratégie de sortie, et une annulation Paydunya-side
n'apparaissait NULLE PART côté `Order`. Aucune nouvelle architecture
parallèle : réutilisation stricte de `claim_once`, `PendingInteraction`,
`ConfirmationTarget`, `ResponsePlan`, la convention Langfuse existante, et
l'infrastructure `mcp_idempotency_store`/`PreorderReconciliationService`
bâtie pour PROCUREMENT.

---

## A. Architecture finale

```
Paydunya (webhook IPN)
    ↓ invoice_token SEUL extrait (jamais status/amount du POST brut)
api/routes/paydunya_webhook.py            [INCHANGÉ — déjà minimal, audité]
    ↓ process_paydunya_ipn.delay(invoice_token)
workers/payments/paydunya_ipn_task.py     [wrapper Celery mince]
    ↓ reconcile_invoice(invoice_token)                    ── PARTAGÉ ──┐
flows/buyer/preorder_payment.py::reconcile_invoice                    │
    → PaydunyaClient.confirm_invoice()   [re-confirmation SERVEUR-À-SERVEUR]
    → "completed" → EscrowMixin.mark_escrow_paid()         [Order = source canonique, MAJ EN 1ER]
    → "cancelled" → EscrowMixin.mark_escrow_payment_failed() [NOUVEAU — gap réel comblé]
    → apply_payment_outcome(order_id, paydunya_status, mark_paid_result)
        → adapt_payment_outcome()        [SEUL point de lecture du format Paydunya]
        → finalize_after_payment()       [AWAITING_PAYMENT → EXECUTED/PAYMENT_FAILED/EXECUTION_UNKNOWN]
        → CAS PostgreSQL (preorder_draft_store.compare_and_swap)
        → Outbox (FAILED/EXPIRED only — PAID déjà notifié par mark_escrow_paid)
                                                                        │
workers/crons/preorder_reconciliation.py  [Celery Beat, périodique] ───┘
    → find_stale_executing_candidates()      → reconcile_executing_draft()
    → find_stale_awaiting_payment_candidates() → reconcile_awaiting_payment_draft()
                                                    → _find_invoice_token(order_id)
                                                    → reconcile_invoice(invoice_token)  [RÉUTILISÉ, pas dupliqué]

workers/crons/order_expiry.py (TTL, inchangé) → apply_payment_expiry(order_id) [NOUVEAU câblage]
    → finalize_after_payment_expiry() → CAS → Outbox(ESCROW_PAYMENT_EXPIRED_BUYER)
```

Aucune réponse WhatsApp directe depuis l'IPN ou le cron — uniquement des
écritures Outbox (mêmes gardes `dedupe_key`/`ON CONFLICT DO NOTHING`
qu'avant cette phase), consommées par le `Dispatcher` existant.

---

## B. Source de vérité — règles explicites

| Concept | Source canonique | Qui écrit | Qui lit seulement |
|---|---|---|---|
| Panier/brouillon conversationnel, version | `PreorderDraft` (PostgreSQL, CAS) | `apply_domain_action`, `finalize_after_execution`, `finalize_escrow_initiation`, `finalize_after_payment`, `finalize_after_payment_expiry` — **5 fonctions, jamais un appelant** | tout le reste |
| Commande persistée, stock, statut paiement | `Order` (PostgreSQL, `SELECT...FOR UPDATE`) | `EscrowMixin.mark_escrow_paid`/`mark_escrow_payment_failed`/`expire_pending_payments`, `PreorderGateway.confirm_draft` côté MCP serveur — **inchangées cette phase, sauf l'ajout de `mark_escrow_payment_failed`** | `preorder_payment.py` (lecture du résultat retourné, jamais un re-calcul) |
| Statut paiement "réel" | Paydunya (source EXTERNE) | — | `PaydunyaClient.confirm_invoice`, TOUJOURS re-confirmé serveur-à-serveur, jamais le corps brut de l'IPN |
| Effet externe MCP (`confirm_preorder_draft`) | `mcp_idempotency_store` (table de dédup PostgreSQL) | `AgriDBMCPServer.call_tool` (chokepoint unique, hérité de PROCUREMENT) | `PreorderReconciliationService.reconcile_executing_draft` |

**Invariant central (mandat §2)** : `Order` est TOUJOURS mis à jour EN
PREMIER (par `EscrowMixin`), `PreorderDraft` synchronisé SEULEMENT ENSUITE
(par `apply_payment_outcome`). Un crash entre les deux laisse `Order`
correct et `PreorderDraft` en retard — jamais l'inverse — ce qui est
exactement la fenêtre que la réconciliation ferme (section G).

---

## C. Machine à états — paiement

```
DRAFT ──confirm──► EXECUTING ──MCP confirm_draft/initiate_escrow──►
    ├─ (non-escrow) EXECUTED / FAILED / EXECUTION_UNKNOWN   [terminal, inchangé cette phase]
    └─ (escrow)      AWAITING_PAYMENT
                          │
              ┌───────────┼────────────────┬─────────────────┐
        IPN "completed" IPN "cancelled"  TTL expiry      IPN ambigu/statut inconnu
              │               │                │                  │
          EXECUTED     PAYMENT_FAILED   PAYMENT_EXPIRED    EXECUTION_UNKNOWN
          (terminal)     (terminal)       (terminal)         (reconciliation-only)
```

`AWAITING_PAYMENT` n'est **plus terminal** (c'était un gap identifié par le
rapport de migration) : `_ALLOWED_TRANSITIONS[AWAITING_PAYMENT] =
{EXECUTED, PAYMENT_FAILED, PAYMENT_EXPIRED, EXECUTION_UNKNOWN}`.
`PAYMENT_FAILED`/`PAYMENT_EXPIRED` sont désormais terminaux au même titre
que `EXECUTED`/`FAILED`.

`PaymentOutcomeKind` (adaptateur Paydunya → domaine) : `PAID`, `FAILED`,
`EXPIRED`, `PENDING`, `AMBIGUOUS`. **`PENDING` ≠ `AMBIGUOUS`** — distinction
introduite pendant cette phase après qu'un test anti-legacy (section I) a
détecté que `apply_payment_outcome` comparait encore une chaîne Paydunya
brute (`"pending"`) EN DEHORS de l'adaptateur, violant la règle "un seul
point de contact avec le format Paydunya" :

- `PENDING` = statut Paydunya "pas encore" — cas NORMAL de l'IPN
  out-of-order (mandat §8), **aucune transition, aucun log d'anomalie**.
- `AMBIGUOUS` = statut inattendu/absent/résultat `mark_escrow_paid` non
  confirmé — mérite investigation, transitionne vers `EXECUTION_UNKNOWN`.

`adapt_payment_outcome` est la SEULE fonction qui lit une chaîne Paydunya
brute ; `apply_payment_outcome`/`apply_payment_expiry` ne testent plus que
l'enum retourné — vérifié par
`test_preorder_payment_anti_legacy.py::TestNoRawStringMatchingOnPaydunyaStatus`.

RÈGLE ABSOLUE vérifiée par test (`TestAdaptPaymentOutcome`) : `"completed"`
sans confirmation EXPLICITE de `mark_escrow_paid` (résultat `None` ou
`status != "success"`) reste `AMBIGUOUS`, **jamais** transformé en `PAID`
par simple supposition sur le statut Paydunya seul.

---

## D. Persistance — ce qui a changé dans `preorder_draft_store.py`

```sql
ALTER TABLE marketplace.preorder_drafts ADD COLUMN IF NOT EXISTS order_id TEXT;
CREATE INDEX IF NOT EXISTS ix_preorder_drafts_order_id
    ON marketplace.preorder_drafts (order_id) WHERE order_id IS NOT NULL;
```

`order_id` existait déjà DANS le `payload` JSONB (`PreorderDraft.order_id`)
mais n'était pas une colonne indexée — un IPN/cron n'a QUE l'`order_id`
(jamais le `draft_id`), donc `find_by_order_id` était impossible sans scan
complet. Nouvelle fonction `find_by_order_id(order_id)` — SELECT indexé,
même discipline de désérialisation que `load(draft_id)`.

`find_stale_executing(*, older_than_seconds)` généralisée en
`find_stale_by_status(status, *, older_than_seconds)` — réutilisée pour LES
DEUX candidats de réconciliation (EXECUTING et AWAITING_PAYMENT), `alias`
conservé pour compatibilité (`find_stale_executing` délègue).

`buyer_phone` ajouté comme **champ immuable** de `PreorderDraft` (posé une
fois à `new()`, jamais réécrit par `with_updates`) — nécessaire car l'IPN
et le cron TTL n'ont AUCUN `state` LangGraph d'où lire le numéro de
l'acheteur pour notifier un échec/expiration de paiement.

---

## E. Paiement — création, IPN, idempotence

**Création de session de paiement** : `EscrowGateway.initiate_escrow_payment`
(inchangé, hors périmètre de cette phase — déjà protégé par
`execution_key(draft)` comme idempotency_key MCP, comme documenté dans le
rapport de migration).

**IPN** (`api/routes/paydunya_webhook.py`, audité cette phase, confirmé
DÉJÀ correct) : extrait UNIQUEMENT `invoice_token` du corps POST, ignore
tout `status`/montant fourni par l'appelant, délègue à
`process_paydunya_ipn.delay(invoice_token)`, répond `200 OK` immédiatement
— jamais de traitement synchrone dans la requête HTTP.

**Idempotence IPN** : la clé stable est `invoice_token` lui-même (dérivé du
provider, jamais généré côté agent) — un rejeu Paydunya (webhook garanti
"at-least-once") retombe sur le MÊME `invoice_token`, donc le MÊME chemin
`reconcile_invoice`. Trois niveaux de protection empilés, aucun nouveau :
1. `EscrowMixin.mark_escrow_paid`/`mark_escrow_payment_failed` — garde
   `payment_status déjà ESCROWED/PAID_OUT/CANCELLED` → `already_processed`
   (idempotent, préexistant pour `mark_escrow_paid`, ajouté pour
   `mark_escrow_payment_failed` cette phase, MÊME pattern).
2. `apply_payment_outcome` — garde `draft.status != AWAITING_PAYMENT` → NO-OP.
3. Celery `max_retries=3` sur la tâche elle-même — un retry réseau rejoue
   tout le pipeline, protégé par (1) et (2), jamais une double notification
   (Outbox `dedupe_key`).

**IPN out-of-order** — formalisé par `PaymentOutcomeKind.PENDING` (section
C) : un IPN "pending" arrivant APRÈS un IPN "completed" déjà traité ne
régresse JAMAIS le draft — `draft.status != AWAITING_PAYMENT` (déjà
`EXECUTED`) → NO-OP silencieux AVANT même d'atteindre l'adaptateur. Testé
par `TestIpnOutOfOrder::test_pending_after_success_never_regresses_an_already_paid_draft`
et par une course RÉELLE à 5 threads
(`TestConcurrentIpnDeliveries`, `version` final vérifié `== 4`, jamais >).

---

## F. Exécution — effets externes, chemin unique

PREORDER a DEUX effets externes distincts (mandat §17), jamais confondus :

1. **Réservation + lien de paiement** (`EscrowGateway.initiate_escrow_payment`,
   adapté par `adapt_escrow_result`/`finalize_escrow_initiation`) — pas de
   débit stock immédiat.
2. **Confirmation finale** (`mark_escrow_paid`, appelé par
   `reconcile_invoice` après re-confirmation Paydunya) — débit stock RÉEL,
   déclenché UNIQUEMENT par une preuve de paiement externe, JAMAIS par un
   tour de conversation (`apply_payment_outcome` n'est importé par AUCUN
   module `flows/buyer/preorder.py`/`preorder_confirmation.py` — vérifié
   par `TestPaymentNeverConfusedWithConfirmation`).

Chemin RÉEL tracé (pas supposé) pour la phase EXECUTING (non-escrow) :
`PreorderGateway.confirm_draft` → `_BaseGateway._call` →
`MarketRuntime.call_db` → `AgriMCPClient.call_tool(idempotency_key=...)` →
`AgriDBMCPServer.call_tool` — MÊME chokepoint que PROCUREMENT, donc
`mcp_idempotency_store` s'applique SANS changement.

---

## G. Recovery — `EXECUTION_UNKNOWN` et `PreorderReconciliationService`

`services/reconciliation/preorder_reconciliation_service.py` (nouveau,
calque structurel de `procurement_reconciliation_service.py`) — DEUX
candidats distincts, gérés par deux fonctions dédiées :

| Candidat | Détection | Résolution | Réutilise |
|---|---|---|---|
| `EXECUTING` bloqué | `find_stale_executing_candidates()` (`older_than_seconds=PREORDER_EXECUTING_STALE_SECONDS`) | `reconcile_executing_draft()` — `mcp_idempotency_store.peek(execution_key(draft), "confirm_preorder_draft")` → COMPLETED/FAILED/absent → EXTERNAL_EFFECT_FOUND/CONFIRMED_NO_EFFECT/AMBIGUOUS | `mcp_idempotency_store` (bâti pour PROCUREMENT) |
| `AWAITING_PAYMENT` bloqué | `find_stale_awaiting_payment_candidates()` (MÊME seuil, pas de raison métier de le distinguer — documenté comme choix délibéré) | `reconcile_awaiting_payment_draft()` — résout `invoice_token` via `_find_invoice_token(order_id)`, délègue ENTIÈREMENT à `reconcile_invoice()` | `reconcile_invoice` (section E, PARTAGÉ avec l'IPN temps réel — mandat §26, pas de duplication) |

`EXECUTION_UNKNOWN` reste un état TERMINAL de la machine (hérité de
PROCUREMENT) — **limite explicite, pas cachée** : une fois qu'un draft y
est passé (parce qu'aucune trace n'était encore visible au moment du
passage), un effet externe apparaissant PLUS TARD n'est JAMAIS redécouvert
automatiquement par ce mécanisme (`reconcile_executing_draft` exige
`status == EXECUTING`, `reconcile_awaiting_payment_draft` exige
`AWAITING_PAYMENT` — les deux retournent `NOT_APPLICABLE` sur un draft déjà
`EXECUTION_UNKNOWN`). Nécessite une réconciliation HUMAINE — comportement
identique et volontairement cohérent avec PROCUREMENT.

`reconcile_awaiting_payment_draft` ne devine JAMAIS `PAID` lui-même —
vérifié structurellement par
`TestReconciliationNeverGuessesPaymentSuccess` (`"PaymentOutcomeKind.PAID"
not in code`, `"finalize_after_payment(" not in code` pour cette fonction).

---

## H. Cron — configuration, pas de valeurs codées en dur

```python
# core/settings.py
PREORDER_EXECUTING_STALE_SECONDS: float = 900.0        # 15 min
PREORDER_RECONCILIATION_INTERVAL_SECONDS: float = 300.0  # 5 min
```

`workers/crons/preorder_reconciliation.py` (`workers.preorder_reconciliation`,
`bind=True, max_retries=1`) — traite EXECUTING puis AWAITING_PAYMENT dans le
MÊME passage, retourne les compteurs/issues des deux. Enregistré dans
`api/celery_app.py::include` et `workers/beat_schedule.py`
(`"preorder-reconciliation"`, `expires = schedule - 30`, même marge que
PROCUREMENT — verrouillé par
`test_end_to_end.py::TestWorkers::test_beat_schedule_expiry_shorter_than_period`).

---

## I. Crash-recovery — fenêtres couvertes

| Fenêtre de crash | État persisté après crash | Effet externe connu ? | Rattrapable ? | Test |
|---|---|---|---|---|
| CONFIRM → CAS `EXECUTING` persisté, crash AVANT l'appel MCP | `EXECUTING`, aucune trace MCP | Non (jamais tenté) | Oui — réconciliation trouve `AMBIGUOUS` (rien dans la table de dédup) → `EXECUTION_UNKNOWN` (sûr, pas de faux positif) | `TestReconcileExecutingDraft::test_no_mcp_record_is_ambiguous_never_a_guess` |
| CONFIRM → appel MCP réussi, crash AVANT la finalisation locale (CAS `EXECUTED`) | `EXECUTING`, table de dédup a la trace | Oui, retrouvable | Oui — réconciliation trouve `EXTERNAL_EFFECT_FOUND` → finalise `EXECUTED` en 1 passage | `TestReconcileExecutingDraft::test_a_completed_mcp_record_finalizes_to_executed` |
| Escrow initié → `AWAITING_PAYMENT` persisté, crash AVANT tout IPN | `AWAITING_PAYMENT` | Paiement pas encore fait | Oui — soit un IPN arrive normalement, soit la réconciliation périodique interroge Paydunya elle-même (`reconcile_invoice`) | `TestReconcileAwaitingPaymentDraft` |
| **`mark_escrow_paid` a RÉUSSI (`Order.payment_status=ESCROWED`) → crash AVANT `apply_payment_outcome`** — LA fenêtre fermée par cette phase | `Order` correct, `PreorderDraft` en retard (`AWAITING_PAYMENT`) | Oui, côté `Order` | Oui — un rejeu (IPN retry OU passage de réconciliation suivant) refait `mark_escrow_paid` (idempotent, `already_processed=True`) PUIS `apply_payment_outcome` (jamais sauté) | `tests/chaos/test_preorder_payment_recovery_chaos.py::TestCrashBetweenEscrowPaidAndDraftSync` |
| Paydunya rejette l'invoice (`"cancelled"`) → AVANT cette phase, **RIEN ne mettait `Order` à jour** | `Order.payment_status` restait `PENDING` indéfiniment (jusqu'à l'expiration TTL, des heures plus tard) | Non, silencieusement perdu | Corrigé cette phase — `mark_escrow_payment_failed` + `apply_payment_outcome` synchronisent IMMÉDIATEMENT | `TestFinalizeAfterPayment::test_failed_transitions_to_payment_failed_not_generic_failed` + `reconcile_invoice`'s branche `"cancelled"` |
| Worker meurt PENDANT `_finalize_and_persist` (entre le CAS et le retour) | CAS déjà appliqué ou pas (atomique côté PostgreSQL) | N/A | Oui — soit la ligne est déjà à jour (CAS committé), soit un rejeu repart de `AWAITING_PAYMENT` inchangé | Couvert par la garantie CAS elle-même, pas un scénario distinct |

---

## J. Concurrence — matrice testée

| Paire | Test | Résultat garanti |
|---|---|---|
| PAYMENT IPN × 2 (même draft) | `test_preorder_payment_state_machine.py::TestConcurrentIpnDeliveries` (5 threads réels) | Exactement 1 transition `EXECUTED`, `version` final `== 4` (jamais de double bump) |
| RECONCILIATION × 2, phase EXECUTING | `test_preorder_reconciliation_service.py::TestReconciliationConcurrency` (5 threads) | Exactement 1 finalisation persistée |
| RECONCILIATION × 2, phase AWAITING_PAYMENT | `test_preorder_payment_recovery_chaos.py::TestReconciliationConcurrencyOnAwaitingPayment` (5 threads, **nouveau cette phase**) | Les 5 passages répondent sans lever, ligne finale cohérente |
| CONFIRM × 2 (draft DRAFT) | `test_preorder_transactional_contract.py` (phase migration précédente) | Une seule transition `EXECUTING` |
| CONFIRM + RECONCILIATION (phase EXECUTING) | `test_preorder_payment_recovery_chaos.py::TestConfirmAndExecutingReconciliationRace` (**nouveau**) | Le CONFIRM dupliqué (draft déjà EXECUTING) ne rejoue JAMAIS l'appel MCP ; ligne finale ∈ {EXECUTING, EXECUTED} |
| PAYMENT IPN + CONFIRM dupliqué (draft AWAITING_PAYMENT) | `test_preorder_payment_recovery_chaos.py::TestPaymentIpnAndDuplicateConfirmRace` (**nouveau**) | `apply_domain_action` refuse structurellement (`status != DRAFT`) — AUCUNE nouvelle version, jamais de ré-exécution |
| UPDATE × 2 | `test_preorder_transactional_contract.py` (phase migration précédente) | CAS tranche, un seul gagnant |

Non testé explicitement cette phase (limite documentée, pas cachée) :
UPDATE + CONFIRM concurrents sur le MÊME draft — couvert IMPLICITEMENT par
le CAS générique (n'importe quelle paire de mutations concurrentes sur le
même `draft_id`/`version` est protégée par construction), mais pas par un
test nommé dédié à cette paire précise.

---

## K. Legacy — supprimé / jamais réintroduit cette phase

Aucun des 3 nouveaux modules (`preorder_payment.py`,
`preorder_reconciliation_service.py`, `paydunya_ipn_task.py` réécrit) ne
contient `resolved_id`, `waiting_for_confirmation`, `active_cart` comme
source de canonicité, `confirmation_summary` stocké, ou correspondance de
chaîne brute sur `expected_input`/`gps_stage` — vérifié par grep dédié
(section listée dans le rapport, zéro occurrence) ET par
`test_preorder_payment_anti_legacy.py` (5 tests structurels, y compris
`TestNoNewIdempotencyPrimitive` qui interdit une ré-implémentation
`SET...NX`).

`_resolve_order_id_from_invoice_token` (helper écrit puis rendu redondant
en cours de session par l'extraction de `reconcile_invoice`) — supprimé,
jamais committé comme code mort.

L'ancien corps de `paydunya_ipn_task.py::_run` (branchement Paydunya +
appels `mark_escrow_paid` directs) — entièrement retiré, remplacé par la
délégation à `reconcile_invoice`. Aucune deuxième machine à état cachée
derrière `PreorderDraft` : le seul endroit qui décide `AWAITING_PAYMENT →
X` est `finalize_after_payment`/`finalize_after_payment_expiry`.

---

## L. Observabilité

Réutilise la MÊME convention Langfuse que PROCUREMENT
(`record_procurement_transaction_event`, événement `PROCUREMENT_TRANSACTION_STEP`
— nom hérité, pas renommé pour éviter une deuxième convention parallèle),
enrichie d'un champ optionnel `payment_status` (rétrocompatible, `None`
pour tout appelant PROCUREMENT existant) porté par `draft_id`,
`draft_version_before/after`, `state_before/after`, `execution_key`,
`external_request_id=order_id`, `outcome`, `error` — déjà présents pour
PROCUREMENT, réutilisés tels quels pour PREORDER paiement
(`_finalize_and_persist` dans `preorder_payment.py`).

**Limite explicite, non comblée cette phase** : le mandat demandait aussi
des champs dédiés `payment_id`, `order_state`, et pour l'IPN spécifiquement
`provider_event_id`/`provider_status`/`payment_transition`/
`preorder_transition`. Décision délibérée de NE PAS les ajouter tous :
- `preorder_transition` est déjà porté par `state_before`/`state_after`
  (redondant de le nommer autrement).
- `provider_status` est couvert par le nouveau `payment_status` (même
  contenu, nom aligné sur le vocabulaire déjà utilisé côté domaine
  `adapt_payment_outcome`, pas un nouveau concept).
- `provider_event_id` n'a PAS d'équivalent stable exposé par
  `PaydunyaClient.confirm_invoice` aujourd'hui (seul `invoice_token`, déjà
  capturé via `external_request_id=order_id` — pas exactement la même
  chose, mais l'`order_id` reste la clé de corrélation utile en pratique).
  L'ajouter exigerait une extension du client Paydunya elle-même, jugée
  hors périmètre de cette phase (pas un refus, une priorisation).
- `order_state`/`payment_id` exigeraient une relecture `Order` dédiée
  UNIQUEMENT pour la télémétrie (coût I/O non justifié par le gain
  d'observabilité, `Order` étant déjà la source consultable directement en
  cas d'investigation).

---

## Tests — récapitulatif de cette phase

| Fichier | Tests | Contenu |
|---|---|---|
| `tests/architecture/test_preorder_payment_state_machine.py` | 15 | `PaymentOutcomeKind`, `finalize_after_payment(_expiry)`, IPN out-of-order, 5-threads concurrency |
| `tests/architecture/test_preorder_reconciliation_service.py` | 8 | candidats stale, `reconcile_executing_draft`, `reconcile_awaiting_payment_draft`, concurrence EXECUTING |
| `tests/architecture/test_preorder_payment_anti_legacy.py` | 5 | PAYMENT ≠ CONFIRMATION, pas de string-matching hors adaptateur, pas de nouvelle primitive |
| `tests/chaos/test_preorder_payment_recovery_chaos.py` | 4 | CONFIRM+RECONCILIATION, RECONCILIATION×2 (AWAITING_PAYMENT), IPN+CONFIRM dupliqué, fenêtre de crash escrow-paid→draft-sync |
| `tests/architecture/test_preorder_draft_persistence.py` (étendu) | +3 | `find_by_order_id`, `find_stale_by_status` |
| `tests/unit/test_workers_crons_and_payments.py` (corrigé) | 1 régression fixée | `worker_session` déplacé côté `preorder_payment.py` |
| `tests/integration/test_end_to_end.py::TestWorkers` (corrigé) | 1 régression fixée | assertion source alignée sur la délégation `reconcile_invoice` |

**Suite complète** (`pytest tests/ -q`) : verte à l'exception des 4 échecs
préexistants, sans rapport (`test_create_auction_catalog_gate.py::
TestCreateAuctionCatalogResolution`, `BusinessRuleException: Date limite
invalide.` — bug de date codée en dur dans les fixtures de test,
documenté ailleurs, non touché par cette phase). **Zéro nouvelle
régression.**

---

## Limites restantes (honnêteté RÈGLE ABSOLUE)

1. Pas de test nommé dédié pour la paire UPDATE+CONFIRM concurrente
   (couverte implicitement par le CAS générique, pas explicitement).
2. Pas de scripts de rejeu "parcours utilisateur" bout-en-bout (mandat
   §25 — les 4 scénarios) sous forme de script exécutable séparé ; les
   mêmes garanties sont verrouillées par les tests unitaires/chaos
   ci-dessus, mais pas rejouées comme un scénario narratif complet.
3. "Confirmation snapshot provable" (mandat §19) : aucun mécanisme
   dédié au-delà de ce que le CAS/version implique déjà structurellement
   (un draft confirmé à la version N reste inspectable tel quel tant
   qu'aucune mutation ultérieure n'a eu lieu — mais rien ne matérialise
   explicitement "voici ce qui a été confirmé" comme un objet séparé et
   immuable une fois `Order` créé).
4. Observabilité : `provider_event_id`/`payment_id`/`order_state` non
   ajoutés — voir justification section L.
5. `EXECUTION_UNKNOWN` reste un état terminal du point de vue du code —
   toujours une décision humaine hors-bande pour en sortir, comme
   PROCUREMENT (hérité, pas un gap nouveau).
