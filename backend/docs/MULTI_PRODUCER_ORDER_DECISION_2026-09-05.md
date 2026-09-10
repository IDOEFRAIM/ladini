# MULTI-PRODUCER ORDER MODEL — DÉCISION (2026-09-05, Phases 6 & 6A)

> **STATUT FINAL : DÉCIDÉ ET IMPLÉMENTÉ (Phase 6A).**
>
> ```
> Modèle          : A — une commande par producteur
> UX              : A3 — confirmation groupée (checkout_group_id)
> Données         : anciennes commandes multi-producteurs GRANDFATHERED
> Nouvel invariant: une NOUVELLE commande de checkout = un producteur
> Checkout        : un groupe → N commandes
> Fulfillment     : par commande
> Annulation      : par commande
> Paiement        : par commande
> Notifications   : par commande
> Enchères        : inchangées (mono-producteur par construction)
> ```
>
> L'analyse ci-dessous (Phase 6) reste le dossier de preuve ayant conduit à
> cette décision ; l'implémentation est décrite en §16 et §17.

**Phase 6 (analyse) — résultat initial : `PRODUCT DECISION REQUIRED`** — le
modèle métier était *démontré* (Modèle A), mais deux points que le dépôt ne
tranchait pas bloquaient l'implémentation. Ces deux points ont été tranchés
en Phase 6A (forme A3, grandfathering).

---

## 1. Current model

| Fait | Preuve |
|---|---|
| `Order` n'a **aucun** `producer_id` | `domain/orders/models.py:98-155` — la colonne n'existe pas |
| `Order` n'a **aucun** lien de groupe (`parent_order_id`, `group_id`) | idem |
| `OrderItem` n'a **aucune** colonne de statut | `models.py:169-197` : `id`, `order_id`, `product_id`, `quantity`, `price_at_sale`, `tier_id`, `base_unit_quantity` — rien d'autre |
| `payment_status` / `delivery_status` sont **au niveau commande** | `models.py` |
| `Delivery` est **1-1** avec `Order` (`uselist=False`) et n'est jamais instanciée | `models.py` + audit Phase 3 (`DeliveryMixin` = code mort) |
| La propriété d'une ligne se déduit uniquement par `OrderItem → Product.producer_id` | `confirm_delivery_and_payment`, `get_producer_orders` |
| Une commande RFQ n'a **jamais** d'`OrderItem` | `auction.py` : `grep -c "OrderItem(" = 0` |

**Aucune structure existante** (sous-commande, expédition, enregistrement
de fulfillment, `Delivery`) ne peut porter une responsabilité par
producteur. Vérifié avant toute idée de nouvelle table, comme demandé.

## 2. Demonstrated P1

`create_preorder_draft` crée **une seule** `Order` et y attache tous les
items du panier, sans regroupement par producteur. Le panier autorise
explicitement des producteurs différents : `cart_service.resolve_product_vendors`
indexe les vendeurs par `product_id:producer_id` et l'acheteur choisit un
vendeur **par produit**.

`confirm_delivery_and_payment` (F1) et `cancel_confirmed_order` (Phase 5)
vérifient la propriété par « possède **au moins un** article », puis
écrivent le statut de la commande **entière**.

→ Sur une commande A+B, A peut marquer `PAID`/`DELIVERED`/`COMPLETED` la
part de B, ou l'annuler. **Confirmé, reproductible.**

## 3. Model A analysis — une commande par producteur

Représentable **sans aucun changement de schéma** pour le modèle lui-même.
Chaque `Order` devient mono-producteur ⇒ les contrôles de propriété
existants deviennent **corrects par construction**, sans le moindre `if`
correctif.

**Cohérence avec le reste du produit — argument décisif** : tous les
autres chemins de création de commande produisent **déjà** des commandes
mono-producteur.

