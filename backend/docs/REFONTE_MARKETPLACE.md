# Refonte « Marketplace Pure » — Proposition d'Architecture

> **Statut : PROPOSITION (aucune suppression appliquée).** À valider avant exécution.
> Cible : pivot d'un système hybride *Conseil + Marketplace* vers une **marketplace transactionnelle pure** (Producteur → Acheteur).
> Périmètre : `frontag/src/db/schema/` (Drizzle) + `ladini/domain/models.py` (SQLAlchemy) + `ladini/services/database/` + `ladini/core/database.py`.

---

## 0. Principe directeur

Une seule règle tranche chaque décision :

> **On garde une table/colonne si, et seulement si, elle sert une transaction Producteur↔Acheteur (offre, stock, commande, paiement, livraison, confiance) ou la persistance de l'agent qui pilote cette transaction. Tout le reste (agronomie, capteurs, météo, croissance, recommandations) part.**

Conséquence sur les tables « grises » (validées avec toi) : **on conserve** `farms`, `warehouses`, `stocks`, `trust_scores`, `marketplace_ratings`, `conversations`, `agent_context_memory`, `agent_actions`, et `crop_cycles` — mais **dégraissées** de tout attribut agronomique, et `crop_cycles` est **renommée `market_offers`**.

---

## 1. Matrice de suppression

### 1.1 — Tables supprimées intégralement (conseil pur)

| Table (DB) | Schéma Drizzle | Modèle SQLAlchemy | Raison de suppression |
|---|---|---|---|
| `crop_profiles` | `intelligence.ts` | `CropProfile` | Base de connaissances agronomiques (GDD, Kc, fumure) — pur conseil. |
| `crop_fertilizer_steps` | `intelligence.ts` | `CropFertilizerStep` | Plan de fertilisation — conseil. |
| `soil_analyses` | `intelligence.ts` | `SoilAnalysis` | Analyses labo de sol — conseil. |
| `ai_recommendations` | `intelligence.ts` | `AIRecommendation` | **Cœur du moteur de conseil.** À supprimer + relation `User.recommendations`. |
| `weather_data_logs` | `intelligence.ts` | `WeatherDataLog` | Météo/phytosanitaire — conseil. |
| `agent_telemetry` | `intelligence.ts` | `AgentTelemetry` | Télémétrie d'agents de terrain (modèle conseil/gouvernance). |
| `external_context_files` | `intelligence.ts` | `ExternalContextFile` | Fichiers RAG/vectorisation pour le conseil. |
| `territory_events` | `intelligence.ts` | `TerritoryEvent` | Surveillance territoriale — gouvernance/conseil. |
| `anomalies` | `intelligence.ts` | `Anomaly` | Détection d'anomalies agronomiques/territoriales. |
| `field_interventions` | `marketplace.ts` | `FieldIntervention` | Carnet de champ (semis, traitement…) — conseil. |
| `sensor_data_summary` | `marketplace.ts` | `SensorDataSummary` | Dernières valeurs capteurs — AgTech. |
| `agronomic_standards` | `marketplace.ts` | `AgronomicStandard` | Référentiel besoins azote/GDD — conseil. |
| `pest_disease_catalog` | `marketplace.ts` | `PestDiseaseCatalog` | Catalogue maladies/ravageurs — conseil. |
| `soil_profiles` | `marketplace.ts` | `SoilProfile` | Profils de sol — conseil. |
| `sensor_telemetry_history` | `marketplace.ts` | `SensorTelemetryHistory` | Historique IoT — AgTech. |
| `crop_growth_logs` | `marketplace.ts` | `CropGrowthLog` | Observations BBCH — conseil. |
| `crop_growth_stages` | `marketplace.ts` | `CropGrowthStage` | Référentiel stades BBCH — conseil. |
| `user_cultures` | `models.py` (auth) | `UserCulture` | Cultures déclaratives pour conseil quotidien. |
| `daily_advice_logs` | `models.py` (auth) | `DailyAdviceLog` | Journal de conseils quotidiens WhatsApp. |

**19 tables** supprimées. Toutes les `relationship()`, `ForeignKey` et index associés partent avec elles.

