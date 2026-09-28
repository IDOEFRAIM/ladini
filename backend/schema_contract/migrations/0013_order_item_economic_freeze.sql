-- Phase B2b (2026-09-28) — GEL DES CHAMPS ÉCONOMIQUES d'une ligne de commande portant un snapshot.
--
-- B2a gelait les champs du snapshot (`quantity_unit`, `commercial_price_amount`, `price_basis`, …) mais laissait
-- `quantity`, `base_unit_quantity`, `price_at_sale` (et `tier_id`, `product_id`, `order_id`) librement modifiables :
-- deux vérités économiques pouvaient diverger (snapshot « 4 sachets à 500 » vs `quantity = 40`).
-- Décision (option A) : une ligne dont `pricing_snapshot_version` est posée est ENTIÈREMENT gelée sur ce qui décrit
-- l'achat. Aucun code applicatif ne met ces colonnes à jour après création (audit : aucun UPDATE sur order_items
-- côté backend ni côté web) ; une correction métier future = une NOUVELLE ligne, jamais une réécriture.
-- Le rattrapage NULL -> renseigné d'une ligne ANTÉRIEURE reste possible ; une ligne sans snapshot reste éditable.
CREATE OR REPLACE FUNCTION marketplace.forbid_order_item_snapshot_rewrite() RETURNS trigger AS $$
BEGIN
  IF OLD.pricing_snapshot_version IS NOT NULL AND (
       NEW.pricing_snapshot_version IS DISTINCT FROM OLD.pricing_snapshot_version
    OR NEW.quantity_unit            IS DISTINCT FROM OLD.quantity_unit
    OR NEW.commercial_price_amount  IS DISTINCT FROM OLD.commercial_price_amount
    OR NEW.price_basis              IS DISTINCT FROM OLD.price_basis
    OR NEW.price_unit               IS DISTINCT FROM OLD.price_unit
    OR NEW.package_type             IS DISTINCT FROM OLD.package_type
    OR NEW.package_content_amount   IS DISTINCT FROM OLD.package_content_amount
    OR NEW.package_content_unit     IS DISTINCT FROM OLD.package_content_unit
    OR NEW.normalized_unit_price    IS DISTINCT FROM OLD.normalized_unit_price
    OR NEW.normalized_unit          IS DISTINCT FROM OLD.normalized_unit
    OR NEW.currency                 IS DISTINCT FROM OLD.currency
    OR NEW.quantity                 IS DISTINCT FROM OLD.quantity
    OR NEW.base_unit_quantity       IS DISTINCT FROM OLD.base_unit_quantity
    OR NEW.price_at_sale            IS DISTINCT FROM OLD.price_at_sale
    OR NEW.tier_id                  IS DISTINCT FROM OLD.tier_id
    OR NEW.product_id               IS DISTINCT FROM OLD.product_id
    OR NEW.order_id                 IS DISTINCT FROM OLD.order_id
  ) THEN
    RAISE EXCEPTION 'order_items economic fields are immutable once a pricing snapshot exists (order_item %)', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
