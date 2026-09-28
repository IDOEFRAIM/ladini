-- ═══════════════════════════════════════════════════════════════════════════
-- AUDIT QUALITÉ DES PRODUITS — STRICTEMENT LECTURE SEULE
-- (incident buyer direct-purchase 2026-09-28 : offre `boeufs / 461000 / 461000 / UNITE`)
--
-- Usage :
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f backend/scripts/sql/audit_product_data_quality.sql
--
-- Garanties :
--   * `BEGIN READ ONLY` : toute écriture est REFUSÉE par PostgreSQL, quoi que contienne ce fichier.
--   * Aucune suppression, aucun UPDATE, aucun DDL. Le ROLLBACK final ferme la transaction.
--   * Ne renvoie ni numéro de téléphone ni donnée personnelle : identifiants techniques et
--     libellés produit uniquement.
--
-- Ce script PRODUIT UN CONSTAT. Il ne nettoie rien. Toute archive/suppression exige l'accord
-- explicite du propriétaire des données (voir docs/agent/BUYER_DIRECT_PURCHASE_FLOW_2026-09-28.md §Data).
-- ═══════════════════════════════════════════════════════════════════════════
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';

\echo '=== 1. Produits suspects (unité incompatible, prix/quantité aberrants, doublons du motif d''incident) ==='
WITH livestock_words(w) AS (
    VALUES ('boeuf'), ('bœuf'), ('vache'), ('taureau'), ('veau'), ('mouton'), ('belier'), ('bélier'),
           ('chevre'), ('chèvre'), ('porc'), ('cochon'), ('poulet'), ('coq'), ('poule'), ('pintade'),
           ('dinde'), ('canard'), ('lapin'), ('ane'), ('âne'), ('cheval'), ('chameau')
),
flagged AS (
    SELECT
        p.id, p.producer_id, p.name, p.price, p.quantity_for_sale AS quantity, p.unit,
        p.is_available, p.created_at,
        ARRAY_REMOVE(ARRAY[
            CASE WHEN EXISTS (SELECT 1 FROM livestock_words l WHERE lower(p.name) LIKE '%' || l.w || '%')
                      AND upper(p.unit) IN ('KG','TONNE','LITRE','L','G')
                 THEN 'ELEVAGE_UNITE_MASSE_OU_VOLUME' END,
            CASE WHEN EXISTS (SELECT 1 FROM livestock_words l WHERE lower(p.name) LIKE '%' || l.w || '%')
                      AND upper(p.unit) = 'UNITE'
                 THEN 'ELEVAGE_UNITE_GENERIQUE' END,
            CASE WHEN p.price = p.quantity_for_sale AND p.price >= 100000
                 THEN 'PRIX_EGAL_QUANTITE (motif d''incident 461000/461000)' END,
            CASE WHEN p.quantity_for_sale >= 100000 AND p.is_available
                 THEN 'STOCK_DISPONIBLE_IMPLAUSIBLE (>=100000)' END,
            CASE WHEN p.price <= 0 AND p.is_available THEN 'PRIX_NUL_OU_NEGATIF_DISPONIBLE' END,
            CASE WHEN p.quantity_for_sale < 0 THEN 'STOCK_NEGATIF' END
        ], NULL) AS anomalies
    FROM marketplace.products p
)
SELECT f.id AS product_id, f.producer_id, f.name, f.price, f.quantity, f.unit, f.is_available,
       f.created_at, f.anomalies,
       (SELECT count(*) FROM marketplace.order_items oi WHERE oi.product_id = f.id) AS order_items_count,
       (SELECT count(*) FROM analytics.business_events be
         WHERE be.entity_id = f.id) AS business_events_count
FROM flagged f
WHERE cardinality(f.anomalies) > 0
ORDER BY f.created_at DESC;

\echo '=== 2. Producteurs ayant PLUSIEURS offres actives pour le même nom de produit (risque d''identité d''offre) ==='
SELECT p.producer_id, lower(p.name) AS product_name, count(*) AS offers,
       array_agg(p.id ORDER BY p.created_at) AS product_ids,
       array_agg(p.price ORDER BY p.created_at) AS prices,
       array_agg(p.quantity_for_sale ORDER BY p.created_at) AS quantities
FROM marketplace.products p
WHERE p.is_available AND p.quantity_for_sale > 0
GROUP BY p.producer_id, lower(p.name)
HAVING count(*) > 1
ORDER BY offers DESC, product_name;

\echo '=== 3. Candidats à un nettoyage SANS dépendance (proposition, aucune action) ==='
-- Produit anormal (section 1) n'ayant AUCUNE ligne de commande ET AUCUN événement métier.
-- Les mouvements de stock sont rattachés à `marketplace.stocks` (pas à `products`) : à vérifier
-- séparément par un humain avant toute archive (voir la section 4).
SELECT p.id AS product_id, p.producer_id, p.name, p.price, p.quantity_for_sale AS quantity,
       p.unit, p.created_at
FROM marketplace.products p
WHERE (p.price = p.quantity_for_sale AND p.price >= 100000
       OR (upper(p.unit) IN ('UNITE','KG','TONNE','LITRE')
           AND lower(p.name) SIMILAR TO '%(boeuf|bœuf|vache|mouton|chevre|chèvre|porc|poulet)%'))
  AND NOT EXISTS (SELECT 1 FROM marketplace.order_items oi WHERE oi.product_id = p.id)
  AND NOT EXISTS (SELECT 1 FROM analytics.business_events be WHERE be.entity_id = p.id)
ORDER BY p.created_at DESC;

\echo '=== 4. Stocks / mouvements liés (par producteur des produits suspects ; à recouper à la main) ==='
SELECT s.id AS stock_id, s.type, count(m.id) AS movements, min(m.created_at) AS first_movement,
       max(m.created_at) AS last_movement
FROM marketplace.stocks s
LEFT JOIN marketplace.stock_movements m ON m.stock_id = s.id
GROUP BY s.id, s.type
HAVING count(m.id) > 0
ORDER BY last_movement DESC
LIMIT 50;

ROLLBACK;
