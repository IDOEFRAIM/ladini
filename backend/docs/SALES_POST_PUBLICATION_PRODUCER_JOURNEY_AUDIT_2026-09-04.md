# Audit + hardening ciblé — parcours producteur POST-publication (SALES) — 2026-09-04

Suite de
[SALES_MIGRATION_AND_HARDENING_TRANSVERSE_2026-09-04.md](SALES_MIGRATION_AND_HARDENING_TRANSVERSE_2026-09-04.md).
**`SALES_PUBLISH_PRODUCT` n'a PAS été retouché.** Ce rapport documente
l'audit du parcours réel APRÈS publication, et les 4 correctifs ciblés
(verrous de ligne manquants) qu'il a révélés — **aucun `Draft`, aucune
nouvelle machine à états, aucune réconciliation créée**, conformément à la
RÈGLE FINALE du mandat.

---

## A. Parcours producteur réel

L'audit révèle **DEUX parcours distincts et indépendants** après
`SALES_PUBLISH_PRODUCT` — pas un seul fil continu comme le diagramme du
mandat le suggérait. Aucun des deux n'est littéralement "producteur reçoit
une demande sur SON produit publié, accepte/refuse" — cette hypothèse a été
testée et invalidée par l'audit (section C explique pourquoi).

### Parcours 1 — Achat direct au catalogue (déjà migré : PREORDER)

```
SALES_PUBLISH_PRODUCT → Product (catalogue)
    ↓
Acheteur cherche/trouve le produit (SEARCH_PRODUCTS)
    ↓
Acheteur commande directement (BUYER_PREORDER_INIT/CONFIRM)
    ↓
PreorderDraft → confirm_preorder_draft (AUCUNE étape d'acceptation producteur —
    débit de stock ATOMIQUE, automatique, dès la confirmation acheteur)
    ↓
[si escrow] initiate_escrow_payment → Paydunya → IPN → mark_escrow_paid
    ↓
Producteur transmet le code livraison (verify_delivery_otp) → fonds débloqués
    ↓
Notification (Outbox)
```

**Déjà entièrement audité/durci lors des phases précédentes** (rapports
PREORDER) — non retouché ici.

### Parcours 2 — Négociation par appel d'offres (PROCUREMENT, existant, HORS
du périmètre de la migration `SalesPublishDraft` mais audité ici car c'est
le SEUL parcours réel qui correspond à "producteur reçoit une demande,
consulte, négocie prix/quantité, l'acheteur accepte")

```
Acheteur crée une DEMANDE (PROCUREMENT_CREATE_REQUEST → Auction, déjà migré)
    ↓
Producteur consulte les demandes ouvertes (MARKET_BROWSE_REQUESTS)
    ↓
Producteur propose un prix (SALES_PLACE_BID → place_bid → Bid)
    │   [corrigeable : SALES_UPDATE_PRODUCT-like via update_bid_price,
    │    retirable : withdraw_bid]
    ↓
Acheteur consulte les offres reçues (MARKET_GET_REQUEST_DETAIL, negotiation_gate)
    ↓
Acheteur ACCEPTE une offre (select_winning_bid)
    ↓
Order(status=CONFIRMED) créé DANS LA MÊME TRANSACTION que la clôture
de l'enchère + le rejet des autres offres (LOST) + la notification Outbox
    ↓
Paiement/livraison : HORS APPLICATION, PAR DESIGN — voir section G
    (le texte de réponse de `select_winning_bid` dit littéralement
    "contactez directement le producteur pour coordonner la livraison")
```

**C'est CE parcours qui a été audité en profondeur** (sections D-J) —
c'est le seul qui contient réellement "réception d'une demande →
consultation → négociation prix/quantité → acceptation".

### Ce qui n'existe PAS (vérifié, pas supposé)

- Aucun mécanisme où un acheteur achète directement une ligne de catalogue
  `Product` en passant par une étape d'acceptation/refus producteur —
  `confirm_preorder_draft` débite le stock automatiquement, sans aucune
  lecture ni écriture côté producteur (audité ligne à ligne,
  `services/database/buyer.py:1704`).