### 1.2 — Colonnes retirées des tables **conservées** (dégraissage)

| Table conservée | Colonnes supprimées | Colonnes gardées (essentiel transactionnel) |
|---|---|---|
| `farms` | `soil_type`, `water_source` (+ relations `soil_profiles`, `telemetry_history`, `sensor_summary`, `cycles`) | `id`, `name`, `location`, `size`, `producer_id`, `zone_id`, timestamps |
| `auth.users` | `daily_advice_time`, relation `recommendations`, (`latitude`/`longitude` → conservées : utile livraison) | identité + `role`, `zone_id`, `identity_verified`, `onboarding_completed` |
| `conversations` | `crop`, `farm_id` (agronomiques) ; **conservée** pour la persistance d'état agent | `user_id`, `query`, `response`, `user_intent`, `missing_slots`, `execution_path`, `is_waiting_for_input`, `needs_follow_up`, tokens |
| `agent_context_memory` | `crop_cycle_id` → renommé `market_offer_id` (ou retiré) ; `farm_id` conservé optionnel | `user_id`, `context_key`, `context_value`, `source`, `expires_at` |

### 1.3 — Renommage structurant : `crop_cycles` → `market_offers`

`crop_cycles` mélangeait un cycle cultural (agronomie) et une offre de vente (préventes). On garde **la moitié transactionnelle** et on renomme.

| `crop_cycles` (avant) | `market_offers` (après) |
|---|---|
| ❌ `area_size`, `planted_at`, `target_yield`, `expected_yield`, `variety`, `farming_method`, `soil_type`, `last_intervention_date`, `growth_stage`, `hatch_date`, `initial_stock` | — supprimés (agronomie/élevage) |
| ✅ `crop_type` → `product_label` | libellé produit offert |
| ✅ `sub_category_id`, `unit`, `price_per_unit`, `available_quantity`, `reserved_quantity`, `current_stock`, `production_type`, `species`, `breed` | catalogue / disponibilité |
| ✅ `expected_harvest_date`, `estimated_available_at`, `is_public`, `preorder_enabled`, `status` | mécanique de prévente |
| ➕ `producer_id` (dénormalisé depuis `farm`) | requêtes catalogue directes sans jointure `farm` |

> Impact code : `Order.crop_cycle_id` → `Order.market_offer_id` ; index `orders_crop_cycle_idx` → `orders_market_offer_idx` ; relation `preorders`/`crop_cycle` renommée `offer`.

### 1.4 — À arbitrer (hors périmètre validé — je ne touche pas sans ton feu vert)

| Élément | Localisation | Question |
|---|---|---|
| `seed_allocations`, `seed_distributions`, `seed_distribution_attempts` | `inventory.ts` | Programme de distribution de semences (ONG/organisation). Pas une transaction Producteur↔Acheteur. **Supprimer ?** |
| `market_matches`, `surplus_offers`, `transaction_staging`, `user_context_states` | `models.py` (sans schéma) | Tables « unrelated » legacy. `transaction_staging` peut servir le checkout ; les autres semblent mortes. **Auditer l'usage puis supprimer.** |
| `expenses` | `marketplace.ts` | Comptabilité producteur. Conservée (utilisée par `finance_dto`) mais non-transactionnelle stricto sensu. |

---

## 2. Nouveaux schémas Drizzle (`frontag/src/db/schema/`)

### 2.1 — `intelligence.ts` (réduit à l'infra opérationnelle de l'agent)

Ne restent que : audit, actions agent, conversations, mémoire agent, confiance.

