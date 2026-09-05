# PHASE 6B — VALIDATION DU MODÈLE DE PRODUCTION & DÉCISIONS RESTANTES (2026-09-05)

## 1. Executive summary

Contexte nouveau : **la base sera réinitialisée avant la mise en
production**. La compatibilité historique n'est donc plus un objectif —
seule compte la question : *le modèle qui partira en production
est-il cohérent et sûr ?*

Résultat : **oui**, avec **une faille réelle trouvée et fermée** pendant
cette validation.

| Sujet | Résultat |
|---|---|
| Invariant « une nouvelle commande = un producteur » | **imposé à la source et verrouillé par contrat** |
| Mutations d'état prenant `order_id` | auditées une par une — **une faille trouvée** : `update_order_status`, mutation arbitraire **sans aucun contrôle de propriété**, exposée via MCP → rendue inatteignable |
| Clôture / annulation / notifications / dashboards | isolés par commande, vérifiés |
| RFQ / enchères | inchangées, mono-producteur par construction |
| Migration legacy | **abandonnée** (base réinitialisée) — aucun chantier de compatibilité |
| Décisions produit | 3 fermées, 1 reste ouverte |

## 2. Production order model

```
NOUVEAU CHECKOUT
  panier (vendeur choisi par produit)
    → regroupement par producer_id
    → une Order(DRAFT) par producteur, même checkout_group_id
    → UNE confirmation acheteur → toutes CONFIRMED (atomique)
    → ensuite : cycles de vie totalement indépendants

RFQ / ENCHÈRE
  select_winning_bid → 1 Order, 1 winning_bid, 0 OrderItem
    → mono-producteur PAR CONSTRUCTION, hors du split

VENTE DIRECTE
  record_sale → 1 Order, 1 article du producteur lui-même
```

`checkout_group_id` reste une **corrélation**, jamais un état : aucune
machine à états globale n'a été créée.

## 3. One-order-one-producer invariant

Imposé à la source (`create_preorder_draft` : la commande est créée **à
partir du producteur de la ligne**, pas remplie après coup) et verrouillé
par [test_order_mutations_require_ownership.py](../tests/architecture/test_order_mutations_require_ownership.py) :

| Contrôle | Ce qu'il empêche |
|---|---|
| `test_checkout_creates_one_order_per_producer` | qu'on remplace le regroupement par un filtrage a posteriori |
| `test_no_live_path_puts_two_producers_in_one_order` | qu'un **nouveau** site de création d'`OrderItem` apparaisse sans traiter le regroupement (liste blanche des 3 sites connus) |
| `test_finalize_multi_order_stays_unwired` | qu'on câble `finalize_multi_order` — qui crée **une** commande pour **tous** les producteurs et réintroduirait exactement le P1 |
| `test_auction_path_creates_no_order_items...` | que le split contamine le chemin enchère |

Sites de création d'`OrderItem` audités : le checkout groupé (regroupe),
`record_sale` (un seul produit du producteur), `OrderService` (**mort**,
zéro appelant).

## 4. Grouped checkout

Comportement retenu (A3) : un seul geste de confirmation pour tout le
groupe ; l'acheteur est prévenu **avant** de confirmer que son panier
produira N commandes (`render_summary`, projection pure).

## 5. Confirmation

- Les commandes sœurs sont chargées `FOR UPDATE` dans un ordre déterministe.
- Le tri anti-deadlock porte sur l'**union** des articles du groupe (la garantie doit être globale, pas par commande).
- **Atomicité** : un stock insuffisant sur une seule part fait échouer *toute* la confirmation — aucune commande à moitié confirmée (`test_insufficient_stock_on_one_order_aborts_the_whole_group`).
- Rejeu : la seconde confirmation échoue sur `not_draft`, sans re-débit ni re-notification.

## 6. Fulfillment

`confirm_delivery_and_payment` (F1) **n'a pas été modifié**. Son contrôle
« le producteur possède au moins un article » devient exact dès lors
qu'une commande n'a qu'un producteur.

Vérifié : A clôture A → `COMPLETED` ; B reste `CONFIRMED` ; A ne peut pas
clôturer B (`not_owner`). Le chemin RFQ est inchangé.

## 7. Cancellation

| Cas | Comportement | Décidé par |
|---|---|---|
| Producteur annule sa commande | seule la sienne passe `CANCELLED` ; stock recrédité **uniquement** pour ses produits | code (Phase 5 + 6A) |
| Producteur annule celle d'un autre | refusé (`not_owner`) | code |
| Acheteur abandonne **avant** confirmation | tout le groupe (sinon commandes sœurs `DRAFT` orphelines) | code (Phase 6A) |
| Acheteur annule **après** confirmation | **par commande** — `cancel_pending_order` prend un `order_id` et ne consulte jamais le groupe | code |

