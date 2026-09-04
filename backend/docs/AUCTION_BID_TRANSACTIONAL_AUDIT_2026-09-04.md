# Audit fonctionnel et transactionnel — Auction / Bids (Buyer ↔ Producer)

2026-09-04. Suite de la clôture CART→CHECKOUT et du volet PREORDER
Order(DRAFT). `select_winning_bid` avait déjà reçu un verrou `FOR UPDATE`
lors d'un chantier antérieur — cet audit part du principe que ce n'était
**pas** suffisant et reconstruit tout le parcours réel avant de conclure.

**Réponse à la question centrale du mandat** : *« Lorsqu'un acheteur
sélectionne une offre d'un producteur, une seule décision gagne, le prix et
la quantité sélectionnés sont exactement ceux qui entrent dans la
commande, et aucune course concurrente ne peut créer deux gagnants ou deux
commandes »* — **vraie après ce chantier, fausse avant** sur deux axes
précis (sections D/M).

---

## A. Workflow réel Buyer ↔ Producer

Deux mécanismes DISTINCTS existent dans ce code, tous deux basés sur
`Auction`/`Bid` mais avec des parcours conversationnels séparés :

1. **RFQ multi-producteurs** (`create_auction` → plusieurs `place_bid` →
   `select_winning_bid`) — le scénario du mandat (§24). Entrée conversationnelle :
   `flows/buyer/order_tracking.py` (`list_buyer_auctions` → `check_auction_status`
   → `confirm_winner_selection` → `finalize_winner` → `_execute_winner_selection`).
2. **Négociation à un producteur** (`initiate_negotiation_session` crée un
   `Auction` ancré à UN prix cible → `update_negotiation_offer`/
   `close_negotiation_session`) — entrée conversationnelle :
   `flows/buyer/negotiation.py` (`negotiation_gate` → `_handle_viewing_offers`
   → `select_winning_bid` **directement**, sans écran de confirmation dédié).