```ts
import {
  uuid, text, timestamp, jsonb, boolean, integer, doublePrecision,
  index, uniqueIndex,
} from 'drizzle-orm/pg-core';
import { intelligenceSchema, agentActionStatusEnum, validationPriorityEnum } from './_config';
import { type InferModel } from 'drizzle-orm';

// ── Audit transactionnel ────────────────────────────────────────────────
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

// ── Validation humaine des actions de l'agent (checkout, litige…) ────────
export const agentActions = intelligenceSchema.table('agent_actions', {
  id: uuid('id').primaryKey().defaultRandom(),
  agentName: text('agent_name').notNull(),
  actionType: text('action_type').notNull(),
  batchId: text('batch_id'),
  payload: jsonb('payload'),
  status: agentActionStatusEnum('status').default('PENDING').notNull(),
  priority: validationPriorityEnum('priority').default('MEDIUM').notNull(),
  orderId: uuid('order_id'),
  userId: uuid('user_id'),
  aiReasoning: text('ai_reasoning'),
  adminNotes: text('admin_notes'),
  validatedById: text('validated_by_id'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('agent_actions_status_idx').on(t.status),
  index('agent_actions_name_idx').on(t.agentName),
  uniqueIndex('agent_actions_order_unique').on(t.orderId),
]);

// ── Persistance conversationnelle (état du panier/commande en cours) ─────
export const conversations = intelligenceSchema.table('conversations', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').notNull(),
  query: text('query').notNull(),
  response: text('response'),
  agentType: text('agent_type'),
  zoneId: uuid('zone_id'),
  mode: text('mode').default('text').notNull(),
  audioUrl: text('audio_url'),
  isWaitingForInput: boolean('is_waiting_for_input').default(false).notNull(),
  missingSlots: jsonb('missing_slots'),
  executionPath: jsonb('execution_path'),
  userIntent: text('user_intent'),
  needsFollowUp: boolean('needs_follow_up').default(false).notNull(),
  totalTokensUsed: integer('total_tokens_used').default(0).notNull(),
  responseTimeMs: integer('response_time_ms'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('conversations_user_idx').on(t.userId),
  index('conversations_agent_idx').on(t.agentType),
  index('conversations_followup_idx').on(t.needsFollowUp),
]);

// ── Mémoire courte de l'agent (slots persistés entre tours) ──────────────
export const agentContextMemory = intelligenceSchema.table('agent_context_memory', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').notNull(),
  marketOfferId: uuid('market_offer_id'),
  contextKey: text('context_key').notNull(),
  contextValue: jsonb('context_value').notNull(),
  source: text('source').default('AGENT').notNull(),
  expiresAt: timestamp('expires_at'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('acm_user_idx').on(t.userId),
  index('acm_key_idx').on(t.contextKey),
  uniqueIndex('acm_user_key_unique').on(t.userId, t.contextKey),
]);

// ── Réputation (cœur de confiance marketplace) ───────────────────────────
export const trustScores = intelligenceSchema.table('trust_scores', {
  id: uuid('id').primaryKey().defaultRandom(),
  userId: uuid('user_id').unique().notNull(),
  globalScore: doublePrecision('global_score').default(0).notNull(),
  reliabilityIndex: doublePrecision('reliability_index').default(0).notNull(),
  qualityIndex: doublePrecision('quality_index').default(0).notNull(),
  complianceIndex: doublePrecision('compliance_index').default(0).notNull(),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
});

export default { auditLogs, agentActions, conversations, agentContextMemory, trustScores };

export type AuditLog = InferModel<typeof auditLogs>;
export type AgentAction = InferModel<typeof agentActions>;
export type Conversation = InferModel<typeof conversations>;
export type AgentContextMemory = InferModel<typeof agentContextMemory>;
export type TrustScore = InferModel<typeof trustScores>;
```

> `ai_rating_reasonings` peut rester si tu veux tracer le « pourquoi » d'un score ; je la garde optionnelle.

### 2.2 — `marketplace.ts` : bloc AgTech supprimé + `market_offers`

On retire tout le bloc *AgTech* (lignes `fieldInterventions` → `cropGrowthStages`) et on remplace `cropCycles` par :