Le dernier cas mérite d'être explicite : après confirmation, un acheteur
qui veut tout annuler doit annuler **chaque** commande. C'est la
conséquence directe et cohérente du modèle (les parts sont des
transactions indépendantes, livrées et payées séparément). **Aucune règle
d'annulation de groupe post-confirmation n'a été inventée** — si le
produit en veut une un jour, c'est une décision UX, pas un défaut.

Aucune logique d'annulation par ligne n'a été créée.

## 8. Dashboards

| Vue | État |
|---|---|
| Acheteur | chaque commande listée avec sa référence, ses articles, **son** total et **son** statut — cohérent, et les statuts divergents (A livrée / B en cours) s'affichent correctement |
| Producteur | ne voit que ses commandes ; lignes filtrées (Phase 5) **et** montant désormais exact — le P3 « total global » est **fermé pour toute nouvelle commande** |
| Origine (préorder / enchère) | conservée par commande (`order_type`, `auction_id`, `market_offer_id`) |

**P3 restant** : le tableau de bord acheteur n'indique pas explicitement
que deux commandes proviennent du même checkout. L'information est donnée
au moment qui compte (message de confirmation : « 2 commandes — #X, #Y »).
Non traité : ce serait du confort, pas une incohérence.

## 9. Notifications

Une notification **par commande**, portant le montant et la référence de
**cette** commande — jamais le total du checkout
(`test_each_producer_is_notified_once_with_his_own_amount`).
`dedupe_key` scopé `(order_id, phone)`.

## 10. RFQ / Auction compatibility

Inchangé, et re-prouvé structurellement : `select_winning_bid` ne crée
aucun `OrderItem`, référence un seul `winning_bid_id`, n'utilise pas
`checkout_group_id`. `Order.auction_id` reste `UNIQUE`.

## 11. Product decisions

| # | Décision | Statut | Fondement |
|---|---|---|---|
| 1 | **Contract** (`SALES_ACCEPT_CONTRACT`) | **OPEN** | Recherche exhaustive finale (code, tests, evals, yaml, json) : aucune entité `Contract`/`StagedTransaction`, aucune persistance, aucune notification, aucun état terminal. Seule trace : handler → domaine → `commit_staged_transaction`, outil **inexistant**. Les 6 questions (qui crée, qui accepte, qu'est-ce qui change, qu'est-ce qui est persisté, état terminal) restent sans réponse déterministe. **Aucune entité créée.** Goal déprécié depuis la Phase 4 : rien n'est exposé à l'utilisateur. |
| 2 | **Anti-abus producteur** | **CLOSED — aucune politique** | Recherche : aucun mécanisme producteur (pénalité, suspension, compteur, trust) n'existe nulle part. Le seul dispositif est **acheteur** (`MAX_CANCELLATIONS=3` + blocage de compte, `moderation.py`). Le produit **n'a pas** de politique anti-abus producteur — et aucune n'a été inventée (ni pénalité, ni déduction de confiance, ni suspension). À rouvrir seulement si un abus réel est observé. |
| 3 | **STOCK** | **CLOSED — non exposé** | `Product.quantity_for_sale` est **la seule autorité de vente** : débit au checkout, recrédit à l'annulation, contrôle « stock insuffisant », recherche produit et `is_available` s'appuient tous dessus. Le ledger `Stock` n'est lu par **aucun** chemin transactionnel. Il reste hors catalogue (Phase 4). **Aucun stock engine construit.** |
| 4 | **Bascule de rôle** | **CLOSED — dépréciée** | Modèle « même identité, rôle déduit de l'action » confirmé dans les fichiers cœur. `PROFILE_SWITCH_ROLE` écrit une ligne `agent_actions` sans consommateur et son outil n'existe pas. Aucun parcours vivant n'en dépend. **Aucune machine à états de rôle créée.** |

## 12. Remaining business gaps

### P0 — aucun

*(La faille `update_order_status` en était un potentiel ; elle est fermée — voir §13.)*

### P1 — aucun

### P2 — aucun nouveau

### P3

1. Le tableau de bord acheteur ne relie pas visuellement les commandes d'un même checkout (§8).
2. `src/agriconnect/services/database/README.md` décrit encore `finalize_multi_order` comme le chemin de checkout — documentation périmée, aggravée par le nouveau modèle. Aucun impact runtime.
3. Code mort connu et inchangé : `OrderService`, `DeliveryMixin`, `product_service.py`, `finalize_multi_order`, `update_production_visibility`.

## 13. Tests

**Nouveau fichier** : [test_order_mutations_require_ownership.py](../tests/architecture/test_order_mutations_require_ownership.py) — 11 contrats.

| Contrat | Rôle |
|---|---|
| `test_mutation_resolves_and_filters_by_actor` (×5) | chaque mutation d'état atteignable prouve, sur son code source, qu'elle résout un acteur **et** filtre la commande par cet acteur |
| `test_update_order_status_is_not_reachable_through_mcp` | **ferme la faille** trouvée en §5 du mandat |
| `test_update_order_status_still_has_no_caller` | si quelqu'un la câble, il devra d'abord lui donner un contrôle de propriété |
| `TestOneNewOrderOneProducer` (×3) | l'invariant métier, verrouillé (§3) |
| `TestAuctionOrdersRemainMonoProducer` | le chemin enchère reste hors du split |

**Réutilisés sans duplication** (§21 du mandat) : les 20 tests de la
Phase 6A couvrent déjà `one_order_one_producer` à l'exécution,
l'atomicité de la confirmation groupée, `A_cannot_complete_B`,
`A_cannot_cancel_B`, `A_completion_does_not_complete_B`, l'isolement des
notifications et des montants.

### La faille fermée pendant cette phase

`ProducerMgmtMixin.update_order_status(order_id, new_status, payment_status)` :
écrivait `Order.status` **et** `Order.payment_status` à des valeurs
arbitraires, **sans aucun contrôle de propriété** ni garde de statut, et
était **exposée comme outil MCP** avec un scope déclaré. Elle contournait
donc intégralement F1 (clôture), l'annulation producteur et l'annulation
acheteur : n'importe quelle commande pouvait être marquée
`PAID`/`COMPLETED`/`CANCELLED`.

Zéro appelant dans tout le dépôt. **Correction minimale** : retrait de son
entrée dans `TOOL_SCOPE_MAP` → le fail-closed de `runtime.py::call_tool`
la refuse désormais. La méthode n'est pas supprimée (pas de suppression de
code métier sans nécessité), elle est rendue **inatteignable**, et deux
contrats empêchent la régression.