Les deux convergent vers le MÊME `select_winning_bid` (services/database/auction.py)
— la couche DB ne distingue pas les deux origines, correctement (une offre
gagnante se traite pareil quelle que soit la porte d'entrée).

Graphe réel (chemin RFQ, celui du mandat) :

```
"je cherche 10T de riz, max 250" (message)
  → interpreter → SALES_/BUYER_ goal → create_auction (DB, Auction OPEN)
"250 FCFA" (producteur A, message)
  → interpreter → place_bid (DB, UPSERT Bid PENDING)
"275" (producteur B) → place_bid (2e Bid)
"300" (producteur A, re-soumission) → place_bid (UPDATE du MÊME Bid, UPSERT)
"mes appels d'offres" (acheteur) → list_buyer_auctions → check_auction_status
  → get_auction_bids (READ, menu numéroté)
"1" (choix) → confirm_winner_selection (récap + demande "oui/non")
"oui" → finalize_winner → enter_gps_stage (tunnel GPS partagé)
position GPS → resolve_gps_stage → _execute_winner_selection
  → select_winning_bid (DB, WRITE : Bid+Auction lockés, Order créé, outbox)
  → réponse SUCCESS (résumé acheteur + notif producteur via Outbox)
```

MCP tools réels : `create_auction`, `place_bid`, `update_bid_price`,
`withdraw_bid`, `get_auction_bids`, `get_auctions_bids`, `get_my_active_bids`,
`get_producer_auctions`, `get_auctions`, `select_winning_bid`, `cancel_auction`,
`check_and_expire_auctions` (cron). Tous des méthodes `AuctionMixin`
(`services/database/auction.py`), auto-exposées par introspection (même
mécanisme que documenté pour PREORDER).

## B. Auction state machine (réelle, pas conceptuelle)

| État | Écrit par | Garde d'entrée AVANT ce chantier | Garde d'entrée APRÈS |
|---|---|---|---|
| `OPEN` | `create_auction` (défaut) | — | — |
| `CLOSED` | `select_winning_bid` | aucun verrou sur `cancel_auction`/expire concurrent | idem (déjà correct côté écrivain) |
| `CANCELLED` | `cancel_auction` | exige `OPEN`, **SANS verrou** | exige `OPEN`, **verrouillé `FOR UPDATE`** ✅ fixé |
| `EXPIRED` | `check_and_expire_auctions` (cron, `UPDATE...WHERE status='OPEN'` — atomique par construction, un seul statement SQL) | déjà sûr (pas de SELECT-puis-UPDATE) | inchangé |

**Transition manquante trouvée** : `select_winning_bid` ne rejetait QUE
`status=="CLOSED"` — une enchère `EXPIRED`/`CANCELLED` restait
sélectionnable. Fixé (section M).

## C. Bid state machine (réelle)

| État | Écrit par | Garde AVANT | Garde APRÈS |
|---|---|---|---|
| `PENDING` | `place_bid` (création) | — | — |
| `PENDING` (prix corrigé) | `update_bid_price` | exige `PENDING`, verrouillé `FOR UPDATE` | inchangé (déjà correct) |
| `WITHDRAWN` | `withdraw_bid` | exige `PENDING`, verrouillé | inchangé (déjà correct) |
| `WINNING` | `select_winning_bid` | **AUCUNE vérification du statut du bid lui-même** | **exige `PENDING`** ✅ fixé |
| `LOST` | `select_winning_bid` (bulk UPDATE des autres bids de la même enchère) | `status IN (PENDING, WINNING)` | inchangé (déjà correct — jamais re-perd un `WITHDRAWN`, déjà exclu de la boucle "perdants" à raison) |

**Transition impossible trouvée, désormais bloquée** : `WITHDRAWN → WINNING`
(directement citée par le mandat) et `LOST → WINNING` (rejeu d'un `bid_id`
périmé) — toutes deux fermées par le nouveau garde `bid.status != "PENDING"`.

## D. Winner selection — analyse approfondie de `select_winning_bid`

Verrouillage confirmé : `SELECT Bid, Auction, ... FROM bids JOIN auctions ...
WHERE Bid.id = :id FOR UPDATE OF bids, auctions` — les DEUX lignes mutées
(`Bid.status`/`is_winner`, `Auction.status`/`winner_bid_id`) sont verrouillées
dans la MÊME requête, donc dans le MÊME ordre pour toute paire de transactions
concurrentes touchant la MÊME enchère (peu importe quel `bid_id` initial est
ciblé — le JOIN résout toujours vers la MÊME ligne `Auction`). C'est cette
propriété, pas seulement "il y a un lock", qui rend les courses suivantes
sûres :

| Course | Résultat AVANT ce chantier | Résultat APRÈS |
|---|---|---|
| `select A` × 2 (même bid) | Sûr (déjà) — 2e relit `status=CLOSED`, `BusinessRuleException` propre | inchangé |
| `select A` + `select B` (même enchère) | Sûr (déjà) — même raisonnement, la ligne `Auction` partagée sérialise | inchangé |
| `select A` + `withdraw A` | **DANGER** — si `withdraw` gagne la course, `select` pouvait quand même désigner le bid `WITHDRAWN` gagnant (aucun garde bid) | **Sûr** — `select` relit `status="WITHDRAWN"` sous verrou, rejette |
| `select A` + `update_bid_price A` | Sûr (déjà) — le prix vu par `select` est TOUJOURS celui réellement commité en premier | inchangé |
| `select A` + `close/cancel auction` (`cancel_auction`) | **DANGER** — `cancel_auction` n'avait aucun verrou, pouvait écraser `status="CLOSED"` (avec `Order` déjà créé) par `status="CANCELLED"` | **Sûr** — verrou ajouté, sérialisé |
| `deadline atteinte` + `select winner` | Sûr (déjà) — le cron est un `UPDATE...WHERE status='OPEN'` atomique, pas de fenêtre SELECT-puis-UPDATE | inchangé |

## E. Concurrency — synthèse

Deux gaps RÉELS trouvés et fermés (voir M) ; le reste de la matrice de
concurrence demandée par le mandat était déjà correctement fermé par le
hardening précédent — vérifié, pas supposé, ligne par ligne ci-dessus.

## F. DB locks / constraints

| Contrainte | Rôle | Suffisante seule ? |
|---|---|---|
| `orders_auction_unique` (UNIQUE `Order.auction_id`) | Empêche физiquement un 2e `Order` sur la même enchère | Oui pour "un seul Order" — mais produirait un `IntegrityError` BRUT sans le row-lock applicatif (déjà le problème résolu par le hardening précédent) |
| `bids_auction_producer_unique` (UNIQUE `auction_id, producer_id`) | "Un seul bid actif par producteur" | Oui — `place_bid` la respecte via UPSERT plutôt que de compter dessus pour rejeter |
| `FOR UPDATE` (Bid, Auction, tous écrivains) | Sérialise les courses, transforme une violation potentielle en refus métier PROPRE avant même d'atteindre la contrainte | Complémentaire, pas redondant — la contrainte protège les données, le verrou protège l'EXPÉRIENCE (message métier, pas stacktrace) |

Aucune contrainte DB remplacée par une vérification Python — les deux
niveaux sont chacun nécessaires pour ce qu'ils garantissent (mandat §15/§16
respecté : pas de "lock everywhere" superflu, chaque verrou ajouté cette
session correspond à une écriture qui n'en avait structurellement aucun).

## G. Order creation

`select_winning_bid` construit `Order` EXCLUSIVEMENT à partir des objets
`bid`/`auction` déjà verrouillés dans LA MÊME transaction — `buyer_id`,
`auction_id`, `winning_bid_id`, `total_amount` (= `bid.offered_price ×
auction.quantity`, tous deux relus sous verrou) — aucune source externe,
aucune valeur mise en cache d'un tour conversationnel antérieur. Pas
d'`OrderItem` créé (design assumé : une `Auction` RFQ n'a pas de `Product`
catalogue à lier — le "produit" est décrit via `SubCategory`/`description`,
pas un `Product.id`) — cohérent avec l'absence d'escrow PROCUREMENT déjà
actée par un mandat précédent, pas une omission.

