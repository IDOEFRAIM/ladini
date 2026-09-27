CREATE TABLE "analytics"."buyer_daily_metrics" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"buyer_id" uuid NOT NULL,
	"zone_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"needs_direct" integer DEFAULT 0 NOT NULL,
	"needs_tender" integer DEFAULT 0 NOT NULL,
	"needs_recurring" integer DEFAULT 0 NOT NULL,
	"satisfied_direct" integer DEFAULT 0 NOT NULL,
	"satisfied_tender" integer DEFAULT 0 NOT NULL,
	"satisfied_recurring" integer DEFAULT 0 NOT NULL,
	"potential_gmv_direct" numeric(16, 2) DEFAULT '0' NOT NULL,
	"potential_gmv_tender" numeric(16, 2) DEFAULT '0' NOT NULL,
	"potential_gmv_recurring" numeric(16, 2) DEFAULT '0' NOT NULL,
	"confirmed_gmv_tender" numeric(16, 2) DEFAULT '0' NOT NULL,
	"confirmed_gmv_recurring" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_gmv_direct" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_gmv_tender" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_gmv_recurring" numeric(16, 2) DEFAULT '0' NOT NULL,
	"digests_queued" integer DEFAULT 0 NOT NULL,
	"digests_accepted" integer DEFAULT 0 NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "buyer_daily_metrics_counts_chk" CHECK ("analytics"."buyer_daily_metrics"."needs_direct" >= 0 AND "analytics"."buyer_daily_metrics"."needs_tender" >= 0 AND "analytics"."buyer_daily_metrics"."needs_recurring" >= 0 AND "analytics"."buyer_daily_metrics"."satisfied_direct" >= 0 AND "analytics"."buyer_daily_metrics"."satisfied_tender" >= 0 AND "analytics"."buyer_daily_metrics"."satisfied_recurring" >= 0 AND "analytics"."buyer_daily_metrics"."digests_queued" >= 0 AND "analytics"."buyer_daily_metrics"."digests_accepted" >= 0),
	CONSTRAINT "buyer_daily_metrics_money_chk" CHECK ("analytics"."buyer_daily_metrics"."potential_gmv_direct" >= 0 AND "analytics"."buyer_daily_metrics"."potential_gmv_tender" >= 0 AND "analytics"."buyer_daily_metrics"."potential_gmv_recurring" >= 0 AND "analytics"."buyer_daily_metrics"."confirmed_gmv_tender" >= 0 AND "analytics"."buyer_daily_metrics"."confirmed_gmv_recurring" >= 0 AND "analytics"."buyer_daily_metrics"."delivered_gmv_direct" >= 0 AND "analytics"."buyer_daily_metrics"."delivered_gmv_tender" >= 0 AND "analytics"."buyer_daily_metrics"."delivered_gmv_recurring" >= 0)
);
--> statement-breakpoint
CREATE TABLE "analytics"."direct_daily_metrics" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"zone_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"sub_category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"searches" integer DEFAULT 0 NOT NULL,
	"successful_searches" integer DEFAULT 0 NOT NULL,
	"orders_created" integer DEFAULT 0 NOT NULL,
	"orders_delivered" integer DEFAULT 0 NOT NULL,
	"created_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "direct_daily_metrics_counts_chk" CHECK ("analytics"."direct_daily_metrics"."searches" >= 0 AND "analytics"."direct_daily_metrics"."successful_searches" >= 0 AND "analytics"."direct_daily_metrics"."orders_created" >= 0 AND "analytics"."direct_daily_metrics"."orders_delivered" >= 0 AND "analytics"."direct_daily_metrics"."created_value" >= 0 AND "analytics"."direct_daily_metrics"."delivered_value" >= 0)
);
--> statement-breakpoint
CREATE TABLE "analytics"."recurring_daily_metrics" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"zone_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"sub_category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"canonical_unit" text NOT NULL,
	"measurement_family" text NOT NULL,
	"occurrences_total" integer DEFAULT 0 NOT NULL,
	"occurrences_active" integer DEFAULT 0 NOT NULL,
	"occurrences_fully_covered" integer DEFAULT 0 NOT NULL,
	"occurrences_notified" integer DEFAULT 0 NOT NULL,
	"occurrences_accepted" integer DEFAULT 0 NOT NULL,
	"occurrences_skipped" integer DEFAULT 0 NOT NULL,
	"occurrences_with_orders" integer DEFAULT 0 NOT NULL,
	"occurrences_all_received" integer DEFAULT 0 NOT NULL,
	"needs_with_occurrence" integer DEFAULT 0 NOT NULL,
	"requested_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"matched_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"confirmed_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"unmatched_quantity" numeric(16, 3) DEFAULT '0' NOT NULL,
	"potential_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"confirmed_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"received_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "recurring_daily_metrics_family_chk" CHECK ("analytics"."recurring_daily_metrics"."measurement_family" IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')),
	CONSTRAINT "recurring_daily_metrics_counts_chk" CHECK ("analytics"."recurring_daily_metrics"."occurrences_total" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_active" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_fully_covered" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_notified" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_accepted" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_skipped" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_with_orders" >= 0 AND "analytics"."recurring_daily_metrics"."occurrences_all_received" >= 0 AND "analytics"."recurring_daily_metrics"."needs_with_occurrence" >= 0),
	CONSTRAINT "recurring_daily_metrics_qty_chk" CHECK ("analytics"."recurring_daily_metrics"."requested_quantity" >= 0 AND "analytics"."recurring_daily_metrics"."matched_quantity" >= 0 AND "analytics"."recurring_daily_metrics"."confirmed_quantity" >= 0 AND "analytics"."recurring_daily_metrics"."unmatched_quantity" >= 0 AND "analytics"."recurring_daily_metrics"."potential_value" >= 0 AND "analytics"."recurring_daily_metrics"."confirmed_value" >= 0 AND "analytics"."recurring_daily_metrics"."received_value" >= 0)
);
--> statement-breakpoint
CREATE TABLE "analytics"."tender_daily_metrics" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_date" date NOT NULL,
	"zone_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"sub_category_id" uuid DEFAULT '00000000-0000-0000-0000-000000000000' NOT NULL,
	"tenders_created" integer DEFAULT 0 NOT NULL,
	"tenders_with_bid" integer DEFAULT 0 NOT NULL,
	"bids_received" integer DEFAULT 0 NOT NULL,
	"tenders_with_winner" integer DEFAULT 0 NOT NULL,
	"tender_orders_created" integer DEFAULT 0 NOT NULL,
	"tender_orders_delivered" integer DEFAULT 0 NOT NULL,
	"first_bid_latency_seconds_sum" numeric(18, 3) DEFAULT '0' NOT NULL,
	"first_bid_latency_count" integer DEFAULT 0 NOT NULL,
	"potential_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"committed_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"delivered_value" numeric(16, 2) DEFAULT '0' NOT NULL,
	"computed_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "tender_daily_metrics_counts_chk" CHECK ("analytics"."tender_daily_metrics"."tenders_created" >= 0 AND "analytics"."tender_daily_metrics"."tenders_with_bid" >= 0 AND "analytics"."tender_daily_metrics"."bids_received" >= 0 AND "analytics"."tender_daily_metrics"."tenders_with_winner" >= 0 AND "analytics"."tender_daily_metrics"."tender_orders_created" >= 0 AND "analytics"."tender_daily_metrics"."tender_orders_delivered" >= 0 AND "analytics"."tender_daily_metrics"."first_bid_latency_seconds_sum" >= 0 AND "analytics"."tender_daily_metrics"."first_bid_latency_count" >= 0 AND "analytics"."tender_daily_metrics"."potential_value" >= 0 AND "analytics"."tender_daily_metrics"."committed_value" >= 0 AND "analytics"."tender_daily_metrics"."delivered_value" >= 0)
);
--> statement-breakpoint
CREATE UNIQUE INDEX "buyer_daily_metrics_grain_uq" ON "analytics"."buyer_daily_metrics" USING btree ("metric_date","buyer_id");--> statement-breakpoint
CREATE INDEX "buyer_daily_metrics_date_zone_idx" ON "analytics"."buyer_daily_metrics" USING btree ("metric_date","zone_id");--> statement-breakpoint
CREATE UNIQUE INDEX "direct_daily_metrics_grain_uq" ON "analytics"."direct_daily_metrics" USING btree ("metric_date","zone_id","category_id","sub_category_id");--> statement-breakpoint
CREATE INDEX "direct_daily_metrics_date_idx" ON "analytics"."direct_daily_metrics" USING btree ("metric_date");--> statement-breakpoint
CREATE UNIQUE INDEX "recurring_daily_metrics_grain_uq" ON "analytics"."recurring_daily_metrics" USING btree ("metric_date","zone_id","category_id","sub_category_id","canonical_unit");--> statement-breakpoint
CREATE INDEX "recurring_daily_metrics_date_idx" ON "analytics"."recurring_daily_metrics" USING btree ("metric_date");--> statement-breakpoint
CREATE UNIQUE INDEX "tender_daily_metrics_grain_uq" ON "analytics"."tender_daily_metrics" USING btree ("metric_date","zone_id","category_id","sub_category_id");--> statement-breakpoint
CREATE INDEX "tender_daily_metrics_date_idx" ON "analytics"."tender_daily_metrics" USING btree ("metric_date");