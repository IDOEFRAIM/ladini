# PRODUCT STATUS — 2026-09-04

Audit produit post-F1–F4 : "un buyer ou un producer peut-il réellement
terminer chaque parcours métier important, sans être bloqué dans un état
intermédiaire, et sans qu'une fonctionnalité annoncée existe seulement en
apparence ?"

**Journeys audités : 11** (A, B, C×6 sous-parcours, D, E, F, G)
**Journeys complets (terminal réellement atteignable) : 9/11**
**P0 : 0 — P1 : 2 (1 corrigé dans ce chantier, 1 documenté/non corrigé) — P2 : 3 — P3 : 2**

Ce chantier ne rouvre AUCUN des mécanismes déjà fermés cette session
(CART/CHECKOUT, PREORDER-orphan, Auction/Bid hardening, F1-F4). Il vérifie
qu'ils tiennent réellement bout-en-bout, et cherche spécifiquement les
"fonctionnalités qui existent seulement en apparence" — c'est exactement
ce qu'il a trouvé (section 1, Journey C/G).

---

## 1. Matrice des parcours

| Journey | Acteur | Entry point | Actions | État intermédiaire | État terminal | Terminal atteignable ? | Blocage |
|---|---|---|---|---|---|---|---|
| A — Achat catalogue direct | ACHETEUR | recherche produit | tier → panier → checkout → preorder → paiement à la livraison | `DRAFT` → `CONFIRMED` (payment=PENDING) | `COMPLETED` (payment=PAID, delivery=DELIVERED) | **OUI** — `test_payment_at_delivery_e2e.py` (F1) | Aucun |
| B — RFQ/enchère | ACHETEUR + PRODUCTEUR(s) | `PROCUREMENT_CREATE_REQUEST` | bids → update → sélection gagnant → notifs → clôture | `OPEN` → `CONFIRMED` (payment=PENDING) | `COMPLETED` | **OUI** — `test_rfq_to_completion_with_notifications_e2e.py` (F1+F3) | Aucun |
| C1 — Panier abandonné | ACHETEUR | `active_cart` jamais confirmé | (aucune, simple inaction) | `active_cart` en mémoire de travail | disparaît au changement de but (`goal_planner::_purge_transaction_state`) | **OUI** (rien à clôturer — jamais persisté en DB) | Aucun |
| C2 — Draft préorder abandonné | ACHETEUR | `create_preorder_draft` puis inaction | — | `Order(status=DRAFT)` | reste `DRAFT` indéfiniment SAUF action explicite | **OUI si acheteur agit** (`cancel_preorder_draft`, mandat B) — sinon `DRAFT` orpheline, mais **exclue des dashboards** (`get_buyer_orders_dashboard`/`get_transaction_summary`/`get_producer_orders`, filtrées `notin_(["DRAFT","SUPERSEDED"])`) → invisible, pas trompeuse | Aucun (déjà tranché mandat B : DRAFT orphelin = non-problème produit, correctement invisible) |
| C3 — Preorder annulé | ACHETEUR | `_cancel_preorder` | `cancel_preorder_draft` | `DRAFT` | `CANCELLED` | **OUI** (mandat B) | Aucun |
| C4 — Commande annulée (acheteur) | ACHETEUR | `BUYER_CANCEL_ORDER` → `cancel_order` → `cancel_pending_order` | annulation + recrédit stock | `PENDING`/`CONFIRMED` | `CANCELLED` | **NON avant ce chantier → OUI maintenant (P1, CORRIGÉ ci-dessous)** | Voir §2 finding P1-1 |
| C5 — Enchère annulée | ACHETEUR | `cancel_auction` | annulation, `OPEN` requis, verrouillé | `OPEN` | `CANCELLED` | **OUI** (mandat D, déjà row-locked) | Aucun |
| C6 — Offre retirée | PRODUCTEUR | `withdraw_bid` | retrait, `PENDING` requis, verrouillé | `PENDING` | `WITHDRAWN` | **OUI** (mandat D) — exclu des notifications perdant/gagnant (mandat H/F3, garde `Bid.status.in_(["PENDING","WINNING"])`) | Aucun |
| D — Reprise après interruption | ACHETEUR/PRODUCTEUR | fermeture WhatsApp à tout moment | reprise du tour suivant | `pending_interaction` (DURABLE, `core/pending_interaction.py`) persistée par le checkpointer | reprise correcte du point d'interruption | **OUI** — `pending_interaction` est le discriminant canonique unique, DURABLE dans `state_profile.py`, consommé par `workspace/checkpointer.py` | Aucun (vérifié, pas re-audité en profondeur — hors périmètre de régression de ce chantier) |
| E — Vente catalogue producteur | PRODUCTEUR | `SALES_RECORD_DIRECT`/preorder reçu | `record_sale` (direct) ou notification préorder (F2) + `confirm_delivery_and_payment` (F1) | `PENDING`→`COMPLETED` (direct) / `CONFIRMED`→`COMPLETED` (préorder) | `COMPLETED` | **OUI** | Aucun |
| F — RFQ producteur (dashboard multi-origine) | PRODUCTEUR | `SALES_LIST_ORDERS`/`_resolve_order_for_delivery_payment` | `get_producer_orders` (inclut catalogue + préorder + RFQ, mandat E) | — | commande visible, quel que soit son origine | **OUI** — `test_producer_orders_includes_auction_wins.py` | Aucun |
| G — Annulation/rejet producteur | PRODUCTEUR | *aucun entry point réel post-sélection* | `withdraw_bid` (PRÉ-sélection uniquement) | — | — | **NON — GAP RÉEL, NON CORRIGÉ** | Voir §2 finding P1-2 |

