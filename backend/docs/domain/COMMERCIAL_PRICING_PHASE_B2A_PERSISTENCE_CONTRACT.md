# Commercial Pricing — Phase B2a : contrat de persistance & instantanés de transaction immuables

Suite de `COMMERCIAL_QUANTITY_PRICING_MODEL.md` (A), `COMMERCIAL_PRICING_PHASE_B1_SALES_PUBLISH_2026-09-28.md` (B1).
B1 a rendu le sens commercial correct **dans la conversation**. B2a empêche la **base de données** de le perdre.

> Règle centrale : *la sémantique d'une transaction doit survivre à la persistance.* Un `OrderItem` historique
> ne demande jamais « quel était le prix à l'époque ? » au `Product` courant ; un `Bid` historique
> « 450000 » n'est jamais relu « 450000 / KG » parce que l'enchère était en KG.

## 0. État Git / dépendances

Phase A (#38), B1 (#39) et le correctif du test périmé (#40) sont **mergés dans `main`**. B2a part de `main`
(pas de recréation de code B1). Deux dépôts, car Drizzle est la source de vérité du schéma :

| Dépôt | Branche | Contenu |
|---|---|---|
| `ladinifront` (Drizzle, dossier `frontag/`) | `feat/commercial-pricing-b2a-schema` | schéma `marketplace.ts` + migration `0012` (générée par `drizzle-kit`) |
| `ladini` (backend) | `feat/commercial-pricing-b2a-persistence` | contrat synchronisé (`schema_contract/`), miroirs SQLAlchemy, domaine, services, tests |

Ordre de merge : **frontend/Drizzle d'abord** (le backend embarque une copie du contrat via `tests/schema/sync_contract.py`,
la CI `--check` échoue si elle diverge), puis backend. Déploiement : **migration avant code** (les modèles SQLAlchemy
mappent les nouvelles colonnes ; un backend neuf sur un schéma non migré casserait tous les SELECT de ces tables —
même piège que `migration-before-code-phantom-user`). L'inverse est sûr : l'ancien code sur le nouveau schéma
n'écrit simplement pas les colonnes (toutes NULL, CHECK satisfaits) → rollback applicatif sans danger.

## 1. Matrice d'écart du schéma (avant B2a)

Lecture du Drizzle réel (`src/db/schema/marketplace.ts`), puis des miroirs SQLAlchemy.
`✓` présent · `~` implicite/partiel · `✗` absent.

