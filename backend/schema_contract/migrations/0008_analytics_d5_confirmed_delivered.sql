ALTER TABLE "analytics"."buyer_daily_metrics" ADD COLUMN "confirmed_gmv_direct" numeric(16, 2) DEFAULT '0' NOT NULL;--> statement-breakpoint
ALTER TABLE "analytics"."direct_daily_metrics" ADD COLUMN "orders_confirmed" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "analytics"."direct_daily_metrics" ADD COLUMN "confirmed_value" numeric(16, 2) DEFAULT '0' NOT NULL;--> statement-breakpoint
ALTER TABLE "analytics"."recurring_daily_metrics" ADD COLUMN "delivered_quantity" numeric(16, 3) DEFAULT '0' NOT NULL;--> statement-breakpoint
ALTER TABLE "analytics"."buyer_daily_metrics" ADD CONSTRAINT "buyer_daily_metrics_confirmed_direct_chk" CHECK ("analytics"."buyer_daily_metrics"."confirmed_gmv_direct" >= 0);--> statement-breakpoint
ALTER TABLE "analytics"."direct_daily_metrics" ADD CONSTRAINT "direct_daily_metrics_confirmed_chk" CHECK ("analytics"."direct_daily_metrics"."orders_confirmed" >= 0 AND "analytics"."direct_daily_metrics"."confirmed_value" >= 0);--> statement-breakpoint
ALTER TABLE "analytics"."recurring_daily_metrics" ADD CONSTRAINT "recurring_daily_metrics_delivered_chk" CHECK ("analytics"."recurring_daily_metrics"."delivered_quantity" >= 0);