```ts
// ── OFFRE DE MARCHÉ (ex crop_cycles, dégraissée) ─────────────────────────
export const marketOffers = marketplaceSchema.table('market_offers', {
  id: uuid('id').primaryKey().defaultRandom(),
  farmId: uuid('farm_id').references(() => farms.id),
  producerId: uuid('producer_id').notNull().references(() => producers.id), // dénormalisé
  subCategoryId: uuid('sub_category_id'),

  productLabel: text('product_label').notNull(),   // ex-cropType
  productionType: text('production_type').default('CROP').notNull(),
  species: text('species'),
  breed: text('breed'),

  unit: unitEnum('unit').default('KG').notNull(),
  pricePerUnit: doublePrecision('price_per_unit'),
  availableQuantity: doublePrecision('available_quantity').default(0).notNull(),
  reservedQuantity: doublePrecision('reserved_quantity').default(0).notNull(),
  currentStock: doublePrecision('current_stock').default(0).notNull(),

  // Mécanique de prévente
  isPublic: boolean('is_public').default(false).notNull(),
  preorderEnabled: boolean('preorder_enabled').default(false).notNull(),
  estimatedAvailableAt: timestamp('estimated_available_at'),
  expectedHarvestDate: timestamp('expected_harvest_date'),
  status: text('status').default('DRAFT').notNull(),

  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('market_offers_producer_idx').on(t.producerId),
  index('market_offers_farm_idx').on(t.farmId),
  index('market_offers_subcategory_idx').on(t.subCategoryId),
  index('market_offers_public_idx').on(t.isPublic),
  index('market_offers_preorder_idx').on(t.preorderEnabled),
  index('market_offers_available_at_idx').on(t.estimatedAvailableAt),
]);
```

`farms` dégraissée :

```ts
export const farms = marketplaceSchema.table('farms', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').notNull(),
  location: text('location'),
  size: doublePrecision('size'),
  zoneId: uuid('zone_id').references(() => zones.id),
  producerId: uuid('producer_id').notNull().references(() => producers.id),
  createdAt: timestamp('created_at').defaultNow().notNull(),
  updatedAt: timestamp('updated_at').defaultNow().notNull().$onUpdate(() => new Date()),
}, (t) => [
  index('farms_producer_idx').on(t.producerId),
  index('farms_zone_idx').on(t.zoneId),
]);
```

`orders.cropCycleId` → `orders.marketOfferId` (référence `marketOffers.id`, index renommé).

---

## 3. Modèles alignés (`ladini/domain/models.py`)

### 3.1 — `BaseMarketplaceModel` (contrainte DRY Pydantic v2)

Un socle unique pour **tous** les DTO marketplace : conversion ORM→DTO en `from_attributes`, tolérance camelCase↔snake_case (frontend Drizzle ↔ backend), validation standardisée.

```python
# ladini/domain/base_model.py
from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, field_validator


def to_camel(s: str) -> str:
    head, *tail = s.split("_")
    return head + "".join(w.capitalize() for w in tail)


class BaseMarketplaceModel(BaseModel):
    """Socle commun à tous les DTO de la marketplace.

    - `from_attributes` : hydrate directement depuis un objet SQLAlchemy.
    - `populate_by_name` + `alias_generator=to_camel` : accepte indifféremment
      `price_per_unit` (backend) et `pricePerUnit` (payload Drizzle/front).
    - validation standardisée des quantités/prix (>= 0).
    """
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
        str_strip_whitespace=True,
        extra="ignore",
    )

    def to_db(self) -> dict[str, Any]:
        """Payload snake_case prêt pour l'ORM (jamais d'alias camelCase)."""
        return self.model_dump(by_alias=False, exclude_none=True)

    @classmethod
    def validate_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("La valeur doit être positive ou nulle.")
        return v
```

### 3.2 — DTO alignés 1-pour-1 sur les tables (extraits critiques)

