# B16 — Inventaire par conditionnement & source de vérité du prix (2026-10-03)

## 1. Défaut reproduit AVANT correction
Produit « gapal » publié avec 50 bidons de 500 ml + 100 bidons de 330 ml (stock physique 58 L) :
- les comptes (50, 100) étaient PERDUS à la publication (seul `quantity_for_sale = 58` persistait) ;
- une commande de 60 bidons de 500 ml (30 L ≤ 58 L) était ACCEPTÉE → survente de 10 bidons.

## 2. Définitions (une seule)
| Notion | Où | Sens |
|---|---|---|
| Stock physique | `Product.quantity_for_sale` (Numeric 14,3) | total en unité de base (L, kg), agrégat |
| Compte de variante | `pricing_tiers[i].available_count` | nombre de conditionnements disponibles |
| Taille de conditionnement | `pricing_tiers[i].base_unit_quantity` | contenu CANONIQUE (0,5 L == 500 ml == 50 cl) |
| Prix de conditionnement | `pricing_tiers[i].price` | FCFA par UN conditionnement |
| `Product.price` | colonne NOT NULL | **shadow legacy** : prix brut du 1er palier / prix par unité de base d'un produit SANS palier. Jamais la vérité commerciale d'une offre par conditionnement. Non nullable, non supprimée. |

Source de vérité du stock d'un produit conditionné : le COMPTE par variante ; `quantity_for_sale` en est l'agrégat physique
(invariant à la publication Σ compte × taille == stock, Decimal, tolérance 0,0005). Aucune migration (JSONB existant).
Identité d'une variante = (type de conditionnement, taille canonique) : sachet 500 ml ≠ bidon 500 ml.

## 3. Primitive unique (`domain/package_inventory.py`)
`debit_stock_for_item` / `restore_stock_for_item` / `check_variant_availability`, appelées sous `FOR UPDATE` du produit :
disponibilité effective = `min(compte, ⌊stock physique / taille⌋)` (les comptes ne permettent jamais de vendre plus que
le physique). Câblées dans : `buyer` (cart→commande, `confirm_preorder_draft`, `cancel_pending_order`,
`validate_stock_availability_atomic`), `escrow`, `producer.cancel_confirmed_order`, `recurring_supply` (timeout).

## 4. Lecteurs de `Product.price`
| Lecteur | Classe | Note |
|---|---|---|
| `buyer.py` finalize (~702) | SAFE | précédé du refus `tier_required` si paliers non certifiés |
| `buyer.py` create_preorder_draft (~1932) | SAFE | `_has_uncertified_tiers` → article ignoré |
| `buyer.py` seller_minimum (~2139) | SAFE | 0 si palier |
| `buyer.py` validate_stock_availability_atomic `unit_price` | **DANGEREUX → CORRIGÉ** | renvoie désormais `None` pour un produit à paliers |
| `buyer.py` recherche/tri (293, 345, 1489-1499) | PER_BASE_UNIT ONLY / SAFE | tri `_is_tiered_sql` après les prix certifiés |
| `cart_service.py` branche sans palier (~953) | PER_BASE_UNIT ONLY | atteinte seulement sans palier choisi ; `tier_required` bloque en aval la commande d'un produit à paliers |
| `category.py` (avg/min, listes) | LEGACY DISPLAY ONLY | commentaire « shadow legacy » ; agrégats indicatifs |
| `producer.py` (804, 971-989) | LEGACY DISPLAY ONLY | listes producteur |
| `product.py:109` update prix | **LIMITE** | modifie le shadow seul ; la mise à jour sémantique des produits conditionnés est HORS PÉRIMÈTRE (chantier futur « Product Update Semantic Engine ») |
| `need_matching_service._CANDIDATES_SQL` | SAFE | exclut les produits à paliers sans prix certifié |
| `order_tracking`/`award_decision` `lookup.price` | SAFE | prix d'enchère/offre, pas `Product.price` |
| Analytics / Market Sense / notifications / dashboards | NON AUDITÉ en profondeur | revenu dérivé des `OrderItem` (prix figé à la vente), pas de lecture de `Product.price` repérée par grep |

## 5. Limites assumées
- Pas de stock « libre » pour un produit conditionné (ex. « 20 sachets + 40 L en vrac » non représentable).
- Les comptes sont perdus si les paliers sont republiés sans `available_count` ; la mise à jour de stock conditionné doit être
  traitée par le chantier de mise à jour de produit (hors périmètre).
- `recurring_supply.accept_match_proposal` débite la quantité de base sans passer par la variante : dérive tolérée (la
  disponibilité effective plafonne par le physique) — non modifié (chantier recurring exclu).
- Tests PostgreSQL (`tests/schema/test_package_inventory_pg.py`) écrits pour la CI, NON exécutés en local (pas de Postgres).
- Parseur « fail-closed » : le périmètre de #81 est conservé ; pas d'extension AMBIGUOUS → clarification dans ce chantier.