## 14. Regression

`pytest tests/` → **4 échecs, exactement les 4 historiques** de
`test_create_auction_catalog_gate.py` (date codée en dur, antérieurs à
cette session, jamais touchés). **Aucune nouvelle défaillance.**

## 15. Production readiness

Aucune migration de données n'est requise : la base est réinitialisée.
`checkout_group_id` est créé par le DDL additif idempotent au démarrage
(`SCHEMA_COLUMN_DDL`), et toute commande créée après le reset porte
l'invariant par construction. Le code de grandfathering conservé (collecte
multi-producteurs par commande à la notification) reste inoffensif : sur
une base neuve, chaque commande n'a qu'un producteur, la boucle en trouve
exactement un.

---

# PRODUCTION MODEL

```
New checkout:
ONE ORDER PER PRODUCER = ENFORCED (verrouillé par contrat)

Grouped checkout:
SUPPORTED (checkout_group_id, confirmation unique, atomique)

Producer ownership:
ISOLATED (propriété vérifiée sur CHAQUE mutation d'état)

Delivery/payment:
PER ORDER

Cancellation:
PER ORDER (groupe uniquement avant confirmation)

Buyer visibility:
COHERENT (référence, articles, total et statut propres à chaque commande)

Producer visibility:
ISOLATED (lignes ET montant exacts)

Auction:
UNCHANGED (mono-producteur par construction)

Legacy database migration:
NOT REQUIRED — DB RESET BEFORE PROD
```

```
REMAINING PRODUCT DECISIONS

1. Contract:      OPEN    (aucune sémantique déterminable ; rien d'exposé)
2. Anti-abuse:    CLOSED  (aucune politique producteur n'existe ; aucune inventée)
3. Stock:         CLOSED  (Product.quantity_for_sale autoritatif ; ledger non exposé)
4. Role switch:   CLOSED  (rôle déduit de l'action ; bascule dépréciée)
```

```
REMAINING P0:
  aucun

REMAINING P1:
  aucun

REMAINING P2:
  aucun

REMAINING P3:
  1. Dashboard acheteur : pas de lien visuel entre commandes d'un même checkout
  2. README services/database : décrit encore finalize_multi_order comme le checkout
  3. Code mort connu : OrderService, DeliveryMixin, product_service,
     finalize_multi_order, update_production_visibility, update_order_status
```

**PRODUCTION MODEL CLOSED.**
