CREATE TABLE "marketplace"."need_allocations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"occurrence_id" uuid NOT NULL,
	"producer_id" uuid NOT NULL,
	"product_id" uuid NOT NULL,
	"quantity" numeric(14, 3) NOT NULL,
	"unit_price" numeric(12, 2) NOT NULL,
	"unit" text NOT NULL,
	"status" text DEFAULT 'PROPOSED' NOT NULL,
	"order_item_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "need_allocations_quantity_chk" CHECK ("marketplace"."need_allocations"."quantity" > 0),
	CONSTRAINT "need_allocations_unit_price_chk" CHECK ("marketplace"."need_allocations"."unit_price" >= 0),
	CONSTRAINT "need_allocations_status_chk" CHECK ("marketplace"."need_allocations"."status" IN ('PROPOSED','ACCEPTED','REJECTED','EXPIRED','CONVERTED'))
);
--> statement-breakpoint
CREATE TABLE "marketplace"."recurring_need_occurrences" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"recurring_need_id" uuid NOT NULL,
	"occurrence_date" timestamp NOT NULL,
	"requested_quantity" numeric(14, 3) NOT NULL,
	"unit" text NOT NULL,
	"status" text DEFAULT 'OPEN' NOT NULL,
	"quantity_matched" numeric(14, 3) DEFAULT '0' NOT NULL,
	"quantity_confirmed" numeric(14, 3) DEFAULT '0' NOT NULL,
	"quantity_delivered" numeric(14, 3) DEFAULT '0' NOT NULL,
	"version" integer DEFAULT 1 NOT NULL,
	"notified_at" timestamp,
	"accepted_at" timestamp,
	"expires_at" timestamp,
	"order_group_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "recurring_need_occurrences_requested_qty_chk" CHECK ("marketplace"."recurring_need_occurrences"."requested_quantity" > 0),
	CONSTRAINT "recurring_need_occurrences_matched_qty_chk" CHECK ("marketplace"."recurring_need_occurrences"."quantity_matched" >= 0),
	CONSTRAINT "recurring_need_occurrences_confirmed_qty_chk" CHECK ("marketplace"."recurring_need_occurrences"."quantity_confirmed" >= 0),
	CONSTRAINT "recurring_need_occurrences_delivered_qty_chk" CHECK ("marketplace"."recurring_need_occurrences"."quantity_delivered" >= 0),
	CONSTRAINT "recurring_need_occurrences_version_chk" CHECK ("marketplace"."recurring_need_occurrences"."version" >= 1),
	CONSTRAINT "recurring_need_occurrences_status_chk" CHECK ("marketplace"."recurring_need_occurrences"."status" IN ('OPEN','SKIPPED','MATCHED','PROPOSED','ACCEPTED','PARTIALLY_ACCEPTED','REJECTED','EXPIRED','FULFILLED','PARTIALLY_FULFILLED','UNFULFILLED','CANCELLED'))
);
--> statement-breakpoint
CREATE TABLE "marketplace"."recurring_needs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"buyer_id" uuid NOT NULL,
	"sub_category_id" uuid NOT NULL,
	"quantity" numeric(14, 3) NOT NULL,
	"unit" text DEFAULT 'KG' NOT NULL,
	"recurrence_type" text NOT NULL,
	"weekly_days" integer[],
	"excluded_weekdays" integer[],
	"starts_at" timestamp NOT NULL,
	"ends_at" timestamp,
	"status" text DEFAULT 'ACTIVE' NOT NULL,
	"paused_until" timestamp,
	"max_price_per_unit" numeric(12, 2),
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "recurring_needs_quantity_chk" CHECK ("marketplace"."recurring_needs"."quantity" > 0),
	CONSTRAINT "recurring_needs_max_price_chk" CHECK ("marketplace"."recurring_needs"."max_price_per_unit" IS NULL OR "marketplace"."recurring_needs"."max_price_per_unit" >= 0),
	CONSTRAINT "recurring_needs_recurrence_type_chk" CHECK ("marketplace"."recurring_needs"."recurrence_type" IN ('DAILY','WEEKLY_DAYS','WEEKLY','ONE_OFF')),
	CONSTRAINT "recurring_needs_status_chk" CHECK ("marketplace"."recurring_needs"."status" IN ('ACTIVE','PAUSED','CANCELLED')),
	CONSTRAINT "recurring_needs_weekly_days_chk" CHECK ("marketplace"."recurring_needs"."recurrence_type" <> 'WEEKLY_DAYS' OR "marketplace"."recurring_needs"."weekly_days" IS NOT NULL)
);
--> statement-breakpoint
ALTER TABLE "marketplace"."need_allocations" ADD CONSTRAINT "need_allocations_occurrence_id_recurring_need_occurrences_id_fk" FOREIGN KEY ("occurrence_id") REFERENCES "marketplace"."recurring_need_occurrences"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."need_allocations" ADD CONSTRAINT "need_allocations_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."need_allocations" ADD CONSTRAINT "need_allocations_product_id_products_id_fk" FOREIGN KEY ("product_id") REFERENCES "marketplace"."products"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."need_allocations" ADD CONSTRAINT "need_allocations_order_item_id_order_items_id_fk" FOREIGN KEY ("order_item_id") REFERENCES "marketplace"."order_items"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."recurring_need_occurrences" ADD CONSTRAINT "recurring_need_occurrences_recurring_need_id_recurring_needs_id_fk" FOREIGN KEY ("recurring_need_id") REFERENCES "marketplace"."recurring_needs"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."recurring_needs" ADD CONSTRAINT "recurring_needs_buyer_id_buyer_profiles_id_fk" FOREIGN KEY ("buyer_id") REFERENCES "marketplace"."buyer_profiles"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."recurring_needs" ADD CONSTRAINT "recurring_needs_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "need_allocations_occurrence_producer_product_uq" ON "marketplace"."need_allocations" USING btree ("occurrence_id","producer_id","product_id");--> statement-breakpoint
CREATE INDEX "need_allocations_occurrence_idx" ON "marketplace"."need_allocations" USING btree ("occurrence_id");--> statement-breakpoint
CREATE INDEX "need_allocations_producer_idx" ON "marketplace"."need_allocations" USING btree ("producer_id");--> statement-breakpoint
CREATE INDEX "need_allocations_product_idx" ON "marketplace"."need_allocations" USING btree ("product_id");--> statement-breakpoint
CREATE INDEX "need_allocations_order_item_idx" ON "marketplace"."need_allocations" USING btree ("order_item_id");--> statement-breakpoint
CREATE UNIQUE INDEX "recurring_need_occurrences_need_date_uq" ON "marketplace"."recurring_need_occurrences" USING btree ("recurring_need_id","occurrence_date");--> statement-breakpoint
CREATE INDEX "recurring_need_occurrences_status_date_idx" ON "marketplace"."recurring_need_occurrences" USING btree ("status","occurrence_date");--> statement-breakpoint
CREATE INDEX "recurring_needs_buyer_idx" ON "marketplace"."recurring_needs" USING btree ("buyer_id");--> statement-breakpoint
CREATE INDEX "recurring_needs_subcategory_status_idx" ON "marketplace"."recurring_needs" USING btree ("sub_category_id","status");