| Chemin | Forme |
|---|---|
| `select_winning_bid` (RFQ) | 1 `Order`, `auction_id` **UNIQUE**, un seul `winning_bid_id`, **zéro** `OrderItem` → mono-producteur **structurellement** |
| `record_sale` (vente directe cash) | 1 `Order` par vente, mono-producteur |
| `create_preorder_draft` (panier) | **seul** chemin multi-producteurs |

Le modèle mono-producteur est donc déjà la norme du produit ; le panier en
est l'exception, non un design assumé.

**Cohérence avec la réalité physique** : en paiement à la livraison, il
n'existe aucune logistique de plateforme (`DeliveryMixin` mort, pas de
réseau de coursiers). A livre sa marchandise et encaisse son argent ; B
fait de même, séparément. **Deux transactions métier distinctes** que le
code agrège artificiellement.

## 4. Model B analysis — commande globale, terminalisation par ligne

**Non représentable aujourd'hui.** Exige :
- une colonne de statut sur `OrderItem` (migration) ;
- un statut de **paiement** et de **livraison** par ligne (les deux sont
  aujourd'hui au niveau commande) — donc trois axes d'état par ligne ;
- une règle d'agrégation ligne → commande (quand la commande est-elle
  `COMPLETED` ?) ;
- la ré-écriture de `confirm_delivery_and_payment`, `cancel_confirmed_order`,
  `get_producer_orders`, `get_buyer_orders_dashboard` et de l'historique.

C'est une **nouvelle machine à états** que le dépôt ne définit nulle part.

## 5. Model C analysis — commande globale à cycle de vie coordonné

Pour savoir que « toutes les parties sont terminées », il faut connaître
l'état de **chaque partie** — donc exactement la représentation du
Modèle B, plus une règle de coordination. **C ⊃ B** : strictement plus
coûteux, et tout aussi indéfini dans le dépôt.

## 6. Checkout impact

Chaîne actuelle reconstituée :

```
active_cart (vendeur choisi PAR PRODUIT)
  → bootstrap_preorder_draft
  → create_preorder_draft      ← UNE Order, tous les items, aucun regroupement
  → PreorderDraft (CAS Postgres, order_id UNIQUE)
  → confirm_preorder_draft(preorder_id)   ← débit stock + CONFIRMED, UNE Order
  → notifications F2 (une par producteur distinct de CETTE Order)
```

**Pourquoi une seule Order ?** Décision **explicite mais orientée
produits, pas producteurs** : la fonction historique s'appelle
`finalize_multi_order` et sa docstring dit « commande ferme
**multi-produits** ». Le multi-**producteurs** n'a jamais été considéré au
moment de ce choix — la propriété n'entrait pas dans le raisonnement.

**Ce qui suppose une seule Order** :

| Composant | Suppose une seule Order ? |
|---|---|
| `PreorderDraft` | **oui** — `order_id: Optional[str]` unique, + colonne `order_id` dans `marketplace.preorder_drafts` |
| `confirm_preorder_draft` | **oui** — signature `preorder_id`, un seul verrou, un seul total |
| Flux de confirmation conversationnel | **oui** — reprise `EXECUTING`, clé d'idempotence, logique de `SUPERSEDED`, rendu du récap, tous dérivés de `draft.order_id` |
| `initiate_escrow_payment` | **oui** (chemin désactivé mais présent) |
| Dashboards acheteur / producteur | **non** — listent N commandes sans difficulté |
| Notifications | **non** — une notification par producteur ; avec le Modèle A, une par commande, plus simple |
| Annulation acheteur / producteur | **non** — opèrent par commande |

## 7. Fulfillment impact

Modèle A : `confirm_delivery_and_payment` **inchangé**, et devient correct
— son contrôle « possède au moins un article » est exact dès lors qu'une
commande n'a qu'un producteur. Aucun patch sur F1, conformément au §22.

Modèles B/C : réécriture complète de la clôture (par ligne, avec
agrégation).

## 8. Cancellation impact

