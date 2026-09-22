CREATE TABLE "marketplace"."recurring_need_drafts" (
	"draft_id" text PRIMARY KEY NOT NULL,
	"conversation_id" text NOT NULL,
	"version" integer NOT NULL,
	"status" text NOT NULL,
	"payload" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX "ix_recurring_need_drafts_conversation" ON "marketplace"."recurring_need_drafts" USING btree ("conversation_id");