CREATE TABLE "intelligence"."agent_llm_calls" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"turn_id" uuid NOT NULL,
	"seq" integer NOT NULL,
	"kind" text NOT NULL,
	"provider" text,
	"model" text,
	"duration_ms" integer NOT NULL,
	"status" text NOT NULL,
	"is_fallback" boolean DEFAULT false NOT NULL,
	"fallback_from" text,
	"error_category" text,
	"prompt_tokens" integer,
	"completion_tokens" integer,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "agent_llm_calls_kind_chk" CHECK ("intelligence"."agent_llm_calls"."kind" IN ('INTERPRETER','RESPONSE','OTHER')),
	CONSTRAINT "agent_llm_calls_status_chk" CHECK ("intelligence"."agent_llm_calls"."status" IN ('SUCCESS','ERROR'))
);
--> statement-breakpoint
CREATE TABLE "intelligence"."agent_tool_calls" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"turn_id" uuid NOT NULL,
	"seq" integer NOT NULL,
	"tool_name" text NOT NULL,
	"tool_category" text DEFAULT 'UNKNOWN' NOT NULL,
	"started_at" timestamp with time zone NOT NULL,
	"completed_at" timestamp with time zone NOT NULL,
	"duration_ms" integer NOT NULL,
	"status" text NOT NULL,
	"error_code" text,
	"error_category" text,
	"trace_id" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "agent_tool_calls_status_chk" CHECK ("intelligence"."agent_tool_calls"."status" IN ('SUCCESS','ERROR','DENIED','REPLAY')),
	CONSTRAINT "agent_tool_calls_category_chk" CHECK ("intelligence"."agent_tool_calls"."tool_category" IN ('READ','WRITE','UNKNOWN'))
);
--> statement-breakpoint
CREATE TABLE "intelligence"."agent_turns" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"conversation_id" uuid NOT NULL,
	"user_id" uuid,
	"phone_hash" text NOT NULL,
	"phone_last4" text,
	"channel" text DEFAULT 'WHATSAPP' NOT NULL,
	"user_role" text,
	"message_sid" text,
	"task_retries" integer DEFAULT 0 NOT NULL,
	"intent" text,
	"intent_confidence" double precision,
	"workflow" text,
	"workflow_step" text,
	"goal_status" text,
	"outcome" text NOT NULL,
	"started_at" timestamp with time zone NOT NULL,
	"completed_at" timestamp with time zone NOT NULL,
	"duration_ms" integer NOT NULL,
	"queue_duration_ms" integer,
	"db_query_count" integer DEFAULT 0 NOT NULL,
	"db_duration_ms" integer DEFAULT 0 NOT NULL,
	"db_max_query_ms" integer DEFAULT 0 NOT NULL,
	"redis_command_count" integer DEFAULT 0 NOT NULL,
	"redis_duration_ms" integer DEFAULT 0 NOT NULL,
	"mcp_call_count" integer DEFAULT 0 NOT NULL,
	"mcp_duration_ms" integer DEFAULT 0 NOT NULL,
	"llm_call_count" integer DEFAULT 0 NOT NULL,
	"llm_duration_ms" integer DEFAULT 0 NOT NULL,
	"intent_llm_duration_ms" integer DEFAULT 0 NOT NULL,
	"response_llm_duration_ms" integer DEFAULT 0 NOT NULL,
	"llm_fallback_count" integer DEFAULT 0 NOT NULL,
	"whatsapp_duration_ms" integer,
	"response_status" text NOT NULL,
	"error_code" text,
	"error_category" text,
	"trace_id" text,
	"user_message_excerpt" text,
	"agent_response_excerpt" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "agent_turns_outcome_chk" CHECK ("intelligence"."agent_turns"."outcome" IN ('COMPLETED','CLARIFICATION','WAITING_USER','BLOCKED','HUMAN_REQUIRED','FALLBACK','ERROR')),
	CONSTRAINT "agent_turns_response_status_chk" CHECK ("intelligence"."agent_turns"."response_status" IN ('SENT','FAILED','SKIPPED','DUPLICATE')),
	CONSTRAINT "agent_turns_channel_chk" CHECK ("intelligence"."agent_turns"."channel" IN ('WHATSAPP','WEBCHAT','API')),
	CONSTRAINT "agent_turns_error_category_chk" CHECK ("intelligence"."agent_turns"."error_category" IS NULL OR "intelligence"."agent_turns"."error_category" IN ('TOOL','LLM','DB','REDIS','WHATSAPP','TIMEOUT','VALIDATION','SECURITY','INTERNAL')),
	CONSTRAINT "agent_turns_duration_chk" CHECK ("intelligence"."agent_turns"."duration_ms" >= 0)
);
--> statement-breakpoint
ALTER TABLE "intelligence"."agent_llm_calls" ADD CONSTRAINT "agent_llm_calls_turn_id_agent_turns_id_fk" FOREIGN KEY ("turn_id") REFERENCES "intelligence"."agent_turns"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."agent_tool_calls" ADD CONSTRAINT "agent_tool_calls_turn_id_agent_turns_id_fk" FOREIGN KEY ("turn_id") REFERENCES "intelligence"."agent_turns"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."agent_turns" ADD CONSTRAINT "agent_turns_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "agent_llm_calls_turn_idx" ON "intelligence"."agent_llm_calls" USING btree ("turn_id");--> statement-breakpoint
CREATE INDEX "agent_llm_calls_provider_idx" ON "intelligence"."agent_llm_calls" USING btree ("provider","created_at");--> statement-breakpoint
CREATE INDEX "agent_tool_calls_turn_idx" ON "intelligence"."agent_tool_calls" USING btree ("turn_id");--> statement-breakpoint
CREATE INDEX "agent_tool_calls_tool_idx" ON "intelligence"."agent_tool_calls" USING btree ("tool_name","created_at");--> statement-breakpoint
CREATE INDEX "agent_tool_calls_status_idx" ON "intelligence"."agent_tool_calls" USING btree ("status","created_at");--> statement-breakpoint
CREATE INDEX "agent_turns_created_idx" ON "intelligence"."agent_turns" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "agent_turns_conversation_idx" ON "intelligence"."agent_turns" USING btree ("conversation_id","started_at");--> statement-breakpoint
CREATE INDEX "agent_turns_user_idx" ON "intelligence"."agent_turns" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "agent_turns_phone_idx" ON "intelligence"."agent_turns" USING btree ("phone_hash","created_at");--> statement-breakpoint
CREATE INDEX "agent_turns_intent_idx" ON "intelligence"."agent_turns" USING btree ("intent","created_at");--> statement-breakpoint
CREATE INDEX "agent_turns_workflow_idx" ON "intelligence"."agent_turns" USING btree ("workflow","created_at");--> statement-breakpoint
CREATE INDEX "agent_turns_outcome_idx" ON "intelligence"."agent_turns" USING btree ("outcome","created_at");--> statement-breakpoint
CREATE INDEX "agent_turns_error_idx" ON "intelligence"."agent_turns" USING btree ("error_code","created_at") WHERE "intelligence"."agent_turns"."error_code" IS NOT NULL;--> statement-breakpoint
CREATE INDEX "agent_turns_trace_idx" ON "intelligence"."agent_turns" USING btree ("trace_id") WHERE "intelligence"."agent_turns"."trace_id" IS NOT NULL;--> statement-breakpoint
CREATE UNIQUE INDEX "agent_turns_sid_retry_uq" ON "intelligence"."agent_turns" USING btree ("message_sid","task_retries") WHERE "intelligence"."agent_turns"."message_sid" IS NOT NULL;