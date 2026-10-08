CREATE TABLE "intelligence"."availability_campaigns" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"status" text DEFAULT 'DRAFT' NOT NULL,
	"audience" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"offer_filter" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"send_at" timestamp,
	"frequency" text DEFAULT 'ONCE' NOT NULL,
	"interval_days" integer,
	"response_window_hours" integer DEFAULT 48 NOT NULL,
	"delivery_date" date,
	"next_run_at" timestamp,
	"last_run_key" text,
	"content_version" integer DEFAULT 1 NOT NULL,
	"content_hash" text,
	"preview" jsonb,
	"validated_at" timestamp,
	"validated_by_id" uuid,
	"created_by_id" uuid,
	"cancelled_at" timestamp,
	"last_error" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."availability_campaign_recipients" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"campaign_id" uuid NOT NULL,
	"run_key" text NOT NULL,
	"user_id" uuid,
	"phone" text NOT NULL,
	"status" text DEFAULT 'PREPARED' NOT NULL,
	"skip_reason" text,
	"last_error" text,
	"outbox_id" uuid,
	"provider_ref" text,
	"offers" jsonb DEFAULT '[]'::jsonb NOT NULL,
	"content_version" integer DEFAULT 1 NOT NULL,
	"queued_at" timestamp,
	"sent_at" timestamp,
	"delivered_at" timestamp,
	"read_at" timestamp,
	"replied_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."communication_consents" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"phone" text NOT NULL,
	"user_id" uuid,
	"topic" text DEFAULT 'AVAILABILITY_CAMPAIGNS' NOT NULL,
	"status" text NOT NULL,
	"source" text NOT NULL,
	"proof" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"consented_at" timestamp,
	"revoked_at" timestamp,
	"version" integer DEFAULT 1 NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."availability_interests" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"campaign_id" uuid NOT NULL,
	"recipient_id" uuid NOT NULL,
	"phone" text NOT NULL,
	"user_id" uuid,
	"product_id" uuid,
	"product_label" text NOT NULL,
	"quantity" double precision,
	"unit" text,
	"kind" text NOT NULL,
	"status" text DEFAULT 'OPEN' NOT NULL,
	"message_ref" text NOT NULL,
	"changes" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"producer_notified_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_validated_by_id_users_id_fk" FOREIGN KEY ("validated_by_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_created_by_id_users_id_fk" FOREIGN KEY ("created_by_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "availability_campaigns_due_idx" ON "intelligence"."availability_campaigns" USING btree ("status","next_run_at");--> statement-breakpoint
CREATE INDEX "availability_campaigns_validated_by_idx" ON "intelligence"."availability_campaigns" USING btree ("validated_by_id");--> statement-breakpoint
CREATE INDEX "availability_campaigns_created_by_idx" ON "intelligence"."availability_campaigns" USING btree ("created_by_id");--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_status_chk" CHECK (status in ('DRAFT','SCHEDULED','RUNNING','COMPLETED','CANCELLED','FAILED'));--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_frequency_chk" CHECK (frequency in ('ONCE','WEEKLY','CUSTOM_DAYS'));--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_version_chk" CHECK (content_version >= 1);--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_interval_chk" CHECK (interval_days is null or interval_days >= 1);--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaigns" ADD CONSTRAINT "availability_campaigns_window_chk" CHECK (response_window_hours >= 1);--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaign_recipients" ADD CONSTRAINT "availability_campaign_recipients_campaign_id_availability_campaigns_id_fk" FOREIGN KEY ("campaign_id") REFERENCES "intelligence"."availability_campaigns"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaign_recipients" ADD CONSTRAINT "availability_campaign_recipients_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaign_recipients" ADD CONSTRAINT "availability_campaign_recipients_outbox_id_notification_outbox_id_fk" FOREIGN KEY ("outbox_id") REFERENCES "intelligence"."notification_outbox"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "availability_recipients_run_phone_uq" ON "intelligence"."availability_campaign_recipients" USING btree ("campaign_id","run_key","phone");--> statement-breakpoint
CREATE INDEX "availability_recipients_provider_ref_idx" ON "intelligence"."availability_campaign_recipients" USING btree ("provider_ref");--> statement-breakpoint
CREATE INDEX "availability_recipients_phone_sent_idx" ON "intelligence"."availability_campaign_recipients" USING btree ("phone","sent_at");--> statement-breakpoint
CREATE INDEX "availability_recipients_user_idx" ON "intelligence"."availability_campaign_recipients" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "availability_recipients_outbox_idx" ON "intelligence"."availability_campaign_recipients" USING btree ("outbox_id");--> statement-breakpoint
ALTER TABLE "intelligence"."availability_campaign_recipients" ADD CONSTRAINT "availability_recipients_status_chk" CHECK (status in ('PREPARED','QUEUED','SENT','DELIVERED','READ','REPLIED','FAILED','SKIPPED'));--> statement-breakpoint
ALTER TABLE "intelligence"."communication_consents" ADD CONSTRAINT "communication_consents_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "communication_consents_phone_topic_uq" ON "intelligence"."communication_consents" USING btree ("phone","topic");--> statement-breakpoint
CREATE INDEX "communication_consents_user_idx" ON "intelligence"."communication_consents" USING btree ("user_id");--> statement-breakpoint
ALTER TABLE "intelligence"."communication_consents" ADD CONSTRAINT "communication_consents_status_chk" CHECK (status in ('OPTED_IN','OPTED_OUT'));--> statement-breakpoint
ALTER TABLE "intelligence"."communication_consents" ADD CONSTRAINT "communication_consents_version_chk" CHECK (version >= 1);--> statement-breakpoint
ALTER TABLE "intelligence"."availability_interests" ADD CONSTRAINT "availability_interests_campaign_id_availability_campaigns_id_fk" FOREIGN KEY ("campaign_id") REFERENCES "intelligence"."availability_campaigns"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."availability_interests" ADD CONSTRAINT "availability_interests_recipient_id_availability_campaign_recipients_id_fk" FOREIGN KEY ("recipient_id") REFERENCES "intelligence"."availability_campaign_recipients"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."availability_interests" ADD CONSTRAINT "availability_interests_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."availability_interests" ADD CONSTRAINT "availability_interests_product_id_products_id_fk" FOREIGN KEY ("product_id") REFERENCES "marketplace"."products"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "availability_interests_recipient_msg_label_uq" ON "intelligence"."availability_interests" USING btree ("recipient_id","message_ref","product_label");--> statement-breakpoint
CREATE INDEX "availability_interests_campaign_idx" ON "intelligence"."availability_interests" USING btree ("campaign_id");--> statement-breakpoint
CREATE INDEX "availability_interests_user_idx" ON "intelligence"."availability_interests" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "availability_interests_product_idx" ON "intelligence"."availability_interests" USING btree ("product_id");--> statement-breakpoint
ALTER TABLE "intelligence"."availability_interests" ADD CONSTRAINT "availability_interests_kind_chk" CHECK (kind in ('INTEREST','PRICE_REQUEST','INFO_REQUEST','PREORDER_STARTED','DECLINED'));--> statement-breakpoint
ALTER TABLE "intelligence"."availability_interests" ADD CONSTRAINT "availability_interests_status_chk" CHECK (status in ('OPEN','PRODUCER_NOTIFIED','CLOSED'));