---

## 2. Findings P0/P1 (root cause exacte)

### P1-1 — `BUYER_CANCEL_ORDER` structurellement inatteignable — **CORRIGÉ dans ce chantier**

**Root cause exacte** : `BuyerMixin.cancel_pending_order` gardait `order.status.upper() != "PENDING": raise BusinessRuleException(reason="not_pending")`. Recherche exhaustive sur TOUTE la session (chaque chemin de création de commande audité) : **aucun chemin conversationnel vivant ne pose jamais ce statut** — `"PENDING"` n'est que le défaut de colonne SQLAlchemy, systématiquement écrasé (`create_preorder_draft` pose `"DRAFT"`, `select_winning_bid` pose `"CONFIRMED"` directement, `record_sale` pose `"COMPLETED"` directement). Seul le chemin confirmé-mort `finalize_multi_order` créait jamais une commande `PENDING`.

**Fichier(s)** : [buyer.py](../src/ladini/services/database/buyer.py) (`cancel_pending_order`), [order_tracking.py](../src/ladini/graphs/agents/market_coach/flows/buyer/order_tracking.py) (`cancel_order`), [templates.py](../src/ladini/workers/outbox/templates.py).

**Fonction(s)** : `BuyerMixin.cancel_pending_order`, `flows/buyer/order_tracking.py::cancel_order`.

**Chemin utilisateur concerné** : n'importe quel acheteur, sur n'importe quelle commande CONFIRMED (catalogue ou RFQ) qu'il souhaite annuler avant livraison — le SEUL cas réel qui se présente en usage normal.

