ALTER TABLE "auth"."users" ADD COLUMN "declared_location" text;--> statement-breakpoint
ALTER TABLE "auth"."users" ADD COLUMN "coverage_status" text DEFAULT 'COVERED' NOT NULL;--> statement-breakpoint
UPDATE "auth"."users" SET "coverage_status" = 'OUT_OF_COVERAGE' WHERE "zone_id" IS NULL;--> statement-breakpoint
ALTER TABLE "auth"."users" ADD CONSTRAINT "users_coverage_status_chk" CHECK ("auth"."users"."coverage_status" IN ('COVERED','NEARBY','OUT_OF_COVERAGE','WAITLIST'));