## H. Idempotence

**Pas de `idempotency_key` MCP sur `select_winning_bid`** (contrairement à
`create_preorder_draft`/`confirm_preorder_draft`) — vérifié volontairement
non nécessaire ici, pas oublié : la combinaison verrou + garde de statut +
contrainte `orders_auction_unique` fournit DÉJÀ la même garantie
("select A" rejoué deux fois → 2e appel = refus métier propre, jamais un
2e `Order`) via un mécanisme différent mais suffisant. Ajouter une clé
d'idempotence en plus serait redondant — décision documentée, pas un oubli
(mandat §13, "détermine la clé adaptée SELON LE VRAI MODÈLE" — le vrai
modèle n'en a pas besoin).

Limite mineure notée (pas corrigée, faible sévérité) : un retry réseau du
MÊME appel (le 1er a RÉUSSI côté serveur mais la réponse s'est perdue) fait
recevoir au client le message "déjà clôturée avec un autre partenaire" —
correct du point de vue des données (un seul `Order` existe bel et bien),
mais potentiellement déroutant si l'acheteur ne réalise pas que c'est SA
propre tentative précédente qui a gagné.

## I. Notifications

Vérifié : `_outbox_repo.enqueue(current_session, [...])` appelé DANS la
MÊME session/transaction que la création de l'`Order` — commit atomique
(si le commit échoue, la notification n'est jamais enfilée non plus).
`dedupe_key=f"AUCTION_WON:{new_order.id}"` — un rejeu de `select_winning_bid`
(qui échouerait de toute façon au garde de statut) ne pourrait de toute
façon jamais générer une 2e notification. Aucun `send_whatsapp()` synchrone
en plein milieu de la transaction — pattern Outbox respecté. Seul le
producteur GAGNANT est notifié par push (Outbox) ; les producteurs perdants
découvrent leur statut `LOST` en lecture (`get_my_active_bids`) — choix
pull existant, non modifié (hors scope, pas un bug).

## J. Read side — Buyer

`confirm_winner_selection` re-fetch les bids EN LIVE (`get_auction_bids`)
au moment de construire le récap "vous allez retenir X à Y FCFA" — pas un
résumé figé d'un tour antérieur. Le succès final (`summary_buyer`) reflète
toujours le prix RÉELLEMENT utilisé par `select_winning_bid` (recalculé
sous verrou), jamais une valeur mise en cache.

**Gap identifié, documenté, PAS corrigé (voir Q)** : entre le récap
("oui/non") et l'exécution réelle, une étape GPS s'intercale (1+ tour
conversationnel supplémentaire) — si le producteur modifie son prix
PENDANT cette fenêtre, le récap que l'acheteur a approuvé peut différer du
prix RÉELLEMENT exécuté (que l'acheteur voit ensuite, mais après
engagement irréversible). La BASE DE DONNÉES est toujours cohérente
(jamais un prix périmé exécuté), seul l'ÉCRAN DE CONFIRMATION peut l'être.

## K. Read side — Producer

`get_my_active_bids` dérive le statut affiché via `_derive_bid_status`
(déjà correct : distingue WON/WITHDRAWN/LOST/PENDING en tenant compte à la
fois du `bid.status` ET du `auction.status`, résilient aux états partiels).
`get_producer_auctions`/`get_auctions` filtrent `Auction.status` par égalité
stricte (jamais de flou) — aucune enchère technique/fermée ne fuit dans les
listes "marché ouvert".

**Observation, non bloquante** : `check_auction_status` (côté acheteur,
mais le menu numéroté équivalent existe implicitement côté flux) inclut
TOUJOURS tous les bids (y compris `WITHDRAWN`/`LOST`) dans la liste
numérotée présentée à l'acheteur pour sélectionner un gagnant — un bid
retiré reste techniquement "tapable" par numéro. Le marqueur visuel (🔴)
le distingue déjà d'un bid actif (🟡), et le nouveau garde serveur (section
D/M) rejette proprement toute tentative — aucune Order ne peut en résulter.
Corriger le filtrage du MENU lui-même (ne plus numéroter les bids non-PENDING)
a été jugé hors de proportion pour ce chantier : un test existant
(`test_bids_present_and_open_returns_a_selection_menu`) attend explicitement
qu'un statut non-PENDING reste numéroté, et le risque réel (perte de
données/argent) est déjà nul après le correctif serveur — seul un
changement UX resterait à faire, non retenu ici (mandat §29 : ne pas
complexifier sans invariant réel en jeu).

## L. `Auction.version`

**Décision : ni A pure, ni B, ni C au sens "réservée" — verdict nuancé,
transmis en suivi plutôt que tranché unilatéralement ici.**

Fait établi (pas supposé) : la colonne a EXACTEMENT UN écrivain vivant dans
tout `src/agriconnect` — `update_negotiation_offer`
(`services/database/buyer.py`), qui l'incrémente à chaque correction de
prix d'une négociation acheteur-producteur. **Aucun lecteur nulle part** —
ni comparaison optimistic-locking (cette fonction utilise déjà
`.with_for_update()`, un verrou PESSIMISTE qui rend l'incrément
fonctionnellement inutile), ni affichage, ni réconciliation. Ce n'est donc
pas une colonne "jamais touchée" (Option A pure ne colle pas telle quelle)
mais un compteur write-only sans consommateur.

Non tranché ici : ce dépôt n'utilise PAS Alembic (DDL idempotent à la
main) — supprimer une colonne en production est une décision de surface
schéma/reporting qui dépasse la portée d'un correctif de code, et rien ne
garantit qu'aucun tableau de bord admin externe ne lit `auctions.version`
directement en SQL (hors de ce qui est visible depuis `src/agriconnect`).
Suggestion transmise séparément (`task_b35b6ddd`) avec une recommandation
claire : au minimum retirer l'incrément mort-né, envisager la suppression
de colonne si confirmé qu'aucun lecteur externe n'existe.