- `SALES_ACCEPT_CONTRACT` (`commit_staged_transaction`) est un intent
  PRODUCER, WRITE, enregistré et atteignable via le pipeline générique —
  mais **son outil MCP cible n'a AUCUNE implémentation dans
  `services/database/`** (grep exhaustif, zéro résultat hors fichiers
  agent/config). Code mort ou fonctionnalité jamais terminée du côté
  serveur — signalé, **non corrigé** (hors périmètre : rien n'indique ce
  qu'il devrait faire de plus que `select_winning_bid`, qui couvre déjà
  "acceptation définitive").

---

## B. Entités métier — carte réelle

```
Product (catalog/models.py)                Auction (orders/models.py)
   │  producer_id, quantity_for_sale,          │  buyer_id, sub_category_id,
   │  pricing_tiers                             │  quantity, max_price_per_unit,
   │                                             │  status, winner_bid_id, version(*)
   ↓ (OrderItem.product_id, achat direct)        ↓
OrderItem ◄──────────────── Order ─────────────► Bid
   │  quantity, price_at_sale                    │  offered_price, status,
   │                                              │  is_winner, producer_id
   ↓                                              (PENDING → WINNING/LOST/WITHDRAWN)
 Order (orders/models.py) ─────────────────────────┘
   │  status (DRAFT/CONFIRMED/…), payment_status (PENDING/ESCROWED/PAID_OUT/…),
   │  delivery_status, auction_id (UNIQUE), market_offer_id, winning_bid_id
   ├──► Payment (journal, provider_ref UNIQUE)
   ├──► Delivery (1:1, delivery_code)
   └──► OrderStatusHistory / OrderReminder / OrderDispute
```

(*) `Auction.version` **existe déjà en colonne** (`orders/models.py:371`,
`Integer, default=0`) mais **n'est lu/écrit NULLE PART** dans
`services/database/auction.py` (vérifié — aucune occurrence de
`auction.version` en dehors de la déclaration de colonne). Colonne morte,
signalée, non exploitée par ce chantier (aucun scénario audité n'en a
besoin — le verrou `FOR UPDATE` ajouté section D suffit à la vraie
protection ; introduire une logique CAS sur cette colonne serait ajouter
une 2e protection redondante là où la mandat demande explicitement de ne
PAS remplacer une protection DB correcte par une nouvelle abstraction).

| Entité | Source de vérité | Identifiant | Versionnement | Statuts réels |
|---|---|---|---|---|
| `Product` | PostgreSQL (`marketplace.products`) | UUID | aucun (mutable en place, `SALES_UPDATE_PRODUCT`) | actif implicite (pas de colonne status dédiée auditée ici) |
| `Auction` | PostgreSQL | UUID | colonne `version` INUTILISÉE (voir ci-dessus) | OPEN/CLOSED/CANCELLED/EXPIRED |
| `Bid` | PostgreSQL | UUID | aucun | PENDING/WINNING/LOST/WITHDRAWN |
| `Order` | PostgreSQL | UUID | aucun (verrouillage par `FOR UPDATE`, pas CAS) | DRAFT/CONFIRMED/… + `payment_status` + `delivery_status` séparés |
| `Payment` | PostgreSQL (journal) | UUID, `provider_ref` UNIQUE | aucun | PENDING/… |
| `Delivery` | PostgreSQL (1:1 Order) | UUID, `order_id` UNIQUE | aucun | PENDING/… |

---

## C. Source de vérité — par étape