| Modèle | A annule sa part |
|---|---|
| **A** | `Order A` → `CANCELLED` ; `Order B` intacte, indépendante. Code Phase 5 **inchangé** |
| **B** | lignes de A → `CANCELLED` ; exige un statut de ligne + une règle pour l'état de la commande |
| **C** | état partiel non représentable sans la sémantique du B |

## 9. Payment impact

Paiement à la livraison : aucun remboursement, aucun escrow, aucune
nouvelle machine à états de paiement, quel que soit le modèle. En
Modèle A, `payment_status` reste au niveau commande **et devient exact** :
chaque producteur encaisse sa propre commande.

## 10. Stock impact

Aucune nouvelle comptabilité. `Product.quantity_for_sale` reste la seule
autorité (confirmé Phase 5). En Modèle A, l'annulation de la commande de A
recrédite **exactement** les produits de A via `resolve_stock_debit` — la
mécanique existante, appliquée à un périmètre désormais correct. C'est
d'ailleurs une correction latente : aujourd'hui, A annulant la commande
globale recrédite **aussi** les produits de B.

## 11. RFQ / Auction impact

**Aucun.** Les commandes d'enchère sont mono-producteur *structurellement* :
`Order.auction_id` est **UNIQUE**, `winning_bid_id` désigne **une** offre,
et `select_winning_bid` ne crée **aucun** `OrderItem`. Le P1 est donc
strictement circonscrit au checkout panier/préorder direct. Propriété
documentée ici pour ne pas être re-vérifiée à chaque chantier.

## 12. Notification impact

Séquence pour un panier A+B, Modèle A :

```
checkout      → Order A (DRAFT), Order B (DRAFT)
confirmation  → Order A CONFIRMED → PREORDER_CONFIRMED_PRODUCER → A
                Order B CONFIRMED → PREORDER_CONFIRMED_PRODUCER → B
A livre+encaisse → Order A COMPLETED → ORDER_COMPLETED_AT_DELIVERY_BUYER (part A)
B toujours en cours → aucune notification trompeuse
B livre+encaisse → Order B COMPLETED → ORDER_COMPLETED_AT_DELIVERY_BUYER (part B)
```

Chaque notification porte alors un montant et un périmètre **exacts**.
Aujourd'hui, la notification de clôture annonce à l'acheteur le total de
la commande **entière** dès que le premier producteur confirme — message
factuellement faux.

Aucune duplication artificielle : une notification par commande réelle.

## 13. Dashboard impact

