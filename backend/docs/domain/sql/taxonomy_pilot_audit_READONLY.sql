-- Phase B1 — audit de la taxonomie pilote (LECTURE SEULE).
-- Objectif : voir, pour les types de produits du pilote, si `priority_unit` / `allowed_units`
-- sont renseignés. NULL/NULL => l'agent retombe sur les règles de repli
-- (`quantity_unit.is_livestock_product` + `ANIMAL_DERIVED_PRODUCT_MARKERS`).
-- Ne modifie RIEN. À exécuter par un humain sur une réplique/lecture seule.
SELECT c.name  AS category,
       s.id    AS sub_category_id,
       s.name  AS sub_category,
       s.priority_unit,
       s.allowed_units,
       s.minimum_order_quantity,
       s.minimum_order_unit
FROM governance.sub_categories s
JOIN governance.categories c ON c.id = s.category_id
WHERE s.name ~* '\m(lait|bovin|boeuf|bœuf|vache|ovin|mouton|caprin|chevre|chèvre|volaille|poulet|coq|poule|mais|maïs|riz|tomate|oignon|huile|oeuf|œuf|fromage)s?\M'
ORDER BY c.name, s.name;

-- Incohérences déjà en base : unité de STOCK incompatible avec la famille attendue (lecture seule).
SELECT p.id, p.name, p.unit, p.price, p.quantity_for_sale, p.pricing_tiers
FROM marketplace.products p
WHERE (p.name ~* '(lait|huile)'  AND p.unit IN ('TETE'))
   OR (p.name ~* '(boeuf|bœuf|mouton|chevre|chèvre)' AND p.unit IN ('KG','LITRE','TONNE') )
ORDER BY p.created_at DESC
LIMIT 200;