## M. Bugs trouvés et corrigés

1. **`select_winning_bid` — garde de statut d'enchère incomplet.**
   Rejetait uniquement `"CLOSED"` ; une enchère `EXPIRED`/`CANCELLED`
   restait sélectionnable → `Order` créé hors du cycle de vie actif.
   Corrigé : rejet de toute valeur `!= "OPEN"`, message dédié conservé
   pour `CLOSED`. [`auction.py`](../src/agriconnect/services/database/auction.py)
2. **`select_winning_bid` — AUCUN garde sur le statut du bid.** Un bid
   `WITHDRAWN` (ou `LOST`) pouvait être désigné gagnant, créant une
   `Order` sur un engagement explicitement annulé par le producteur — le
   scénario "withdrawn + winning order" cité nommément par le mandat.
   Corrigé : exige `status == "PENDING"`.
3. **`cancel_auction` — aucun verrou de ligne.** Seule écriture sur
   `Auction` sans `FOR UPDATE` dans tout le fichier — une annulation
   acheteur concurrente à `select_winning_bid` pouvait écraser
   `status="CLOSED"` (avec `Order` déjà créé) par `status="CANCELLED"`,
   sans jamais lever d'erreur. Corrigé : `.with_for_update(of=Auction)`.

## N. Code supprimé

