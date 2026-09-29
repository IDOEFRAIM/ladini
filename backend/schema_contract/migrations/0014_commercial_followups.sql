CREATE TABLE "intelligence"."commercial_followups" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"status" text DEFAULT 'NONE' NOT NULL,
	"assigned_commercial_id" uuid,
	"last_follow_up_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "commercial_followups_user_id_unique" UNIQUE("user_id")
);
--> statement-breakpoint
ALTER TABLE "intelligence"."commercial_followups" ADD CONSTRAINT "commercial_followups_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."commercial_followups" ADD CONSTRAINT "commercial_followups_assigned_commercial_id_users_id_fk" FOREIGN KEY ("assigned_commercial_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "commercial_followups_status_idx" ON "intelligence"."commercial_followups" USING btree ("status");--> statement-breakpoint
CREATE INDEX "commercial_followups_assigned_idx" ON "intelligence"."commercial_followups" USING btree ("assigned_commercial_id");--> statement-breakpoint
ALTER TABLE "intelligence"."commercial_followups" ADD CONSTRAINT "commercial_followups_status_chk" CHECK ("intelligence"."commercial_followups"."status" IN ('NONE','TO_FOLLOW_UP','FOLLOWED_UP','RESOLVED','NOT_INTERESTED'));