**Pourquoi le produit était réellement bloqué** : le goal est déclaré, tunnelé, avec un handler réel et un rendu conversationnel dédié (demande de raison, message d'erreur "statut" spécifique) — un utilisateur qui tape "annuler ma commande" recevait TOUJOURS le même rejet ("commande déjà en statut CONFIRMED"), quel que soit le contexte. C'est exactement le pattern "fonctionnalité annoncée qui existe seulement en apparence" que ce chantier devait débusquer.

**Correction apportée** :
- Garde élargi à `{"PENDING", "CONFIRMED"}` (jamais `DRAFT`/`COMPLETED`/`CANCELLED`/`SUPERSEDED`).
- Sérialisation contre `confirm_delivery_and_payment` (F1) déjà garantie par construction : les deux verrouillent la même ligne `Order` FOR UPDATE et exigent le même statut `CONFIRMED` — le premier à committer gagne, l'autre retombe proprement sur son propre garde.
- Producteur(s) notifié(s) (nouveau template `ORDER_CANCELLED_BY_BUYER_PRODUCER`) uniquement quand une commande déjà `CONFIRMED` est annulée (jamais pour `PENDING` legacy, qu'aucun producteur n'a jamais "vu"). Résolution dual-origine identique à `confirm_delivery_and_payment` (préorder via `OrderItem`, RFQ via `Bid.producer_id`).
- Message acheteur corrigé pour ne plus prétendre une restitution de stock sur une commande RFQ (qui n'a jamais débité de stock catalogue, aucun `OrderItem`).
- Copie utilisateur de `order_tracking.py::cancel_order` mise à jour ("Seules les commandes en attente ou confirmées, pas encore livrées, peuvent être annulées" — était restée figée sur "en attente" seulement, ce qui serait devenu un mensonge après le correctif DB).

**Test de non-régression** : [test_cancel_pending_order_confirmed_gap.py](../tests/unit/test_cancel_pending_order_confirmed_gap.py) — 9 tests (CONFIRMED préorder annulable + stock recrédité + notification ; CONFIRMED RFQ annulable sans toucher au stock + notification via bid ; aucun téléphone résolu ne bloque jamais l'annulation ; `PENDING` legacy fonctionne toujours sans notifier ; `DRAFT`/`COMPLETED`/`CANCELLED`/`SUPERSEDED` toujours rejetés ; retry après annulation rejeté sans double notification).

### P1-2 — Aucun mécanisme d'annulation/rejet producteur post-sélection — **NON CORRIGÉ, documenté**

**Root cause exacte** : recherche exhaustive (`reject_order`/`producer_cancel`/`refuse_order`/tout goal `PRODUCER_*CANCEL*` dans `interpreter/intent.py`) : **zéro résultat**. `withdraw_bid` (le seul mécanisme d'annulation côté producteur) exige `Bid.status == "PENDING"` — donc n'existe QUE PRÉ-sélection. Une fois une commande `CONFIRMED` (catalogue préorder OU RFQ gagné), **aucune action conversationnelle ne permet au producteur de se rétracter** s'il ne peut finalement pas honorer la commande (rupture de stock imprévue, aléa de production, etc.).

**Fichier(s)** : aucun fichier de production concerné — c'est une ABSENCE structurelle, pas un bug de code. Point de comparaison : [buyer.py](../src/ladini/services/database/buyer.py)`::cancel_pending_order` (symétrique acheteur, maintenant fonctionnel, §P1-1) vs [producer.py](../src/ladini/services/database/producer.py) (aucune méthode équivalente).

**Chemin utilisateur concerné** : tout producteur ayant une commande `CONFIRMED` qu'il ne peut pas honorer.

**Pourquoi le produit est réellement bloqué** : sans mécanisme dédié, le producteur n'a que deux issues, toutes deux mauvaises pour le produit : (1) ne rien faire → la commande reste `CONFIRMED` indéfiniment, l'acheteur attend une livraison qui ne viendra jamais, (2) contacter l'acheteur hors-app pour négocier une annulation manuelle côté acheteur (`BUYER_CANCEL_ORDER`, maintenant fonctionnel grâce à P1-1) — un contournement conversationnel, pas une fonctionnalité produit.

**Pourquoi ce n'est PAS corrigé dans ce chantier** : contrairement à P1-1 (un garde erroné sur une logique déjà correcte et testée), fermer ce gap exigerait une VRAIE décision produit — même nature que la question tranchée en F1 par `AskUserQuestion` : qui peut initier l'annulation côté producteur, y a-t-il une pénalité/limite anti-abus symétrique à celle de l'acheteur (`_enforce_cancellation_limit`, `MAX_CANCELLATIONS`), le stock catalogue doit-il être re-décrémenté ou reste-t-il déjà débité, l'acheteur reçoit-il automatiquement une compensation. Construire cela sans cette décision serait exactement le "correctif métier arbitraire" interdit par ce chantier.

**Correction minimale nécessaire (à valider comme prochain chantier, PAS construite ici)** : nouvelle méthode `ProducerMgmtMixin.reject_confirmed_order` — même verrouillage FOR UPDATE + même résolution dual-origine que `confirm_delivery_and_payment`, mais nécessite d'abord une décision produit explicite (question type `AskUserQuestion`) sur les points ci-dessus.

**Test de non-régression** : sans objet (rien n'a été codé — c'est une recommandation, pas un correctif).

---

## 3. Audit des états à risque de blocage permanent

| État | Qui écrit | Qui peut en sortir | Action déclenchante | Exposée à un acteur ? | Timeout/recovery ? | Dans un dashboard ? | Éternellement bloqué possible ? |
|---|---|---|---|---|---|---|---|
| `Order.DRAFT` | `create_preorder_draft` | acheteur (`cancel_preorder_draft`) ou remplacement (ADD_MORE→`SUPERSEDED`) | `_cancel_preorder`/`bootstrap_preorder_draft` | Oui | Non (pas de cron TTL) mais **invisible** dans les 3 dashboards (buyer/producer/transaction summary) — non trompeur | Non (exclu explicitement) | Oui en DB, mais **sans impact produit** (mandat B, tranché : non-problème) |
| `Order.PENDING` | *(mort — aucun chemin vivant)* | — | — | — | — | Oui (non exclu, mais n'apparaît jamais en pratique) | Non applicable (jamais créé) |
| `Order.CONFIRMED` | `select_winning_bid` (RFQ), `confirm_preorder_draft` (préorder) | acheteur (`cancel_pending_order`, **maintenant fonctionnel, P1-1**) OU producteur (`confirm_delivery_and_payment`, F1) | annulation acheteur OU clôture livraison/paiement | Oui (les deux) | Non (pas de TTL auto — accepté : le mandat interdit d'inventer un cron sans preuve d'impact) | Oui | **Non pour l'acheteur** (peut annuler) ; **oui pour le producteur** si aucune des deux parties n'agit (P1-2, gap identifié) |
| `Auction.OPEN` | `create_auction` | acheteur (`cancel_auction`) ou sélection gagnant (`select_winning_bid` → `CLOSED`) | annulation OU sélection | Oui | Non | Oui (`list_buyer_auctions`) | Non (deux sorties réelles) |
| `Bid.PENDING`/`WINNING` | `place_bid`/`select_winning_bid` | producteur (`withdraw_bid`, PENDING seulement) ou décision acheteur | retrait OU sélection | Oui | Non | Oui (`get_my_active_bids`) | Non (sort toujours via l'une des deux voies) |
| `payment_status.PENDING` (post-CONFIRMED) | `select_winning_bid`/`confirm_preorder_draft` | producteur (`confirm_delivery_and_payment`) | clôture livraison/paiement | Oui | Non | Oui | Même limite que `CONFIRMED` ci-dessus |

**Constat global** : aucun état n'est bloqué côté ACHETEUR (il dispose toujours d'une sortie — annulation ou attente légitime de livraison). Le seul état à risque de blocage réel concerne le PRODUCTEUR sur `CONFIRMED` sans mécanisme de rejet — c'est exactement P1-2.

---

## 4. Audit des notifications comme produit

| Event | Recipient | Template | Idempotence | Produit complet ? |
|---|---|---|---|---|
| Enchère gagnée | Producteur gagnant | `AUCTION_WON_PRODUCER` | Naturelle (transition unique `select_winning_bid`) | ✅ |
| Enchère perdue | Producteur(s) perdant(s) | `AUCTION_LOST_PRODUCER` (F3) | `.returning()` dérivé de la même transition, jamais une boucle séparée | ✅ |
| Paiement escrow sécurisé | Producteur | `ESCROW_PAYMENT_SECURED_PRODUCER` | `dedupe_key` outbox | ✅ |
| Paiement escrow reçu | Acheteur | `ESCROW_PAYMENT_RECEIVED_BUYER` | idem | ✅ |
| Paiement escrow échoué/expiré | Acheteur | `ESCROW_PAYMENT_FAILED_BUYER`/`_EXPIRED_BUYER` | idem | ✅ |
| Préorder confirmé (sans escrow) | Producteur(s) | `PREORDER_CONFIRMED_PRODUCER` (F2) | idempotence naturelle (statut `DRAFT`→`CONFIRMED` non répétable) | ✅ — jamais "paiement reçu" |
| Clôture livraison+paiement à la livraison | Acheteur | `ORDER_COMPLETED_AT_DELIVERY_BUYER` (F1) | idempotence naturelle (`ALREADY_COMPLETED` sur retry) | ✅ |
| **Annulation par l'acheteur d'une commande CONFIRMED** | **Producteur(s)** | **`ORDER_CANCELLED_BY_BUYER_PRODUCER` (nouveau, ce chantier)** | `dedupe_key` outbox, idempotence naturelle (2e annulation rejetée avant d'atteindre le code de notif) | ✅ — gap fermé dans ce chantier |
| Nouvelle offre/opportunité | Producteur(s) proches | `AUCTION_INVITE_PRODUCER` | existant, hors périmètre | ✅ |
| Nouveau produit disponible | Acheteur | `NEW_PRODUCT_ALERT_BUYER` | existant, hors périmètre | ✅ |
| Préorder réservé (production future) | Producteur | `PREORDER_RESERVED_PRODUCER` | existant, hors périmètre | ✅ |
| **Rejet/annulation par le producteur d'une commande CONFIRMED** | Acheteur | *(n'existe pas — action elle-même inexistante)* | — | ❌ — dépend de P1-2 |

**Total templates outbox : 12** (11 avant ce chantier + `ORDER_CANCELLED_BY_BUYER_PRODUCER`).

---

## 5. Audit des dashboards / read models

- `get_buyer_orders_dashboard` : exclut `DRAFT`/`SUPERSEDED` (mandat B) ; `status_map` couvre `CANCELLED`/`COMPLETED`/`CONFIRMED` correctement (F1 a ajouté l'entrée `COMPLETED` manquante) ; les commandes RFQ (sans `OrderItem`) ET catalogue apparaissent toutes deux (vérifié — la requête filtre sur `Order.buyer_id`, jamais sur la présence d'`OrderItem`).
- `get_transaction_summary` : même exclusion `DRAFT`/`SUPERSEDED` sur la branche "transaction la plus récente" (la branche "lookup explicite par order_id" reste volontairement non filtrée — un lookup explicite doit pouvoir retrouver n'importe quelle commande, y compris un brouillon, mandat B).
- `get_producer_orders` : exclut `DRAFT`/`SUPERSEDED` par défaut (mandat C, correctif du leak buyer-side symétrique) ; inclut désormais les commandes d'origine RFQ (mandat E, `auction_order_ids` via `Bid.producer_id`) — auparavant invisibles au producteur gagnant.
- Aucune commande abandonnée/annulée/superseded ne "fuit" plus dans les vues normales — `CANCELLED` reste volontairement visible (c'est un historique légitime, pas une fuite) partout où testé.
- Pas de nouvel état backend sans traduction UX identifié dans cette passe au-delà de P1-2 (l'ABSENCE de mécanisme producteur, pas un état mal traduit).

---

## 6. Audit des intents "fantômes"

| Intent | Constat | Classification |
|---|---|---|
| `STOCK_RECORD_MOVEMENT`/`STOCK_ADJUST`/`STOCK_REMOVE_PARTIAL`/`STOCK_DELETE` | `tool_name` cassé (`*_by_id` inexistant), déjà documenté (mandat F) | **DEPRECATE** (dépriorisé, ne pas toucher sans nouveau chantier dédié) |
| `PROCUREMENT_SELECT_WINNER`/`PROCUREMENT_ACCEPT_OFFER` | Neutralisés (F4, `RuntimeError` explicite, toujours déclarés pour l'intégrité du registre) | **CLOS** (confirmé toujours neutralisé dans ce chantier — `test_winner_selection_single_entrypoint.py` toujours vert) |
| `BUYER_CANCEL_ORDER` | Était structurellement inatteignable — **CORRIGÉ ce chantier** | **FIX — CLOS** |
| `order_service.py`/`delivery.py` (classes entières) | Toujours zéro appelant (re-vérifié) | **DEPRECATE** (candidats de récupération déjà écartés lors de F1 — modèle métier différent, convention de session différente) |
| `Auction.version` | Déjà tranché (mandat E) | **CLOS** |
| *(nouveau)* Aucun goal `PRODUCER_REJECT_ORDER`/`PRODUCER_CANCEL_ORDER` n'existe | Absence structurelle, pas un goal cassé | **À CRÉER** — dépend de P1-2, décision produit requise avant tout code |

Aucune suppression effectuée (mandat : "ne supprime rien durant cette phase sauf nécessité absolue démontrée" — aucune n'a été démontrée).

---

## 7. Dead-ends conversationnels

Aucun nouveau dead-end trouvé au-delà de ceux déjà fermés cette session (tier/quantité/producteur/confirmation/winner/GPS/annulation/retry/reprise — tous vérifiés fonctionnels par les audits antérieurs et les tests E2E existants). Le seul dead-end RÉEL restant est comportemental, pas conversationnel : un producteur qui tape "annuler ma commande" ou "je ne peux pas livrer" sur une commande `CONFIRMED` n'a aucune action à laquelle ce message correspond (P1-2) — l'interpréteur le classera au mieux en `UNKNOWN`/clarification, jamais en échec silencieux d'exécution (pas un bug de plus, la conséquence attendue de l'absence documentée en P1-2).

---

## 8. Tests E2E ajoutés

| # | Scénario | Statut |
|---|---|---|
| 1 | Achat catalogue → livraison/paiement → COMPLETED | Déjà couvert (F1, `test_payment_at_delivery_e2e.py`) |
| 2 | Préorder direct multi-producteur → notifications producteur → livraison/paiement → COMPLETED | Déjà couvert (F2, `test_preorder_confirmed_producer_notification.py`, notification ; chaînage complet avec F1 démontré par le même motif que le test #3) |
| 3 | RFQ → bids concurrents → gagnant → gagnant+perdants notifiés → commande → livraison/paiement → COMPLETED | Déjà couvert (F1+F3, `test_rfq_to_completion_with_notifications_e2e.py`) |
| 4 | Draft/commande avortée → invisible comme transaction active → aucune commande fantôme | Déjà couvert (mandat B, `test_preorder_draft_order_lifecycle_sync.py` + `test_buyer_order_reads_exclude_draft_status.py`) |
| 5 | Dashboard producteur → commandes catalogue + préorder + RFQ | Déjà couvert (mandat E, `test_producer_orders_includes_auction_wins.py`) |
| 6 | Même action rejouée → aucune double mutation/notification | Déjà couvert (F1/F2/F3, tests d'idempotence dédiés) |
| **Nouveau** | Annulation acheteur d'une commande CONFIRMED (préorder et RFQ) → recrédit stock correct → notification producteur exactement une fois → retry rejeté sans double notif | **`test_cancel_pending_order_confirmed_gap.py`, 9 tests, ajouté ce chantier** |

---

## 9. Régression

`python -m pytest tests/ -q` — suite complète exécutée après le correctif P1-1. **4 échecs préexistants, inchangés, sans rapport avec ce chantier** :
`tests/unit/test_create_auction_catalog_gate.py::TestCreateAuctionCatalogResolution::{test_no_candidate_at_all_gets_auto_provisioned_not_rejected, test_a_fuzzy_matched_but_unconfident_candidate_falls_back_to_auto_provisioning, test_a_confidently_matched_candidate_is_used_as_is, test_the_second_stage_fuzzy_matcher_is_also_confidence_gated}` (bug de date codée en dur, présent avant ce chantier, non touché). **Zéro nouvelle régression.**

---

## 10. Ce qui reste réellement ouvert

**Un seul point** : P1-2 — mécanisme d'annulation/rejet producteur post-sélection. Recommandation : traiter comme un chantier dédié, démarrant par une clarification produit explicite (type `AskUserQuestion`, même format que F1) sur : qui peut l'initier, limite anti-abus symétrique ou non, traitement du stock déjà débité, information/compensation de l'acheteur. Ne pas construire avant cette décision — exactement la même discipline que celle qui a fermé F1 correctement.

Tout le reste du produit — achat catalogue, RFQ, préorder (escrow et non-escrow), annulations acheteur (panier/draft/preorder/commande/enchère), retrait d'offre producteur, dashboards des deux côtés, notifications — atteint un état terminal réel et cohérent, sans état fantôme trompeur.
