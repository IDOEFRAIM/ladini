-- Phase B2a — audit de qualité des données de prix (LECTURE SEULE, NON EXÉCUTÉ par l'agent).
-- Base distante gérée : à lancer par un humain, sur une réplique lecture seule de préférence, APRÈS
-- application de la migration 0012 (les colonnes de snapshot doivent exister). Ne modifie rien.

-- 1. Bids sans base de prix : ils ne seront JAMAIS réinterprétés (« base historique inconnue »).
SELECT count(*)                                              AS bids_total,
       count(*) FILTER (WHERE offered_price_basis IS NULL)   AS bids_without_basis,
       count(*) FILTER (WHERE pricing_snapshot_version IS NOT NULL) AS bids_certified
FROM marketplace.bids;

-- 2. Lignes de commande sans snapshot (sens du prix inconnu ; unité seulement via le produit COURANT).
SELECT count(*) AS order_items_total,
       count(*) FILTER (WHERE pricing_snapshot_version IS NULL) AS order_items_without_snapshot
FROM marketplace.order_items;

-- 3. Commandes d'appel d'offres : aucune ligne d'article (product_id NOT NULL, un appel d'offres n'a pas de
--    produit) et attribuées avant B2a => total = offered_price × quantité de l'enchère, base du bid INCONNUE.
SELECT count(*) AS tender_orders,
       count(*) FILTER (WHERE award_pricing_snapshot IS NULL) AS tender_orders_without_award_snapshot,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM marketplace.order_items i WHERE i.order_id = o.id)) AS tender_orders_without_items
FROM marketplace.orders o
WHERE o.auction_id IS NOT NULL;

-- 4. Produits à conditionnement (pricing_tiers avec `packaging`) sans sémantique certifiée.
SELECT count(*) AS packaged_products_without_certified_pricing
FROM marketplace.products p
WHERE p.commercial_pricing IS NULL
  AND p.pricing_tiers IS NOT NULL
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(p.pricing_tiers) t WHERE t ? 'packaging' AND t->>'packaging' IS NOT NULL);

-- 5. Paliers invalides (clé manquante ou valeur non numérique) — jamais corrigés automatiquement.
SELECT p.id, p.name, t.value AS tier
FROM marketplace.products p, jsonb_array_elements(p.pricing_tiers) t
WHERE p.pricing_tiers IS NOT NULL
  AND (NOT (t.value ? 'price' AND t.value ? 'quantity' AND t.value ? 'unit')
       OR jsonb_typeof(t.value->'price') <> 'number' OR jsonb_typeof(t.value->'quantity') <> 'number')
LIMIT 200;

-- 6. Lots « aplatis » PROBABLES (heuristique, à revoir à la main) : gros stock à prix unitaire dérisoire, typique
--    d'un prix total divisé par la quantité (ex. 5 000 000 FCFA / 200 000 kg = 25 FCFA/kg). Pas une preuve.
SELECT id, name, unit, price, quantity_for_sale, quantity_for_sale * price AS implied_total
FROM marketplace.products
WHERE commercial_pricing IS NULL AND quantity_for_sale >= 1000 AND price < 5
ORDER BY implied_total DESC
LIMIT 200;

-- 7. Offres de marché sans sémantique de prix (toutes, avant câblage B2b).
SELECT count(*) AS market_offers_total,
       count(*) FILTER (WHERE pricing_snapshot IS NULL) AS market_offers_without_snapshot
FROM marketplace.market_offers;

-- 8. (Phase B2c.5) Appels d'offres (Auction) : `max_price_per_unit` reste la SEULE colonne de prix
--    (pas de migration cette phase, voir rapport B2c.5) — cette requête ne peut donc PAS distinguer
--    un plafond PER_BASE_UNIT certifié d'un ancien plafond ambigu pré-B2c.5 : elle sert seulement à
--    dater le volume concerné par créneau, à comparer manuellement avec la date de déploiement de
--    B2c.5, jamais à "corriger" une ligne.
SELECT date_trunc('week', created_at) AS week, count(*) AS auctions_created
FROM marketplace.auctions
GROUP BY 1
ORDER BY 1 DESC
LIMIT 52;

-- 9. (Phase B2c.5) Besoins récurrents : `max_price_per_unit` n'a jamais eu de colonne de base — sûr en
--    pratique via `products.price`/`unit` (toujours la projection normalisée, voir
--    assert_legacy_projection_matches), mais AUCUNE ligne `recurring_needs` n'est "certifiée" au sens
--    snapshot. Ce compte sert juste à dimensionner l'existant avant le pilote, pas à le corriger.
SELECT count(*) AS recurring_needs_with_a_price_cap
FROM marketplace.recurring_needs
WHERE max_price_per_unit IS NOT NULL;
