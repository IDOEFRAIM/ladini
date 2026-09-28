ALTER TABLE "marketplace"."bids" ADD COLUMN "offered_price_basis" text;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "offered_price_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "offered_price_currency" text;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "package_type" text;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "package_content_amount" numeric(14, 3);--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "package_content_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "normalized_unit_price" numeric(18, 4);--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "normalized_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD COLUMN "pricing_snapshot_version" integer;--> statement-breakpoint
ALTER TABLE "marketplace"."market_offers" ADD COLUMN "pricing_snapshot" jsonb;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "quantity_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "commercial_price_amount" numeric(14, 2);--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "price_basis" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "price_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "package_type" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "package_content_amount" numeric(14, 3);--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "package_content_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "normalized_unit_price" numeric(18, 4);--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "normalized_unit" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "currency" text;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD COLUMN "pricing_snapshot_version" integer;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD COLUMN "award_pricing_snapshot" jsonb;--> statement-breakpoint
ALTER TABLE "marketplace"."products" ADD COLUMN "commercial_pricing" jsonb;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD CONSTRAINT "bids_price_basis_chk" CHECK ("marketplace"."bids"."offered_price_basis" IS NULL OR "marketplace"."bids"."offered_price_basis" IN ('PER_BASE_UNIT','PER_PACKAGE','TOTAL_LOT','LEGACY_UNSPECIFIED'));--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD CONSTRAINT "bids_snapshot_chk" CHECK ("marketplace"."bids"."pricing_snapshot_version" IS NULL OR ("marketplace"."bids"."pricing_snapshot_version" >= 1 AND "marketplace"."bids"."offered_price" > 0 AND "marketplace"."bids"."offered_price_currency" IS NOT NULL AND "marketplace"."bids"."offered_price_basis" IS NOT NULL AND "marketplace"."bids"."offered_price_basis" IN ('PER_BASE_UNIT','PER_PACKAGE','TOTAL_LOT') AND ("marketplace"."bids"."offered_price_basis" <> 'PER_BASE_UNIT' OR "marketplace"."bids"."offered_price_unit" IS NOT NULL) AND ("marketplace"."bids"."offered_price_basis" = 'PER_BASE_UNIT' OR "marketplace"."bids"."offered_price_unit" IS NULL) AND ("marketplace"."bids"."offered_price_basis" <> 'PER_PACKAGE' OR ("marketplace"."bids"."package_type" IS NOT NULL AND "marketplace"."bids"."package_content_amount" IS NOT NULL AND "marketplace"."bids"."package_content_amount" > 0 AND "marketplace"."bids"."package_content_unit" IS NOT NULL)) AND ("marketplace"."bids"."offered_price_basis" = 'PER_PACKAGE' OR ("marketplace"."bids"."package_type" IS NULL AND "marketplace"."bids"."package_content_amount" IS NULL AND "marketplace"."bids"."package_content_unit" IS NULL)) AND ("marketplace"."bids"."normalized_unit_price" IS NULL OR ("marketplace"."bids"."normalized_unit_price" > 0 AND "marketplace"."bids"."normalized_unit" IS NOT NULL))));--> statement-breakpoint
ALTER TABLE "marketplace"."market_offers" ADD CONSTRAINT "market_offers_pricing_snapshot_chk" CHECK ("marketplace"."market_offers"."pricing_snapshot" IS NULL OR (jsonb_typeof("marketplace"."market_offers"."pricing_snapshot") = 'object' AND "marketplace"."market_offers"."pricing_snapshot" ? 'schema_version'));--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD CONSTRAINT "order_items_snapshot_all_null_chk" CHECK ("marketplace"."order_items"."pricing_snapshot_version" IS NOT NULL OR ("marketplace"."order_items"."commercial_price_amount" IS NULL AND "marketplace"."order_items"."price_basis" IS NULL AND "marketplace"."order_items"."price_unit" IS NULL AND "marketplace"."order_items"."package_type" IS NULL AND "marketplace"."order_items"."package_content_amount" IS NULL AND "marketplace"."order_items"."package_content_unit" IS NULL AND "marketplace"."order_items"."normalized_unit_price" IS NULL AND "marketplace"."order_items"."normalized_unit" IS NULL AND "marketplace"."order_items"."quantity_unit" IS NULL AND "marketplace"."order_items"."currency" IS NULL));--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD CONSTRAINT "order_items_snapshot_chk" CHECK ("marketplace"."order_items"."pricing_snapshot_version" IS NULL OR ("marketplace"."order_items"."pricing_snapshot_version" >= 1 AND "marketplace"."order_items"."commercial_price_amount" IS NOT NULL AND "marketplace"."order_items"."commercial_price_amount" > 0 AND "marketplace"."order_items"."quantity_unit" IS NOT NULL AND "marketplace"."order_items"."currency" IS NOT NULL AND "marketplace"."order_items"."price_basis" IS NOT NULL AND "marketplace"."order_items"."price_basis" IN ('PER_BASE_UNIT','PER_PACKAGE','TOTAL_LOT') AND ("marketplace"."order_items"."price_basis" <> 'PER_BASE_UNIT' OR "marketplace"."order_items"."price_unit" IS NOT NULL) AND ("marketplace"."order_items"."price_basis" = 'PER_BASE_UNIT' OR "marketplace"."order_items"."price_unit" IS NULL) AND ("marketplace"."order_items"."price_basis" <> 'PER_PACKAGE' OR ("marketplace"."order_items"."package_type" IS NOT NULL AND "marketplace"."order_items"."package_content_amount" IS NOT NULL AND "marketplace"."order_items"."package_content_amount" > 0 AND "marketplace"."order_items"."package_content_unit" IS NOT NULL)) AND ("marketplace"."order_items"."price_basis" = 'PER_PACKAGE' OR ("marketplace"."order_items"."package_type" IS NULL AND "marketplace"."order_items"."package_content_amount" IS NULL AND "marketplace"."order_items"."package_content_unit" IS NULL)) AND ("marketplace"."order_items"."normalized_unit_price" IS NULL OR ("marketplace"."order_items"."normalized_unit_price" > 0 AND "marketplace"."order_items"."normalized_unit" IS NOT NULL))));--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_award_pricing_snapshot_chk" CHECK ("marketplace"."orders"."award_pricing_snapshot" IS NULL OR (jsonb_typeof("marketplace"."orders"."award_pricing_snapshot") = 'object' AND "marketplace"."orders"."award_pricing_snapshot" ? 'schema_version'));--> statement-breakpoint
ALTER TABLE "marketplace"."products" ADD CONSTRAINT "products_commercial_pricing_chk" CHECK ("marketplace"."products"."commercial_pricing" IS NULL OR (jsonb_typeof("marketplace"."products"."commercial_pricing") = 'object' AND "marketplace"."products"."commercial_pricing" ? 'schema_version'));
--> statement-breakpoint
-- ── Phase B2a : IMMUABILITÉ des instantanés de prix (ajout manuel, drizzle-kit ne génère pas de triggers) ──
-- Un instantané certifié (pricing_snapshot_version non NULL) ne se réécrit JAMAIS : ni par le backend, ni
-- par un script, ni par un UPDATE manuel. Le passage NULL -> renseigné (rattrapage explicite d'une ligne
-- antérieure) reste possible ; toute autre modification du snapshot est refusée. Les colonnes de QUANTITÉ
-- (quantity, base_unit_quantity, price_at_sale) ne sont volontairement pas gelées ici.
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
  ) THEN
    RAISE EXCEPTION 'order_items pricing snapshot is immutable (order_item %)', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
--> statement-breakpoint
CREATE TRIGGER order_items_snapshot_immutable_trg
  BEFORE UPDATE ON marketplace.order_items
  FOR EACH ROW EXECUTE FUNCTION marketplace.forbid_order_item_snapshot_rewrite();
--> statement-breakpoint
CREATE OR REPLACE FUNCTION marketplace.forbid_order_award_snapshot_rewrite() RETURNS trigger AS $$
BEGIN
  IF OLD.award_pricing_snapshot IS NOT NULL
     AND NEW.award_pricing_snapshot IS DISTINCT FROM OLD.award_pricing_snapshot THEN
    RAISE EXCEPTION 'orders award pricing snapshot is immutable (order %)', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
--> statement-breakpoint
CREATE TRIGGER orders_award_snapshot_immutable_trg
  BEFORE UPDATE ON marketplace.orders
  FOR EACH ROW EXECUTE FUNCTION marketplace.forbid_order_award_snapshot_rewrite();
