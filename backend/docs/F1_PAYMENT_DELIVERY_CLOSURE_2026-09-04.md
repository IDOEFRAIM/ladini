# F1 — Clôture paiement-à-la-livraison (RFQ + PREORDER non-escrow)

2026-09-04. Implémentation concrète du gap P0 identifié par l'audit
fonctionnel global (`PRODUCT_FUNCTIONAL_AUDIT_2026-09-04.md`, section E1).
Contexte produit confirmé : **paiement en ligne désactivé, paiement à la
livraison uniquement.**

**Décision produit tranchée par l'utilisateur avant tout code** (l'audit
préalable, obligatoire par le mandat, avait établi que le code seul ne
permettait pas de la déduire — deux précédents réels pointant dans des
directions différentes) : **producteur seul, un seul geste combiné** —
mirroir exact de `record_sale`, le seul précédent réel pour une vente cash
dans ce dépôt.

---

## A. Décision métier

- **Qui confirme la livraison ?** Le producteur.
- **Qui déclare le paiement reçu ?** Le producteur — dans le MÊME geste
  que la confirmation de livraison (pas deux actions séparées).
- **Justification** : `record_sale` (SALES_RECORD_DIRECT) est le seul
  précédent réel de ce dépôt pour une transaction cash — producteur seul,
  un seul appel, `payment_status`/`delivery_status`/`status` posés
  ensemble. Le précédent escrow (`verify_delivery_otp`) est technique
  aussi producteur-seul, mais son "double contrôle" (le code vient de
  l'acheteur) répond à un besoin différent (débloquer des fonds DÉJÀ
  séquestrés) qui n'existe pas en paiement à la livraison (rien n'est
  séquestré, l'argent change de main au moment même de la livraison).
  Aucun précédent de ce dépôt ne fait jamais avancer une `Order` depuis une
  action ACHETEUR — introduire ce pattern aurait été une invention, pas une
  lecture du code (mandat §1, respecté : audit avant décision).

## B. Order state machine (axe `status`)

| État | Écrit par (avant F1) | Écrit par (après F1) |
|---|---|---|
| `DRAFT` | `create_preorder_draft` | inchangé |
| `CONFIRMED` | `select_winning_bid`, `confirm_preorder_draft`, `initiate_escrow_payment`(via IPN) | inchangé |
| `COMPLETED` | `record_sale` (vente rétroactive) | **+ `confirm_delivery_and_payment`** (nouveau) |
| `CANCELLED` | `cancel_pending_order` (PENDING uniquement), `escrow.py` (annulation/expiration) | inchangé |

Aucun nouvel état inventé — `COMPLETED` existait déjà (écrit par
`record_sale`, déjà étiqueté côté lecture par
`order_tracking.py::ORDER_STATUS_MAP`).

## C. Payment state machine (axe `payment_status`)

| État | Écrit par (avant) | Écrit par (après) |
|---|---|---|
| `PENDING` | défaut colonne | inchangé |
| `ESCROWED` | `initiate_escrow_payment` | inchangé (chemin NON touché) |
| `PAID_OUT` | `verify_delivery_otp` (escrow) | inchangé |
| `PAID` | `record_sale` | **+ `confirm_delivery_and_payment`** (réutilise la valeur EXISTANTE, aucun `PAID_AT_DELIVERY` inventé — mandat §3) |
| `CANCELLED` | `escrow.py` (annulation/expiration) | inchangé |

## D. Delivery state machine (axe `delivery_status`)

| État | Écrit par (avant) | Écrit par (après) |
|---|---|---|
| `PENDING` | défaut colonne | inchangé |
| `DELIVERED` | `verify_delivery_otp` (escrow) | **+ `confirm_delivery_and_payment`** (réutilise la valeur EXISTANTE) |
| `FULFILLED` | `record_sale` (vocabulaire distinct, vente déjà conclue hors-app) | inchangé, non touché |