```python
# ladini/domain/dto/catalog.py
from datetime import datetime
from typing import Optional
from pydantic import field_validator
from ladini.domain.base_model import BaseMarketplaceModel


class FarmModel(BaseMarketplaceModel):
    """↔ marketplace.farms"""
    id: Optional[str] = None
    name: str
    location: Optional[str] = None
    size: Optional[float] = None
    producer_id: str
    zone_id: Optional[str] = None


class MarketOfferModel(BaseMarketplaceModel):
    """↔ marketplace.market_offers (ex crop_cycles)"""
    id: Optional[str] = None
    producer_id: str
    farm_id: Optional[str] = None
    sub_category_id: Optional[str] = None
    product_label: str
    production_type: str = "CROP"
    unit: str = "KG"
    price_per_unit: Optional[float] = None
    available_quantity: float = 0.0
    reserved_quantity: float = 0.0
    is_public: bool = False
    preorder_enabled: bool = False
    expected_harvest_date: Optional[datetime] = None
    status: str = "DRAFT"

    _check_qty = field_validator("available_quantity", "reserved_quantity", "price_per_unit")(
        BaseMarketplaceModel.validate_non_negative
    )


class ProductModel(BaseMarketplaceModel):
    """↔ marketplace.products"""
    id: Optional[str] = None
    name: str = "Produit"
    category_label: str
    sub_category_id: Optional[str] = None
    price: float
    unit: str = "KG"
    quantity_for_sale: float = 0.0
    is_available: bool = True
    producer_id: str

    _check_price = field_validator("price", "quantity_for_sale")(
        BaseMarketplaceModel.validate_non_negative
    )


class OrderItemModel(BaseMarketplaceModel):
    """↔ marketplace.order_items"""
    id: Optional[str] = None
    order_id: Optional[str] = None
    product_id: str
    quantity: float
    price_at_sale: float


class OrderModel(BaseMarketplaceModel):
    """↔ marketplace.orders — étapes Commande→Paiement→Livraison→Confirmation"""
    id: Optional[str] = None
    buyer_id: Optional[str] = None
    market_offer_id: Optional[str] = None   # ex crop_cycle_id (prévente)
    status: str = "PENDING"            # PENDING→CONFIRMED→FULFILLED→CLOSED
    payment_status: str = "PENDING"   # PENDING→PAID→REFUNDED
    delivery_status: str = "PENDING"  # PENDING→ASSIGNED→DELIVERED
    payment_method: str = "CASH"
    currency: str = "XOF"
    subtotal: float = 0.0
    delivery_fee: float = 0.0
    tax_amount: float = 0.0
    total_amount: float
    items: list[OrderItemModel] = []
```

> **Cohérence stricte garantie** : chaque `field` snake_case correspond à une colonne ORM ; l'`alias_generator` camelCase absorbe les payloads Drizzle sans divergence. Un changement de colonne côté `frontag` casse immédiatement la validation Pydantic (fail-fast), ce qui est l'objectif.

Côté **ORM** (`models.py`), suppression des 19 classes de la §1.1, renommage `CropCycle`→`MarketOffer` (`__tablename__ = "market_offers"`) et purge des colonnes de la §1.2. Les `__all__` sont mis à jour en conséquence.

---

## 4. Couche d'accès unifiée (`services/database/`)

### 4.1 — Constat

Aujourd'hui `AgriDatabaseService` compose **12 mixins** dont plusieurs 100 % conseil : `CropMixin`, `IntelligenceMixin`, une grande partie de `DashboardsMixin`. Le dispatcher `__getattribute__` référence ~25 méthodes de conseil dans `_READ_ONLY_METHODS` (`get_crop_profile`, `calculate_irrigation_need`, `evaluate_disease_and_climate_risk`, `analyze_thermal_stress`, …).

### 4.2 — Cible : 3 services spécialisés

On remplace la nébuleuse de mixins « récupération » par **3 services à responsabilité unique**, tous adossés au même socle transactionnel.

```
services/database/
  engine.py            # DatabaseEngine (voir §5)
  base_service.py      # _BaseService : session ContextVar + @transactional
  product_service.py   # ProductService   (catalogue, market_offers, stocks)
  order_service.py     # OrderService     (panier→commande→paiement→livraison)
  user_context_service.py  # UserContextService (identité, profils, mémoire agent, trust)
```

`_BaseService` factorise l'accès session (repris de l'actuel `BaseMixin`/dispatcher) sans le polymorphisme conseil :

