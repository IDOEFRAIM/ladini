CREATE TABLE "analytics"."producer_daily_metrics" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"producer_id" uuid NOT NULL,
	"zone_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"products_published" integer DEFAULT 0 NOT NULL,
	"quantity_changes" integer DEFAULT 0 NOT NULL,
	"bids_received" integer DEFAULT 0 NOT NULL,
	"orders_confirmed_direct" integer DEFAULT 0 NOT NULL,
	"orders_delivered_direct" integer DEFAULT 0 NOT NULL,
	"orders_confirmed_tender" integer DEFAULT 0 NOT NULL,
	"orders_delivered_tender" integer DEFAULT 0 NOT NULL,
	"orders_confirmed_recurring" integer DEFAULT 0 NOT NULL,
	"orders_delivered_recurring" integer DEFAULT 0 NOT NULL,
	"delivered_gmv_direct" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_gmv_tender" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_gmv_recurring" numeric(16, 2) DEFAULT '0' NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "producer_daily_metrics_counts_chk" CHECK ("analytics"."producer_daily_metrics"."products_published" >= 0 AND "analytics"."producer_daily_metrics"."quantity_changes" >= 0 AND "analytics"."producer_daily_metrics"."bids_received" >= 0 AND "analytics"."producer_daily_metrics"."orders_confirmed_direct" >= 0 AND "analytics"."producer_daily_metrics"."orders_delivered_direct" >= 0 AND "analytics"."producer_daily_metrics"."orders_confirmed_tender" >= 0 AND "analytics"."producer_daily_metrics"."orders_delivered_tender" >= 0 AND "analytics"."producer_daily_metrics"."orders_confirmed_recurring" >= 0 AND "analytics"."producer_daily_metrics"."orders_delivered_recurring" >= 0),
	CONSTRAINT "producer_daily_metrics_money_chk" CHECK ("analytics"."producer_daily_metrics"."delivered_gmv_direct" >= 0 AND "analytics"."producer_daily_metrics"."delivered_gmv_tender" >= 0 AND "analytics"."producer_daily_metrics"."delivered_gmv_recurring" >= 0)
);
--> statement-breakpoint
CREATE TABLE "analytics"."producer_quantity_daily_metrics" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"producer_id" uuid NOT NULL,
	"canonical_unit" text NOT NULL,
	"measurement_family" text NOT NULL,
	"confirmed_quantity_direct" numeric(16, 3) DEFAULT '0' NOT NULL,
	"delivered_quantity_direct" numeric(16, 3) DEFAULT '0' NOT NULL,
	"confirmed_quantity_recurring" numeric(16, 3) DEFAULT '0' NOT NULL,
	"delivered_quantity_recurring" numeric(16, 3) DEFAULT '0' NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "producer_quantity_daily_metrics_family_chk" CHECK ("analytics"."producer_quantity_daily_metrics"."measurement_family" IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')),
	CONSTRAINT "producer_quantity_daily_metrics_qty_chk" CHECK ("analytics"."producer_quantity_daily_metrics"."confirmed_quantity_direct" >= 0 AND "analytics"."producer_quantity_daily_metrics"."delivered_quantity_direct" >= 0 AND "analytics"."producer_quantity_daily_metrics"."confirmed_quantity_recurring" >= 0 AND "analytics"."producer_quantity_daily_metrics"."delivered_quantity_recurring" >= 0)
);
--> statement-breakpoint
CREATE TABLE "analytics"."producer_supply_daily_snapshot" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"producer_id" uuid NOT NULL,
	"zone_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"sub_category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"canonical_unit" text NOT NULL,
	"measurement_family" text NOT NULL,
	"available_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"product_count" integer DEFAULT 0 NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "producer_supply_daily_snapshot_family_chk" CHECK ("analytics"."producer_supply_daily_snapshot"."measurement_family" IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')),
	CONSTRAINT "producer_supply_daily_snapshot_qty_chk" CHECK ("analytics"."producer_supply_daily_snapshot"."available_quantity" >= 0 AND "analytics"."producer_supply_daily_snapshot"."product_count" >= 0)
);
--> statement-breakpoint
CREATE UNIQUE INDEX "producer_daily_metrics_grain_uq" ON "analytics"."producer_daily_metrics" USING btree ("metric_date","producer_id");--> statement-breakpoint
CREATE INDEX "producer_daily_metrics_date_zone_idx" ON "analytics"."producer_daily_metrics" USING btree ("metric_date","zone_id");--> statement-breakpoint
CREATE UNIQUE INDEX "producer_quantity_daily_metrics_grain_uq" ON "analytics"."producer_quantity_daily_metrics" USING btree ("metric_date","producer_id","canonical_unit");--> statement-breakpoint
CREATE INDEX "producer_quantity_daily_metrics_date_idx" ON "analytics"."producer_quantity_daily_metrics" USING btree ("metric_date");--> statement-breakpoint
CREATE UNIQUE INDEX "producer_supply_daily_snapshot_grain_uq" ON "analytics"."producer_supply_daily_snapshot" USING btree ("metric_date","producer_id","zone_id","category_id","sub_category_id","canonical_unit");--> statement-breakpoint
CREATE INDEX "producer_supply_daily_snapshot_date_idx" ON "analytics"."producer_supply_daily_snapshot" USING btree ("metric_date");