| Vue | Aujourd'hui | Modèle A |
|---|---|---|
| Acheteur | 1 commande, total global, statut global (faux dès qu'un seul producteur agit) | N commandes, chacune avec son producteur, son montant et son statut réels |
| Producteur A | voit la commande ; **lignes correctement filtrées** (vérifié Phase 5) mais `total_amount` global, incluant la part de B | voit sa commande, son montant exact — le P3 « montant global » **disparaît** |
| Historique | une entrée agrégée | une entrée par transaction réelle |

Le Modèle A ferme donc aussi le P3 de la Phase 5, sans travail
supplémentaire.

## 14. Recommended model

```
RECOMMENDED MODEL: A — une commande par producteur

Reason:
  • Seul modèle représentable avec le schéma actuel (B exige un statut de
    ligne inexistant sur 3 axes ; C ⊃ B).
  • Déjà la norme du produit : RFQ (auction_id UNIQUE + winning_bid_id,
    zéro OrderItem) et vente directe créent des commandes mono-producteur.
    Le panier est la seule exception.
  • Correspond à la réalité physique du paiement à la livraison : chaque
    producteur livre et encaisse séparément, sans logistique de plateforme.
  • Rend F1 et l'annulation Phase 5 corrects PAR CONSTRUCTION — aucun
    patch de symptôme, ce que le §22 exige.
  • Ferme au passage le P3 « montant global » et le message de clôture
    factuellement faux envoyé à l'acheteur.

Why current model is insufficient:
  La responsabilité producteur n'est représentée NULLE PART. Elle n'est
  déductible que par jointure OrderItem→Product, alors que toutes les
  transitions d'état sont écrites au niveau commande. L'écart entre
  granularité de la responsabilité (ligne) et granularité de l'état
  (commande) EST le bug.

Schema impact:
  Aucun pour le modèle. Une colonne additive `checkout_group_id` sur
  `Order` est nécessaire pour conserver UNE confirmation acheteur (voir
  §15) — via SCHEMA_COLUMN_DDL, la convention additive du dépôt.

Conversation impact:
  Le récapitulatif de checkout doit annoncer N commandes issues d'un même
  panier, et la confirmation doit rester UNIQUE (option A3 ci-dessous).

Dashboard impact:
  Aucun code à changer : les deux dashboards listent déjà N commandes.

Notification impact:
  Aucun code à changer : une notification par commande, périmètre exact.

Migration impact:
  Les commandes multi-producteurs DÉJÀ en base ne peuvent pas être
  éclatées rétroactivement (items, historique, Outbox, états déjà
  terminaux). Voir §18.
```

## 15. Product decision

Le modèle est démontré. **Deux points restent indécidables depuis le
dépôt** — ce sont eux, et eux seuls, qui bloquent l'implémentation.

### Décision 1 — forme de l'implémentation

| Option | Confirmation acheteur | Impact sur le CAS/draft durci |
|---|---|---|
| **A1** — 1 draft → N commandes | unique | **fort** : reprise `EXECUTING`, clé d'idempotence, logique `SUPERSEDED` et rendu deviennent multi-cibles |
| **A2** — N drafts (1 par producteur) | **N confirmations** | **nul** : chaque draft reste mono-commande |
| **A3** — 1 draft + `checkout_group_id` sur `Order` (recommandée) | unique | **faible** : le draft garde un `order_id` primaire ; seule `confirm_preorder_draft` traite le groupe, en verrouillant les commandes dans un ordre déterministe (discipline anti-deadlock **déjà** présente dans ce fichier) |

**Question produit** : accepte-t-on qu'un panier mixte produise plusieurs
numéros de commande visibles par l'acheteur, avec **une seule**
confirmation (A3, recommandée), ou préfère-t-on une confirmation par
producteur (A2) ?

### Décision 2 — traitement de l'existant

Les commandes multi-producteurs déjà en base : les laisser sous
l'ancienne sémantique (« grandfathering », le P1 reste latent pour
elles), ou tenter un backfill par éclatement (risqué : historique,
Outbox déjà émis, commandes déjà `COMPLETED`) ?

**Aucune de ces deux questions n'a de réponse dans le code.** Les trancher
seul reviendrait à décider une politique produit et une politique de
données — ce que ce mandat interdit (§13).

## 16. Implementation — RÉALISÉE (Phase 6A)

Décisions reçues : **forme A3** (confirmation groupée) et **grandfathering**
de l'existant. Livré :