| Entité | qté inventaire | unité inv. | qté commerciale | prix | **base du prix** | unité du prix | conditionnement | prix normalisé | devise | TOTAL_LOT | version | snapshot immuable |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `products` | ✓ `quantity_for_sale` | ✓ `unit` | ✗ | ~ `price` (par unité, par convention) | ✗ | ✗ | ~ `pricing_tiers` JSONB `{quantity,unit,price,packaging}` + `packaging_type` (1 seul, déconnecté) | ~ `price` **est** la projection normalisée | ✗ | ✗ (aplati en prix/unité) | ✗ | ✗ (mutable) |
| `market_offers` | ✓ `available_quantity` | ✓ `unit` | ✗ | ~ `price_per_unit` | ✗ | ✗ | ✗ | ~ | ✗ | ✗ | ✗ | ✗ (projection mutable) |
| `auctions` | ✓ `quantity` | ✓ `unit` | ✗ | ~ `max_price_per_unit` (le nom porte « par unité ») | ~ | ~ (= `unit`) | ~ `preferred_packaging` (texte) | ✗ | ✗ | ✗ | ✗ | ✗ |
| `bids` | — | — | — | ✓ `offered_price` | **✗** | **✗** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `order_items` | ✓ `base_unit_quantity` | ✗ | ✓ `quantity` (nb de paquets si palier) | ✓ `price_at_sale` | **✗** | **✗** | ~ `tier_id` (FK **texte nue** vers un palier mutable) | ✗ | ✗ (au niveau `orders`) | ✗ | ✗ | **✗** (relit `products`) |
| `orders` | — | — | — | ✓ `total_amount` | — | — | — | — | ✓ `currency` | — | — | ✗ |
| `recurring_needs` | ✓ `quantity` | ✓ `unit` | — | ~ `max_price_per_unit` | ~ (par unité, par le nom) | ~ | ✗ | — | ✗ | ✗ | ✗ | ✗ |
| `recurring_need_occurrences` | ✓ `requested_quantity` | ✓ `unit` | — | — | — | — | — | — | — | — | — | ✓ (quantité/unité figées par occurrence) |
| `need_allocations` | ✓ `quantity` | ✓ `unit` | — | ✓ `unit_price` | ~ (par unité) | ~ (= `unit`) | ✗ | — | ✗ | ✗ | ✗ | ~ (prix convenu à l'appariement) |
| précommandes | — | — | — | — | — | — | — | — | — | — | — | pas de table propre : `orders.order_type` + `order_items` + drafts |

**Trouvaille structurante — appel d'offres.** `select_winning_bid` crée une `Order` **sans aucune ligne `order_items`** :
`order_items.product_id` est `NOT NULL` et un appel d'offres n'a pas de produit. Le total était
`bid.offered_price × auction.quantity`, soit exactement l'hypothèse « le bid est par unité de l'enchère » que la mission
interdit, et faux pour un prix total. Il n'existe donc **pas d'`OrderItem` à figer** pour le flux Bid → Order.

## 2. Décisions de conception

### 2.1 Un seul contrat : `CommercialPricingSnapshot` (`domain/commercial_pricing_snapshot.py`)
Projection persistée du **même** modèle que `CommercialOffer` (qui reste canonique) — pas de modèle par flux.
Immuable (`frozen`), versionné (`schema_version = 1`), en `Decimal`. `snapshot_from_offer(offer)` est le seul passage
offre → snapshot ; il refuse toute offre non `VALID` ou dont la base n'est pas d'une provenance exécutable.

Champs : `commercial_price_amount`, `price_basis` (`PER_BASE_UNIT|PER_PACKAGE|TOTAL_LOT`), `price_unit` (PER_BASE_UNIT),
`package_type` / `package_content_amount` / `package_content_unit` (PER_PACKAGE), quantité commerciale et d'inventaire
(montant + unité), `normalized_unit_price` + `normalized_unit` (**dérivé**), `currency`, `price_source` (audit), `schema_version`.

### 2.2 Deux formes, un seul objet (« sérialisation unique »)
| Forme | Lignes | Pourquoi |
|---|---|---|
| **colonnes typées** (`to_order_item_columns`, `to_bid_columns`) | `order_items`, `bids` — lignes **historiques** | contraignables en base (CHECK), interrogeables, pas un blob |
| **JSONB versionné** (`to_dict` / `from_dict`) | `products.commercial_pricing`, `market_offers.pricing_snapshot`, `orders.award_pricing_snapshot` | lignes **mutables** (produit, offre) ou sans table d'articles (attribution) |

`tests/unit/test_commercial_pricing_snapshot.py::TestSingleSerialization` prouve que JSON et colonnes projettent la même
sémantique. Interdit : un format JSON par flux.

### 2.3 Relationnel vs snapshot
- **`order_items`, `bids` → relationnel** : c'est l'historique économique ; il doit être contraint et lisible sans jointure.
- **`products`, `market_offers` → JSONB** : projections mutables ; le snapshot dit *ce qui a été certifié à la publication*,
  jamais ce qui a été acheté (c'est le rôle d'`order_items`). `MarketOffer` **n'est pas** un instantané : c'est une
  projection mutable du catalogue de prévente → JSONB, colonnes legacy conservées.
- **`orders.award_pricing_snapshot` → JSONB** : un appel d'offres n'a pas d'`order_items` ; l'instantané du prix attribué
  (base réelle du bid gagnant + quantité/unité de l'enchère + total) est écrit **une fois** à l'attribution.
- `auctions`, `recurring_needs`, `need_allocations` : **inchangés**. Leur prix est *par unité* par contrat de nom
  (`max_price_per_unit`, `unit_price`) et leur unité est déjà colonne. Y ajouter une base serait du bruit tant qu'aucun
  flux n'y écrit un prix par conditionnement ou total (B2b).

### 2.4 Précision monétaire
Aucun `float` ne porte un montant dans le snapshot. `float` → `Decimal` par son `repr` le plus court (0.5 → `0.5`,
jamais 0.5000000000000000277). JSON : **chaînes**. Colonnes : `numeric(14,2)` (montant commercial, exact et **autorité**),
`numeric(14,3)` (contenu), `numeric(18,4)` (prix normalisé, **dérivé**).
Politique d'arrondi : `ROUND_HALF_UP`, montants à 0,01, dérivés à 0,0001. 5 000 000 / 200 000 kg = `25.0000` ;
1 000 000 / 3 kg = `333333.3333` tandis que le total commercial reste **exactement** 1 000 000 (`total_for` d'un TOTAL_LOT
retourne le montant, jamais `normalisé × quantité`). `products.price numeric(12,2)` n'est que la projection à 2 décimales ;
tolérance de dual-write : 0,01.

### 2.5 TOTAL_LOT
Représenté explicitement (`price_basis = TOTAL_LOT`, `commercial_price_amount` = le total). Le prix unitaire n'est qu'un
dérivé. Un produit TOTAL_LOT se vend **entier** (`build_order_item_pricing_snapshot` refuse une vente partielle). Une
édition de quantité ou de prix le retire du produit (voir 2.8).

### 2.6 Devise
`XOF` (convention déjà en place : `orders.currency`, `payments.currency`). `FCFA`/`CFA` → `XOF`. Aucune refonte
multi-devise ; le champ est porté par chaque snapshot pour ne pas la bloquer.

### 2.7 Contraintes DB (nouvelles données uniquement, lignes anciennes intactes)
- `order_items_snapshot_all_null_chk` : sans version, **aucun** champ de snapshot (pas de demi-écriture) ;
- `order_items_snapshot_chk` / `bids_snapshot_chk` : avec version → montant > 0, base ∈ {3 bases}, `PER_BASE_UNIT` ⇒ unité,
  `PER_PACKAGE` ⇒ type + contenu > 0 + unité, sinon ni conditionnement ni unité de prix, dérivé > 0 ;
- `bids_price_basis_chk` (`LEGACY_UNSPECIFIED` accepté **sans** version), `products_commercial_pricing_chk`,
  `market_offers_pricing_snapshot_chk`, `orders_award_pricing_snapshot_chk` (objet JSON portant `schema_version`).

> **Piège corrigé pendant B2a.** En SQL un `CHECK` passe quand l'expression vaut `NULL`. La première version acceptait
> une ligne versionnée avec `commercial_price_amount` ou `price_basis` `NULL` (et un `package_content_amount` `NULL`).
> Chaque champ requis est désormais gardé par un `IS NOT NULL` explicite ; les tests PostgreSQL couvrent ces cas.

### 2.8 Immuabilité
Triggers `BEFORE UPDATE` (ajout manuel à la migration — `drizzle-kit` ne génère pas de triggers) :
`order_items_snapshot_immutable_trg` gèle tous les champs de snapshot une fois `pricing_snapshot_version` posée ;
`orders_award_snapshot_immutable_trg` gèle `award_pricing_snapshot`. Le passage `NULL → renseigné` (rattrapage explicite
d'une ligne ancienne) reste possible ; `quantity`, `base_unit_quantity`, `price_at_sale` ne sont pas gelés.
Côté produit : une édition ad hoc de prix / unité / paliers (ou de quantité pour un TOTAL_LOT) **retire**
`commercial_pricing` (`invalidate_commercial_pricing_on_edit`) — un snapshot qui contredit les champs legacy serait pire
qu'aucun.

## 3. Écriture (dual-write)

| Chemin | Point d'entrée | Ce qui est écrit |
|---|---|---|
| publication d'un produit | `create_product(commercial_offer=…)` → `certify_commercial_offer` | `products.commercial_pricing` + legacy (`price` normalisé, `unit`, `pricing_tiers`) |
| achat direct / précommande / allocation récurrente / vente déclarée | `order_item_snapshot_columns` / `declared_sale_snapshot_columns` → `build_order_item_pricing_snapshot` (**seul constructeur**) | colonnes `order_items.*` |
| bid | `place_bid(price_basis=…)` → `bid_snapshot_columns` | colonnes `bids.*` |
| modification du montant d'un bid | `reprice_bid_columns` | même base, snapshot recalculé (bid ancien : montant seul, base inconnue) |
| attribution | `award_total_and_snapshot` | `orders.award_pricing_snapshot`, total selon la **base du bid** |

**Assertion de dual-write** (`assert_legacy_projection_matches`, `assert_order_item_legacy_matches`) : les champs legacy
doivent être la projection du snapshot ; sinon `BusinessRuleException` **avant** le commit.
Exemples : `price=500` posé pour « 500 / sachet de 0,5 L » (devrait être 1000 / L) → rejeté ; `price_at_sale=1000` sur un
palier à 500 → rejeté ; `quantity_for_sale` ≠ inventaire du snapshot → rejeté.

L'offre voyage `SalesPublishDraft.execution_payload()` → DTO → commande → outil `create_product` **sous forme d'offre**,
jamais de snapshot : l'appelant ne peut pas fournir un JSON de snapshot arbitraire (test dédié), le service reconstruit et
re-valide l'offre.

## 4. Lecture rétro-compatible

`bid_pricing_view` / `order_item_pricing_view` / `product_pricing_view` :
ligne avec snapshot → `CERTIFIED` ; ligne ancienne → base `None` (`UNKNOWN`), `UNKNOWN_BASIS` (bid) ou `LEGACY_PARTIAL`
(commande, produit). **Jamais** « PER_KG parce que l'enchère était en KG ». La migration ne rétro-remplit rien (aucun
`UPDATE`, aucun `DEFAULT`).

## 5. Achat d'un produit conditionné (Étape 19)

Contrat métier actuel, audité dans `pricing_tiers.classify_pack_count_unit` : *« pas de tetris de conditionnements »* —
l'acheteur donne un **nombre de paquets** ; « 30 L → 3 bidons » est refusé, jamais converti. B2a le rend explicite :
`PackageSaleMode.PACKAGE_ONLY` (**défaut**) ou `BASE_UNIT_ALLOWED` (division exacte requise). `resolve_package_purchase` :
4 sachets → OK ; 2 L en `PACKAGE_ONLY` → `NEEDS_PACKAGE_COUNT` ; 2 L en `BASE_UNIT_ALLOWED` → 4 sachets ; 1,2 L → `NOT_DIVISIBLE` ;
contenu de 1 unité → « 3 L » = 3 paquets. Le mode par produit (`TIER_DEPENDENT`) est une décision produit non prise : le défaut
suit le comportement existant.
Un produit à prix par conditionnement acheté **sans palier** (en unité de base) est enregistré tel qu'il a été facturé :
`PER_BASE_UNIT` au prix de vente, `price_source = NORMALIZED_FROM_CERTIFIED_PACKAGE` — on n'écrit pas « 500 / sachet » pour
une ligne facturée au litre.

## 6. Exemples

```json
// products.commercial_pricing — 50 L de lait, 500 FCFA / sachet de 0,5 L
{"schema_version":1,"currency":"XOF","commercial_price_amount":"500","price_basis":"PER_PACKAGE","price_unit":null,
 "package_type":"SACHET","package_content_amount":"0.5","package_content_unit":"LITRE",
 "commercial_quantity_amount":"50","commercial_quantity_unit":"LITRE","inventory_quantity_amount":"50","inventory_quantity_unit":"LITRE",
 "normalized_unit_price":"1000","normalized_unit":"LITRE","price_source":"USER_EXPLICIT"}
```
`order_items` (4 sachets) : `quantity=4, quantity_unit=SACHET, commercial_price_amount=500.00, price_basis=PER_PACKAGE,
package_type=SACHET, package_content_amount=0.500, package_content_unit=LITRE, normalized_unit_price=1000.0000,
normalized_unit=LITRE, currency=XOF, pricing_snapshot_version=1` (+ `base_unit_quantity=2`, `price_at_sale=500`).
`bids` (10 t) : `offered_price=450000.00, offered_price_basis=PER_BASE_UNIT, offered_price_unit=TONNE, offered_price_currency=XOF`.
`orders.award_pricing_snapshot` : le snapshot du bid + `"award":{"auction_quantity":"10","auction_unit":"TONNE","total_amount":"4500000.00"}`.

## 7. Tests

| Fichier | Rôle |
|---|---|
| `tests/unit/test_commercial_pricing_snapshot.py` | domaine : forme, précision, sérialisation unique, combinaisons impossibles, OrderItem/Bid, dual-write, lectures legacy, achat conditionné |
| `tests/unit/test_pricing_persistence_services.py` | pont ORM : chemin réel draft→DTO→commande→outil, rejets avant écriture, bids, totaux d'attribution, invalidation produit |
| `tests/architecture/test_commercial_pricing_persistence_contract.py` | verrous A–G (snapshot obligatoire, un seul constructeur, un seul point d'écriture de bid, TOTAL_LOT, pas de base inventée, migration additive) |
| `tests/schema/test_commercial_pricing_persistence_pg.py` | **PostgreSQL réel** : migration, lignes anciennes lisibles, immuabilité, CHECK, TOTAL_LOT, bid, attribution — *ignoré hors CI sans `SCHEMA_TEST_DSN`* |
| `tests/schema/test_schema_contract_static.py` (existant) | SQLAlchemy ≡ Drizzle (colonnes, CHECK, index) |

## 8. Audit de qualité des données

`docs/domain/sql/pricing_persistence_data_quality_READONLY.sql` (lecture seule). **Non exécuté** : base distante gérée, pas
d'approbation. Le taxonomy seed de B1 reste séparé et non appliqué.

## 9. Risques résiduels

1. Les tests PostgreSQL n'ont pas pu tourner en local (pas de PostgreSQL dans l'environnement) : les triggers, les CHECK et
   la migration ne sont vérifiés en base réelle que par la CI.
2. `bids` reste modifiable (négociation) : le snapshot est recalculé, il n'est pas gelé — seule la commande gagnante l'est.
3. Les 4 sites `OrderItem` sont câblés ; un futur 5ᵉ site est arrêté par le test d'architecture A.
4. `TIER_DEPENDENT` (mode d'achat par produit) non tranché.

## 10. Phase B2b — travail restant, exact

1. **Conversation Bid → CommercialPricing** : le producteur dit « 450000 la tonne » / « 4,5 millions pour tout » ; `auctions.py::_first_number`
   jette encore l'unité. Réutiliser `commercial_offer_flow` (question de base, provenance) pour produire `price_basis`/`price_unit`
   et appeler `place_bid(price_basis=…)`. Tant que ce n'est pas fait, les bids conversationnels sont écrits **sans base** (journal
   `BID_PRICE_BASIS_UNSPECIFIED`).
2. **Affichage** : `recap_bid`, listes de bids, messages de gain affichent encore `offered_price` avec l'unité de l'enchère ; passer par
   `bid_pricing_view`.
3. **Sélection du gagnant** : confirmer côté acheteur la base + le total dérivé (`award_total`) avant `select_winning_bid`.
4. **`market_offers`** : aucun écrivain ne renseigne `pricing_snapshot` (`declare_future_production` n'a pas de tarification par palier).
5. **Recherche acheteur** : afficher « 500 FCFA / sachet de 0,5 L » depuis `product_pricing_view` plutôt que `price`/`unit`.
6. **Rattrapage des anciennes lignes** (optionnel, jamais automatique) : audit SQL puis décision humaine par lot.
7. **Contrat LLM** : champs typés `package_size` / `price_basis` dans `new_task_contract.py`.
8. **Goals PROCUREMENT / PRODUCTION / PREORDER / RECURRING** encore sur `reconcile_price_basis`.
