import { uuid, text, timestamp, index, uniqueIndex, integer, boolean, time, doublePrecision } from 'drizzle-orm/pg-core';
import { type InferModel } from 'drizzle-orm';
import { authSchema, roleEnum } from './_config';

export const users = authSchema.table('users', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name'),
  email: text('email').unique(),
  emailVerified: timestamp('email_verified'),
  image: text('image'),
  password: text('password'),
  phone: text('phone').unique(),
  whatsappEnabled: boolean('whatsapp_enabled').default(true), // Canaux de communication
  dailyAdviceTime: time('daily_advice_time').default('07:00'), // Heure idéale d'envoi
  latitude: doublePrecision('latitude'), // Pour la météo locale
  longitude: doublePrecision('longitude'), // Pour la météo locale
  cnibNumber: text('cnib_number').unique(),
  role: roleEnum('role').default('USER').notNull(),
  identityVerified: boolean('identity_verified').default(false),
  zoneId: uuid('zone_id'),
  deletedAt: timestamp('deleted_at'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('users_role_idx').on(t.role),
  index('users_zone_idx').on(t.zoneId),
  index('users_created_idx').on(t.createdAt),
]);

export const accounts = authSchema.table('accounts', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').notNull(),
  type: text('type').notNull(),
  provider: text('provider').notNull(),
  providerAccountId: text('provider_account_id').notNull(),
  refreshToken: text('refresh_token'),
  accessToken: text('access_token'),
  expiresAt: integer('expires_at'),
  tokenType: text('token_type'),
  scope: text('scope'),
  idToken: text('id_token'),
  sessionState: text('session_state'),
}, (t) => [
  uniqueIndex('accounts_provider_unique').on(t.provider, t.providerAccountId),
  index('accounts_user_idx').on(t.userId),
]);

export const sessions = authSchema.table('sessions', {
  id: uuid('id').primaryKey().defaultRandom(),
  sessionToken: text('session_token').unique().notNull(),
  userId: uuid('user_id').notNull(),
  expires: timestamp('expires').notNull(),
}, (t) => [
  index('sessions_user_idx').on(t.userId),
]);

export default {
  users,
  accounts,
  sessions,
};

//news part

export const userCultures = authSchema.table('user_cultures', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').references(() => users.id).notNull(),
  cultureName: text('culture_name').notNull(), // ex: "Sorgho"
  plantingDate: timestamp('planting_date').notNull(), // Date du semis
  isAssociation: boolean('is_association').default(false), // Pour les cultures associées
  status: text('status').default('active'), // active, récoltée
},(t) => [
  index('user_cultures_user_idx').on(t.userId), // Index important pour les performances du script quotidien
]);

export const dailyAdviceLogs = authSchema.table('daily_advice_logs', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').references(() => users.id).notNull(),
  cultureName: text('culture_name').notNull(),
  adviceContent: text('advice_content').notNull(), // Le conseil envoyé
  sentAt: timestamp('sent_at').defaultNow().notNull(),
  isUseful: boolean('is_useful'), // Feedback utilisateur (optionnel)
});

// Relations are defined centrally in ./relations.ts

// Type helpers
export type User = InferModel<typeof users>;
export type Account = InferModel<typeof accounts>;
export type Session = InferModel<typeof sessions>;      import {
  uuid,
  text,
  integer,
  doublePrecision,
  boolean,
  timestamp,
  jsonb,
  uniqueIndex,
  index,
} from 'drizzle-orm/pg-core';
import { sql, type InferModel } from 'drizzle-orm';
import { governanceSchema, organizationTypeEnum, orgRoleEnum, orgStatusEnum, unitEnum } from './_config';

export const organizations = governanceSchema.table('organizations', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').notNull(),
  type: organizationTypeEnum('type').notNull(),
  taxId: text('tax_id').unique(),
  description: text('description'),
  status: orgStatusEnum('status').default('PENDING').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
});

export const userOrganizations = governanceSchema.table('user_organizations', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').notNull(),
  organizationId: uuid('organization_id').notNull(),
  role: orgRoleEnum('role').default('FIELD_AGENT').notNull(),
  roleId: uuid('role_id'),
  managedZoneId: uuid('managed_zone_id'),
}, (t) => [
  uniqueIndex('user_org_unique').on(t.userId, t.organizationId),
]);

export const roleDefs = governanceSchema.table('role_definitions', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').unique().notNull(),
  description: text('description'),
  permissions: text('permissions').array().notNull().default(sql`'{}'::text[]`),
  createdAt: timestamp('created_at').defaultNow().notNull(),
});

export const climaticRegions = governanceSchema.table('climatic_regions', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').unique().notNull(),
  description: text('description'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
});

export const zones = governanceSchema.table('zones', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').unique().notNull(),
  code: text('code').unique().notNull(),
  climaticRegionId: uuid('climatic_region_id').notNull(),
  organizationId: uuid('organization_id'),
  parentId: uuid('parent_id'),
  path: text('path'),
  depth: integer('depth').default(0).notNull(),
  latitude: doublePrecision('latitude'),
  longitude: doublePrecision('longitude'),
  isActive: boolean('is_active').default(true).notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('zones_region_idx').on(t.climaticRegionId),
  index('zones_org_idx').on(t.organizationId),
  index('zones_active_idx').on(t.isActive),
  index('zones_parent_idx').on(t.parentId),
  index('zones_path_idx').on(t.path),
]);