```python
# services/database/base_service.py
from __future__ import annotations
import functools
from contextvars import ContextVar
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ladini.services.database.engine import engine

db_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar("db_session_ctx", default=None)


def transactional(*, write: bool = False):
    """Décorateur explicite (remplace le __getattribute__ magique et fragile).
    Ouvre une session racine si aucune n'est active, commit si write=True."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            existing = db_session_ctx.get()
            if existing is not None:
                return await fn(self, existing, *args, **kwargs)
            async with engine.sessionmaker()() as session:
                token = db_session_ctx.set(session)
                try:
                    result = await fn(self, session, *args, **kwargs)
                    if write:
                        await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise
                finally:
                    db_session_ctx.reset(token)
        return wrapper
    return deco


class BaseService:
    @property
    def session(self) -> Optional[AsyncSession]:
        return db_session_ctx.get()
```

Exemple `ProductService` (le `@transactional` explicite remplace le `_READ_ONLY_METHODS` implicite) :

```python
# services/database/product_service.py
from sqlalchemy import select
from ladini.domain.models import Product, MarketOffer
from ladini.domain.dto.catalog import ProductModel, MarketOfferModel
from .base_service import BaseService, transactional


class ProductService(BaseService):
    @transactional(write=False)
    async def search_products(self, session, *, query: str, limit: int = 20) -> list[ProductModel]:
        stmt = (
            select(Product)
            .where(Product.is_available.is_(True))
            .where(Product.name.op("%")(query))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [ProductModel.model_validate(p) for p in rows]

    @transactional(write=True)
    async def publish_offer(self, session, offer: MarketOfferModel) -> str:
        obj = MarketOffer(**offer.to_db())
        session.add(obj)
        await session.flush()
        return str(obj.id)
```

`OrderService` porte la machine à états `Commande→Paiement→Livraison→Confirmation` (transitions atomiques via `session.begin_nested()`), `UserContextService` absorbe l'actuel `BaseMixin` (résolution téléphone→profils, mémoire agent, trust score).

> **Migration douce** : on peut conserver `AgriDatabaseService` comme *façade* déléguant aux 3 services le temps de basculer les nœuds, puis le retirer.

---

## 5. Stratégie de connexion — `DatabaseEngine`

L'actuel `core/database.py` est déjà solide (contexte SSL explicite, `statement_cache_size=0` pour DigitalOcean, `pool_pre_ping`). On **encapsule** cette logique dans une classe unique pour supprimer les globals `_async_engine/_engine/_SessionLocal` et **éliminer définitivement le warning SSL**.

**Origine du warning SSL** : asyncpg **ne comprend pas** le paramètre libpq `sslmode=` s'il reste dans l'URL — il faut le retirer de l'URL et passer un `ssl.SSLContext` via `connect_args`. Le code actuel le fait déjà (`clean_url = url.split("?")[0]`), mais le rendre systématique et centralisé garantit qu'aucun `?sslmode=require` ne refait surface.