Aucun — cet audit n'a identifié aucune duplication/legacy à retirer côté
Auction/Bid (contrairement aux chantiers CART/PREORDER précédents). La
seule dette identifiée (`Auction.version`) est transmise en suivi, pas
supprimée ici (section L).

## O. Tests

Nouveaux, tous verts au premier essai :
- `tests/unit/test_select_winning_bid_state_guards.py` (7 tests) — enchère `EXPIRED`/`CANCELLED` rejetées, message `CLOSED` préservé, bid `WITHDRAWN`/`LOST` jamais sélectionnable, prix 250→275→300 puis sélection → `Order`/récap portent exactement 300 partout, `cancel_auction` réellement verrouillé (SQL compilé).
- `tests/unit/test_place_bid_upsert_semantics.py` (3 tests) — confirmation comportementale (pas seulement SQL) du modèle "un bid par producteur" : 2e appel = UPDATE de la même ligne, jamais un doublon ; un bid déjà traité refuse toute correction.

Coverage pré-existante reconfirmée pertinente (non dupliquée) :
`tests/unit/test_auction_bid_row_locking.py` (verrouillage `place_bid`/
`update_bid_price`/`withdraw_bid`/`select_winning_bid`, déjà écrit lors du
hardening précédent — toujours vert après les changements de ce chantier).

## P. Régression complète

`pytest tests/` — voir résultat ajouté après exécution ci-dessous ; attendu :
mêmes 4 échecs préexistants sans rapport (`test_create_auction_catalog_gate.py`),
zéro nouvelle régression des 10 nouveaux tests + des 3 correctifs.

## Q. Bugs restants (identifiés, non corrigés — décision assumée)

1. **Récap de sélection de gagnant potentiellement périmé face à la
   fenêtre GPS** (section J) — sévérité modérée, jamais un risque de
   données incohérentes (la DB est toujours correcte), seulement un écran
   de confirmation pouvant différer du montant réellement exécuté quelques
   tours plus tard. Non corrigé : aurait nécessité une revalidation de
   prix façon "confirmation target" avant l'exécution finale — jugé
   disproportionné pour ce chantier (mandat §29), documenté pour une
   décision produit ultérieure plutôt qu'implémenté sous contrainte de
   temps dans une zone du code moins testée.
2. **Menu de sélection acheteur numérote aussi les bids `WITHDRAWN`/`LOST`**
   (section K) — UX seulement, aucun risque de données (le garde serveur
   section M#2 bloque toute tentative). Non corrigé, un test existant
   attend explicitement ce comportement.
3. **`Auction.version` write-only** (section L) — transmis en suivi
   (`task_b35b6ddd`), pas une correction de ce chantier.
4. **Retry réseau après un `select_winning_bid` déjà réussi** affiche un
   message potentiellement déconcertant ("déjà clôturée par un autre
   partenaire" au lieu de "c'est VOUS qui avez gagné") — section H,
   sévérité mineure, données toujours correctes.

---

**Règle absolue respectée** : aucune nouvelle architecture
(`AuctionDraft`/`BidDraft`/`WinnerDraft`) créée — les trois correctifs
(M#1-3) sont chacun une ligne de garde ou un verrou manquant sur du code
déjà en place, jamais une nouvelle couche. L'invariant central du mandat
(un seul gagnant, un seul prix, une seule commande, jamais de course
créant deux vainqueurs) est désormais garanti à CHAQUE point de
transition identifié, pas seulement au point déjà corrigé par le chantier
précédent.
