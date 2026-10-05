CREATE TABLE "governance"."platform_settings" (
	"key" text PRIMARY KEY NOT NULL,
	"value" jsonb NOT NULL,
	"version" integer DEFAULT 1 NOT NULL,
	"updated_by_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "governance"."platform_settings" ADD CONSTRAINT "platform_settings_updated_by_id_users_id_fk" FOREIGN KEY ("updated_by_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "platform_settings_updated_by_idx" ON "governance"."platform_settings" USING btree ("updated_by_id");--> statement-breakpoint
ALTER TABLE "governance"."platform_settings" ADD CONSTRAINT "platform_settings_version_chk" CHECK ("governance"."platform_settings"."version" >= 1);