```python
# services/database/engine.py
from __future__ import annotations
import logging, ssl
from pathlib import Path
from typing import Optional
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, create_async_engine, async_sessionmaker,
)
from ladini.core.settings import settings

logger = logging.getLogger("Ladini.Engine")


class DatabaseEngine:
    """Point d'entrée unique, asynchrone et robuste (DigitalOcean Managed PG)."""

    def __init__(self) -> None:
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None

    # -- Construction paresseuse et idempotente ------------------------------
    def _build(self) -> None:
        if self._engine is not None:
            return
        if not settings.DATABASE_URL:
            raise RuntimeError("DATABASE_URL manquante.")

        # 1. URL propre : jamais de sslmode dans l'URL (asyncpg le rejette).
        raw = str(settings.DATABASE_URL).strip().strip('"').strip("'")
        url = raw.split("?")[0].replace("postgresql://", "postgresql+asyncpg://")

        # 2. SSL piloté par settings, passé via connect_args (pas l'URL).
        connect_args: dict = {
            "prepared_statement_cache_size": 0,  # DO rote les plans en maintenance
            "statement_cache_size": 0,
        }
        ctx = self._ssl_context()
        if ctx is not None:
            connect_args["ssl"] = ctx

        self._engine = create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=max(int(getattr(settings, "DB_POOL_SIZE", 10) or 10), 5),
            max_overflow=max(int(getattr(settings, "DB_POOL_MAX_OVERFLOW", 5) or 5), 0),
            pool_timeout=max(float(getattr(settings, "DB_POOL_TIMEOUT", 30) or 30), 10.0),
            pool_recycle=1800,        # recycle avant coupure côté DO
            pool_pre_ping=True,       # anti connexion morte
            echo=bool(getattr(settings, "DEBUG", False)),
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine, class_=AsyncSession, expire_on_commit=False,
        )
        logger.info("✅ DatabaseEngine prêt (asyncpg, SSL=%s).", getattr(settings, "DB_SSL_MODE", "verify-full"))

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        mode = str(getattr(settings, "DB_SSL_MODE", "verify-full") or "verify-full").lower()
        if mode == "disable":
            logger.warning("SSL désactivé (local uniquement).")
            return None
        if mode == "require":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # TLS sans vérif CA
            return ctx
        if mode == "verify-full":
            ca = getattr(settings, "DB_CA_PATH", None)
            if not ca:
                raise RuntimeError("DB_CA_PATH requis pour verify-full.")
            p = Path(ca)
            if not p.is_absolute():
                p = (Path(settings.BASE_DIR) / p).resolve()
            if not p.exists():
                raise FileNotFoundError(f"CA introuvable : {p}")
            ctx = ssl.create_default_context(cafile=str(p))
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            return ctx
        raise RuntimeError(f"DB_SSL_MODE invalide : {mode}")

    # -- API publique --------------------------------------------------------
    def engine(self) -> AsyncEngine:
        self._build()
        return self._engine  # type: ignore[return-value]

    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        self._build()
        return self._sessionmaker  # type: ignore[return-value]

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None


# Singleton importable partout
engine = DatabaseEngine()
```

Gains : plus de globals dispersés, cycle de vie clair (`engine.dispose()`), `pool_recycle` ajouté (les connexions DO sont coupées après ~30 min d'inactivité), et **zéro `sslmode` dans l'URL** — la source classique du warning asyncpg. `core/database.py` peut ré-exporter `get_db`/`get_sessionmaker` en délégant à `engine` pour ne rien casser.

---

## 6. Impact `interpreter/` et `core/` (selon ta directive)

Non bloquant pour la refonte data, mais listé pour la cohérence du pivot :

- **`interpreter/intent.py`** : ne conserver que les intents transactionnels (`SEARCH_PRODUCT`, `ADD_TO_CART`, `CHECKOUT`, `CONFIRM_PAYMENT`, `TRACK_ORDER`, `PUBLISH_OFFER`). Supprimer les intents de conseil.
- **`interpreter/strategy.py`** : à supprimer si sa logique est de la recommandation agronomique (à confirmer par lecture).
- **`core/slots.py`** : standardiser sur les slots transactionnels (`product_id`, `quantity`, `delivery_location`, `payment_method`).
- **`core/state_compaction.py`** : prioritaire (état ~16 Ko). Purger les entités conseil (profils culture, météo, capteurs) dès qu'une intention d'achat est détectée.

> Ces fichiers seront traités en **passe 2**, une fois le socle data validé — ils dépendent des DTO définis en §3.

---

## 7. Ordre d'exécution proposé (quand tu valides)

1. **Schémas Drizzle** (`intelligence.ts`, `marketplace.ts`, retrait `inventory.ts` si arbitré) → `drizzle-kit generate` (migration de suppression + rename).
2. **`models.py`** : purge 19 classes + rename `MarketOffer` + dégraissage colonnes + `BaseMarketplaceModel` + DTO.
3. **Services** : `engine.py` → `base_service.py` → 3 services ; façade de compat sur `AgriDatabaseService`.
4. **Nœuds/flows** market_coach : bascule des appels vers les 3 services.
5. **`interpreter`/`core`** : passe 2 (intents, slots, compaction).
6. Migration SQL `crop_cycles`→`market_offers` avec `ALTER TABLE ... RENAME` (préserve les données) + `RENAME COLUMN` sur `orders.crop_cycle_id`.

Chaque étape est indépendamment testable ; la 1 et la 2 doivent atterrir dans le **même commit** pour garder frontag↔ladini synchrones.
