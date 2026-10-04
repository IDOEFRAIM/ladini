# B17 — Intégrité inter-flux de l'inventaire par conditionnement (2026-10-04)

Baseline : `main` @ `6d935b8` (#82 mergé). Aucune migration. Aucun nouveau parseur.

## 1. Writers de `pricing_tiers` / `quantity_for_sale` (audit exhaustif)

| # | Writer | Classe AVANT | Action |
|---|---|---|---|
| 1 | `producer.create_product` (publication) | SAFE_REBUILDS_INVENTORY | invariant Σ comptes × taille == stock (B16) |
| 2 | `product.update_product_price_and_qty` (`pricing_tiers=`) | **DANGEROUS_DROPS_AVAILABLE_COUNT** | **corrigé** : fusion serveur par identité canonique, compte lu sur la ligne VERROUILLÉE |
| 3 | idem, `quantity=` / `unit=` sur produit à comptes | **DANGEROUS** (stock physique ≠ comptes) | **corrigé** : refus `package_inventory_requires_counts` |
| 4 | `package_inventory._with_count` (débit/restitution) | SAFE_PRESERVES_INVENTORY | — |
| 5 | `actions/sales.py` → `dto.pricing_tiers` → #1 ou #2 | SAFE après correction de #2 | — |
| 6 | `flow.py` `_apply_tier_correction` / `_extract_pricing_tier_update` (cache de conversation) | UNKNOWN → SAFE | recopie les dicts ; un cache périmé est neutralisé par la fusion serveur de #2 |
| 7 | `producer.py` MarketOffer (`pricing_tiers=None`) | N/A | pas de paliers |
| 8 | `marketplace.py` vente directe (produit auto, stock 0, sans paliers) | SAFE | — |
| 9 | `product.delete` (soft delete → stock 0) | LEGACY | comptes non remis à 0 ; disponibilité effective plafonnée par le physique → 0 ; documenté |
| 10 | `recurring_supply.accept_match_proposal` (débit base seul) | **DANGEREUX** pour un produit conditionné | **corrigé** : verrou + refus ; produits conditionnés exclus du matching |
| 11 | tests/fixtures | — | — |
| 12 | Frontend admin (autre dépôt) | UNKNOWN | non audité ici |

Perte de compte REPRODUITE avant correction : `update_product_price_and_qty(pricing_tiers=[… sans available_count])`
effaçait les comptes ; renvoyer le cache de conversation (compte 50) écrasait le compte réel (40) après des ventes.

## 2. Stratégie JSONB
**Fusion** (pas remplacement) par identité canonique `(type, taille en unité de base)` : `available_count` et `tier_id`
sont repris de la ligne verrouillée ; le prix et les autres champs viennent de l'entrant. Une variante en stock absente de
l'entrant ne peut pas être supprimée par une édition de prix. Primitive unique : `merge_tiers_preserving_inventory`.

## 3. Recurring — réponse binaire
**OUI, avant B17** : `_CANDIDATES_SQL` acceptait un produit à paliers dès que `commercial_pricing IS NOT NULL` (un produit
conditionné publié via #81 l'est), prenait `p.price` (prix d'un conditionnement) comme prix PAR UNITÉ, puis
`accept_match_proposal` ne débitait que le stock physique (sans verrou, `session.get`). **NON après B17** : exclusion SQL
(`packaging` non vide) + refus défensif `recurring_package_product_unsupported` + `FOR UPDATE`. Aucune autre
modification recurring.

## 4. Stock « disponible » vs « réservé »
Le stock est débité à la CRÉATION de la commande (`finalize_multi_order`, `confirm_preorder_draft`, escrow) :
`quantity_for_sale` et `available_count` sont donc déjà NETS des commandes en cours ; aucun stock « réservé » séparé.
Restitution à l'annulation (acheteur/producteur/timeout recurring). Invariant : `quantity_for_sale == Σ available_count × taille`.
Modèle mixte (conditionné + vrac) non représentable : jamais d'égalité imposée si certains conditionnements n'ont pas de compte
(refus à la publication).

## 5. `Product.price`
Corrigé : achat d'un produit vendu par conditionnement SANS variante choisie → `tier_required` (avant : un produit
certifié retombait sur `Product.price`, facturé comme un prix par litre) ; `unit_price` du contrôle de stock = `None` pour un
produit à conditionnements. Définition unique inchangée (B16) : `Product.price` = shadow legacy NOT NULL.

## 6. Limites
- Récupération conversationnelle après un REFUS de stock : la quantité résiduelle (en unité de base) peut perturber la saisie
  suivante (UX, pas d'effet sur l'inventaire) — non traité (hors périmètre).
- Édition du stock d'un produit conditionné : refusée (chantier « mise à jour sémantique » distinct).
- Soft delete : comptes non remis à zéro.
