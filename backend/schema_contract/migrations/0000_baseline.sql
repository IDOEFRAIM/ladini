-- Baseline Drizzle (2026-09-21) — repart d'une base vide ; remplace l'ancien historique 0000..0007.
-- Prérequis créés ici (drizzle-kit ne les émet pas) : schémas métier + extension pg_trgm
-- (index GIN trigram de recherche floue). Rien n'est créé par le backend Python au démarrage.
CREATE EXTENSION IF NOT EXISTS pg_trgm;--> statement-breakpoint
CREATE SCHEMA IF NOT EXISTS "auth";--> statement-breakpoint
CREATE SCHEMA IF NOT EXISTS "governance";--> statement-breakpoint
CREATE SCHEMA IF NOT EXISTS "marketplace";--> statement-breakpoint
CREATE SCHEMA IF NOT EXISTS "intelligence";--> statement-breakpoint
CREATE TABLE "auth"."accounts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"type" text NOT NULL,
	"provider" text NOT NULL,
	"provider_account_id" text NOT NULL,
	"refresh_token" text,
	"access_token" text,
	"expires_at" integer,
	"token_type" text,
	"scope" text,
	"id_token" text,
	"session_state" text
);
--> statement-breakpoint
CREATE TABLE "auth"."sessions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"session_token" text NOT NULL,
	"user_id" uuid NOT NULL,
	"expires" timestamp NOT NULL,
	CONSTRAINT "sessions_session_token_unique" UNIQUE("session_token")
);
--> statement-breakpoint
CREATE TABLE "auth"."users" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text,
	"email" text,
	"email_verified" timestamp,
	"image" text,
	"password" text,
	"phone" text,
	"whatsapp_enabled" boolean DEFAULT true,
	"latitude" double precision,
	"longitude" double precision,
	"location_updated_at" timestamp,
	"cnib_number" text,
	"role" text DEFAULT 'USER' NOT NULL,
	"identity_verified" boolean DEFAULT false,
	"zone_id" uuid,
	"onboarding_completed" boolean DEFAULT false NOT NULL,
	"account_status" text DEFAULT 'ACTIVE' NOT NULL,
	"blocked_reason" text,
	"blocked_at" timestamp,
	"deleted_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "users_email_unique" UNIQUE("email"),
	CONSTRAINT "users_phone_unique" UNIQUE("phone"),
	CONSTRAINT "users_cnib_number_unique" UNIQUE("cnib_number")
);
--> statement-breakpoint
CREATE TABLE "governance"."categories" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "categories_name_unique" UNIQUE("name")
);
--> statement-breakpoint
CREATE TABLE "governance"."climatic_regions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "climatic_regions_name_unique" UNIQUE("name")
);
--> statement-breakpoint
CREATE TABLE "governance"."organizations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"type" text NOT NULL,
	"tax_id" text,
	"description" text,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "organizations_tax_id_unique" UNIQUE("tax_id")
);
--> statement-breakpoint
CREATE TABLE "governance"."prohibited_terms" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"term" text NOT NULL,
	"category" text DEFAULT 'ILLICIT' NOT NULL,
	"severity" text DEFAULT 'HIGH' NOT NULL,
	"is_active" boolean DEFAULT true NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "prohibited_terms_term_unique" UNIQUE("term")
);
--> statement-breakpoint
CREATE TABLE "governance"."role_definitions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"permissions" text[] DEFAULT '{}'::text[] NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "role_definitions_name_unique" UNIQUE("name")
);
--> statement-breakpoint
CREATE TABLE "governance"."standard_prices" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"sub_category_id" uuid NOT NULL,
	"zone_id" uuid NOT NULL,
	"price_per_unit" double precision NOT NULL,
	"unit" text DEFAULT 'KG' NOT NULL,
	"updated_by_id" uuid NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "governance"."sub_categories" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"category_id" uuid NOT NULL,
	"name" text NOT NULL,
	"blocked_zone_ids" text[] DEFAULT '{}'::text[] NOT NULL,
	"minimum_order_quantity" numeric(14, 3),
	"minimum_order_unit" text,
	"priority_unit" text,
	"allowed_units" text[],
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "governance"."user_organizations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"organization_id" uuid NOT NULL,
	"role" text DEFAULT 'FIELD_AGENT' NOT NULL,
	"role_id" uuid,
	"managed_zone_id" uuid
);
--> statement-breakpoint
CREATE TABLE "governance"."work_zones" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"organization_id" uuid NOT NULL,
	"zone_id" uuid NOT NULL,
	"manager_id" uuid,
	"role" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "governance"."zones" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"code" text NOT NULL,
	"climatic_region_id" uuid NOT NULL,
	"organization_id" uuid,
	"parent_id" uuid,
	"path" text,
	"depth" integer DEFAULT 0 NOT NULL,
	"latitude" double precision,
	"longitude" double precision,
	"is_active" boolean DEFAULT true NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "zones_name_unique" UNIQUE("name"),
	CONSTRAINT "zones_code_unique" UNIQUE("code")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."auctions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"buyer_id" uuid NOT NULL,
	"sub_category_id" uuid NOT NULL,
	"description" text,
	"quantity" numeric(14, 3) NOT NULL,
	"unit" text DEFAULT 'TONNE' NOT NULL,
	"max_price_per_unit" numeric(12, 2) NOT NULL,
	"incoterm" text DEFAULT 'DDP' NOT NULL,
	"delivery_location" text NOT NULL,
	"delivery_deadline" timestamp NOT NULL,
	"quality_grading" text,
	"required_certifications" text[] DEFAULT '{}'::text[] NOT NULL,
	"preferred_packaging" text,
	"deadline" timestamp NOT NULL,
	"auto_extend" boolean DEFAULT true NOT NULL,
	"escrow_status" text DEFAULT 'NONE' NOT NULL,
	"status" text DEFAULT 'OPEN' NOT NULL,
	"winner_bid_id" uuid,
	"escrow_wallet_id" uuid,
	"awarded_at" timestamp,
	"cancelled_at" timestamp,
	"cancellation_reason" text,
	"target_zone_id" uuid,
	"version" integer DEFAULT 0 NOT NULL,
	"images" text[] DEFAULT '{}'::text[] NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."bids" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"auction_id" uuid NOT NULL,
	"producer_id" uuid NOT NULL,
	"offered_price" numeric(12, 2) NOT NULL,
	"linked_stock_id" uuid,
	"is_winner" boolean DEFAULT false NOT NULL,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"message" text,
	"notified_at" timestamp,
	"valid_until" timestamp,
	"estimated_delivery_date" timestamp,
	"images" text[] DEFAULT '{}'::text[] NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."buyer_profiles" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"buyer_type_id" uuid,
	"establishment_name" text,
	"default_delivery_address" text,
	"is_verified" boolean DEFAULT false NOT NULL,
	"trust_badge" text,
	"rating" double precision,
	"reviews_count" integer DEFAULT 0 NOT NULL,
	"company_registration_number" text,
	"verified_at" timestamp,
	"verified_by_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "buyer_profiles_user_id_unique" UNIQUE("user_id")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."buyer_types" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "buyer_types_name_unique" UNIQUE("name")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."clients" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"phone" text NOT NULL,
	"email" text,
	"location" text,
	"total_orders" integer DEFAULT 0 NOT NULL,
	"total_spent" double precision DEFAULT 0 NOT NULL,
	"last_order_date" timestamp,
	"tax_id" text,
	"prefered_payement_method" jsonb,
	"producer_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."deliveries" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"order_id" uuid NOT NULL,
	"delivery_agent_id" uuid,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"delivery_code" text,
	"origin_gps_lat" double precision,
	"origin_gps_lng" double precision,
	"destination_gps_lat" double precision,
	"destination_gps_lng" double precision,
	"destination_desc" text,
	"estimated_distance_km" double precision,
	"actual_distance_km" double precision,
	"shipping_condition" text,
	"proof_of_delivery_url" text,
	"assigned_at" timestamp,
	"picked_up_at" timestamp,
	"delivered_at" timestamp,
	"failed_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."delivery_agents" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"vehicle_type" text,
	"license_number" text,
	"zone_id" uuid,
	"status" text DEFAULT 'OFFLINE' NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "delivery_agents_user_id_unique" UNIQUE("user_id")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."expenses" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"farm_id" uuid NOT NULL,
	"label" text NOT NULL,
	"amount" numeric(14, 2) NOT NULL,
	"category" text DEFAULT 'OTHER' NOT NULL,
	"date" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."farms" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"location" text,
	"size" double precision,
	"zone_id" uuid,
	"producer_id" uuid NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."market_offers" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"producer_id" uuid NOT NULL,
	"farm_id" uuid,
	"sub_category_id" uuid,
	"product_label" text NOT NULL,
	"production_type" text DEFAULT 'CROP' NOT NULL,
	"species" text,
	"breed" text,
	"unit" text DEFAULT 'KG' NOT NULL,
	"price_per_unit" numeric(12, 2),
	"available_quantity" numeric(14, 3) DEFAULT '0' NOT NULL,
	"reserved_quantity" numeric(14, 3) DEFAULT '0' NOT NULL,
	"current_stock" numeric(14, 3) DEFAULT '0' NOT NULL,
	"is_public" boolean DEFAULT false NOT NULL,
	"preorder_enabled" boolean DEFAULT false NOT NULL,
	"estimated_available_at" timestamp,
	"expected_harvest_date" timestamp,
	"status" text DEFAULT 'DRAFT' NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."order_disputes" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"order_id" uuid NOT NULL,
	"escrow_wallet_id" uuid,
	"raised_by_id" uuid NOT NULL,
	"reason_category" text NOT NULL,
	"description" text NOT NULL,
	"evidence_images" text[] DEFAULT '{}'::text[] NOT NULL,
	"requested_solution" text NOT NULL,
	"disputed_amount" numeric(14, 2) DEFAULT '0' NOT NULL,
	"escrow_payout_status" text DEFAULT 'HELD' NOT NULL,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"resolution_notes" text,
	"resolved_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."order_items" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"order_id" uuid NOT NULL,
	"product_id" uuid NOT NULL,
	"quantity" numeric(14, 3) NOT NULL,
	"price_at_sale" numeric(12, 2) NOT NULL,
	"tier_id" text,
	"base_unit_quantity" numeric(14, 3)
);
--> statement-breakpoint
CREATE TABLE "marketplace"."order_reminders" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"order_id" uuid NOT NULL,
	"type" text NOT NULL,
	"channel" text DEFAULT 'WHATSAPP' NOT NULL,
	"status" text DEFAULT 'SCHEDULED' NOT NULL,
	"scheduled_at" timestamp NOT NULL,
	"sent_at" timestamp,
	"attempts" integer DEFAULT 0 NOT NULL,
	"last_error" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."order_status_history" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"order_id" uuid NOT NULL,
	"status_type" text NOT NULL,
	"from_status" text,
	"to_status" text NOT NULL,
	"actor_id" uuid,
	"note" text,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."orders" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"buyer_id" uuid,
	"client_id" uuid,
	"organization_id" uuid,
	"zone_id" uuid,
	"customer_name" text,
	"customer_phone" text,
	"payment_method" text DEFAULT 'CASH' NOT NULL,
	"payment_status" text DEFAULT 'PENDING' NOT NULL,
	"city" text,
	"gps_lat" real,
	"gps_lng" real,
	"delivery_desc" text,
	"audio_url" text,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"delivery_status" text DEFAULT 'PENDING' NOT NULL,
	"source" text DEFAULT 'APP' NOT NULL,
	"whatsapp_id" text,
	"total_amount" numeric(14, 2) NOT NULL,
	"is_agent_order" boolean DEFAULT false NOT NULL,
	"delivery_date" timestamp,
	"subtotal" numeric(14, 2) DEFAULT '0' NOT NULL,
	"tax_amount" numeric(14, 2) DEFAULT '0' NOT NULL,
	"currency" text DEFAULT 'XOF' NOT NULL,
	"delivery_fee" numeric(14, 2) DEFAULT '0' NOT NULL,
	"cancellation_role" text,
	"escrow_wallet_id" uuid,
	"paydunya_invoice_token" text,
	"delivery_otp" text,
	"payment_expires_at" timestamp,
	"locked_amount" numeric(14, 2),
	"delivery_otp_attempts" integer DEFAULT 0 NOT NULL,
	"delivery_otp_locked_until" timestamp,
	"checkout_group_id" uuid,
	"auction_id" uuid,
	"winning_bid_id" uuid,
	"order_type" text DEFAULT 'STANDARD' NOT NULL,
	"market_offer_id" uuid,
	"expected_fulfillment_date" timestamp,
	"preorder_converted_at" timestamp,
	"confirmed_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."payments" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"order_id" uuid NOT NULL,
	"amount" numeric(14, 2) NOT NULL,
	"currency" text DEFAULT 'XOF' NOT NULL,
	"method" text DEFAULT 'CASH' NOT NULL,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"provider" text,
	"provider_ref" text,
	"escrow_wallet_id" uuid,
	"failure_reason" text,
	"authorized_at" timestamp,
	"captured_at" timestamp,
	"refunded_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."producers" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"organization_id" uuid,
	"business_name" text,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"is_certified" boolean DEFAULT false NOT NULL,
	"zone_id" uuid,
	"region" text,
	"province" text,
	"commune" text,
	"logo_url" text,
	"phone_number" text,
	"rating" integer,
	"reviews_count" integer DEFAULT 0 NOT NULL,
	"company_registration_number" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "producers_user_id_unique" UNIQUE("user_id")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."products" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"short_code" text,
	"name" text DEFAULT 'Produit' NOT NULL,
	"category_label" text NOT NULL,
	"sub_category_id" uuid,
	"local_names" jsonb,
	"description" text,
	"price" numeric(12, 2) NOT NULL,
	"unit" text DEFAULT 'KG' NOT NULL,
	"quantity_for_sale" numeric(14, 3) DEFAULT '0' NOT NULL,
	"pricing_tiers" jsonb,
	"images" text[] DEFAULT '{}'::text[] NOT NULL,
	"audio_url" text,
	"quality_class" text,
	"min_order_quality" text,
	"packaging_type" text,
	"harvest_date" timestamp,
	"is_available" boolean DEFAULT true NOT NULL,
	"producer_id" uuid NOT NULL,
	"verified_at" timestamp,
	"verified_by_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "products_short_code_unique" UNIQUE("short_code")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."seed_allocations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"organization_id" uuid NOT NULL,
	"zone_id" uuid NOT NULL,
	"seed_type" text NOT NULL,
	"total_quantity" numeric(14, 3) NOT NULL,
	"remaining_quantity" numeric(14, 3) NOT NULL,
	"unit" text DEFAULT 'KG' NOT NULL,
	"allocated_by_id" uuid NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."seed_distribution_attempts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"distribution_id" uuid NOT NULL,
	"actor_id" uuid NOT NULL,
	"attempt_type" text NOT NULL,
	"success" boolean NOT NULL,
	"ip_address" text,
	"metadata" jsonb,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."seed_distributions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"allocation_id" uuid NOT NULL,
	"producer_id" uuid NOT NULL,
	"agent_id" uuid NOT NULL,
	"organization_id" uuid NOT NULL,
	"zone_id" uuid NOT NULL,
	"quantity" numeric(14, 3) NOT NULL,
	"cnib_provided" text,
	"verification_code_hash" text,
	"verification_code_expires_at" timestamp,
	"verification_channel" text DEFAULT 'IN_APP' NOT NULL,
	"attempts_count" integer DEFAULT 0 NOT NULL,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"metadata" jsonb,
	"receipt_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."stock_movements" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"stock_id" uuid NOT NULL,
	"type" text NOT NULL,
	"quantity" numeric(14, 3) NOT NULL,
	"reason" text,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."stocks" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"farm_id" uuid,
	"warehouse_id" uuid,
	"organization_id" uuid,
	"verified_by_id" uuid,
	"item_name" text NOT NULL,
	"quantity" numeric(14, 3) DEFAULT '0' NOT NULL,
	"unit" text DEFAULT 'KG' NOT NULL,
	"type" text DEFAULT 'HARVEST' NOT NULL,
	"verified_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."warehouses" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"type" text NOT NULL,
	"capacity" double precision,
	"location" text,
	"zone_id" uuid,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."agent_actions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"agent_name" text NOT NULL,
	"action_type" text NOT NULL,
	"batch_id" text,
	"payload" jsonb,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"priority" text DEFAULT 'MEDIUM' NOT NULL,
	"order_id" uuid,
	"user_id" uuid,
	"audit_trail_id" text,
	"ai_reasoning" text,
	"admin_notes" text,
	"validated_by_id" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."audit_logs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"actor_id" uuid NOT NULL,
	"action" text NOT NULL,
	"entity_id" text NOT NULL,
	"entity_type" text NOT NULL,
	"old_value" jsonb,
	"new_value" jsonb,
	"ip_address" text,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."conversations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"query" text NOT NULL,
	"response" text,
	"agent_type" text,
	"zone_id" uuid,
	"mode" text DEFAULT 'text' NOT NULL,
	"audio_url" text,
	"is_waiting_for_input" boolean DEFAULT false NOT NULL,
	"missing_slots" jsonb,
	"execution_path" jsonb,
	"confidence_score" double precision,
	"user_intent" text,
	"needs_follow_up" boolean DEFAULT false NOT NULL,
	"total_tokens_used" integer DEFAULT 0 NOT NULL,
	"response_time_ms" integer,
	"audit_trail_id" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."demand_signals" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"normalized_term" text NOT NULL,
	"raw_query" text NOT NULL,
	"phone" text,
	"user_id" uuid,
	"zone_id" uuid,
	"occurrences" integer DEFAULT 1 NOT NULL,
	"resolved" boolean DEFAULT false NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."moderation_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid,
	"phone" text NOT NULL,
	"kind" text NOT NULL,
	"matched_term" text,
	"excerpt" text,
	"action_taken" text,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."notification_outbox" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"solicitation_id" uuid,
	"channel" text NOT NULL,
	"recipient_user_id" uuid,
	"recipient_phone" text,
	"template_key" text NOT NULL,
	"payload" jsonb NOT NULL,
	"dedupe_key" text NOT NULL,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"attempts" integer DEFAULT 0 NOT NULL,
	"max_attempts" integer DEFAULT 5 NOT NULL,
	"next_attempt_at" timestamp DEFAULT now() NOT NULL,
	"last_error" text,
	"sent_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."solicitations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"kind" text NOT NULL,
	"auction_id" uuid,
	"market_offer_id" uuid,
	"target_producer_id" uuid,
	"target_buyer_id" uuid,
	"sub_category_id" uuid,
	"zone_id" uuid,
	"status" text DEFAULT 'PENDING' NOT NULL,
	"notified_at" timestamp,
	"responded_at" timestamp,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "intelligence"."trust_scores" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"global_score" double precision DEFAULT 0 NOT NULL,
	"reliability_index" double precision DEFAULT 0 NOT NULL,
	"quality_index" double precision DEFAULT 0 NOT NULL,
	"compliance_index" double precision DEFAULT 0 NOT NULL,
	"resilience_bonus" double precision DEFAULT 0 NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "trust_scores_user_id_unique" UNIQUE("user_id")
);
--> statement-breakpoint
CREATE TABLE "agri_workspaces" (
	"workspace_id" text PRIMARY KEY NOT NULL,
	"workspace_type" text DEFAULT 'producer' NOT NULL,
	"active_agent" text DEFAULT 'market' NOT NULL,
	"active_goal" text DEFAULT '' NOT NULL,
	"active_form" text,
	"locked_agent" text,
	"metadata" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"langgraph_state" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"updated_at" double precision DEFAULT 0 NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."mcp_idempotency_records" (
	"idempotency_key" text NOT NULL,
	"tool_name" text NOT NULL,
	"request_hash" text NOT NULL,
	"status" text NOT NULL,
	"external_result" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "mcp_idempotency_records_idempotency_key_tool_name_pk" PRIMARY KEY("idempotency_key","tool_name")
);
--> statement-breakpoint
CREATE TABLE "marketplace"."preorder_drafts" (
	"draft_id" text PRIMARY KEY NOT NULL,
	"conversation_id" text NOT NULL,
	"version" integer NOT NULL,
	"status" text NOT NULL,
	"payload" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"order_id" text
);
--> statement-breakpoint
CREATE TABLE "marketplace"."procurement_drafts" (
	"draft_id" text PRIMARY KEY NOT NULL,
	"conversation_id" text NOT NULL,
	"version" integer NOT NULL,
	"status" text NOT NULL,
	"payload" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "marketplace"."sales_publish_drafts" (
	"draft_id" text PRIMARY KEY NOT NULL,
	"conversation_id" text NOT NULL,
	"version" integer NOT NULL,
	"status" text NOT NULL,
	"payload" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "auth"."accounts" ADD CONSTRAINT "accounts_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "auth"."sessions" ADD CONSTRAINT "sessions_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "auth"."users" ADD CONSTRAINT "users_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."standard_prices" ADD CONSTRAINT "standard_prices_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."standard_prices" ADD CONSTRAINT "standard_prices_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."standard_prices" ADD CONSTRAINT "standard_prices_updated_by_id_users_id_fk" FOREIGN KEY ("updated_by_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."sub_categories" ADD CONSTRAINT "sub_categories_category_id_categories_id_fk" FOREIGN KEY ("category_id") REFERENCES "governance"."categories"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."user_organizations" ADD CONSTRAINT "user_organizations_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."user_organizations" ADD CONSTRAINT "user_organizations_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."user_organizations" ADD CONSTRAINT "user_organizations_role_id_role_definitions_id_fk" FOREIGN KEY ("role_id") REFERENCES "governance"."role_definitions"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."user_organizations" ADD CONSTRAINT "user_organizations_managed_zone_id_zones_id_fk" FOREIGN KEY ("managed_zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."work_zones" ADD CONSTRAINT "work_zones_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."work_zones" ADD CONSTRAINT "work_zones_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."work_zones" ADD CONSTRAINT "work_zones_manager_id_users_id_fk" FOREIGN KEY ("manager_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."zones" ADD CONSTRAINT "zones_climatic_region_id_climatic_regions_id_fk" FOREIGN KEY ("climatic_region_id") REFERENCES "governance"."climatic_regions"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."zones" ADD CONSTRAINT "zones_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "governance"."zones" ADD CONSTRAINT "zones_parent_id_zones_id_fk" FOREIGN KEY ("parent_id") REFERENCES "governance"."zones"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."auctions" ADD CONSTRAINT "auctions_buyer_id_buyer_profiles_id_fk" FOREIGN KEY ("buyer_id") REFERENCES "marketplace"."buyer_profiles"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."auctions" ADD CONSTRAINT "auctions_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."auctions" ADD CONSTRAINT "auctions_winner_bid_id_bids_id_fk" FOREIGN KEY ("winner_bid_id") REFERENCES "marketplace"."bids"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."auctions" ADD CONSTRAINT "auctions_target_zone_id_zones_id_fk" FOREIGN KEY ("target_zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD CONSTRAINT "bids_auction_id_auctions_id_fk" FOREIGN KEY ("auction_id") REFERENCES "marketplace"."auctions"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD CONSTRAINT "bids_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."bids" ADD CONSTRAINT "bids_linked_stock_id_stocks_id_fk" FOREIGN KEY ("linked_stock_id") REFERENCES "marketplace"."stocks"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."buyer_profiles" ADD CONSTRAINT "buyer_profiles_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."buyer_profiles" ADD CONSTRAINT "buyer_profiles_buyer_type_id_buyer_types_id_fk" FOREIGN KEY ("buyer_type_id") REFERENCES "marketplace"."buyer_types"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."buyer_profiles" ADD CONSTRAINT "buyer_profiles_verified_by_id_users_id_fk" FOREIGN KEY ("verified_by_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."clients" ADD CONSTRAINT "clients_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."deliveries" ADD CONSTRAINT "deliveries_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."deliveries" ADD CONSTRAINT "deliveries_delivery_agent_id_delivery_agents_id_fk" FOREIGN KEY ("delivery_agent_id") REFERENCES "marketplace"."delivery_agents"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."delivery_agents" ADD CONSTRAINT "delivery_agents_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."delivery_agents" ADD CONSTRAINT "delivery_agents_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."expenses" ADD CONSTRAINT "expenses_farm_id_farms_id_fk" FOREIGN KEY ("farm_id") REFERENCES "marketplace"."farms"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."farms" ADD CONSTRAINT "farms_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."farms" ADD CONSTRAINT "farms_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."market_offers" ADD CONSTRAINT "market_offers_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."market_offers" ADD CONSTRAINT "market_offers_farm_id_farms_id_fk" FOREIGN KEY ("farm_id") REFERENCES "marketplace"."farms"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."market_offers" ADD CONSTRAINT "market_offers_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."order_disputes" ADD CONSTRAINT "order_disputes_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."order_disputes" ADD CONSTRAINT "order_disputes_raised_by_id_users_id_fk" FOREIGN KEY ("raised_by_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD CONSTRAINT "order_items_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."order_items" ADD CONSTRAINT "order_items_product_id_products_id_fk" FOREIGN KEY ("product_id") REFERENCES "marketplace"."products"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."order_reminders" ADD CONSTRAINT "order_reminders_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."order_status_history" ADD CONSTRAINT "order_status_history_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_buyer_id_buyer_profiles_id_fk" FOREIGN KEY ("buyer_id") REFERENCES "marketplace"."buyer_profiles"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_client_id_clients_id_fk" FOREIGN KEY ("client_id") REFERENCES "marketplace"."clients"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_auction_id_auctions_id_fk" FOREIGN KEY ("auction_id") REFERENCES "marketplace"."auctions"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_winning_bid_id_bids_id_fk" FOREIGN KEY ("winning_bid_id") REFERENCES "marketplace"."bids"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."orders" ADD CONSTRAINT "orders_market_offer_id_market_offers_id_fk" FOREIGN KEY ("market_offer_id") REFERENCES "marketplace"."market_offers"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."payments" ADD CONSTRAINT "payments_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."producers" ADD CONSTRAINT "producers_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."producers" ADD CONSTRAINT "producers_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."producers" ADD CONSTRAINT "producers_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."products" ADD CONSTRAINT "products_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."products" ADD CONSTRAINT "products_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."products" ADD CONSTRAINT "products_verified_by_id_users_id_fk" FOREIGN KEY ("verified_by_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_allocations" ADD CONSTRAINT "seed_allocations_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_allocations" ADD CONSTRAINT "seed_allocations_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_allocations" ADD CONSTRAINT "seed_allocations_allocated_by_id_users_id_fk" FOREIGN KEY ("allocated_by_id") REFERENCES "auth"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distribution_attempts" ADD CONSTRAINT "seed_distribution_attempts_distribution_id_seed_distributions_id_fk" FOREIGN KEY ("distribution_id") REFERENCES "marketplace"."seed_distributions"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distribution_attempts" ADD CONSTRAINT "seed_distribution_attempts_actor_id_users_id_fk" FOREIGN KEY ("actor_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distributions" ADD CONSTRAINT "seed_distributions_allocation_id_seed_allocations_id_fk" FOREIGN KEY ("allocation_id") REFERENCES "marketplace"."seed_allocations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distributions" ADD CONSTRAINT "seed_distributions_producer_id_producers_id_fk" FOREIGN KEY ("producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distributions" ADD CONSTRAINT "seed_distributions_agent_id_users_id_fk" FOREIGN KEY ("agent_id") REFERENCES "auth"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distributions" ADD CONSTRAINT "seed_distributions_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."seed_distributions" ADD CONSTRAINT "seed_distributions_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."stock_movements" ADD CONSTRAINT "stock_movements_stock_id_stocks_id_fk" FOREIGN KEY ("stock_id") REFERENCES "marketplace"."stocks"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."stocks" ADD CONSTRAINT "stocks_farm_id_farms_id_fk" FOREIGN KEY ("farm_id") REFERENCES "marketplace"."farms"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."stocks" ADD CONSTRAINT "stocks_warehouse_id_warehouses_id_fk" FOREIGN KEY ("warehouse_id") REFERENCES "marketplace"."warehouses"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."stocks" ADD CONSTRAINT "stocks_organization_id_organizations_id_fk" FOREIGN KEY ("organization_id") REFERENCES "governance"."organizations"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."stocks" ADD CONSTRAINT "stocks_verified_by_id_users_id_fk" FOREIGN KEY ("verified_by_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "marketplace"."warehouses" ADD CONSTRAINT "warehouses_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."agent_actions" ADD CONSTRAINT "agent_actions_order_id_orders_id_fk" FOREIGN KEY ("order_id") REFERENCES "marketplace"."orders"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."agent_actions" ADD CONSTRAINT "agent_actions_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."audit_logs" ADD CONSTRAINT "audit_logs_actor_id_users_id_fk" FOREIGN KEY ("actor_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."conversations" ADD CONSTRAINT "conversations_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE restrict ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."conversations" ADD CONSTRAINT "conversations_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."demand_signals" ADD CONSTRAINT "demand_signals_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."demand_signals" ADD CONSTRAINT "demand_signals_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."moderation_events" ADD CONSTRAINT "moderation_events_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."notification_outbox" ADD CONSTRAINT "notification_outbox_solicitation_id_solicitations_id_fk" FOREIGN KEY ("solicitation_id") REFERENCES "intelligence"."solicitations"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."notification_outbox" ADD CONSTRAINT "notification_outbox_recipient_user_id_users_id_fk" FOREIGN KEY ("recipient_user_id") REFERENCES "auth"."users"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."solicitations" ADD CONSTRAINT "solicitations_auction_id_auctions_id_fk" FOREIGN KEY ("auction_id") REFERENCES "marketplace"."auctions"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."solicitations" ADD CONSTRAINT "solicitations_market_offer_id_market_offers_id_fk" FOREIGN KEY ("market_offer_id") REFERENCES "marketplace"."market_offers"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."solicitations" ADD CONSTRAINT "solicitations_target_producer_id_producers_id_fk" FOREIGN KEY ("target_producer_id") REFERENCES "marketplace"."producers"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."solicitations" ADD CONSTRAINT "solicitations_target_buyer_id_buyer_profiles_id_fk" FOREIGN KEY ("target_buyer_id") REFERENCES "marketplace"."buyer_profiles"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."solicitations" ADD CONSTRAINT "solicitations_sub_category_id_sub_categories_id_fk" FOREIGN KEY ("sub_category_id") REFERENCES "governance"."sub_categories"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."solicitations" ADD CONSTRAINT "solicitations_zone_id_zones_id_fk" FOREIGN KEY ("zone_id") REFERENCES "governance"."zones"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "intelligence"."trust_scores" ADD CONSTRAINT "trust_scores_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "accounts_provider_unique" ON "auth"."accounts" USING btree ("provider","provider_account_id");--> statement-breakpoint
CREATE INDEX "accounts_user_idx" ON "auth"."accounts" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "sessions_user_idx" ON "auth"."sessions" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "users_role_idx" ON "auth"."users" USING btree ("role");--> statement-breakpoint
CREATE INDEX "users_zone_idx" ON "auth"."users" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "users_created_idx" ON "auth"."users" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "users_account_status_idx" ON "auth"."users" USING btree ("account_status");--> statement-breakpoint
CREATE INDEX "prohibited_terms_active_idx" ON "governance"."prohibited_terms" USING btree ("is_active");--> statement-breakpoint
CREATE UNIQUE INDEX "standard_prices_sub_zone_unique" ON "governance"."standard_prices" USING btree ("sub_category_id","zone_id");--> statement-breakpoint
CREATE INDEX "standard_prices_zone_idx" ON "governance"."standard_prices" USING btree ("zone_id");--> statement-breakpoint
CREATE UNIQUE INDEX "sub_categories_cat_name_unique" ON "governance"."sub_categories" USING btree ("category_id","name");--> statement-breakpoint
CREATE INDEX "ix_subcategories_name_trgm" ON "governance"."sub_categories" USING gin ("name" gin_trgm_ops);--> statement-breakpoint
CREATE UNIQUE INDEX "user_org_unique" ON "governance"."user_organizations" USING btree ("user_id","organization_id");--> statement-breakpoint
CREATE INDEX "user_org_user_idx" ON "governance"."user_organizations" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "user_org_role_idx" ON "governance"."user_organizations" USING btree ("role_id");--> statement-breakpoint
CREATE INDEX "user_org_org_idx" ON "governance"."user_organizations" USING btree ("organization_id");--> statement-breakpoint
CREATE UNIQUE INDEX "work_zones_org_zone_unique" ON "governance"."work_zones" USING btree ("organization_id","zone_id");--> statement-breakpoint
CREATE INDEX "work_zones_org_idx" ON "governance"."work_zones" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "work_zones_zone_idx" ON "governance"."work_zones" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "zones_region_idx" ON "governance"."zones" USING btree ("climatic_region_id");--> statement-breakpoint
CREATE INDEX "zones_org_idx" ON "governance"."zones" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "zones_active_idx" ON "governance"."zones" USING btree ("is_active");--> statement-breakpoint
CREATE INDEX "zones_parent_idx" ON "governance"."zones" USING btree ("parent_id");--> statement-breakpoint
CREATE INDEX "zones_path_idx" ON "governance"."zones" USING btree ("path");--> statement-breakpoint
CREATE INDEX "ix_zones_name_trgm" ON "governance"."zones" USING gin ("name" gin_trgm_ops);--> statement-breakpoint
CREATE INDEX "auctions_status_idx" ON "marketplace"."auctions" USING btree ("status");--> statement-breakpoint
CREATE INDEX "auctions_subcategory_idx" ON "marketplace"."auctions" USING btree ("sub_category_id");--> statement-breakpoint
CREATE INDEX "auctions_buyer_idx" ON "marketplace"."auctions" USING btree ("buyer_id");--> statement-breakpoint
CREATE INDEX "auctions_escrow_status_idx" ON "marketplace"."auctions" USING btree ("escrow_status");--> statement-breakpoint
CREATE INDEX "auctions_zone_idx" ON "marketplace"."auctions" USING btree ("target_zone_id");--> statement-breakpoint
CREATE INDEX "auctions_deadline_idx" ON "marketplace"."auctions" USING btree ("deadline");--> statement-breakpoint
CREATE UNIQUE INDEX "bids_auction_producer_unique" ON "marketplace"."bids" USING btree ("auction_id","producer_id");--> statement-breakpoint
CREATE INDEX "bids_auction_idx" ON "marketplace"."bids" USING btree ("auction_id");--> statement-breakpoint
CREATE INDEX "bids_producer_idx" ON "marketplace"."bids" USING btree ("producer_id");--> statement-breakpoint
CREATE INDEX "bids_linked_stock_idx" ON "marketplace"."bids" USING btree ("linked_stock_id");--> statement-breakpoint
CREATE INDEX "bids_status_idx" ON "marketplace"."bids" USING btree ("status");--> statement-breakpoint
CREATE UNIQUE INDEX "bids_one_winner_per_auction_uq" ON "marketplace"."bids" USING btree ("auction_id") WHERE "marketplace"."bids"."is_winner" = true;--> statement-breakpoint
CREATE INDEX "buyer_profiles_user_idx" ON "marketplace"."buyer_profiles" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "buyer_profiles_type_idx" ON "marketplace"."buyer_profiles" USING btree ("buyer_type_id");--> statement-breakpoint
CREATE INDEX "buyer_profiles_verified_idx" ON "marketplace"."buyer_profiles" USING btree ("is_verified");--> statement-breakpoint
CREATE INDEX "clients_phone_idx" ON "marketplace"."clients" USING btree ("phone");--> statement-breakpoint
CREATE INDEX "clients_name_idx" ON "marketplace"."clients" USING btree ("name");--> statement-breakpoint
CREATE INDEX "clients_producer_idx" ON "marketplace"."clients" USING btree ("producer_id");--> statement-breakpoint
CREATE UNIQUE INDEX "deliveries_order_unique" ON "marketplace"."deliveries" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "deliveries_agent_idx" ON "marketplace"."deliveries" USING btree ("delivery_agent_id");--> statement-breakpoint
CREATE INDEX "deliveries_status_idx" ON "marketplace"."deliveries" USING btree ("status");--> statement-breakpoint
CREATE INDEX "deliveries_agent_status_idx" ON "marketplace"."deliveries" USING btree ("delivery_agent_id","status");--> statement-breakpoint
CREATE INDEX "delivery_agents_zone_idx" ON "marketplace"."delivery_agents" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "delivery_agents_status_idx" ON "marketplace"."delivery_agents" USING btree ("status");--> statement-breakpoint
CREATE INDEX "expenses_farm_idx" ON "marketplace"."expenses" USING btree ("farm_id");--> statement-breakpoint
CREATE INDEX "expenses_category_idx" ON "marketplace"."expenses" USING btree ("category");--> statement-breakpoint
CREATE INDEX "expenses_date_idx" ON "marketplace"."expenses" USING btree ("date");--> statement-breakpoint
CREATE INDEX "farms_producer_idx" ON "marketplace"."farms" USING btree ("producer_id");--> statement-breakpoint
CREATE INDEX "farms_zone_idx" ON "marketplace"."farms" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "market_offers_producer_idx" ON "marketplace"."market_offers" USING btree ("producer_id");--> statement-breakpoint
CREATE INDEX "market_offers_farm_idx" ON "marketplace"."market_offers" USING btree ("farm_id");--> statement-breakpoint
CREATE INDEX "market_offers_subcategory_idx" ON "marketplace"."market_offers" USING btree ("sub_category_id");--> statement-breakpoint
CREATE INDEX "market_offers_available_at_idx" ON "marketplace"."market_offers" USING btree ("estimated_available_at");--> statement-breakpoint
CREATE INDEX "market_offers_public_status_idx" ON "marketplace"."market_offers" USING btree ("is_public","status");--> statement-breakpoint
CREATE INDEX "market_offers_preorder_idx" ON "marketplace"."market_offers" USING btree ("preorder_enabled");--> statement-breakpoint
CREATE INDEX "ix_market_offers_label_trgm" ON "marketplace"."market_offers" USING gin ("product_label" gin_trgm_ops);--> statement-breakpoint
CREATE INDEX "order_disputes_order_idx" ON "marketplace"."order_disputes" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "order_disputes_status_idx" ON "marketplace"."order_disputes" USING btree ("status");--> statement-breakpoint
CREATE INDEX "order_disputes_raised_by_idx" ON "marketplace"."order_disputes" USING btree ("raised_by_id");--> statement-breakpoint
CREATE INDEX "order_disputes_escrow_idx" ON "marketplace"."order_disputes" USING btree ("escrow_wallet_id");--> statement-breakpoint
CREATE INDEX "order_items_order_idx" ON "marketplace"."order_items" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "order_items_product_idx" ON "marketplace"."order_items" USING btree ("product_id");--> statement-breakpoint
CREATE INDEX "order_reminders_order_idx" ON "marketplace"."order_reminders" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "order_reminders_due_idx" ON "marketplace"."order_reminders" USING btree ("status","scheduled_at");--> statement-breakpoint
CREATE INDEX "osh_order_idx" ON "marketplace"."order_status_history" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "osh_order_type_idx" ON "marketplace"."order_status_history" USING btree ("order_id","status_type");--> statement-breakpoint
CREATE INDEX "osh_created_idx" ON "marketplace"."order_status_history" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "orders_buyer_idx" ON "marketplace"."orders" USING btree ("buyer_id");--> statement-breakpoint
CREATE INDEX "orders_client_idx" ON "marketplace"."orders" USING btree ("client_id");--> statement-breakpoint
CREATE INDEX "orders_org_idx" ON "marketplace"."orders" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "orders_status_idx" ON "marketplace"."orders" USING btree ("status");--> statement-breakpoint
CREATE INDEX "orders_delivery_status_idx" ON "marketplace"."orders" USING btree ("delivery_status");--> statement-breakpoint
CREATE INDEX "orders_zone_idx" ON "marketplace"."orders" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "orders_created_idx" ON "marketplace"."orders" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "orders_phone_idx" ON "marketplace"."orders" USING btree ("customer_phone");--> statement-breakpoint
CREATE UNIQUE INDEX "orders_auction_unique" ON "marketplace"."orders" USING btree ("auction_id");--> statement-breakpoint
CREATE INDEX "orders_winning_bid_idx" ON "marketplace"."orders" USING btree ("winning_bid_id");--> statement-breakpoint
CREATE INDEX "orders_type_idx" ON "marketplace"."orders" USING btree ("order_type");--> statement-breakpoint
CREATE INDEX "orders_market_offer_idx" ON "marketplace"."orders" USING btree ("market_offer_id");--> statement-breakpoint
CREATE INDEX "orders_buyer_status_idx" ON "marketplace"."orders" USING btree ("buyer_id","status");--> statement-breakpoint
CREATE INDEX "orders_payment_status_idx" ON "marketplace"."orders" USING btree ("payment_status");--> statement-breakpoint
CREATE UNIQUE INDEX "ix_orders_paydunya_token" ON "marketplace"."orders" USING btree ("paydunya_invoice_token") WHERE "marketplace"."orders"."paydunya_invoice_token" IS NOT NULL;--> statement-breakpoint
CREATE INDEX "ix_orders_payment_expires_at" ON "marketplace"."orders" USING btree ("payment_expires_at") WHERE "marketplace"."orders"."payment_status" = 'PENDING';--> statement-breakpoint
CREATE INDEX "ix_orders_checkout_group" ON "marketplace"."orders" USING btree ("checkout_group_id") WHERE "marketplace"."orders"."checkout_group_id" IS NOT NULL;--> statement-breakpoint
CREATE INDEX "payments_order_idx" ON "marketplace"."payments" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "payments_status_idx" ON "marketplace"."payments" USING btree ("status");--> statement-breakpoint
CREATE UNIQUE INDEX "payments_provider_ref_unique" ON "marketplace"."payments" USING btree ("provider_ref");--> statement-breakpoint
CREATE INDEX "producers_status_idx" ON "marketplace"."producers" USING btree ("status");--> statement-breakpoint
CREATE INDEX "producers_org_idx" ON "marketplace"."producers" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "producers_zone_idx" ON "marketplace"."producers" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "products_producer_idx" ON "marketplace"."products" USING btree ("producer_id");--> statement-breakpoint
CREATE INDEX "products_category_idx" ON "marketplace"."products" USING btree ("category_label");--> statement-breakpoint
CREATE INDEX "products_subcategory_idx" ON "marketplace"."products" USING btree ("sub_category_id");--> statement-breakpoint
CREATE INDEX "products_price_idx" ON "marketplace"."products" USING btree ("price");--> statement-breakpoint
CREATE INDEX "products_created_idx" ON "marketplace"."products" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "products_verifier_idx" ON "marketplace"."products" USING btree ("verified_by_id");--> statement-breakpoint
CREATE INDEX "products_producer_available_idx" ON "marketplace"."products" USING btree ("producer_id","is_available");--> statement-breakpoint
CREATE INDEX "products_category_available_idx" ON "marketplace"."products" USING btree ("category_label","is_available");--> statement-breakpoint
CREATE INDEX "ix_products_name_trgm" ON "marketplace"."products" USING gin ("name" gin_trgm_ops);--> statement-breakpoint
CREATE INDEX "seed_allocations_org_idx" ON "marketplace"."seed_allocations" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "seed_allocations_zone_idx" ON "marketplace"."seed_allocations" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "seed_dist_attempts_distribution_idx" ON "marketplace"."seed_distribution_attempts" USING btree ("distribution_id");--> statement-breakpoint
CREATE INDEX "seed_distributions_allocation_idx" ON "marketplace"."seed_distributions" USING btree ("allocation_id");--> statement-breakpoint
CREATE INDEX "seed_distributions_producer_idx" ON "marketplace"."seed_distributions" USING btree ("producer_id");--> statement-breakpoint
CREATE INDEX "seed_distributions_agent_idx" ON "marketplace"."seed_distributions" USING btree ("agent_id");--> statement-breakpoint
CREATE INDEX "seed_distributions_org_idx" ON "marketplace"."seed_distributions" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "seed_distributions_status_idx" ON "marketplace"."seed_distributions" USING btree ("status");--> statement-breakpoint
CREATE INDEX "stock_movements_stock_idx" ON "marketplace"."stock_movements" USING btree ("stock_id");--> statement-breakpoint
CREATE INDEX "stock_movements_created_idx" ON "marketplace"."stock_movements" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "stocks_farm_idx" ON "marketplace"."stocks" USING btree ("farm_id");--> statement-breakpoint
CREATE INDEX "stocks_warehouse_idx" ON "marketplace"."stocks" USING btree ("warehouse_id");--> statement-breakpoint
CREATE INDEX "stocks_org_idx" ON "marketplace"."stocks" USING btree ("organization_id");--> statement-breakpoint
CREATE INDEX "stocks_type_idx" ON "marketplace"."stocks" USING btree ("type");--> statement-breakpoint
CREATE INDEX "stocks_verifier_idx" ON "marketplace"."stocks" USING btree ("verified_by_id");--> statement-breakpoint
CREATE INDEX "warehouses_zone_idx" ON "marketplace"."warehouses" USING btree ("zone_id");--> statement-breakpoint
CREATE INDEX "agent_actions_status_idx" ON "intelligence"."agent_actions" USING btree ("status");--> statement-breakpoint
CREATE INDEX "agent_actions_user_idx" ON "intelligence"."agent_actions" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "agent_actions_batch_idx" ON "intelligence"."agent_actions" USING btree ("batch_id");--> statement-breakpoint
CREATE INDEX "agent_actions_name_idx" ON "intelligence"."agent_actions" USING btree ("agent_name");--> statement-breakpoint
CREATE UNIQUE INDEX "agent_actions_order_unique" ON "intelligence"."agent_actions" USING btree ("order_id");--> statement-breakpoint
CREATE INDEX "agent_actions_queue_idx" ON "intelligence"."agent_actions" USING btree ("status","priority");--> statement-breakpoint
CREATE INDEX "audit_logs_actor_idx" ON "intelligence"."audit_logs" USING btree ("actor_id");--> statement-breakpoint
CREATE INDEX "audit_logs_entity_idx" ON "intelligence"."audit_logs" USING btree ("entity_id");--> statement-breakpoint
CREATE INDEX "audit_logs_entity_time_idx" ON "intelligence"."audit_logs" USING btree ("entity_type","created_at");--> statement-breakpoint
CREATE INDEX "conversations_user_idx" ON "intelligence"."conversations" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "conversations_agent_idx" ON "intelligence"."conversations" USING btree ("agent_type");--> statement-breakpoint
CREATE INDEX "conversations_created_idx" ON "intelligence"."conversations" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "conversations_followup_idx" ON "intelligence"."conversations" USING btree ("needs_follow_up");--> statement-breakpoint
CREATE UNIQUE INDEX "conversations_audit_unique" ON "intelligence"."conversations" USING btree ("audit_trail_id");--> statement-breakpoint
CREATE UNIQUE INDEX "demand_signals_term_unique" ON "intelligence"."demand_signals" USING btree ("normalized_term");--> statement-breakpoint
CREATE INDEX "demand_signals_occurrences_idx" ON "intelligence"."demand_signals" USING btree ("occurrences");--> statement-breakpoint
CREATE INDEX "moderation_events_phone_idx" ON "intelligence"."moderation_events" USING btree ("phone");--> statement-breakpoint
CREATE INDEX "moderation_events_user_idx" ON "intelligence"."moderation_events" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "moderation_events_kind_idx" ON "intelligence"."moderation_events" USING btree ("kind");--> statement-breakpoint
CREATE UNIQUE INDEX "outbox_dedupe_uq" ON "intelligence"."notification_outbox" USING btree ("dedupe_key");--> statement-breakpoint
CREATE INDEX "outbox_due_idx" ON "intelligence"."notification_outbox" USING btree ("status","next_attempt_at");--> statement-breakpoint
CREATE INDEX "outbox_solicitation_idx" ON "intelligence"."notification_outbox" USING btree ("solicitation_id");--> statement-breakpoint
CREATE UNIQUE INDEX "solicitations_auction_producer_uq" ON "intelligence"."solicitations" USING btree ("auction_id","target_producer_id");--> statement-breakpoint
CREATE UNIQUE INDEX "solicitations_offer_buyer_uq" ON "intelligence"."solicitations" USING btree ("market_offer_id","target_buyer_id");--> statement-breakpoint
CREATE INDEX "solicitations_kind_status_idx" ON "intelligence"."solicitations" USING btree ("kind","status");--> statement-breakpoint
CREATE INDEX "solicitations_auction_idx" ON "intelligence"."solicitations" USING btree ("auction_id");--> statement-breakpoint
CREATE INDEX "ix_preorder_drafts_conversation" ON "marketplace"."preorder_drafts" USING btree ("conversation_id");--> statement-breakpoint
CREATE INDEX "ix_preorder_drafts_order_id" ON "marketplace"."preorder_drafts" USING btree ("order_id") WHERE "marketplace"."preorder_drafts"."order_id" IS NOT NULL;--> statement-breakpoint
CREATE INDEX "ix_procurement_drafts_conversation" ON "marketplace"."procurement_drafts" USING btree ("conversation_id");--> statement-breakpoint
CREATE INDEX "ix_sales_publish_drafts_conversation" ON "marketplace"."sales_publish_drafts" USING btree ("conversation_id");