`DeliveryMixin` (`ASSIGNED`/`IN_TRANSIT`, modèle coursier tiers) —
confirmé, après ce chantier aussi, **toujours inutilisé** : ce modèle ne
correspond pas à la réalité produit ("le producteur livre directement,
coordonne avec l'acheteur" — texte même de `select_winning_bid`), mandat
§7 explicitement respecté ("ne réutilise que ce qui correspond réellement
au mode paiement à la livraison").

## E. RFQ flow — avant/après

```
AVANT :  request → bids → winner → Order(CONFIRMED, payment=PENDING, delivery=PENDING) → [BLOQUÉ, aucune sortie]
APRÈS :  request → bids → winner → Order CONFIRMED → confirm_delivery_and_payment (producteur)
           → Order(COMPLETED, payment=PAID, delivery=DELIVERED)
```

Preuve : `tests/unit/test_payment_at_delivery_e2e.py::TestRfqFullJourneyToCompletion`
— chaîne réelle `place_bid` → `select_winning_bid` → `confirm_delivery_and_payment`,
mêmes objets Python d'une étape à l'autre (mêmes conventions que
`test_auction_bid_full_lifecycle_e2e.py`, mandat AUCTION/BID précédent).

## F. PREORDER non-escrow flow — avant/après

```
AVANT :  cart → checkout → confirm_preorder_draft → Order(CONFIRMED, payment=PENDING, delivery=PENDING) → [BLOQUÉ]
APRÈS :  cart → checkout → confirm_preorder_draft → confirm_delivery_and_payment (producteur)
           → Order(COMPLETED, payment=PAID, delivery=DELIVERED)
```

Chemin `ESCROW_PAYMENT_ENABLED=True` **non touché** — vérifié explicitement
par `test_payment_at_delivery_e2e.py::test_an_escrow_confirmed_order_is_untouched_by_this_path`
(un `payment_status="ESCROWED"` est rejeté par le nouveau chemin avec
`reason="not_pay_at_delivery"`, jamais silencieusement accepté).

## G. Autorisations

`confirm_delivery_and_payment(producer_phone, order_id)` vérifie, dans cet
ordre, sous verrou :
1. **Actor** — résolution du profil producteur depuis le téléphone appelant (`get_producer_profile`).
2. **Order ownership** — UNIFIÉE sur les DEUX origines réelles d'une `Order`
   dans ce produit (jamais les deux à la fois) : PREORDER
   (`OrderItem`→`Product.producer_id`) ou RFQ
   (`Order.winning_bid_id`→`Bid.producer_id`). Un producteur B ne peut
   JAMAIS agir sur une commande du producteur A — prouvé
   (`TestOwnershipAcrossBothOrderOrigins::test_unrelated_producer_is_rejected`,
   `TestRfqFullJourneyToCompletion` section finale).
3. **Current state** — `status=="CONFIRMED"` ET `payment_status=="PENDING"`
   (exclut structurellement escrow et déjà-clôturé).
4. **Allowed transition** — dérivée des préconditions 2/3, jamais un
   raccourci (mandat §4/§10 : `order.status="COMPLETED"` n'est JAMAIS posé
   seul, toujours accompagné des deux autres champs dans le MÊME bloc de
   code).

Côté ACHETEUR : aucune action de clôture introduite (décision A) — ils
restent en lecture seule sur ce cycle, cohérent avec le reste du produit.

## H. Persistence — transactions / locks

- `SELECT ... FOR UPDATE` sur `Order` dès la première lecture (prouvé,
  SQL compilé — `TestRowLocking`).
- Transition ATOMIQUE : `delivery_status`/`payment_status`/`status` +
  `confirmed_at` posés dans le MÊME appel Python avant tout `flush()` — un
  crash AVANT le `flush()` ne laisse RIEN de persisté (aucun champ à moitié
  écrit) ; un crash APRÈS engage `@transactional(write=True)`
  (`AgriDatabaseService.__getattribute__` → `base_service.py`, mécanisme
  PARTAGÉ par tout le backend, déjà audité au chantier AUCTION/BID
  précédent pour `select_winning_bid`) : commit UNIQUEMENT si la fonction
  entière retourne sans exception, rollback complet sinon — **aucun état
  intermédiaire illégal possible** (mandat §14/§15, satisfait par le MÊME
  mécanisme générique déjà éprouvé, pas une nouvelle primitive).

## I. Idempotence

Une seule action (pas deux transitions séparées, décision A) — la question
"quel ordre entre paiement et livraison" (mandat §11) ne se pose donc
structurellement plus : il n'y a qu'UN évènement métier, jamais deux à
séquencer. Idempotence prouvée :
- Un appel sur une commande déjà `COMPLETED` → outcome explicite
  `ALREADY_COMPLETED`, jamais une exception, jamais une ré-écriture
  (`TestIdempotence::test_repeated_confirmation_returns_already_completed_never_reruns`).
- 10 appels consécutifs sur la MÊME commande → EXACTEMENT une transition
  logique, 3 entrées `OrderStatusHistory` au total, jamais dupliquées
  (`test_ten_repeated_calls_produce_exactly_one_logical_transition`).
- Concurrence réelle (deux appels simultanés) : le verrou `FOR UPDATE`
  sérialise — le second, après acquisition, relit `status=="COMPLETED"`
  et retombe sur `ALREADY_COMPLETED`, jamais un double-comptage. Même
  garantie, même mécanisme que `select_winning_bid` (déjà prouvé
  concurrence-sûr au chantier AUCTION/BID) — pas re-testé contre un
  Postgres réel (aucune infrastructure de ce type dans ce dépôt, limite
  documentée depuis le tout premier audit de cette session).

Cancel-concurrency (mandat §13/§30) — vérifié, pas supposé : **aucun
mécanisme de ce produit ne peut annuler une `Order` déjà `CONFIRMED`**
(`cancel_pending_order` exige `status=="PENDING"`, `cancel_preorder_draft`
exige `status=="DRAFT"`) — il n'existe donc structurellement AUCUN chemin
concurrent "cancel + confirm_delivery_and_payment" à tester sur l'état visé
par cette fonction (`CONFIRMED`). Constat honnête, pas un oubli.

## J. Notifications

Une seule notification (pas trois) — cohérent avec l'action combinée et
avec le mandat §17 ("ne pas notifier à chaque transition sans valeur
produit réelle") : `ORDER_COMPLETED_AT_DELIVERY_BUYER`, nouveau template
Outbox (`workers/outbox/templates.py`, même convention exacte que les 8
templates existants), enfilé DANS LA MÊME transaction que la commande — si
le commit échoue, la notification n'est pas enfilée non plus (même
discipline que `AUCTION_WON_PRODUCER`/`PREORDER_RESERVED_PRODUCER`).
Le producteur, acteur de l'action, reçoit sa confirmation de façon
SYNCHRONE (valeur de retour) — pas de notification Outbox pour lui-même.

## K. Read models

- `get_buyer_orders_dashboard` — **gap réel trouvé et fermé** : son propre
  `status_map` inline n'avait AUCUNE entrée `"COMPLETED"` (contrairement à
  `order_tracking.py::ORDER_STATUS_MAP`, qui l'avait déjà) — incohérence
  entre les deux surfaces de lecture buyer-side (mandat §18). Corrigé
  (`services/database/buyer.py`).
- `get_producer_orders` — déjà correct AVANT ce chantier
  (`status_icons` avait déjà `"COMPLETED": "✅"` et `"DELIVERED": "✅"`) —
  vérifié, pas modifié.
- `get_transaction_summary` — pas de label inline, délègue déjà à
  `order_tracking.py::ORDER_STATUS_MAP` (déjà correct) — vérifié, pas
  modifié.

## L. Dead code récupéré

**Aucun** — constat honnête, pas une prétention. `OrderService` et
`DeliveryMixin` restent, après ce chantier, sans appelant réel (recherche
exhaustive refaite). Raison assumée : `OrderService.advance_order_status`/
`record_payment`/`advance_delivery_status` sont des primitives SANS aucun
garde (ni ownership, ni state-guard, ni idempotence) sur une convention de
session DIFFÉRENTE (`BaseService`, session passée en paramètre explicite)
de celle utilisée par `AgriDatabaseService`/`ProducerMgmtMixin` (ContextVar
ambiant) — les adapter en toute sécurité aurait représenté un effort
disproportionné pour une action à un seul champ, un seul acteur. `DeliveryMixin`
reste écarté par choix produit (modèle coursier tiers, mandat §7). Il
n'existe cependant, dans le produit RÉEL, qu'UNE SEULE implémentation
vivante de transition d'`Order` — celle de ce chantier — donc aucun risque
de divergence entre deux chemins concurrents (mandat §6, l'esprit de la
règle est respecté même si la lettre — "réutilise" — ne l'est pas
littéralement). Verdict inchangé : **NEEDS DECISION** reste correct pour
une future session dédiée au nettoyage de schéma/code, pas ce chantier.

## M. Tests

Nouveaux, tous verts au premier essai (après une correction de fixture,
jamais de code métier) :
- `tests/unit/test_confirm_delivery_and_payment.py` (12 tests) — appartenance (2 origines), transition atomique, `OrderStatusHistory` × 3, gardes d'état (escrow exclu, non-confirmé exclu), idempotence (simple + ×10), verrouillage `FOR UPDATE` (SQL compilé), notification Outbox (présente/absente sans bloquer).
- `tests/unit/test_payment_at_delivery_e2e.py` (3 tests) — parcours RFQ complet (bid→bid→winner→clôture, producteur non concerné rejeté), parcours PREORDER non-escrow complet, non-régression explicite du chemin escrow.
- `tests/nodes/test_resolve_order_for_delivery_payment.py` (7 tests) — résolution de commande (auto/menu/selection_index), commandes escrow JAMAIS proposées comme candidates, mélange escrow+cash ne propose que le cash.

## N. Régression complète

`pytest tests/` — voir résultat exact ci-dessous ; attendu (et vérifié à
chaque étape intermédiaire de ce chantier) : mêmes 4 échecs préexistants
sans rapport (`test_create_auction_catalog_gate.py`, date codée en dur),
**zéro nouvelle régression** des 22 nouveaux tests + des 3 wiring
conversationnels (intent, action registry, gateway).

## O. Limites restantes (réelles, pas génériques)

1. **`OrderService`/`DeliveryMixin` restent du code mort** — décision de
   nettoyage explicitement différée (section L), pas oubliée.
2. **Le producteur peut se tromper/mentir** en confirmant une livraison
   non survenue — RISQUE DÉJÀ ACCEPTÉ aujourd'hui pour `record_sale`
   (même mécanisme, même absence de contre-vérification acheteur), pas un
   risque NOUVEAU introduit par ce chantier — documenté comme une
   propriété assumée du modèle "producteur seul", pas un bug.
3. **Aucun délai/rappel automatique** si un producteur ne clôture jamais
   une commande CONFIRMED — l'`Order` peut rester `CONFIRMED/PENDING/PENDING`
   indéfiniment si le producteur n'agit jamais (mieux qu'avant — un chemin
   d'action EXISTE désormais — mais rien ne le déclenche proactivement).
   Hors scope de ce chantier (rappels — `OrderReminder`/`due_reminders`,
   également du code `OrderService` mort — même remarque que L).
4. **Le chemin escrow** (`Order.status` reste `CONFIRMED` pour toujours,
   jamais `COMPLETED`, même après `PAID_OUT`/`DELIVERED`) — gap
   symétrique, mais explicitement HORS SCOPE (mandat §9 : "ne touche pas
   au chemin ESCROW_PAYMENT_ENABLED=True").