export const workZones = governanceSchema.table('work_zones', {
  id: uuid('id').primaryKey().defaultRandom(),
  organizationId: uuid('organization_id').notNull(),
  zoneId: uuid('zone_id').notNull(),
  managerId: uuid('manager_id'),
  role: text('role'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  uniqueIndex('work_zones_org_zone_unique').on(t.organizationId, t.zoneId),
  index('work_zones_org_idx').on(t.organizationId),
  index('work_zones_zone_idx').on(t.zoneId),
]);

export const zoneMetrics = governanceSchema.table('zone_metrics', {
  id: uuid('id').primaryKey().defaultRandom(),
  zoneId: uuid('zone_id').notNull(),
  date: timestamp('date').defaultNow().notNull(),
  metricName: text('metric_name').notNull(),
  value: doublePrecision('value').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
  index('zone_metrics_composite_idx').on(t.zoneId, t.date, t.metricName),
]);

export const categories = governanceSchema.table('categories', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').unique().notNull(),
  description: text('description'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
});

export const subCategories = governanceSchema.table('sub_categories', {
  id: uuid('id').primaryKey().defaultRandom(),
  categoryId: uuid('category_id').notNull(),
  name: text('name').notNull(),
  blockedZoneIds: text('blocked_zone_ids').array().notNull().default(sql`'{}'::text[]`),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  uniqueIndex('sub_categories_cat_name_unique').on(t.categoryId, t.name),
]);

export const standardPrices = governanceSchema.table('standard_prices', {
  id: uuid('id').primaryKey().defaultRandom(),
  subCategoryId: uuid('sub_category_id').notNull(),
  zoneId: uuid('zone_id').notNull(),
  pricePerUnit: doublePrecision('price_per_unit').notNull(),
  unit: unitEnum('unit').default('KG').notNull(),
  updatedById: uuid('updated_by_id').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  uniqueIndex('standard_prices_sub_zone_unique').on(t.subCategoryId, t.zoneId),
  index('standard_prices_zone_idx').on(t.zoneId),
]);

export const zoneSettings = governanceSchema.table('zone_settings', {
  id: uuid('id').primaryKey().defaultRandom(),
  zoneId: uuid('zone_id').notNull(),
  key: text('key').notNull(),
  value: jsonb('value').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  uniqueIndex('zone_settings_zone_key_unique').on(t.zoneId, t.key),
  index('zone_settings_zone_idx').on(t.zoneId),
]);

export const overlayLayers = governanceSchema.table('overlay_layers', {
  id: uuid('id').primaryKey().defaultRandom(),
  zoneId: uuid('zone_id').notNull(),
  key: text('key').notNull(),
  label: text('label').notNull(),
  enabled: boolean('enabled').default(false).notNull(),
  settings: jsonb('settings'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  uniqueIndex('overlay_layers_zone_key_unique').on(t.zoneId, t.key),
  index('overlay_layers_zone_idx').on(t.zoneId),
]);

export default {
  organizations,
  userOrganizations,
  roleDefs,
  climaticRegions,
  zones,
  workZones,
  zoneMetrics,
  categories,
  subCategories,
  standardPrices,
  zoneSettings,
  overlayLayers,
};

// Types
export type Organization = InferModel<typeof organizations>;
export type UserOrganization = InferModel<typeof userOrganizations>;
export type RoleDef = InferModel<typeof roleDefs>;
export type Zone = InferModel<typeof zones>;
export type SubCategory = InferModel<typeof subCategories>;
export type StandardPrice = InferModel<typeof standardPrices>;
// governance schema proxy

// Relations are defined centrally in ./relations.ts       export * from './auth';
export * from './governance';
export * from './marketplace';
export * from './intelligence';
export * from './inventory';
export * from './relations';

// Si tu as besoin d'un objet global pour ton client Drizzle
import * as auth from './auth';
import * as governance from './governance';
import * as marketplace from './marketplace';
import * as intelligence from './intelligence';
import * as inventory from './inventory';

export const schema = {
  ...auth,
  ...governance,
  ...marketplace,
  ...intelligence,
  ...inventory,
}               import {
	uuid,
	text,
	timestamp,
	jsonb,
	boolean,
	integer,
	doublePrecision,
	index,
	uniqueIndex,
} from 'drizzle-orm/pg-core';
import { intelligenceSchema, agentActionStatusEnum, validationPriorityEnum } from './_config';
import { type InferModel } from 'drizzle-orm';

// Audit Logs
export const auditLogs = intelligenceSchema.table('audit_logs', {
	id: uuid('id').primaryKey().defaultRandom(),
	actorId: uuid('actor_id').notNull(),
	action: text('action').notNull(),
	entityId: text('entity_id').notNull(),
	entityType: text('entity_type').notNull(),
	oldValue: jsonb('old_value'),
	newValue: jsonb('new_value'),
	ipAddress: text('ip_address'),
	createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
	index('audit_logs_actor_idx').on(t.actorId),
	index('audit_logs_entity_idx').on(t.entityId),
]);

// Agent Actions
export const agentActions = intelligenceSchema.table('agent_actions', {
	id: uuid('id').primaryKey().defaultRandom(),
	agentName: text('agent_name').notNull(),
	actionType: text('action_type').notNull(),
	// Keep as text to match existing DB and avoid unsafe ALTER TYPE migrations
	batchId: text('batch_id'),
	payload: jsonb('payload'),
	status: agentActionStatusEnum('status').default('PENDING').notNull(),
	priority: validationPriorityEnum('priority').default('MEDIUM').notNull(),
	orderId: uuid('order_id'),
	userId: uuid('user_id'),
	auditTrailId: text('audit_trail_id'),
	aiReasoning: text('ai_reasoning'),
	adminNotes: text('admin_notes'),
	// Keep as text to match existing DB and avoid unsafe ALTER TYPE migrations
	validatedById: text('validated_by_id'),
	createdAt: timestamp('created_at').defaultNow().notNull(),
	updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
	index('agent_actions_status_idx').on(t.status),
	index('agent_actions_batch_idx').on(t.batchId),
	index('agent_actions_name_idx').on(t.agentName),
	uniqueIndex('agent_actions_order_unique').on(t.orderId),
]);

// Agent Telemetry
export const agentTelemetry = intelligenceSchema.table('agent_telemetry', {
	id: uuid('id').primaryKey().defaultRandom(),
	userId: uuid('user_id').notNull(),
	latitude: doublePrecision('latitude'),
	longitude: doublePrecision('longitude'),
	battery: integer('battery'),
	signal: text('signal'),
	createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
	index('agent_telemetry_user_idx').on(t.userId),
]);

// ExternalContext (files and vectorized assets)
export const externalContexts = intelligenceSchema.table('external_context_files', {
	id: uuid('id').primaryKey().defaultRandom(),
	fileName: text('file_name').notNull(),
	fileType: text('file_type').notNull(),
	fileUrl: text('file_url').notNull(),
	category: text('category'),
	zoneId: uuid('zone_id'),
	isVectorized: boolean('is_vectorized').default(false).notNull(),
	mcpServerId: text('mcp_server_id'),
	createdAt: timestamp('created_at').defaultNow().notNull(),
});
// Conversations
export const conversations = intelligenceSchema.table('conversations', {
	id: uuid('id').primaryKey().defaultRandom(),
	userId: uuid('user_id').notNull(),
	query: text('query').notNull(),
	response: text('response'),
	agentType: text('agent_type'),
	crop: text('crop'),
	zoneId: uuid('zone_id'),
	mode: text('mode').default('text').notNull(),
	audioUrl: text('audio_url'),
	isWaitingForInput: boolean('is_waiting_for_input').default(false).notNull(),
	missingSlots: jsonb('missing_slots'),
	executionPath: jsonb('execution_path'),
	confidenceScore: doublePrecision('confidence_score'),
	totalTokensUsed: integer('total_tokens_used').default(0).notNull(),
	responseTimeMs: integer('response_time_ms'),
	auditTrailId: text('audit_trail_id'),
	anomalyId: uuid('anomaly_id'),
	createdAt: timestamp('created_at').defaultNow().notNull(),
	updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
	index('conversations_user_idx').on(t.userId),
	index('conversations_agent_idx').on(t.agentType),
	index('conversations_created_idx').on(t.createdAt),
	uniqueIndex('conversations_audit_unique').on(t.auditTrailId),
]);

// Territory Event
export const territoryEvents = intelligenceSchema.table('territory_events', {
	id: uuid('id').primaryKey().defaultRandom(),
	zoneId: uuid('zone_id').notNull(),
	eventType: text('event_type').notNull(),
	payload: jsonb('payload'),
	meta: jsonb('meta'),
	status: text('status').default('NEW').notNull(),
	createdAt: timestamp('created_at').defaultNow().notNull(),
	processedAt: timestamp('processed_at'),
	processedById: uuid('processed_by_id'),
}, (t) => [
	index('territory_events_zone_idx').on(t.zoneId),
	index('territory_events_type_idx').on(t.eventType),
]);

// Anomalies
export const anomalies = intelligenceSchema.table('anomalies', {
	id: uuid('id').primaryKey().defaultRandom(),
	zoneId: uuid('zone_id').notNull(),
	source: text('source'),
	level: text('level').notNull(),
	title: text('title').notNull(),
	message: text('message'),
	details: jsonb('details'),
	isResolved: boolean('is_resolved').default(false).notNull(),
	resolvedById: uuid('resolved_by_id'),
	resolvedAt: timestamp('resolved_at'),
	createdAt: timestamp('created_at').defaultNow().notNull(),
	updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
	index('anomalies_zone_idx').on(t.zoneId),
	index('anomalies_resolved_idx').on(t.isResolved),
]);

// Trust Scores
export const trustScores = intelligenceSchema.table('trust_scores', {
	id: uuid('id').primaryKey().defaultRandom(),
	userId: uuid('user_id').unique().notNull(),
	globalScore: doublePrecision('global_score').default(0.0).notNull(),
	reliabilityIndex: doublePrecision('reliability_index').default(0.0).notNull(),
	qualityIndex: doublePrecision('quality_index').default(0.0).notNull(),
	complianceIndex: doublePrecision('compliance_index').default(0.0).notNull(),
	resilienceBonus: doublePrecision('resilience_bonus').default(0.0).notNull(),
	createdAt: timestamp('created_at').defaultNow().notNull(),
	updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
});

// AI Rating Reasonings
export const aiRatingReasonings = intelligenceSchema.table('ai_rating_reasonings', {
	id: uuid('id').primaryKey().defaultRandom(),
	trustScoreId: uuid('trust_score_id').notNull(),
	agentName: text('agent_name').notNull(),
	justification: text('justification').notNull(),
	dataPoints: jsonb('data_points').notNull(),
	createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
	index('ai_rating_trust_idx').on(t.trustScoreId),
	index('ai_rating_agent_idx').on(t.agentName),
]);

// Relations (defined here to avoid circular imports)
// export const auditLogsRelations = relations(auditLogs, ({ one }) => ({
// 	actor: one(() => users, { fields: [auditLogs.actorId], references: [users.id] }),
// }));

export default {
	auditLogs,
	agentActions,
	agentTelemetry,
	externalContexts,
	conversations,
	territoryEvents,
	anomalies,
	trustScores,
	aiRatingReasonings,
};

// Types
export type AgentAction = InferModel<typeof agentActions>;
export type AuditLog = InferModel<typeof auditLogs>;
export type Conversation = InferModel<typeof conversations>;
export type ExternalContext = InferModel<typeof externalContexts>;
export type TrustScore = InferModel<typeof trustScores>;     import {
  uuid,
  text,
  integer,
  timestamp,
  boolean,
  jsonb,
  index,
} from 'drizzle-orm/pg-core';
import { type InferModel } from 'drizzle-orm';
import { marketplaceSchema, unitEnum } from './_config';

// Seed allocations: stock assigned by an organization to a zone
export const seedAllocations = marketplaceSchema.table('seed_allocations', {
  id: uuid('id').primaryKey().defaultRandom(),
  organizationId: uuid('organization_id').notNull(),
  zoneId: uuid('zone_id').notNull(),
  seedType: text('seed_type').notNull(),
  totalQuantity: integer('total_quantity').notNull(),
  remainingQuantity: integer('remaining_quantity').notNull(),
  unit: unitEnum('unit').default('KG').notNull(),
  allocatedById: uuid('allocated_by_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('seed_allocations_org_idx').on(t.organizationId),
  index('seed_allocations_zone_idx').on(t.zoneId),
  index('seed_allocations_seedtype_idx').on(t.seedType),
]);

// Seed distributions: proof of handover and verification data
export const seedDistributions = marketplaceSchema.table('seed_distributions', {
  id: uuid('id').primaryKey().defaultRandom(),
  allocationId: uuid('allocation_id').notNull(),
  producerId: uuid('producer_id').notNull(),
  agentId: uuid('agent_id').notNull(),
  organizationId: uuid('organization_id').notNull(),
  zoneId: uuid('zone_id').notNull(),
  quantity: integer('quantity').notNull(),
  cnibProvided: text('cnib_provided'),
  verificationCodeHash: text('verification_code_hash'),
  verificationCodeExpiresAt: timestamp('verification_code_expires_at'),
  verificationChannel: text('verification_channel').default('IN_APP'),
  attemptsCount: integer('attempts_count').default(0).notNull(),
  status: text('status').default('PENDING').notNull(),
  receiptAt: timestamp('receipt_at'),
  metadata: jsonb('metadata'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('seed_distributions_alloc_idx').on(t.allocationId),
  index('seed_distributions_producer_idx').on(t.producerId),
  index('seed_distributions_agent_idx').on(t.agentId),
  index('seed_distributions_status_idx').on(t.status),
]);

// Attempts for verification (success or failure) — useful for antifraud and rate-limiting
export const seedDistributionAttempts = marketplaceSchema.table('seed_distribution_attempts', {
  id: uuid('id').primaryKey().defaultRandom(),
  distributionId: uuid('distribution_id').notNull(),
  actorId: uuid('actor_id'),
  attemptType: text('attempt_type'),
  success: boolean('success').default(false).notNull(),
  ipAddress: text('ip_address'),
  metadata: jsonb('metadata'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
  index('sda_distribution_idx').on(t.distributionId),
  index('sda_actor_idx').on(t.actorId),
]);

export default {
  seedAllocations,
  seedDistributions,
  seedDistributionAttempts,
};

// Types
export type SeedAllocation = InferModel<typeof seedAllocations>;
export type SeedDistribution = InferModel<typeof seedDistributions>;
export type SeedDistributionAttempt = InferModel<typeof seedDistributionAttempts>;
  import {
  uuid,
  text,
  integer,
  doublePrecision,
  boolean,
  timestamp,
  jsonb,
  uniqueIndex,
  index,
} from 'drizzle-orm/pg-core';
import { sql } from 'drizzle-orm';
import {
  marketplaceSchema,
  producerStatusEnum,
  orderStatusEnum,
  unitEnum,
  stockTypeEnum,
  movementTypeEnum,
  expenseCategoryEnum,
  paymentMethodEnum,
  paymentStatusEnum,
  orderSourceEnum,
  auctionStatusEnum,
} from './_config';
import { type InferModel } from 'drizzle-orm';

export const warehouses = marketplaceSchema.table('warehouses', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').notNull(),
  type: text('type').notNull(),
  capacity: doublePrecision('capacity'),
  location: text('location'),
  zoneId: uuid('zone_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('warehouses_zone_idx').on(t.zoneId),
]);
//
export const producers = marketplaceSchema.table('producers', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').unique().notNull(),
  organizationId: uuid('organization_id'),
  businessName: text('business_name'),
  status: producerStatusEnum('status').default('PENDING').notNull(),
  isCertified: boolean('is_certified').default(false).notNull(),
  zoneId: uuid('zone_id'),
  region: text('region'),
  province: text('province'),
  commune: text('commune'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('producers_status_idx').on(t.status),
  index('producers_org_idx').on(t.organizationId),
  index('producers_zone_idx').on(t.zoneId),
]);

export const clients = marketplaceSchema.table('clients', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').notNull(),
  phone: text('phone').notNull(),
  email: text('email'),
  location: text('location'),
  totalOrders: integer('total_orders').default(0).notNull(),
  totalSpent: doublePrecision('total_spent').default(0).notNull(),
  lastOrderDate: timestamp('last_order_date'),
  producerId: uuid('producer_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('clients_phone_idx').on(t.phone),
  index('clients_name_idx').on(t.name),
  index('clients_producer_idx').on(t.producerId),
]);

export const farms = marketplaceSchema.table('farms', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').notNull(),
  location: text('location'),
  size: doublePrecision('size'),
  soilType: text('soil_type'),
  waterSource: text('water_source'),
  zoneId: uuid('zone_id'),
  producerId: uuid('producer_id').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('farms_producer_idx').on(t.producerId),
  index('farms_zone_idx').on(t.zoneId),
]);

export const cropCycles = marketplaceSchema.table('crop_cycles', {
  id: uuid('id').primaryKey().defaultRandom(),
  farmId: uuid('farm_id').notNull(),
  cropType: text('crop_type').notNull(),
  areaSize: doublePrecision('area_size').notNull(),
  plantedAt: timestamp('planted_at').notNull(),
  expectedHarvestDate: timestamp('expected_harvest_date').notNull(),
  expectedYield: doublePrecision('expected_yield').notNull(),
  status: text('status').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('crop_cycles_farm_idx').on(t.farmId),
]);

export const stocks = marketplaceSchema.table('stocks', {
  id: uuid('id').primaryKey().defaultRandom(),
  farmId: uuid('farm_id'),
  warehouseId: uuid('warehouse_id'),
  organizationId: uuid('organization_id'),
  itemName: text('item_name').notNull(),
  quantity: doublePrecision('quantity').default(0).notNull(),
  unit: unitEnum('unit').default('KG').notNull(),
  type: stockTypeEnum('type').default('HARVEST').notNull(),
  verifiedAt: timestamp('verified_at'),
  verifiedById: uuid('verified_by_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('stocks_farm_idx').on(t.farmId),
  index('stocks_warehouse_idx').on(t.warehouseId),
  index('stocks_org_idx').on(t.organizationId),
  index('stocks_type_idx').on(t.type),
  index('stocks_verifier_idx').on(t.verifiedById),
]);

export const stockMovements = marketplaceSchema.table('stock_movements', {
  id: uuid('id').primaryKey().defaultRandom(),
  stockId: uuid('stock_id').notNull(),
  type: movementTypeEnum('type').notNull(),
  quantity: doublePrecision('quantity').notNull(),
  reason: text('reason'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
  index('stock_movements_stock_idx').on(t.stockId),
  index('stock_movements_created_idx').on(t.createdAt),
]);

export const batches = marketplaceSchema.table('batches', {
  id: uuid('id').primaryKey().defaultRandom(),
  stockId: uuid('stock_id').notNull(),
  organizationId: uuid('organization_id').notNull(),
  batchNumber: text('batch_number').unique().notNull(),
  originFarmId: uuid('origin_farm_id'),
  quantity: doublePrecision('quantity').notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('batches_stock_idx').on(t.stockId),
  index('batches_org_idx').on(t.organizationId),
]);

export const expenses = marketplaceSchema.table('expenses', {
  id: uuid('id').primaryKey().defaultRandom(),
  farmId: uuid('farm_id').notNull(),
  label: text('label').notNull(),
  amount: doublePrecision('amount').notNull(),
  category: expenseCategoryEnum('category').default('OTHER').notNull(),
  date: timestamp('date').defaultNow().notNull(),
}, (t) => [
  index('expenses_farm_idx').on(t.farmId),
  index('expenses_category_idx').on(t.category),
  index('expenses_date_idx').on(t.date),
]);

export const products = marketplaceSchema.table('products', {
  id: uuid('id').primaryKey().defaultRandom(),
  shortCode: text('short_code').unique(),
  name: text('name').default('Produit').notNull(),
  categoryLabel: text('category_label').notNull(),
  subCategoryId: uuid('sub_category_id'),
  localNames: jsonb('local_names'),
  description: text('description'),
  price: doublePrecision('price').notNull(),
  unit: unitEnum('unit').default('KG').notNull(),
  quantityForSale: doublePrecision('quantity_for_sale').default(0).notNull(),
  images: text('images').array().notNull().default(sql`'{}'::text[]`),
  audioUrl: text('audio_url'),
  producerId: uuid('producer_id').notNull(),
  verifiedAt: timestamp('verified_at'),
  verifiedById: uuid('verified_by_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('products_producer_idx').on(t.producerId),
  index('products_category_idx').on(t.categoryLabel),
  index('products_subcategory_idx').on(t.subCategoryId),
  index('products_price_idx').on(t.price),
  index('products_created_idx').on(t.createdAt),
  index('products_verifier_idx').on(t.verifiedById),
]);

export const orders = marketplaceSchema.table('orders', {
  id: uuid('id').primaryKey().defaultRandom(),
  buyerId: uuid('buyer_id'),
  organizationId: uuid('organization_id'),
  customerName: text('customer_name'),
  customerPhone: text('customer_phone'),
  zoneId: uuid('zone_id'),
  paymentMethod: paymentMethodEnum('payment_method').default('CASH').notNull(),
  paymentStatus: paymentStatusEnum('payment_status').default('PENDING').notNull(),
  city: text('city'),
  gpsLat: doublePrecision('gps_lat'),
  gpsLng: doublePrecision('gps_lng'),
  deliveryDesc: text('delivery_desc'),
  audioUrl: text('audio_url'),
  status: orderStatusEnum('status').default('PENDING').notNull(),
  source: orderSourceEnum('source').default('APP').notNull(),
  whatsappId: text('whatsapp_id'),
  totalAmount: doublePrecision('total_amount').notNull(),
  isAgentOrder: boolean('is_agent_order').default(false).notNull(),
  clientId: uuid('client_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('orders_buyer_idx').on(t.buyerId),
  index('orders_org_idx').on(t.organizationId),
  index('orders_status_idx').on(t.status),
  index('orders_zone_idx').on(t.zoneId),
  index('orders_created_idx').on(t.createdAt),
  index('orders_phone_idx').on(t.customerPhone),
]);

export const orderItems = marketplaceSchema.table('order_items', {
  id: uuid('id').primaryKey().defaultRandom(),
  orderId: uuid('order_id').notNull(),
  productId: uuid('product_id').notNull(),
  quantity: doublePrecision('quantity').notNull(),
  priceAtSale: doublePrecision('price_at_sale').notNull(),
}, (t) => [
  index('order_items_order_idx').on(t.orderId),
  index('order_items_product_idx').on(t.productId),
]);

export const auctions = marketplaceSchema.table('auctions', {
  id: uuid('id').primaryKey().defaultRandom(),
  buyerId: uuid('buyer_id').notNull(),
  subCategoryId: uuid('sub_category_id').notNull(),
  quantity: doublePrecision('quantity').notNull(),
  unit: unitEnum('unit').default('TONNE').notNull(),
  maxPricePerUnit: doublePrecision('max_price_per_unit').notNull(),
  deadline: timestamp('deadline').notNull(),
  status: auctionStatusEnum('status').default('OPEN').notNull(),
  targetZoneId: uuid('target_zone_id'),
  version: integer('version').default(0).notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('auctions_status_idx').on(t.status),
  index('auctions_zone_idx').on(t.targetZoneId),
  index('auctions_deadline_idx').on(t.deadline),
]);

export const bids = marketplaceSchema.table('bids', {
  id: uuid('id').primaryKey().defaultRandom(),
  auctionId: uuid('auction_id').notNull(),
  producerId: uuid('producer_id').notNull(),
  offeredPrice: doublePrecision('offered_price').notNull(),
  isWinner: boolean('is_winner').default(false).notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
}, (t) => [
  uniqueIndex('bids_auction_producer_unique').on(t.auctionId, t.producerId),
  index('bids_auction_idx').on(t.auctionId),
  index('bids_producer_idx').on(t.producerId),
]);

export default {
  warehouses,
  producers,
  clients,
  farms,
  cropCycles,
  stocks,
  stockMovements,
  batches,
  expenses,
  products,
  orders,
  orderItems,
  auctions,
  bids,
};

// Types
export type Producer = InferModel<typeof producers>;
export type Product = InferModel<typeof products>;
export type Order = InferModel<typeof orders>;
export type OrderItem = InferModel<typeof orderItems>;
export type Warehouse = InferModel<typeof warehouses>;
// marketplace schema proxy       /**
 * DRIZZLE RELATIONS — Ladini v3
 * ══════════════════════════════════════════════════════════════════════════
 * All relations are defined centrally here to:
 *   1. Avoid circular import issues between schema files
 *   2. Enable `db.query.X.findMany({ with: { ... } })` across the codebase
 *   3. Provide a single source of truth for relation metadata
 *
 * Every `with:` used in services/API routes MUST have a corresponding
 * relation defined here, otherwise Drizzle will throw:
 *   "Cannot read properties of undefined (reading 'referencedTable')"
 */

import { relations } from 'drizzle-orm';

// ── Auth tables ──
import { users, accounts, sessions } from './auth';

// ── Governance tables ──
import {
  organizations,
  userOrganizations,
  roleDefs,
  climaticRegions,
  zones,
  workZones,
  zoneMetrics,
  categories,
  subCategories,
  standardPrices,
} from './governance';

// ── Marketplace tables ──
import {
  warehouses,
  producers,
  clients,
  farms,
  cropCycles,
  stocks,
  stockMovements,
  batches,
  expenses,
  products,
  orders,
  orderItems,
  auctions,
  bids,
} from './marketplace';

// Inventory / distribution tables
import {
  seedAllocations,
  seedDistributions,
  seedDistributionAttempts,
} from './inventory';

// ── Intelligence tables ──
import {
  auditLogs,
  agentActions,
  agentTelemetry,
  conversations,
  anomalies,
  trustScores,
  aiRatingReasonings,
  territoryEvents,
} from './intelligence';

// ╔══════════════════════════════════════════════╗
// ║  AUTH RELATIONS                               ║
// ╚══════════════════════════════════════════════╝

export const usersRelations = relations(users, ({ one, many }) => ({
  producer: one(producers, {
    fields: [users.id],
    references: [producers.userId],
  }),
  userOrganizations: many(userOrganizations),
  trustScore: one(trustScores, {
    fields: [users.id],
    references: [trustScores.userId],
  }),
}));

// ╔══════════════════════════════════════════════╗
// ║  GOVERNANCE RELATIONS                         ║
// ╚══════════════════════════════════════════════╝

export const userOrganizationsRelations = relations(userOrganizations, ({ one }) => ({
  dynRole: one(roleDefs, {
    fields: [userOrganizations.roleId],
    references: [roleDefs.id],
  }),
  organization: one(organizations, {
    fields: [userOrganizations.organizationId],
    references: [organizations.id],
  }),
  user: one(users, {
    fields: [userOrganizations.userId],
    references: [users.id],
  }),
  zone: one(zones, {
    fields: [userOrganizations.managedZoneId],
    references: [zones.id],
  }),
}));

export const organizationsRelations = relations(organizations, ({ many }) => ({
  members: many(userOrganizations),
  workZones: many(workZones),
}));

export const climaticRegionsRelations = relations(climaticRegions, ({ many }) => ({
  zones: many(zones),
}));

export const zonesRelations = relations(zones, ({ one, many }) => ({
  climaticRegion: one(climaticRegions, {
    fields: [zones.climaticRegionId],
    references: [climaticRegions.id],
  }),
  organization: one(organizations, {
    fields: [zones.organizationId],
    references: [organizations.id],
  }),
  parent: one(zones, {
    fields: [zones.parentId],
    references: [zones.id],
    relationName: 'zoneParent',
  }),
  children: many(zones, { relationName: 'zoneParent' }),
  producers: many(producers),
  farms: many(farms),
  orders: many(orders),
}));

export const workZonesRelations = relations(workZones, ({ one }) => ({
  zone: one(zones, {
    fields: [workZones.zoneId],
    references: [zones.id],
  }),
  manager: one(users, {
    fields: [workZones.managerId],
    references: [users.id],
  }),
  organization: one(organizations, {
    fields: [workZones.organizationId],
    references: [organizations.id],
  }),
}));

export const categoriesRelations = relations(categories, ({ many }) => ({
  subCategories: many(subCategories),
}));

export const subCategoriesRelations = relations(subCategories, ({ one, many }) => ({
  category: one(categories, {
    fields: [subCategories.categoryId],
    references: [categories.id],
  }),
  standardPrices: many(standardPrices),
}));

export const standardPricesRelations = relations(standardPrices, ({ one }) => ({
  subCategory: one(subCategories, {
    fields: [standardPrices.subCategoryId],
    references: [subCategories.id],
  }),
  zone: one(zones, {
    fields: [standardPrices.zoneId],
    references: [zones.id],
  }),
}));

// ╔══════════════════════════════════════════════╗
// ║  MARKETPLACE RELATIONS                        ║
// ╚══════════════════════════════════════════════╝

export const producersRelations = relations(producers, ({ one, many }) => ({
  user: one(users, {
    fields: [producers.userId],
    references: [users.id],
  }),
  zone: one(zones, {
    fields: [producers.zoneId],
    references: [zones.id],
  }),
  organization: one(organizations, {
    fields: [producers.organizationId],
    references: [organizations.id],
  }),
  farms: many(farms),
  products: many(products),
  clients: many(clients),
}));

export const clientsRelations = relations(clients, ({ one }) => ({
  producer: one(producers, {
    fields: [clients.producerId],
    references: [producers.id],
  }),
}));

export const farmsRelations = relations(farms, ({ one, many }) => ({
  producer: one(producers, {
    fields: [farms.producerId],
    references: [producers.id],
  }),
  zone: one(zones, {
    fields: [farms.zoneId],
    references: [zones.id],
  }),
  // Named "inventory" to match the `with: { inventory: true }` used in dashboard routes
  inventory: many(stocks),
  cropCycles: many(cropCycles),
  expenses: many(expenses),
}));

export const cropCyclesRelations = relations(cropCycles, ({ one }) => ({
  farm: one(farms, {
    fields: [cropCycles.farmId],
    references: [farms.id],
  }),
}));

export const stocksRelations = relations(stocks, ({ one, many }) => ({
  farm: one(farms, {
    fields: [stocks.farmId],
    references: [farms.id],
  }),
  warehouse: one(warehouses, {
    fields: [stocks.warehouseId],
    references: [warehouses.id],
  }),
  movements: many(stockMovements),
}));

export const stockMovementsRelations = relations(stockMovements, ({ one }) => ({
  stock: one(stocks, {
    fields: [stockMovements.stockId],
    references: [stocks.id],
  }),
}));

export const batchesRelations = relations(batches, ({ one }) => ({
  stock: one(stocks, {
    fields: [batches.stockId],
    references: [stocks.id],
  }),
}));

export const expensesRelations = relations(expenses, ({ one }) => ({
  farm: one(farms, {
    fields: [expenses.farmId],
    references: [farms.id],
  }),
}));

export const productsRelations = relations(products, ({ one }) => ({
  producer: one(producers, {
    fields: [products.producerId],
    references: [producers.id],
  }),
}));

export const ordersRelations = relations(orders, ({ one, many }) => ({
  buyer: one(users, {
    fields: [orders.buyerId],
    references: [users.id],
  }),
  zone: one(zones, {
    fields: [orders.zoneId],
    references: [zones.id],
  }),
  client: one(clients, {
    fields: [orders.clientId],
    references: [clients.id],
  }),
  items: many(orderItems),
}));

export const orderItemsRelations = relations(orderItems, ({ one }) => ({
  order: one(orders, {
    fields: [orderItems.orderId],
    references: [orders.id],
  }),
  product: one(products, {
    fields: [orderItems.productId],
    references: [products.id],
  }),
}));

export const auctionsRelations = relations(auctions, ({ one, many }) => ({
  buyer: one(users, {
    fields: [auctions.buyerId],
    references: [users.id],
  }),
  subCategory: one(subCategories, {
    fields: [auctions.subCategoryId],
    references: [subCategories.id],
  }),
  targetZone: one(zones, {
    fields: [auctions.targetZoneId],
    references: [zones.id],
  }),
  bids: many(bids),
}));

export const bidsRelations = relations(bids, ({ one }) => ({
  auction: one(auctions, {
    fields: [bids.auctionId],
    references: [auctions.id],
  }),
  producer: one(producers, {
    fields: [bids.producerId],
    references: [producers.id],
  }),
}));

export const warehousesRelations = relations(warehouses, ({ one, many }) => ({
  zone: one(zones, {
    fields: [warehouses.zoneId],
    references: [zones.id],
  }),
  stocks: many(stocks),
}));

export const seedAllocationsRelations = relations(seedAllocations, ({ one, many }) => ({
  organization: one(organizations, {
    fields: [seedAllocations.organizationId],
    references: [organizations.id],
  }),
  zone: one(zones, {
    fields: [seedAllocations.zoneId],
    references: [zones.id],
  }),
  allocatedBy: one(users, {
    fields: [seedAllocations.allocatedById],
    references: [users.id],
  }),
  distributions: many(seedDistributions),
}));

export const seedDistributionsRelations = relations(seedDistributions, ({ one, many }) => ({
  allocation: one(seedAllocations, {
    fields: [seedDistributions.allocationId],
    references: [seedAllocations.id],
  }),
  producer: one(producers, {
    fields: [seedDistributions.producerId],
    references: [producers.id],
  }),
  agent: one(users, {
    fields: [seedDistributions.agentId],
    references: [users.id],
  }),
  organization: one(organizations, {
    fields: [seedDistributions.organizationId],
    references: [organizations.id],
  }),
  zone: one(zones, {
    fields: [seedDistributions.zoneId],
    references: [zones.id],
  }),
  attempts: many(seedDistributionAttempts),
}));

export const seedDistributionAttemptsRelations = relations(seedDistributionAttempts, ({ one }) => ({
  distribution: one(seedDistributions, {
    fields: [seedDistributionAttempts.distributionId],
    references: [seedDistributions.id],
  }),
  actor: one(users, {
    fields: [seedDistributionAttempts.actorId],
    references: [users.id],
  }),
}));

// ╔══════════════════════════════════════════════╗
// ║  INTELLIGENCE RELATIONS                       ║
// ╚══════════════════════════════════════════════╝

export const agentActionsRelations = relations(agentActions, ({ one }) => ({
  order: one(orders, {
    fields: [agentActions.orderId],
    references: [orders.id],
  }),
  user: one(users, {
    fields: [agentActions.userId],
    references: [users.id],
  }),
}));

export const conversationsRelations = relations(conversations, ({ one }) => ({
  user: one(users, {
    fields: [conversations.userId],
    references: [users.id],
  }),
  zone: one(zones, {
    fields: [conversations.zoneId],
    references: [zones.id],
  }),
}));

export const trustScoresRelations = relations(trustScores, ({ one, many }) => ({
  user: one(users, {
    fields: [trustScores.userId],
    references: [users.id],
  }),
  reasonings: many(aiRatingReasonings),
}));

export const aiRatingReasoningsRelations = relations(aiRatingReasonings, ({ one }) => ({
  trustScore: one(trustScores, {
    fields: [aiRatingReasonings.trustScoreId],
    references: [trustScores.id],
  }),
}));

export const anomaliesRelations = relations(anomalies, ({ one }) => ({
  zone: one(zones, {
    fields: [anomalies.zoneId],
    references: [zones.id],
  }),
}));

export const territoryEventsRelations = relations(territoryEvents, ({ one }) => ({
  zone: one(zones, {
    fields: [territoryEvents.zoneId],
    references: [zones.id],
  }),
}));