| Étape | Écrit par | Lu par | Source de vérité |
|---|---|---|---|
| Publication catalogue | `create_product` (inchangé) | `SALES_UPDATE_PRODUCT`, recherche | `Product` (PostgreSQL) |
| Demande (auction) | `create_auction` (PROCUREMENT, déjà migré) | `MARKET_BROWSE_REQUESTS` | `Auction` |
| Proposition prix | `place_bid`/`update_bid_price` | `MARKET_GET_REQUEST_DETAIL`, `track_my_bids` | `Bid` |
| Acceptation | `select_winning_bid` | — | `Order`+`Auction`+`Bid` (transaction unique) |
| Confirmation producteur/acheteur | `PendingInteraction(CONFIRM_ACTION)` + `confirmation_gate` générique (`transaction_payload`/`confirmation_summary`) | `confirmation_gate` | **mécanisme GÉNÉRIQUE, pas un draft canonique** — voir section E pour pourquoi c'est une décision KEEP, pas un gap |
| Livraison (PREORDER seul) | `verify_delivery_otp` | — | `Order.payment_status`/`delivery_status` |
| Notification | `outbox_repo.enqueue` (même transaction que l'écriture métier) | cron `outbox-dispatch` | `outbox` table |

---

## D. Machine d'état

### `Auction`

| État | Transitions autorisées | Transitions interdites |
|---|---|---|
| OPEN | → CLOSED (`select_winning_bid`), → CANCELLED (`cancel_auction`), → EXPIRED (cron) | → OPEN (jamais de réouverture) |
| CLOSED | *(terminal)* | toute autre |
| CANCELLED | *(terminal)* | toute autre |
| EXPIRED | *(terminal)* | toute autre |

### `Bid`

| État | Transitions autorisées | Transitions interdites |
|---|---|---|
| PENDING | → WINNING (`select_winning_bid`, ce bid), → LOST (`select_winning_bid`, les AUTRES bids de la même enchère), → WITHDRAWN (`withdraw_bid`) | toute transition depuis WINNING/LOST/WITHDRAWN (les 3 gardés `if bid.status.upper() != "PENDING": raise`) |
| WINNING / LOST / WITHDRAWN | *(terminaux)* | toute autre |

**Centralisation** : ni `Auction` ni `Bid` n'ont de fonction `_transition`
centralisée façon `ProcurementDraft._ALLOWED_TRANSITIONS` — chaque mixin
vérifie son propre `if status != X: raise` au point d'appel. **Décision
(Phase 19) : KEEP** — 2 entités, transitions peu nombreuses (4 et 4 états),
DÉJÀ gardées individuellement à chaque écriture ; centraliser dans une
table de transitions séparée ajouterait une indirection sans fermer un
bug réel constaté (contrairement à PROCUREMENT/PREORDER/SALES, où
l'absence de centralisation ÉTAIT la cause du bug historique parce que
PLUSIEURS représentations concurrentes du même fait existaient — ici, une
seule ligne par entité, un seul point d'écriture par transition).

### `Order` (portion PROCUREMENT — DRAFT n'existe pas sur ce chemin)

| État | Transitions autorisées | Transitions interdites |
|---|---|---|
| CONFIRMED (créé directement ainsi par `select_winning_bid`) | *(pas de suite modélisée dans ce parcours — coordination hors app)* | — |

---

## E. Confirmation — comment fonctionne-t-elle réellement ?

`SALES_PLACE_BID`/`SALES_ACCEPT_CONTRACT` (côté producteur) et
`PROCUREMENT_ACCEPT_OFFER`/`PROCUREMENT_SELECT_WINNER` (côté acheteur, pour
`select_winning_bid`) passent TOUS par le mécanisme **GÉNÉRIQUE** de
`nodes/confirmation_gate.py` — `PendingInteraction(CONFIRM_ACTION)` (déjà
la primitive canonique, RÉUTILISÉE, pas de `resolved_id`/`waiting_for_confirmation`
trouvé sur ce chemin) + `transaction_payload`/`confirmation_summary` pour
le contenu.

**Décision explicite (Phase 6/19) : KEEP, pas de 2e autorité à supprimer.**
Raison : contrairement à `SALES_PUBLISH_PRODUCT` AVANT sa migration (où le
récap affiché POUVAIT diverger de `transaction_payload` relu à
l'exécution, PARCE QUE plusieurs corrections successives pouvaient
s'accumuler sur plusieurs tours avant confirmation), les actions
`place_bid`/`update_bid_price`/`select_winning_bid` sont des **écritures à
UN SEUL champ variable** (`price`, ou juste `bid_id`) décidées et
exécutées **dans le MÊME tour de confirmation** — il n'existe PAS de phase
"brouillon multi-tours" où le contenu pourrait dériver entre l'affichage
du récap et l'exécution (vérifié : `confirmation_gate` construit
`confirmation_summary` à partir du `transaction_payload` du tour COURANT,
puis `mcp_tool_executor` relit CE MÊME `transaction_payload` dans le tour
SUIVANT — un seul saut, pas d'accumulation). Le test de la section F
(scénario A→B→C→confirm) le vérifie explicitement plutôt que de le
supposer.

---

## F. Concurrence — protections actuelles + gaps trouvés et fermés

| Fonction | AVANT cet audit | Gap réel | Correctif |
|---|---|---|---|
| `place_bid` | `Auction` lu via `.get()` (aucun verrou), `existing_bid` lu sans verrou | Une enchère pouvait recevoir une offre APRÈS sa clôture (race avec `select_winning_bid`) ; un double-tap producteur pouvait tenter un INSERT en double, protégé seulement par la contrainte unique DB (IntegrityError brute, pas une erreur métier propre) | `.with_for_update()` sur `Auction` ET sur `existing_bid` |
| `update_bid_price` | Aucun verrou | Une correction de prix pouvait courir contre `select_winning_bid` — le montant VERROUILLÉ pour l'acheteur pouvait être une valeur périmée, silencieusement | `.with_for_update(of=Bid)` |
| `withdraw_bid` | Aucun verrou | Même classe que ci-dessus | `.with_for_update(of=Bid)` |
| `select_winning_bid` | **AUCUN verrou du tout** — le gap le plus critique (mandat Phase 9 "double accept") | Deux `select_winning_bid` concurrents (2 bids différents, même enchère) pouvaient TOUS LES DEUX lire `status="OPEN"` avant que l'un ne commite `CLOSED` — la contrainte unique `orders_auction_unique` empêchait bien 2 `Order`, mais via une `IntegrityError` brute non gérée (crash-like pour l'utilisateur), pas un refus métier propre | `.with_for_update(of=[Bid, Auction])` |
| `add_bid_photo` | **DÉJÀ correct** (`.with_for_update()` déjà présent) | aucun | KEEP, non touché |
| `verify_delivery_otp` (escrow release, PREORDER) | **DÉJÀ correct** (`.with_for_update()` + garde `payment_status == "ESCROWED"`, idempotent par construction) | aucun (message d'erreur générique sur rejeu — cosmétique, pas une faille) | KEEP, non touché |

**Convention réutilisée, pas inventée** : `.with_for_update(of=...)` était
déjà établi dans `producer.py` (`MarketOffer`, `Stock`) — appliqué ici
à l'identique, aucune nouvelle primitive de verrouillage.

**Preuve** (`tests/unit/test_auction_bid_row_locking.py`, 4 tests) : la
requête RÉELLEMENT compilée (dialecte `postgresql`, jamais exécutée contre
une vraie base — ce dépôt n'a aucune infrastructure Postgres de test,
même limite honnête que documentée pour les 3 migrations précédentes) porte
bien la clause `FOR UPDATE` pour les 4 fonctions corrigées. **Limite
explicite** : ceci prouve que le correctif est dans le chemin de code
exécuté, PAS un comportement sous concurrence réelle (qui exigerait une
vraie instance Postgres — hors de la portée de cette session, comme pour
tout le reste de la suite).

---

## G. Paiement + IPN

**`initiate_escrow_payment`/l'IPN Paydunya sont EXCLUSIVEMENT un mécanisme
PREORDER** — vérifié par grep exhaustif : `payment_status = "ESCROWED"`
n'est écrit qu'à UN SEUL endroit (`escrow.py:287`, dans `mark_escrow_paid`,
atteint uniquement via la chaîne PREORDER). Aucun appelant n'initie
d'escrow pour un `Order` né d'un `select_winning_bid`.

**Ce n'est PAS un gap — c'est une décision produit déjà actée**, confirmée
par le texte même retourné par `select_winning_bid` :
> "Veuillez contacter l'acheteur pour coordonner les détails de la
> livraison."

Les deals PROCUREMENT (achats en gros, B2B, négociés) sont **explicitement
hors application** pour le paiement/la livraison — coordination directe
entre les 2 parties, pas de code OTP, pas d'escrow. `verify_delivery_otp`
ne peut donc structurellement JAMAIS matcher un `Order` de ce parcours
(cherche `payment_status == "ESCROWED"`) — cohérent, pas cassé.

**Séparation déjà respectée** : confirmation d'acceptation (`select_winning_bid`)
≠ paiement (jamais initié sur ce chemin) ≠ exécution (`Order` créé dans la
MÊME transaction que la clôture — pas de fenêtre d'ambiguïté entre les
deux ici, contrairement à PREORDER où l'exécution MCP est un appel séparé).

---

## H. Effets externes — idempotence, retry, recovery

| Effet externe | Idempotence | Retry | Recovery |
|---|---|---|---|
| Création d'`Order` (`select_winning_bid`) | Contrainte unique `orders_auction_unique` (DB) + désormais `FOR UPDATE` (section F) — un rejeu sur une enchère déjà `CLOSED` lève une exception métier propre AVANT toute tentative d'écriture | Pas de `execution_key`/idempotency_key applicative — inutile ici : l'opération est **purement transactionnelle SQL** (tout dans une seule transaction Postgres, sans appel réseau externe intermédiaire) | Une transaction SQL avortée (crash worker en plein milieu) est automatiquement annulée par Postgres — aucun état intermédiaire visible, aucune réconciliation nécessaire |
| Notification Outbox (`AUCTION_WON_PRODUCER`) | `dedupe_key=f"AUCTION_WON:{order.id}"`, MÊME transaction que la création de commande | Cron `outbox-dispatch` (existant) | Si le commit échoue, la notification n'est PAS non plus enfilée (même transaction) — cohérence garantie sans mécanisme dédié |

**`EXECUTION_UNKNOWN` : délibérément PAS créé pour ce parcours** (mandat
Phase 13 : "ne construis pas une reconciliation inutile"). Contrairement à
PROCUREMENT/PREORDER/SALES_PUBLISH_PRODUCT (où l'effet externe RÉEL est un
appel MCP séparé de la transaction locale — fenêtre d'ambiguïté réelle si
le worker meurt ENTRE la persistance locale et la confirmation de l'appel
externe), `select_winning_bid`/`place_bid`/`update_bid_price`/`withdraw_bid`
n'ont **aucun effet externe au-delà de la transaction SQL elle-même** — la
notification Outbox est ÉCRITE (pas envoyée) dans la MÊME transaction. Il
n'existe structurellement AUCUNE fenêtre d'ambiguïté à réconcilier.

---

## I. Notifications — reactive vs proactive

- **Réactive** (réponse au message entrant) : `summary_buyer`/`summary_producer`
  retournés directement par `select_winning_bid`, rendus via le pipeline
  `ResponsePlan`/générique existant — inchangé.
- **Proactive** (l'AUTRE partie, qui n'a rien tapé ce tour-ci) :
  `AUCTION_WON_PRODUCER` via Outbox — **déjà** le bon pattern
  (`DomainOutcome`-adjacent → Outbox → dispatch cron), le domaine
  n'appelle JAMAIS Twilio/WhatsApp directement (vérifié : `select_winning_bid`
  ne fait qu'`outbox_repo.enqueue`, zéro import Twilio dans `auction.py`).

Aucun changement nécessaire ici — déjà conforme au critère du mandat.

---

## J. Legacy

| Élément | Classification | Action |
|---|---|---|
| `PendingInteraction(CONFIRM_ACTION)` pour SALES_PLACE_BID/SALES_ACCEPT_CONTRACT/PROCUREMENT_ACCEPT_OFFER | CANONICAL | Aucune (déjà la bonne primitive) |
| `transaction_payload`/`confirmation_summary` pour ces mêmes goals | CANONICAL pour ce cas précis (voir section E — pas un legacy à supprimer, une écriture/lecture en un seul saut) | KEEP |
| Colonne `Auction.version` | DEAD (jamais lue/écrite) | Signalée, non supprimée (retirer une colonne DB est un chantier de migration séparé, hors périmètre) |
| `SALES_ACCEPT_CONTRACT`/`commit_staged_transaction` | DEAD (outil MCP jamais implémenté côté serveur de ce repo) | Signalée, non supprimée (pourrait être une intégration externe légitime — pas assez de certitude pour supprimer sans casser quelque chose d'invisible à cet audit) |
| `resolved_id`/`waiting_for_confirmation`/`quantity_display`/`unit_display`/`expected_input` | **ABSENTS** de tout le parcours audité (`auction.py`, `flows/producer/auctions.py`, `flows/buyer/negotiation.py`) — vérifié par grep ciblé | Rien à supprimer |

---

## K. Tests

| Fichier | Tests | Contenu |
|---|---|---|
| `tests/unit/test_auction_bid_row_locking.py` (NOUVEAU) | 4 | Preuve compilée `FOR UPDATE` sur les 4 fonctions corrigées (section F) |
| `tests/unit/test_select_winning_bid_geofencing.py` (existant) | 2 | Re-vérifié vert après le correctif (geofencing avant toute requête, chemin inchangé) |
| `tests/unit/test_initiate_escrow_payment_geofencing.py`, `test_producer_bid_photo_hint.py`, `test_negotiation_resilience.py`, `test_negotiation_gps_autoattach.py` (existants) | 13 | Re-vérifiés verts après les 4 correctifs — aucune régression |

**Limite honnête** (déjà signalée section F) : pas de test de concurrence
RÉELLE à threads multiples pour ces 4 fonctions (contrairement aux
tests CAS des drafts) — ce dépôt n'a pas de faux moteur ORM fidèle à la
sémantique `SELECT...FOR UPDATE` de Postgres (bloquant un lecteur
concurrent), et en construire un pour un correctif ciblé serait
disproportionné (RÈGLE FINALE du mandat). La preuve apportée est
"la protection existe dans le code exécuté", pas "elle a été observée
sous charge concurrente réelle".

---

## L. Décision par sous-workflow

| Sous-workflow | Décision | Justification |
|---|---|---|
| `place_bid`/`update_bid_price`/`withdraw_bid` | **CORRIGER CIBLÉMENT** | Architecture déjà saine (transaction SQL unique, mécanisme de confirmation générique suffisant) — seul le verrouillage de ligne manquait. Fait. |
| `select_winning_bid` | **CORRIGER CIBLÉMENT** | Idem — c'était le gap le plus critique (double-accept financier possible en théorie, bloqué en pratique par une contrainte DB brute plutôt qu'une garde métier propre). Fait. |
| `verify_delivery_otp` (PREORDER) | **GARDER** | Déjà correct (verrou + idempotence). |
| Confirmation (`PendingInteraction`/`confirmation_summary` générique) | **GARDER** | Pas de phase multi-tours à protéger sur ce chemin — le mécanisme générique EST suffisant ici (contrairement à SALES_PUBLISH_PRODUCT). |
| `SALES_ACCEPT_CONTRACT`/`commit_staged_transaction` | **NE PAS TOUCHER** | Fonctionnalité dont le backend est absent — hors périmètre, risque de casser une intégration invisible à cet audit en la supprimant sans certitude. |
| `Auction.version` (colonne morte) | **NE PAS TOUCHER** | Migration de schéma séparée, aucun bénéfice fonctionnel immédiat. |
| Paiement/livraison digitale pour PROCUREMENT | **NE PAS CONSTRUIRE** | Décision produit déjà actée (off-platform, par design) — construire un pipeline escrow pour ce chemin serait AJOUTER une fonctionnalité non demandée, pas fermer un bug. |

**Aucun `Draft` créé.** Réponse explicite à la question posée par le
mandat (Phase 3) : "Quel est l'objet que l'utilisateur est en train de
construire ou de modifier avant exécution ?" → **aucun** — chaque action
(`place_bid`, `update_bid_price`, `withdraw_bid`, `select_winning_bid`)
est une écriture à un seul saut, décidée et exécutée dans le même tour,
sur une entité déjà canonique (`Bid`/`Auction`/`Order`) protégée
directement par verrou de ligne — exactement le cas "aucun" que le mandat
anticipait comme réponse valide.

---

## M. CART — audit uniquement (PAS migré)

| Critère | Constat |
|---|---|
| Source de vérité | `state["active_cart"]` — projection LangGraph, PAS de table SQL dédiée. |
| Persistance | Checkpointer (`WorkspaceCheckpointer`), champ `DURABLE` (`core/state_profile.py:191`) — un blob JSON par conversation, PAS une ligne PostgreSQL versionnée. |
| Concurrence | AUCUNE protection dédiée — dernier tour gagnant (sémantique du checkpointer). Risque réel MAIS bas : aucune donnée financière/stock n'est engagée tant que le panier n'est pas converti (`bootstrap_preorder_draft`, déjà CAS-protégé). |
| Quantité/unité | Portée par chaque ligne du panier (dict brut), pas de validation de cohérence centralisée avant le passage en `PreorderDraft`. |
| Frontière de checkout | `bootstrap_preorder_draft` — c'est LE point où le panier (mutable, non protégé) devient un `PreorderDraft` (immuable, versionné, CAS) — frontière déjà correcte et déjà auditée (phase PREORDER). |
| Idempotence | Aucune au niveau panier lui-même — seulement à partir de la frontière de checkout (`creation_key`). |
| Risque | **BAS à MODÉRÉ** — le pire cas concret est une perte de correction utilisateur entre deux tours concurrents (UX dégradée, jamais une incohérence financière/stock), PARCE QUE la frontière de checkout est déjà protégée. Pas de raison d'urgence à migrer CART tant que cette frontière reste intacte. |

**Conformément au mandat : arrêt ici, aucune migration CART commencée.**