| Fichier | Changement |
|---|---|
| [common.py](../src/ladini/services/database/common.py) | `SCHEMA_COLUMN_DDL` : `checkout_group_id UUID` + index partiel — additif et idempotent, convention du dépôt (pas d'Alembic) |
| [models.py](../src/ladini/domain/orders/models.py) | `Order.checkout_group_id`, nullable, **sans état** |
| [buyer.py](../src/ladini/services/database/buyer.py) `create_preorder_draft` | regroupement par `product.producer_id` ; une `Order(DRAFT)` par producteur, créée **tardivement** (à la première ligne retenue) pour ne jamais produire de commande vide ; total **par commande** ; réponse enrichie (`checkout_group_id`, `order_ids`, `orders`) |
| `confirm_preorder_draft` | confirmation **groupée** : commandes sœurs chargées `FOR UPDATE` dans un ordre déterministe, tri anti-deadlock étendu à l'**union** des articles du groupe, total et notification **par commande** |
| `cancel_preorder_draft` | abandon du brouillon = abandon de **tout** le groupe (sinon commandes sœurs `DRAFT` orphelines) |
| [preorder_draft.py](../src/ladini/graphs/agents/market_coach/domain/preorder_draft.py) `render_summary` | prévient l'acheteur **avant** confirmation qu'un panier mixte fera N commandes (projection pure) |

**Non modifiés, volontairement** : `confirm_delivery_and_payment` (F1) et
`cancel_confirmed_order` (Phase 5). Leur contrôle de propriété
« possède au moins un article » **devient exact** dès lors qu'une commande
n'a qu'un producteur — le défaut de modélisation est corrigé à sa source,
jamais masqué par un garde (§22 du mandat de Phase 6).

**Machinerie CAS intacte** : `PreorderDraft.order_id` continue de désigner
une commande unique (la **primaire**, première créée). Reprise `EXECUTING`,
clé d'idempotence, logique `SUPERSEDED` et rendu conversationnel
fonctionnent sans modification ; le groupe voyage à côté
(`checkout_group_id`), jamais à leur place.

### Plan d'origine (Phase 6), conservé pour référence

1. `SCHEMA_COLUMN_DDL` : ajout additif idempotent de `checkout_group_id` (UUID, nullable) sur `marketplace.orders` + index.
2. `Order` : déclaration de la colonne.
3. `create_preorder_draft` : regrouper les items résolus par `product.producer_id` ; créer une `Order(DRAFT)` par groupe, toutes partageant le même `checkout_group_id` ; `PreorderDraft.order_id` = commande primaire (compat colonne + machinerie CAS inchangée) ; récap listant les N commandes.
4. `confirm_preorder_draft` : charger le groupe (`checkout_group_id`), verrouiller les commandes **et** les produits dans un ordre déterministe (motif anti-deadlock existant), débiter, confirmer chaque commande, notifier chaque producteur, renvoyer un résultat consolidé. Une commande dont le stock manque échoue **seule** — sémantique correcte : ce sont des transactions indépendantes.
5. `cancel_preorder_draft` : même traitement de groupe.
6. **Aucune modification** de `confirm_delivery_and_payment` (F1) ni de `cancel_confirmed_order` (Phase 5) : ils deviennent corrects par construction.

## 17. Tests — ÉCRITS (Phase 6A)

**20 tests**, tous verts.

[test_multi_producer_checkout.py](../tests/unit/test_multi_producer_checkout.py) (11) :

| Test | Vérifie |
|---|---|
| `test_two_producers_produce_two_orders_sharing_one_group` | N commandes, un seul `checkout_group_id` |
| `test_no_order_ever_contains_another_producers_line` | répartition stricte des lignes |
| `test_totals_are_per_order_never_the_checkout_total` | **ferme aussi le P3** ; échoue avec l'ancien modèle |
| `test_single_producer_checkout_is_unchanged` | non-régression du cas majoritaire |
| `test_a_producer_whose_items_are_all_rejected_gets_no_empty_order` | aucune commande vide |
| **`test_producer_a_cannot_terminalize_producer_b_order`** | **régression F1 explicite** — `not_owner` ; c'est le test qui matérialisait le P1 |
| `test_producer_a_cannot_cancel_producer_b_order` | idem côté annulation |
| `test_completing_one_order_never_touches_the_sibling` | pas de terminalisation prématurée : B reste `CONFIRMED` |
| `test_cancelling_one_order_never_touches_the_sibling` | + stock : seul le produit de A est recrédité |
| `test_select_winning_bid_never_creates_order_items` / `..._no_checkout_group` | enchères mono-producteur, hors du split |

[test_multi_producer_group_confirmation.py](../tests/unit/test_multi_producer_group_confirmation.py) (9) :

| Test | Vérifie |
|---|---|
| `test_one_confirmation_confirms_every_order_of_the_group` | UX A3 : une seule confirmation |
| `test_each_order_keeps_its_own_total` | totaux par commande + total checkout |
| `test_stock_is_debited_once_per_product` | débit exact, une fois |
| `test_each_producer_is_notified_once_with_his_own_amount` | **jamais le total du checkout** dans la notification d'un producteur |
| `test_dedupe_keys_are_per_order_and_phone` | idempotence Outbox |
| `test_confirming_twice_never_re_debits_nor_re_notifies` | rejeu sûr |
| `test_insufficient_stock_on_one_order_aborts_the_whole_group` | **atomicité** : rien de confirmé à moitié |
| `test_a_lone_order_without_group_behaves_exactly_as_before` | non-régression |
| `test_an_old_order_without_group_still_confirms_and_notifies_both` | **grandfathering** : une commande héritée multi-producteurs continue de fonctionner |

## 18. Migration

- **Aucune migration destructive.** `checkout_group_id` est additif et nullable ; les commandes existantes le laissent à `NULL` = « groupe d'une seule commande », comportement identique à aujourd'hui.
- **Commandes multi-producteurs existantes** : non éclatables rétroactivement (leurs `OrderItem`, `OrderStatusHistory` et messages Outbox sont déjà émis ; certaines sont déjà `COMPLETED`). → objet de la **Décision 2**.
- **Références conservées** : `winning_bid_id`, `auction_id`, historiques et Outbox ne sont pas touchés (les commandes RFQ sont déjà mono-producteur).

## 19. Remaining risks

| Risque | Portée |
|---|---|
| Le P1 reste ouvert tant que la décision n'est pas prise | limité : exige un panier mixte **et** qu'un producteur agisse sur la part d'un autre |
| Aucun garde-fou provisoire n'a été posé | délibéré : le §22 interdit de corriger le symptôme ; un `if` masquerait le défaut de modélisation |
| Message de clôture inexact pour l'acheteur (montant global) | existant, disparaît avec le Modèle A |
| Commandes multi-producteurs historiques | resteront sous l'ancienne sémantique sauf décision contraire |

---

# RÉSULTAT — PHASE 6A : MULTI-PRODUCER CLOSED

```
Modèle                : A — une commande par producteur
UX                    : A3 — confirmation groupée (checkout_group_id)
Nouvel invariant      : une NOUVELLE commande de checkout = un producteur
Commandes historiques : grandfathered (checkout_group_id IS NULL)
Périmètre             : checkout panier/préorder UNIQUEMENT
                        (RFQ/enchères mono-producteur par construction)

Tests ajoutés         : 20 (11 + 9), tous verts
F1 / annulation Ph.5  : NON modifiés — corrects par construction
Machinerie CAS        : intacte (draft toujours mono-`order_id`)
Migration             : additive, aucune réécriture rétroactive
```

## Critère de sortie — vérifié

| Propriété exigée | Preuve |
|---|---|
| A n'agit que sur sa responsabilité | `test_producer_a_cannot_terminalize_producer_b_order`, `..._cannot_cancel_...` |
| B idem | même garde, symétrique (`not_owner`) |
| L'acheteur voit un état global cohérent | `checkout_total` + `orders[]` ; récapitulatif prévenant avant confirmation |
| Pas de clôture prématurée | `test_completing_one_order_never_touches_the_sibling` |
| Une annulation n'affecte pas l'autre part | `test_cancelling_one_order_never_touches_the_sibling` (statut **et** stock) |
| Notifications = responsabilité réelle | `test_each_producer_is_notified_once_with_his_own_amount` |
| Historique cohérent | un `OrderStatusHistory` par commande, jamais fusionné |
| Aucun dashboard n'expose les données d'un autre producteur | lignes déjà filtrées (Phase 5) **+** montant désormais exact (P3 fermé) |

## Sujets explicitement NON traités ici

`ROLE SWITCH`, `STOCK LEDGER`, `ABUSE POLICY` — hors périmètre de ce P1
(§23), inchangés.
