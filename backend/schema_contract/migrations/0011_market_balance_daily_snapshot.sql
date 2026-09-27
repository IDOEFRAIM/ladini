CREATE TABLE "analytics"."market_balance_daily_snapshot" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"snapshot_day" date NOT NULL,
	"zone_scope" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"sub_category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"canonical_unit" text NOT NULL,
	"measurement_family" text NOT NULL,
	"demand_scope" text NOT NULL,
	"open_demand_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"available_supply_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"potential_coverable_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"demand_gap_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"excess_supply_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "market_balance_daily_snapshot_family_chk" CHECK ("analytics"."market_balance_daily_snapshot"."measurement_family" IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')),
	CONSTRAINT "market_balance_daily_snapshot_scope_chk" CHECK ("analytics"."market_balance_daily_snapshot"."demand_scope" IN ('RECURRING','TENDER','RECURRING+TENDER')),
	CONSTRAINT "market_balance_daily_snapshot_qty_chk" CHECK ("analytics"."market_balance_daily_snapshot"."open_demand_quantity" >= 0 AND "analytics"."market_balance_daily_snapshot"."available_supply_quantity" >= 0 AND "analytics"."market_balance_daily_snapshot"."potential_coverable_quantity" >= 0 AND "analytics"."market_balance_daily_snapshot"."demand_gap_quantity" >= 0 AND "analytics"."market_balance_daily_snapshot"."excess_supply_quantity" >= 0)
);
--> statement-breakpoint
CREATE UNIQUE INDEX "market_balance_daily_snapshot_grain_uq" ON "analytics"."market_balance_daily_snapshot" USING btree ("snapshot_day","zone_scope","category_id","sub_category_id","canonical_unit","demand_scope");--> statement-breakpoint
CREATE INDEX "market_balance_daily_snapshot_date_idx" ON "analytics"."market_balance_daily_snapshot" USING btree ("snapshot_day");