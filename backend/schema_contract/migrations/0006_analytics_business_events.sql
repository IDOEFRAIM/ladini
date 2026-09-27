CREATE SCHEMA IF NOT EXISTS "analytics";--> statement-breakpoint
CREATE TABLE "analytics"."business_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"event_name" text NOT NULL,
	"journey" text NOT NULL,
	"actor_type" text NOT NULL,
	"actor_id" uuid,
	"buyer_id" uuid,
	"producer_id" uuid,
	"entity_type" text NOT NULL,
	"entity_id" uuid NOT NULL,
	"category_id" uuid,
	"sub_category_id" uuid,
	"zone_id" uuid,
	"quantity" numeric(14, 3),
	"unit" text,
	"canonical_quantity" numeric(14, 3),
	"canonical_unit" text,
	"measurement_family" text,
	"amount" numeric(14, 2),
	"currency" text DEFAULT 'XOF' NOT NULL,
	"metadata" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"occurred_at" timestamp with time zone NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"idempotency_key" text NOT NULL,
	CONSTRAINT "business_events_event_name_chk" CHECK ("analytics"."business_events"."event_name" IN ('DIRECT_SEARCH_PERFORMED','DIRECT_SEARCH_SUCCEEDED','DIRECT_ORDER_CREATED','DIRECT_ORDER_CONFIRMED','DIRECT_ORDER_DELIVERED','DIRECT_ORDER_FAILED','TENDER_CREATED','TENDER_PUBLISHED','TENDER_BID_RECEIVED','TENDER_WINNER_SELECTED','TENDER_ORDER_CREATED','TENDER_DELIVERED','RECURRING_NEED_CREATED','RECURRING_OCCURRENCE_CREATED','RECURRING_MATCH_FOUND','RECURRING_DIGEST_SENT','RECURRING_DIGEST_ACCEPTED','RECURRING_DIGEST_MODIFIED','RECURRING_OCCURRENCE_SKIPPED','RECURRING_OCCURRENCE_CONFIRMED','RECURRING_OCCURRENCE_DELIVERED')),
	CONSTRAINT "business_events_journey_chk" CHECK ("analytics"."business_events"."journey" IN ('DIRECT','TENDER','RECURRING')),
	CONSTRAINT "business_events_actor_type_chk" CHECK ("analytics"."business_events"."actor_type" IN ('BUYER','PRODUCER','SYSTEM','ADMIN')),
	CONSTRAINT "business_events_measurement_family_chk" CHECK ("analytics"."business_events"."measurement_family" IS NULL OR "analytics"."business_events"."measurement_family" IN ('MASS','VOLUME','COUNT','PACKAGE','OTHER')),
	CONSTRAINT "business_events_quantity_non_negative_chk" CHECK ("analytics"."business_events"."quantity" IS NULL OR "analytics"."business_events"."quantity" >= 0),
	CONSTRAINT "business_events_amount_non_negative_chk" CHECK ("analytics"."business_events"."amount" IS NULL OR "analytics"."business_events"."amount" >= 0)
);
--> statement-breakpoint
CREATE TABLE "analytics"."event_outbox" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"event_name" text NOT NULL,
	"journey" text NOT NULL,
	"payload" jsonb NOT NULL,
	"dedupe_key" text NOT NULL,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"attempts" integer DEFAULT 0 NOT NULL,
	"last_error" text,
	"next_attempt_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "event_outbox_status_chk" CHECK ("analytics"."event_outbox"."status" IN ('PENDING','SENDING','SENT','FAILED','DEAD')),
	CONSTRAINT "event_outbox_journey_chk" CHECK ("analytics"."event_outbox"."journey" IN ('DIRECT','TENDER','RECURRING'))
);
--> statement-breakpoint
CREATE TABLE "analytics"."metric_targets" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"metric_name" text NOT NULL,
	"scope_type" text NOT NULL,
	"scope_id" uuid,
	"target_value" numeric(10, 4) NOT NULL,
	"warning_threshold" numeric(10, 4),
	"critical_threshold" numeric(10, 4),
	"valid_from" date NOT NULL,
	"valid_until" date,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "metric_targets_scope_type_chk" CHECK ("analytics"."metric_targets"."scope_type" IN ('GLOBAL','JOURNEY','CATEGORY','SUBCATEGORY','ZONE')),
	CONSTRAINT "metric_targets_scope_id_matches_scope_type_chk" CHECK (("analytics"."metric_targets"."scope_type" = 'GLOBAL' AND "analytics"."metric_targets"."scope_id" IS NULL) OR ("analytics"."metric_targets"."scope_type" <> 'GLOBAL' AND "analytics"."metric_targets"."scope_id" IS NOT NULL))
);
--> statement-breakpoint
ALTER TABLE "analytics"."business_events" ADD CONSTRAINT "business_events_buyer_id_buyer_profiles_id_fk" FOREIGN KEY ("buyer_id") REFERENCES "marketplace"."buyer_profiles"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "analytics"."business_events" ADD CONSTRAINT "business_events_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "analytics"."business_events" ADD CONSTRAINT "business_events_category_id_categories_id_fk" FOREIGN KEY ("category_id") REFERENCES "governance"."categories"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "analytics"."business_events" ADD CONSTRAINT "business_events_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "analytics"."business_events" ADD CONSTRAINT "business_events_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "business_events_idempotency_key_uq" ON "analytics"."business_events" USING btree ("idempotency_key");--> statement-breakpoint
CREATE INDEX "business_events_event_name_idx" ON "analytics"."business_events" USING btree ("event_name","occurred_at");--> statement-breakpoint
CREATE INDEX "business_events_journey_idx" ON "analytics"."business_events" USING btree ("journey","occurred_at");--> statement-breakpoint
CREATE INDEX "business_events_occurred_at_idx" ON "analytics"."business_events" USING btree ("occurred_at");--> statement-breakpoint
CREATE INDEX "business_events_buyer_idx" ON "analytics"."business_events" USING btree ("buyer_id","occurred_at");--> statement-breakpoint
CREATE INDEX "business_events_entity_idx" ON "analytics"."business_events" USING btree ("entity_type","entity_id");--> statement-breakpoint
CREATE INDEX "business_events_sub_category_idx" ON "analytics"."business_events" USING btree ("sub_category_id","occurred_at");--> statement-breakpoint
CREATE INDEX "business_events_zone_idx" ON "analytics"."business_events" USING btree ("zone_id","occurred_at");--> statement-breakpoint
CREATE UNIQUE INDEX "event_outbox_dedupe_key_uq" ON "analytics"."event_outbox" USING btree ("dedupe_key");--> statement-breakpoint
CREATE INDEX "event_outbox_claim_idx" ON "analytics"."event_outbox" USING btree ("status","next_attempt_at");--> statement-breakpoint
CREATE INDEX "metric_targets_metric_scope_idx" ON "analytics"."metric_targets" USING btree ("metric_name","scope_type","scope_id");