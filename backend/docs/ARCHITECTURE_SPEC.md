# ARCHITECTURE_SPEC.md — Ladini Backend

**Périmètre** : `backend/src/ladini/**` (Python). Exclus : `.venv`, `__pycache__`, `.pytest_cache`, `tests/`, `migrations/`. Le sous-système `graphs/agents/market_coach/` (LangGraph, ~95 fichiers) est traité en détail séparé (§3.B) vu son volume.
**Non couvert** : `frontend/`, `infra/`, `bin/`, `scripts/` (hors scope backend Python).

---

# 1. ARBORESCENCE & DÉPENDANCES

## 1.1 Arborescence exacte

```
backend/src/ladini/
├── main.py                          # FastAPI factory alternative (NON utilisée en prod, voir §5.5)
├── agents/
│   ├── __init__.py                  # re-exports
│   ├── dispatcher.py                # BaseAgentDispatcher générique
│   ├── forms.py                     # FormSpec/SlotSpec engine (PRODUCT/AUCTION/CROP_CYCLE forms)
│   ├── gateway.py                   # DataGateway (MCP-first, DB-fallback)
│   ├── identity.py                  # UserIdentity + resolve_identity
│   ├── onboarding.py                 # OnboardingState/Step FSM
│   ├── reducers.py                  # merge_dict/replace_value/replace_list/load_snapshot (LangGraph reducers, PARTAGÉS)
│   └── task_handler.py              # TaskHandler FSM alternatif (Pydantic), semble indépendant du graphe MarketCoach
├── api/
│   ├── __init__.py
│   ├── celery_app.py                 # Celery singleton
│   ├── main.py                       # **FastAPI app RÉELLEMENT utilisée en prod**
│   ├── schemas.py                    # UserRequest/SuccessResponse/AsyncQueuedResponse/TaskStatusResponse
│   ├── security.py                   # verify_twilio_signature
│   ├── server.py                     # launch script (gunicorn/uvicorn)
│   ├── tasks.py                      # process_agent_task (Celery task, worker entrypoint)
│   └── routes/
│       ├── __init__.py               # VIDE (voir §5.5)
│       ├── market.py                 # /market/producer, /market/buyer, /market/status/{id}
│       └── twilio_webhook.py         # /webhook/twilio
├── core/
│   ├── __init__.py                   # re-exports settings/logger/database/security/llm
│   ├── database.py                   # engine SQLAlchemy async unique
│   ├── get_llm.py                    # Groq client + _GroqAdapter (choke-point télémétrie)
│   ├── llm.py                        # re-export de get_llm
│   ├── logger.py                     # setup_logging + Sentry init
│   ├── security.py                   # sanitize_user_input, validate_api_key
│   ├── settings.py                   # Settings(BaseSettings) — config globale
│   ├── telemetry.py                  # OTel + Prometheus + Langfuse unifiés
│   └── tracing.py                    # LangSmith (séparé de telemetry.py)
├── domain/                           # ORM SQLAlchemy + DTO Pydantic (persistance métier globale)
│   ├── __init__.py
│   ├── base_model.py                 # BaseMarketplaceModel (Pydantic, alias camelCase)
│   ├── models.py                     # façade re-export de tous les modèles ORM
│   ├── orm_base.py                   # Base = declarative_base(), _uuid4()
│   ├── catalog/{models,dto}.py       # Warehouse, Farm, MarketOffer(=CropCycle), Stock, StockMovement, Batch, Expense, Product
│   ├── governance/models.py          # Organization, Zone, Category, SubCategory, StandardPrice, ProhibitedTerm, ...
│   ├── identity/{models,dto}.py      # User, Account, Session, Producer, Client, BuyerType, BuyerProfile, DeliveryAgent, TrustScore
│   ├── intelligence/models.py        # AuditLog, AgentAction, Conversation, ModerationEvent, DemandSignal, Solicitation, NotificationOutbox
│   └── orders/{models,dto}.py        # Delivery, Order, OrderItem, Payment, OrderStatusHistory, OrderReminder, OrderDispute, Auction, Bid, MarketplaceRating
├── graphs/
│   ├── __init__.py
│   ├── factory.py                    # GraphFactory (cache par role/runtime/checkpointer)
│   ├── roles.py                      # normalize_role, is_goal_allowed, is_tool_allowed
│   ├── state.py                      # GlobalAgriState (TypedDict LEGACY multi-expert, probablement mort)
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── common/{config,domains,output,voice}.py   # BaseAgentConfig, AgentDomain, AgriAgentOutput, VoiceAgent
│   │   └── market_coach/             # === §3.B, arborescence détaillée séparée ===
├── infrastructure/mcp/
│   ├── __init__.py                   # lazy re-exports
│   ├── base.py                       # MCPServerApp, MCPToolSpec, BaseServer
│   ├── client.py                     # AgriMCPClient + adapters (stdio/http/grpc)
│   ├── context.py                    # FastMCP "context server" (FarmerContext, cache sémantique)
│   ├── main.py                       # script de test manuel
│   ├── runtime.py                    # MCPRuntime, AgriDBMCPServer (singleton global `runtime`)
│   ├── security.py                   # PermissionScope, TOOL_SCOPE_MAP, ToolExecutionPolicy, MCPPermissionHostApp (préflight)
│   └── utils.py                      # run_coro_blocking (bridge sync/async)
├── orchestrator/
│   ├── __init__.py
│   └── orchestrator.py               # **Orchestrator** — point d'entrée métier unique
├── protocols/
│   ├── __init__.py
│   ├── ag_ui/{components,formatter,renderer}.py   # AGUIComponent hierarchy, WhatsApp/Web/SMS renderers — **renderer.py CASSÉ** (§5.1)
│   └── mcp/
│       ├── __init__.py
│       └── servers/{db_server,h,http_server}.py   # stdio entrypoint, tool-registry generator, HTTP daemon (port 8003)
├── services/
│   ├── __init__.py                    # `__all__` référence `AgriDatabase` **jamais défini** (§5.6)
│   ├── database/                      # === couche transactionnelle SQL, doc complète §3.C ===
│   └── memory/                        # user_profile, episodic_memory, profile_extractor, context_optimizer — **import cassé** (§5.2)
├── workers/
│   ├── __init__.py
│   ├── beat_schedule.py               # BEAT_SCHEDULE (Celery Beat, 3 crons)
│   ├── runtime.py                     # run_async, worker_session
│   ├── automation/{auction_automation_service,proximity_matching_service,targeting}.py
│   ├── crons/{auction_solicitation,outbox_dispatch,proximity_matching}.py
│   ├── outbox/{dispatcher,templates}.py + channels/{base,email,push,whatsapp}.py
│   └── repositories/{outbox_repo,solicitation_repo}.py
└── workspace/
    ├── __init__.py
    ├── checkpointer.py                 # WorkspaceCheckpointer(BaseCheckpointSaver) — LangGraph persistence
    ├── context_guard.py                # ContextGuard (no-op, mono-agent)
    ├── metadata.py                     # clean_form_data/clean_mapping/build_metadata_from_state
    ├── models.py                       # Workspace (dataclass)
    ├── resolver.py                     # WorkspaceResolver
    └── store.py                        # WorkspaceStore (Postgres, table agri_workspaces)
```

## 1.2 Graphe des imports (fichier → module importé, intra-repo)

```
main.py -> core.settings, core.logger, core.database, api.routes (VIDE, mort)
api/main.py -> api.routes.market, api.routes.twilio_webhook, core.telemetry
api/server.py -> api.main
api/celery_app.py -> workers.beat_schedule
api/tasks.py -> api.celery_app, core.database, core.settings, orchestrator, core.telemetry
api/routes/market.py -> api.tasks, api.celery_app
api/routes/twilio_webhook.py -> api.tasks, graphs.roles, workspace.store, api.security, core.settings

core/logger.py -> core.settings
core/telemetry.py -> core.settings (lazy)
core/tracing.py -> core.settings
core/database.py -> core.settings, domain.models (lazy), services.database.model (lazy, **CASSÉ**)
core/get_llm.py -> core.settings, core.telemetry (lazy)
core/llm.py -> core.get_llm
core/__init__.py -> core.{settings,logger,database,security,llm}

domain/models.py -> domain.{orm_base, identity.models, catalog.models, orders.models, governance.models, intelligence.models}

graphs/roles.py -> graphs.agents.market_coach.interpreter.intent.INTENT_CONFIG
graphs/factory.py -> graphs.agents.market_coach.core.graph_builder.build_graph, graphs.roles
graphs/agents/common/voice.py -> services.voice_engine (**MODULE MANQUANT**), core.settings, orchestrator (lazy)

infrastructure/mcp/client.py -> infrastructure.mcp.security
infrastructure/mcp/runtime.py -> core.database, core.settings, infrastructure.mcp.{context,security,utils}, services.database, protocols.mcp.servers.h (lazy)
infrastructure/mcp/base.py -> infrastructure.mcp.{context,security,utils}
infrastructure/mcp/context.py -> infrastructure.mcp.utils, services.memory (lazy), protocols.core (lazy, **CASSÉ**)
infrastructure/mcp/security.py -> protocols.mcp.servers.h (lazy)
infrastructure/mcp/main.py -> infrastructure.mcp.security

protocols/mcp/servers/http_server.py -> core.database, infrastructure.mcp.runtime
protocols/mcp/servers/db_server.py -> infrastructure.mcp.runtime
protocols/mcp/servers/h.py -> services.database.d
protocols/ag_ui/renderer.py -> protocols.core (**CASSÉ, module inexistant**)

workspace/store.py -> core.database, workspace.metadata, workspace.models
workspace/resolver.py -> workspace.models, workspace.store
workspace/checkpointer.py -> workspace.metadata, workspace.models, workspace.store

agents/forms.py -> graphs.agents.market_coach.services.domain.quantity_unit
agents/identity.py -> agents.gateway
agents/gateway.py -> infrastructure.mcp.context

orchestrator/orchestrator.py -> workspace.*, graphs.factory, graphs.roles, graphs.agents.market_coach.utils

services/database/d.py -> services.database.{auth,utils,marketplace,category,buyer,buyer_verification,producer,product,auction,moderation,base_service}
services/database/{producer,marketplace,buyer,auction,product,category,delivery,buyer_verification,moderation,order}.py -> domain.models, services.database.{base,common,search,errors}
services/memory/{user_profile,episodic_memory}.py -> services.database.model (**CASSÉ, module inexistant**)

workers/runtime.py -> core.database, services.database.base_service, api.tasks (lazy)
workers/crons/*.py -> api.celery_app, workers.automation.*, workers.runtime
workers/automation/*.py -> domain.models, workers.outbox.templates, workers.repositories.{outbox_repo,solicitation_repo}
workers/outbox/dispatcher.py -> workers.outbox.{templates,channels}, workers.repositories.outbox_repo, workers.runtime
workers/repositories/*.py -> domain.models
```

Import graph interne à `market_coach/` : voir §3.B.0 (pipeline) et §3.B.13 (imports croisés).

## 1.3 Défauts d'imports détectés (voir détail §5)

| Import cassé | Cause |
|---|---|
| `protocols.core` (`ClientCapabilities`, `TraceCategory`, `TraceEnvelope`, `CachePolicy`) | Module inexistant (seul un `.pyc` orphelin subsiste) — casse `protocols/ag_ui/renderer.py` et `infrastructure/mcp/context.py` |
| `services.database.model` (`Base`) | Module inexistant — casse `services/memory/user_profile.py` et `services/memory/episodic_memory.py` |
| `services.voice_engine` (`VoiceEngine`) | Module inexistant — casse `graphs/agents/common/voice.py::VoiceAgent` |

---

# 2. INTERFACES & SCHÉMAS DE DONNÉES (EXTRACT BRUT)

## 2.1 `api/schemas.py`
```python
class UserRequest(BaseModel):
    user_id: str
    zone_id: str = "Centre"
    query: str
    crop: Optional[str] = "Inconnue"
    flow_type: str = "MESSAGE"
    async_mode: bool = False
    user_level: str = "debutant"

class SuccessResponse(BaseModel):
    status: str = "success"
    response: str
    audio_url: Optional[str] = None
    trace: List[str] = []

class AsyncQueuedResponse(BaseModel):
    status: str = "queued"
    task_id: str
    message: str = "Votre demande est en cours de traitement..."
    check_status_at: str

class TaskStatusResponse(BaseModel):
    status: str
    task_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
```

## 2.2 `api/routes/market.py`
```python
class AgentRequest(BaseModel):
    message: str = Field(...)
    phone_number: str = Field(...)
    state_updates: Dict[str, Any] = Field(default_factory=dict)
```

## 2.3 `core/settings.py` — `class Settings(BaseSettings)` (extrait, champs saillants)
```
APP_NAME, APP_VERSION, DEBUG: bool, HOST, PORT: int, ALLOWED_ORIGINS: list[str]
LLM_PROVIDER="groq", LADINI_APIKEY, GROQ_API_KEY, LLM_MODEL="llama-3.1-8b-instant",
LLM_MODEL_REASONING="llama-3.3-70b-versatile", LLM_TEMPERATURE: float=0.0, ROUTING_MAP: dict[str,str]
AZURE_OPENAI_API_KEY/ENDPOINT/DEPLOYMENT_NAME/API_VERSION
DATABASE_URL, DO_DATABASE_URL, DB_SSL_MODE="require", DB_POOL_SIZE=20, DB_POOL_MAX_OVERFLOW=20, DB_POOL_TIMEOUT=60.0
REDIS_URL, VALKEY_ENDPOINT/AUTH_TOKEN/USE_TLS, CELERY_BROKER_URL/RESULT_BACKEND
MCP_ALLOW_DEGRADED_START, MCP_DB_STARTUP_RETRIES/RETRY_DELAY_SEC, MCP_DB_SERVER_HOST/PORT, MCP_DB_TRANSPORT="http",
MCP_DB_STDIO_ENTRYPOINT/CWD/PYTHON/ENV, MCP_DB_HTTP_URL/HEADERS, MCP_DB_GRPC_TARGET/TLS/METADATA/LIST_TOOLS_METHOD/CALL_TOOL_METHOD
AZURE_SPEECH_KEY(_2), AZURE_REGION, AZURE_SPEECH_ENDPOINT, USE_AZURE_SPEECH
TWILIO_ACCOUNT_SID/AUTH_TOKEN/WHATSAPP_NUMBER, TWILIO_INTERACTIVE_ENABLED, TWILIO_CONFIRM_CONTENT_SID
LANGCHAIN_TRACING_V2/API_KEY/PROJECT/ENDPOINT, LANGSMITH_API_KEY
OTEL_ENABLED, OTEL_EXPORTER_OTLP_ENDPOINT
PROMETHEUS_ENABLED=True
LANGFUSE_ENABLED/PUBLIC_KEY/SECRET_KEY/HOST
EMBEDDING_MODEL, RAG_EMBEDDING_DIM=768, CHUNK_SIZE/OVERLAP, TOP_K_RETRIEVAL/RERANK (+ par niveau DEBUTANT/INTER/EXPERT)
SENTRY_DSN, SENTRY_ENVIRONMENT
AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY/SESSION_TOKEN/REGION, S3_BUCKET/KEY_PREFIX/REGION, INGESTION_S3_EXCLUDE_PREFIXES: list[str]
```
Propriétés : `llm_api_key`, `celery_broker`, `celery_backend`, `langsmith_enabled`. `@model_validator(mode="after") _build_routing_map`.

## 2.4 `protocols/ag_ui/components.py` (multi-canal WhatsApp/Web/SMS)
```python
class ComponentType(str, Enum): TEXT, CARD, ACTION, LIST_PICKER, FORM_FIELD, CHART, ALERT, USER_APPROVAL, CONTEXT_REQUEST
class Severity(str, Enum): INFO, WARNING, HIGH, CRITICAL
class ActionType(str, Enum): SELL, BUY, CONTACT, NAVIGATE, CONFIRM, CANCEL, CALL_EXPERT, VIEW_DETAIL

@dataclass
class AGUIComponent:
    type: ComponentType
    id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TextBlock(AGUIComponent):
    type: ComponentType = ComponentType.TEXT
    content: str = ""
    format: str = "plain"
    voice_text: str = ""

@dataclass
class ActionButton(AGUIComponent):
    type: ComponentType = ComponentType.ACTION
    label: str = ""
    action_type: ActionType = ActionType.NAVIGATE
    payload: Dict[str, Any] = field(default_factory=dict)
    confirm_required: bool = False

@dataclass
class Card(AGUIComponent):
    type: ComponentType = ComponentType.CARD
    title: str = ""
    subtitle: str = ""
    body: str = ""
    image_url: str = ""
    fields: List[Dict[str, str]] = field(default_factory=list)
    actions: List[ActionButton] = field(default_factory=list)
    severity: Optional[Severity] = None

@dataclass
class ListPicker(AGUIComponent):
    type: ComponentType = ComponentType.LIST_PICKER
    title: str = ""
    items: List[Dict[str, str]] = field(default_factory=list)
    multi_select: bool = False

@dataclass
class FormField(AGUIComponent):
    type: ComponentType = ComponentType.FORM_FIELD
    label: str = ""
    field_type: str = "text"
    placeholder: str = ""
    required: bool = True
    validation: Dict[str, Any] = field(default_factory=dict)
    options: List[str] = field(default_factory=list)

@dataclass
class ChartData(AGUIComponent):
    type: ComponentType = ComponentType.CHART
    chart_type: str = "line"
    title: str = ""
    labels: List[str] = field(default_factory=list)
    datasets: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class AlertBanner(AGUIComponent):
    type: ComponentType = ComponentType.ALERT
    title: str = ""
    message: str = ""
    severity: Severity = Severity.INFO
    zone: str = ""
    expires_at: str = ""
    actions: List[ActionButton] = field(default_factory=list)

@dataclass
class UserApproval(AGUIComponent):
    type: ComponentType = ComponentType.USER_APPROVAL
    action_id: str = ""
    action_summary: str = ""
    risk_level: Severity = Severity.WARNING
    requires_validation: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300
    callback_url: str = ""

@dataclass
class ContextRequest(AGUIComponent):
    type: ComponentType = ComponentType.CONTEXT_REQUEST
    missing_fields: List[Dict[str, str]] = field(default_factory=list)
    message: str = ""
```

## 2.5 `domain/` — modèles ORM SQLAlchemy (schémas Postgres)

**Schéma `auth`** (`domain/identity/models.py`):
- `User` (`auth.users`): `id PG_UUID PK`, `name`, `email UNIQUE`, `email_verified DateTime`, `image`, `password`, `phone UNIQUE`, `whatsapp_enabled Bool=True`, `onboarding_completed Bool NOT NULL default false`, `latitude/longitude Float`, `cnib_number UNIQUE`, `role="USER"`, `identity_verified Bool=False`, `zone_id FK governance.zones.id`, `account_status="ACTIVE"`, `blocked_reason Text`, `blocked_at`, `deleted_at`, `created_at/updated_at`.
- `Account` (`auth.accounts`): `id`, `user_id FK CASCADE`, `type`, `provider`, `provider_account_id`, `refresh_token/access_token Text`, `expires_at Int`, `token_type`, `scope`, `id_token`, `session_state`.
- `Session` (`auth.sessions`): `id`, `session_token UNIQUE`, `user_id FK CASCADE`, `expires DateTime NOT NULL`.

**Schéma `marketplace`** (`domain/identity/models.py`, `domain/catalog/models.py`, `domain/orders/models.py`):
- `Producer`: `id`, `user_id FK UNIQUE`, `organization_id FK`, `zone_id FK`, `business_name`, `status="PENDING"`, `is_certified=False`, `region/province/commune`, `logo_url`, `phone_number`, `rating Int`, `reviews_count=0`, `company_registration_number`, timestamps.
- `Client`: `id`, `producer_id FK`, `name NOT NULL`, `phone NOT NULL`, `email`, `location`, `total_orders=0`, `total_spent=0.0`, `last_order_date`, `tax_id`, `prefered_payement_method JSONB`.
- `BuyerType`: `id`, `name UNIQUE NOT NULL`, `description`.
- `BuyerProfile`: `id`, `user_id FK UNIQUE`, `buyer_type_id FK`, `establishment_name`, `default_delivery_address Text`, `is_verified=False`, `trust_badge`, `rating Float`, `reviews_count=0`, `company_registration_number`, `verified_at/verified_by_id`.
- `DeliveryAgent`: `id`, `user_id FK UNIQUE`, `vehicle_type`, `license_number`, `status="OFFLINE"`, `zone_id FK`.
- `Warehouse`: `id`, `name NOT NULL`, `type NOT NULL`, `capacity Float`, `location`, `zone_id FK`.
- `Farm`: `id`, `name NOT NULL`, `location`, `size Float`, `producer_id FK NOT NULL`, `zone_id FK`.
- `MarketOffer` (alias `CropCycle`): `id`, `producer_id FK NOT NULL`, `farm_id FK`, `sub_category_id FK`, `product_label NOT NULL`, `production_type="CROP"`, `species`, `breed`, `unit="KG"`, `price_per_unit Numeric(12,2)`, `available_quantity/reserved_quantity/current_stock Numeric(14,3)=0`, `is_public/preorder_enabled=False`, `estimated_available_at/expected_harvest_date`, `status="DRAFT"`.
- `Stock`: `id`, `farm_id FK`, `warehouse_id FK`, `verified_by_id FK`, `organization_id FK`, `item_name NOT NULL`, `quantity Numeric(14,3)=0`, `unit="KG"`, `type="HARVEST"`, `verified_at`.
- `StockMovement`: `id`, `stock_id FK NOT NULL`, `type NOT NULL`, `quantity Numeric(14,3) NOT NULL`, `reason`, `created_at`.
- `Batch`: `id`, `stock_id FK NOT NULL`, `organization_id FK NOT NULL`, `batch_number UNIQUE NOT NULL`, `origin_farm_id FK`, `quantity Numeric(14,3) NOT NULL`.
- `Expense`: `id`, `farm_id FK NOT NULL`, `label NOT NULL`, `amount Numeric(14,2) NOT NULL`, `category default OTHER`, `date`.
- `Product`: `id`, `short_code UNIQUE`, `name="Produit"`, `category_label NOT NULL`, `sub_category_id FK`, `local_names JSONB`, `description Text`, `price Numeric(12,2) NOT NULL`, `unit="KG"`, `quantity_for_sale Numeric(14,3)=0`, `images PG_ARRAY(String) default '{}'`, `audio_url`, `quality_class`, `min_order_quality`, `packaging_type`, `harvest_date`, `is_available=True`, `producer_id FK NOT NULL`, `verified_at/verified_by_id`.
- `Delivery`: `id`, `order_id FK UNIQUE NOT NULL`, `delivery_agent_id FK`, `status="PENDING"`, `delivery_code`, `origin_gps_lat/lng`, `destination_gps_lat/lng`, `destination_desc Text`, `estimated_distance_km/actual_distance_km Float`, `shipping_condition`, `proof_of_delivery_url`, `assigned_at/picked_up_at/delivered_at/failed_at`.
- `Order`: 30+ colonnes: `buyer_id FK`, `client_id FK`, `organization_id FK`, `zone_id FK`, `customer_name/phone`, `payment_method="CASH"`, `payment_status="PENDING"`, `city`, `gps_lat/lng`, `delivery_desc Text`, `audio_url`, `status="PENDING"`, `delivery_status="PENDING"`, `source="APP"`, `order_type="STANDARD"`, `whatsapp_id`, `total_amount Numeric(14,2) NOT NULL`, `is_agent_order=False`, `delivery_date`, `subtotal/tax_amount/delivery_fee Numeric(14,2)=0`, `currency="XOF"`, `cancellation_role`, `escrow_wallet_id`, `market_offer_id FK`, `expected_fulfillment_date`, `preorder_converted_at`, `confirmed_at`, `auction_id FK UNIQUE`, `winning_bid_id FK`.
- `OrderItem`: `id`, `order_id FK NOT NULL`, `product_id FK NOT NULL`, `quantity Numeric(14,3) NOT NULL`, `price_at_sale Numeric(12,2) NOT NULL`.
- `Payment`: `id`, `order_id FK NOT NULL`, `amount Numeric(14,2) NOT NULL`, `currency="XOF"`, `method="CASH"`, `status="PENDING"`, `provider`, `provider_ref UNIQUE`, `escrow_wallet_id`, `failure_reason Text`, `authorized_at/captured_at/refunded_at`.
- `OrderStatusHistory`: `id`, `order_id FK NOT NULL`, `status_type NOT NULL`, `from_status`, `to_status NOT NULL`, `actor_id`, `note Text`, `created_at`.
- `OrderReminder`: `id`, `order_id FK NOT NULL`, `type NOT NULL`, `channel="WHATSAPP"`, `status="SCHEDULED"`, `scheduled_at NOT NULL`, `sent_at`, `attempts=0`, `last_error Text`.
- `OrderDispute`: `id`, `order_id FK NOT NULL`, `escrow_wallet_id`, `raised_by_id NOT NULL`, `reason_category NOT NULL`, `description Text NOT NULL`, `evidence_images PG_ARRAY`, `requested_solution NOT NULL`, `disputed_amount Numeric(14,2)=0`, `escrow_payout_status="HELD"`, `status="PENDING"`, `resolution_notes Text`, `resolved_at`.
- `Auction`: `id`, `buyer_id FK NOT NULL`, `sub_category_id FK NOT NULL`, `winner_bid_id FK`, `quantity Numeric(14,3) NOT NULL`, `unit="TONNE"`, `max_price_per_unit Numeric(12,2) NOT NULL`, `description Text`, `incoterm="DDP"`, `delivery_location NOT NULL`, `delivery_deadline NOT NULL`, `quality_grading`, `required_certifications PG_ARRAY`, `preferred_packaging`, `deadline NOT NULL`, `auto_extend=True`, `escrow_wallet_id`, `escrow_status="NONE"`, `status="OPEN"`, `cancellation_reason`, `target_zone_id FK`, `version=0`, `awarded_at/cancelled_at`.
- `Bid`: `id`, `auction_id FK NOT NULL`, `producer_id FK NOT NULL`, `offered_price Numeric(12,2) NOT NULL`, `linked_stock_id FK`, `is_winner=False`, `status="PENDING"`, `message Text`, `notified_at`, `valid_until`, `estimated_delivery_date`; UNIQUE `(auction_id, producer_id)`.
- `MarketplaceRating`: `id`, `order_id FK NOT NULL`, `author_type/author_id NOT NULL`, `target_type/target_id NOT NULL`, `rating_product_quality/rating_packaging/rating_reception_speed/rating_communication Integer`, `rating_reliability Integer NOT NULL`, `global_rating Float NOT NULL`, `comment Text`; UNIQUE `(order_id, author_id)`.

**Schéma `intelligence`**: `TrustScore` (`global_score/reliability_index/quality_index/compliance_index/resilience_bonus DOUBLE_PRECISION=0.0`), `AuditLog`, `AgentAction` (UNIQUE `order_id`), `Conversation` (UNIQUE `audit_trail_id`), `AgentContextMemory` (UNIQUE `user_id+context_key`), `AIRatingReasoning`, `ModerationEvent` (`kind`: PROHIBITED_PRODUCT|SCAM), `DemandSignal` (UNIQUE `normalized_term`), `Solicitation` (UNIQUE `auction_id+target_producer_id` et `market_offer_id+target_buyer_id`), `NotificationOutbox` (UNIQUE `dedupe_key`).

**Schéma `governance`**: `Organization`, `UserOrganization` (UNIQUE `user_id+organization_id`), `RoleDefinition`, `ClimaticRegion`, `Zone` (hiérarchique: `parent_id/path/depth/lat/lon/is_active`), `WorkZone` (UNIQUE `organization_id+zone_id`), `ZoneMetric`, `Category`, `SubCategory` (UNIQUE `category_id+name`, `blocked_zone_ids PG_ARRAY`), `StandardPrice` (UNIQUE `sub_category_id+zone_id`), `ZoneSetting` (UNIQUE `zone_id+key`, `value JSONB`), `ProhibitedTerm` (`term UNIQUE`, `category="ILLICIT"`, `severity="HIGH"`, `is_active=True`), `OverlayLayer` (UNIQUE `zone_id+key`).

## 2.6 `domain/base_model.py` / DTO pattern
```python
def to_camel(s: str) -> str: ...

class BaseMarketplaceModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, alias_generator=to_camel, str_strip_whitespace=True, extra="ignore", ser_json_inf_nan="constants")
    def to_db(self) -> dict[str, Any]: ...
    @staticmethod
    def _non_negative(v: Decimal | float | None) -> Decimal | float | None: ...
```
Toutes les DTO (`UserDTO`, `ProducerDTO`, `BuyerProfileDTO`, `TrustScoreDTO`, `UserContextDTO`, `FarmDTO`, `MarketOfferDTO`, `ProductDTO`, `OrderItemDTO`, `OrderDTO`, `PaymentDTO`, `AuctionDTO`, `BidDTO`, `DeliveryDTO`, `OrderStatusHistoryDTO`, `OrderReminderDTO`) héritent de `BaseMarketplaceModel`, champs `Optional[...] = Field(default=..., description=...)`, `@field_validator` non-négatif où pertinent.

## 2.7 `agents/task_handler.py` (FSM Pydantic alternative, indépendante du graphe MarketCoach)
```python
class GoalState(str, enum.Enum):
    WAITING_INPUT, WAITING_CONFIRMATION, EXECUTING, COMPLETED, ERROR_RECOVERY, HUMAN_INTERVENTION

class TaskPayload(BaseModel):
    phone: str = Field(..., min_length=8, max_length=32)
    goal: str = Field(..., min_length=1)
    price: Optional[float] = Field(None, ge=0)
    quantity: Optional[float] = Field(None, ge=0)
    unit: Optional[str] = Field("KG", max_length=12)
    context: Dict[str, Any] = Field(default_factory=dict)
    required_fields: List[str] = Field(default_factory=list)

class AgentState(BaseModel):
    payload: TaskPayload
    goal_state: GoalState = GoalState.WAITING_INPUT
    retry_count: int = 0
    goal_stack: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

## 2.8 `agents/dispatcher.py`
```python
@dataclass(slots=True)
class ActionContext:
    user_id: Optional[str] = None
    user_phone: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: Optional[Dict[str, Any]] = None

@dataclass(slots=True)
class PreparedAction:
    transport: Literal["mcp","db","noop"] = "mcp"
    target: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    fallbacks: Tuple["PreparedAction", ...] = ()

@dataclass(slots=True)
class ActionSpec:
    name: str
    mode: Literal["READ","WRITE","UTILITY"] = "READ"
    handler: ActionHandler
    description: str = ""
    required_fields: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## 2.9 `workspace/models.py`
```python
@dataclass(slots=True)
class Workspace:
    workspace_id: str
    workspace_type: str = "producer"
    active_goal: str = ""
    active_form: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    agent_state: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    tunnel_locked: bool = False
    _is_dirty: bool = field(default=False, init=False, repr=False, compare=False)
```

## 2.10 `infrastructure/mcp/client.py`
```python
@dataclass
class MCPTransportConfig:
    kind: str = "stdio"
    stdio_script: Optional[str] = None
    stdio_cwd: Optional[str] = None
    stdio_env: Mapping[str,str] = field(default_factory=dict)
    stdio_python: Optional[str] = None
    http_base_url: Optional[str] = None
    http_headers: Mapping[str,str] = field(default_factory=dict)
    grpc_target: Optional[str] = None
    grpc_tls: bool = False
    grpc_metadata: Mapping[str,str] = field(default_factory=dict)
    grpc_list_tools_method: str = "/ladini.mcp.MCP/ListTools"
    grpc_call_tool_method: str = "/ladini.mcp.MCP/CallTool"
    stdio_log_path: Optional[str] = None
```

## 2.11 `infrastructure/mcp/security.py`
```python
class PermissionScope(str, Enum): DB_READ_ONLY, DB_DATA_WRITE, DB_SCHEMA_MODIFY
class MCPServerKind(str, Enum): DB = "db"

class MCPToolMeta(BaseModel):
    name: str
    server: MCPServerKind
    scope: PermissionScope
    description: str = ""
    timeout_seconds: float = Field(default=15.0, ge=0.1)
    retries: int = Field(default=1, ge=0, le=5)

class ToolExecutionMeta(BaseModel):
    request_id: str
    user_id: str
    tool_name: str
    duration_ms: float
    timed_out: bool = False
    token_estimate: int = 0

class ToolExecutionEnvelope(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    meta: ToolExecutionMeta
```
Exceptions : `PermissionDenied(tool_name, reason)`, `ToolExecutionTimeout(tool_name, timeout_seconds)`, `HostBlockedError(tool_name, agent_message, suggestion)`.

**Audit MCP/AGUI 2026-08-26 — nettoyage architectural :** `MCPPermissionClient`,
`ShieldHub`, `MCPShield`, `UnifiedMCPClient`, `PermissionDecision`,
`RiskLevel` et `TOOL_RISK_MAP` ont été **supprimés**. Ce sous-arbre
construisait un moteur de décision de risque à trois niveaux
(`HITL_REQUIRED` pour les outils `HIGH`/`CRITICAL`) dont le seul appelant
dans tout le dépôt était un script de test manuel
(`infrastructure/mcp/main.py`, désormais adapté pour appeler
`AgriDBMCPServer` directement). Le chemin RÉELLEMENT emprunté par chaque
appel d'outil de l'agent (`AgriDBMCPServer.call_tool`, `runtime.py`) n'a
jamais consulté ce moteur : il applique uniquement `TOOL_SCOPE_MAP`
(fail-closed) + `MCPPermissionHostApp` (préflight SQL/fichiers sensibles,
toujours actif). Le VRAI point de confirmation humaine (HITL) est
`graphs/agents/market_coach/nodes/confirmation_gate.py`, en amont de tout
appel d'outil, dans le graphe de l'agent — pas au niveau du protocole MCP.

---

# 3. DÉTAIL DES MODULES & SIGNATURES

## 3.A — Backend hors `market_coach`

### `api/main.py` — **App FastAPI de production** ("LADINI MarketCoach API")
Rôle : point d'entrée HTTP réel, wiring télémétrie + routes.
```python
app = FastAPI(title=" LADINI MarketCoach API", version="1.1.0")
async def _startup_telemetry() -> None                       # @app.on_event("startup")
async def trace_and_metrics_middleware(request, call_next)   # @app.middleware("http")
@app.get("/health") async def health_check()
@app.get("/health/ready") async def readiness_check()        # SELECT 1 + Redis ping
@app.get("/metrics") async def metrics()                      # Prometheus scrape
```
Externe : Twilio (indirect via routes), OTel, Prometheus.

### `api/celery_app.py`
```python
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("ladini_worker", broker=REDIS_URL, backend=REDIS_URL,
    include=["ladini.api.tasks", "ladini.workers.crons.auction_solicitation",
             "ladini.workers.crons.proximity_matching", "ladini.workers.crons.outbox_dispatch"])
```
Config : `task_acks_late=True, worker_prefetch_multiplier=1, task_serializer="json", task_time_limit=600, result_expires=3600, beat_schedule=BEAT_SCHEDULE`.
Externe : Redis (broker+backend), env `REDIS_URL`.

### `api/tasks.py` — worker entrypoint Celery
```python
async def _warmup_db() -> None
def init_worker_process(**kwargs)                            # @worker_process_init.connect
def shutdown_worker_process(**kwargs)                         # @worker_process_shutdown.connect
def _chunk_whatsapp_body(body: str, limit: int = 1500) -> List[str]
def send_whatsapp_message(client, from_, to, body, *, content_sid=None, content_vars=None)
@celery_app.task(bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=5, retry_jitter=True)
def process_agent_task(self, phone_number: str = "", user_query: str = "", workspace_type: Optional[str] = None,
                        role: Optional[str] = None, force_role: bool = False,
                        interactive_id: Optional[str] = None, trace_id: Optional[str] = None)
```
Externe : Twilio (`twilio.rest.Client`), Langfuse/OTel via `core.telemetry`.

### `api/security.py`
```python
async def verify_twilio_signature(request: Request) -> None
```
FastAPI dependency, valide `X-Twilio-Signature`; bypass si `ENV=development` sans `TWILIO_AUTH_TOKEN`.

### `api/routes/market.py`
```python
@router.post("/producer") async def producer_agent(request: AgentRequest)
@router.post("/buyer") async def buyer_agent(request: AgentRequest)
@router.get("/status/{task_id}") async def get_task_status(task_id: str)
```

### `api/routes/twilio_webhook.py`
```python
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
def send_wait_message(phone_number: str)
def _extract_interactive_id(form: dict) -> Optional[str]
@router.post("/webhook/twilio")
async def twilio_webhook(request, background_tasks, From: str=Form(...), Body: str=Form(""), MessageSid: str=Form(...))
```
Dédup Redis (`SET ... NX EX=600` puis `SETEX 3600`), résolution workspace, dispatch `process_agent_task.delay(...)`.

### `core/database.py`
```python
def init_db() -> None
def _init_db_locked() -> None
def get_engine()
def get_sessionmaker()
async def close_db() -> None
@asynccontextmanager async def get_db() -> AsyncGenerator[AsyncSession, None]
async def check_connection() -> bool
async def check_connection_detailed() -> Tuple[bool, str]
async def check_connection_aggressive() -> Tuple[bool, str]
async def ensure_extensions() -> dict          # pg_trgm, vector, pgcrypto, uuid-ossp
```
Moteur async unique, SSL via `DB_SSL_MODE` (`disable`/`require`/`verify-full`+`DB_CA_PATH`), `prepared_statement_cache_size=0` (asyncpg), `pool_pre_ping=True`.
Externe : PostgreSQL (asyncpg).

### `core/telemetry.py`
```python
def init_telemetry(service_name: str = "ladini") -> None
def instrument_fastapi(app: Any) -> None
def instrument_celery() -> None
def new_trace_id() -> str
def set_trace_context(trace_id: Optional[str], **meta: Any) -> None
def get_trace_id() -> Optional[str]
def record_generation(*, model: str, messages: Any, output: Optional[str], latency_s: float,
                       usage: Optional[Dict[str,int]] = None, error: Optional[str] = None,
                       name: str = "groq_completion") -> None
def flush() -> None
def observe_http(method: str, route: str, status_code: int, duration_s: float) -> None
def count_webhook(channel: str = "twilio") -> None
def prometheus_asgi_response()
```
Externe : OpenTelemetry (OTLP/gRPC), Prometheus (`Counter`/`Histogram`), Langfuse.

### `core/get_llm.py` — choke-point unique Groq
```python
def get_groq_sdk(force_refresh: bool = False) -> Any
class _NormalizedChatResponse: def __init__(self, text: str)
class _GroqAdapter:
    class _Chat: ...
    class _Completions:
        def create(self, **kwargs)   # intercepte + record_generation()
def get_llm(llm_client: Optional[Any] = None) -> Optional[Any]
```
Externe : Groq API (`GROQ_API_KEY`/`LADINI_APIKEY`).

### `orchestrator/orchestrator.py` — **point d'entrée métier unique**
```python
class AgentCircuitBreaker(RuntimeError): ...
class _WorkspaceRunGuard:
    def __init__(self, workspace, checkpointer, *, max_steps: int) -> None
    def attach(self) -> None
    def detach(self) -> None

class Orchestrator:
    def __init__(self) -> None
    async def handle(self, phone: str, user_query: str, workspace_type: str|None=None,
                      force_role: bool=False, interactive_id: str|None=None) -> Dict[str, Any]
    async def _run_market(self, ws: Workspace, user_query: str, phone: str, *,
                           force_role: bool=False, interactive_id: str|None=None) -> Dict[str, Any]
    @staticmethod def _sync_workspace(ws: Workspace, final: Dict[str,Any], langgraph_state: Dict[str,Any]|None) -> None
    async def _flush_workspace(self, ws: Workspace, *, reason: str) -> None
```
Constantes : `_MAX_AGENT_STEPS = 48`, `_AGENT_TIMEOUT_SECONDS = 45.0`, `_ROLE_CHECK_TIMEOUT = 8.0`.
Flux interne : `Workspace → get_user_by_phone (résolution rôle) → GraphFactory.get_graph(role,...) → graph.ainvoke(inputs, config) → _sync_workspace → _flush_workspace`.

### `workspace/store.py`
```python
class WorkspaceStore:
    _table_ready: bool = False
    async def _ensure_table(self) -> bool
    async def get(self, workspace_id: str) -> Optional[Workspace]
    async def save(self, workspace: Workspace) -> bool
    async def _reset_workspace_row(self, workspace_id: str, workspace_type: str = "producer") -> bool
```
Table `agri_workspaces`, colonnes JSONB, compression zlib si `>_COMPRESS_THRESHOLD=100_000` bytes, caps `_MAX_STATE_BYTES=480_000`/`_MAX_META_BYTES=48_000`.

### `workspace/checkpointer.py` — LangGraph `BaseCheckpointSaver`
```python
class WorkspaceCheckpointer(BaseCheckpointSaver):
    def __init__(self, *, store=None, serde=None, state_key=LANGGRAPH_STATE_KEY) -> None
    async def aget_tuple(self, config) -> CheckpointTuple | None
    async def aput(self, config, checkpoint, metadata, new_versions) -> RunnableConfig
    async def aput_writes(self, config, writes, task_id, task_path="") -> None
    def finalize_for_persistence(self, workspace: Workspace) -> Dict[str, Any]
    def attach_workspace(self, workspace, *, on_checkpoint=None) -> None
```
Constantes : `_MAX_CHECKPOINTS_PER_NS=5`, `_MAX_PERSISTED_CHECKPOINTS=1`, `_MAX_PERSISTED_BYTES=480_000`, `_MAX_MESSAGE_WINDOW=32`. Prune agressif à `finalize_for_persistence` (1×/tour), pas par nœud.

### `infrastructure/mcp/runtime.py`
```python
def get_mcp() -> Any                                          # singleton FastMCP lazy
class MCPRuntime:
    def __init__(self) -> None
    @asynccontextmanager async def lifespan(self)
    def start_with_policy(self, require_connection=None, retries=None, retry_delay_s=None) -> bool
    async def ensure_initialized(self) -> None
runtime = MCPRuntime()                                         # instance globale

_PUBLIC_CATALOG_TOOLS: frozenset[str] = {"get_zone_by_name","get_available_zones","get_prohibited_terms"}

class AgriDBMCPServer:
    name = "agri_db_full_access"
    FAIL_CLOSED: bool = True
    @staticmethod def list_tools() -> list[dict]
    async def call_tool(self, name: str, arguments: dict|None=None, **kwargs)
    async def _run_preflight(self, tool_name, sanitized_args) -> None
```
Modèle fail-closed, préflight sécurité intégré. Externe : `fastmcp`, `asyncpg`.

### `services/database/` — couche transactionnelle SQL (voir §2.5 pour les modèles ORM)

Deux systèmes transactionnels coexistent, partageant `db_session_ctx: ContextVar[Optional[AsyncSession]]` (`base_service.py`) :
1. **Dispatcher MRO** (`d.py::AgriDatabaseService.__getattribute__`) — pour les tools MCP/LangGraph.
2. **`@transactional`** explicite (`base_service.py::transactional(*, write=False)`) — pour `OrderService`, `ProductService`, `UserContextService`.

```python
# base_service.py
db_session_ctx: ContextVar[Optional[AsyncSession]]
def transactional(*, write: bool=False)          # décorateur : root/nested detection, commit/rollback, 1 retry sur connexion perdue
class BaseService: property session

# d.py
class AgriDatabaseService:
    # MRO: AuthMixin, UtilsMixin, MarketplaceMixin, PublicProductMixin, BuyerMixin,
    #      BuyerVerificationMixin, ProducerMgmtMixin, ProductMixin, AuctionMixin, ModerationMixin
    _READ_ONLY_METHODS: Set[str]   # ~25 noms
    _DISPATCH_CACHE: Dict[str, Callable]
    def __getattribute__(self, name) -> Any     # proxy : cache classe + cache instance
    async def ensure_performance_indexes(self, session=None)
    class DatabaseServiceError(Exception): ...
    class IntegrityError(DatabaseServiceError): ...
```

**`base.py::BaseMixin`** (hérité par tous les mixins) :
```python
async def create_user_profile(self, data: Dict[str,Any]) -> Dict[str,Any]
async def get_user_by_phone(self, phone: str) -> Dict[str,Any]|None
async def get_producer_profile(self, phone: str) -> Tuple[User,Producer]
async def get_buyer_profile(self, phone: str) -> Tuple[Any,Any]        # auto-crée BuyerProfile
async def get_producer_farm(self, phone: str, *args, **kwargs) -> Dict[str,Any]
async def get_zone_by_name(self, name: str) -> Dict[str,Any]           # trigram fuzzy
async def get_available_zones(self) -> list[dict[str,Any]]
```

**`auth.py::AuthMixin`**:
```python
async def identify_or_create_user(self, phone, name=None, zone_id=None, initial_role="USER") -> Optional[Dict]
async def update_geo_location(self, user_id, lat, lon) -> Dict[str,str]
async def verify_user_identity(self, user_id, cnib_number) -> bool
async def update_communication_prefs(self, user_id, advice_time, enabled=True) -> Dict[str,str]
async def mark_onboarding_completed(self, user_id) -> Dict[str,str]
```

**`producer.py::ProducerMgmtMixin(BaseMixin)`** (extrait clé):
```python
async def guess_category(self, product_name) -> str
async def get_or_create_farm(self, producer_id=None, farm_name="Ma ferme", zone_id=None, *, phone=None) -> Dict
async def create_farm(self, name, location=None, size=None, zone_id=None, *, producer_id=None, phone=None) -> Dict
async def update_farm(self, phone=None, farm_id=None, *, producer_id=None, **kwargs) -> Optional[Dict]
async def get_farms(self, producer_id=None, *, phone=None) -> Dict
async def create_product(self, name, price, quantity_for_sale, unit="KG", category_label=None,
                          sub_category_id=None, description=None, local_names=None, *, producer_id=None, phone=None) -> Dict
async def list_products(self, producer_id=None, *, phone=None) -> Dict
async def get_stocks(self, phone=None, *, producer_id=None, **kwargs) -> Dict
async def declare_future_production(self, payload, *, phone=None, producer_id=None) -> Dict
async def get_producer_orders(self, *, phone=None, producer_id=None, status=None, limit=20) -> Dict
async def update_production_visibility(self, cycle_id, *, phone=None, producer_id=None, is_public=None, preorder_enabled=None) -> Dict
async def add_stock_movement(self, phone, stock_id, mtype, quantity, reason=None) -> Any
async def delete_stock(self, phone, stock_id) -> bool
async def update_order_status(self, order_id, new_status, payment_status=None) -> Optional[Dict]
async def get_or_create_client(self, name, client_phone, email=None, location=None, *, producer_id=None, phone=None) -> Dict
```

**`marketplace.py::MarketplaceMixin(BaseMixin)`**:
```python
async def add_stock(self, farm_id, item_name, quantity, unit="KG", stock_type="HARVEST", reason="Ajout via agent", warehouse_id=None, organization_id=None) -> Dict
async def remove_stock(self, farm_id, item_name, quantity, reason="Retrait", movement_type="OUT") -> Dict
async def adjust_stock(self, farm_id, item_name, quantity_change, reason="Adjustment via MCP", unit="KG", stock_type="HARVEST", warehouse_id=None, organization_id=None) -> Dict
async def get_stock_movements(self, stock_id, limit=20) -> List[Dict]
async def record_sale(self, phone, product_name, quantity, total_price, unit="KG", client_name=None) -> Dict
async def add_expense(self, farm_id, label, amount, category="OTHER", date=None) -> Dict
async def get_expenses(self, farm_id, category=None, limit=50) -> List[Dict]
async def get_expense_summary(self, farm_id) -> Dict
```

**`buyer.py::BuyerMixin(BaseMixin)`**:
```python
async def get_buyer_context(self, phone) -> Dict
async def search_products(self, product, phone, limit=15) -> Dict
async def finalize_multi_order(self, items, phone) -> Dict            # row-locked, anti-deadlock trié par product_id
async def rate_delivery(self, order_id, rating, comment) -> Dict
async def get_buyer_orders_dashboard(self, phone) -> Dict
async def cancel_pending_order(self, order_id, phone, reason=None) -> Dict
async def validate_stock_availability_atomic(self, product_id, quantity, unit=None, buyer_phone=None) -> Dict
async def reserve_future_offer(self, buyer_phone, market_offer_id, quantity, desired_price=None) -> Dict
async def create_preorder_draft(self, buyer_phone, cart_items, expected_fulfillment_date=None, payment_method="CASH", delivery_zone_id=None) -> Dict
async def initiate_negotiation_session(self, buyer_phone, product_id, offered_price, quantity=None, message=None) -> Dict
async def get_transaction_summary(self, buyer_phone=None, order_id=None) -> Dict
async def confirm_preorder_draft(self, buyer_phone, preorder_id) -> Dict
async def cancel_preorder_draft(self, buyer_phone, preorder_id, reason=None) -> Dict
async def update_negotiation_offer(self, buyer_phone, negotiation_id, new_price) -> Dict
async def close_negotiation_session(self, buyer_phone, negotiation_id, reason=None) -> Dict
```

**`auction.py::AuctionMixin(BaseMixin)`**:
```python
async def create_auction(self, phone, product_query, qty, unit, max_price, deadline, delivery_location, delivery_deadline, incoterm="DDP", zone_query=None, description=None, auto_extend=True) -> Dict
async def place_bid(self, auction_id, phone, offered_price, message=None) -> Dict          # UPSERT
async def get_auctions(self, phone=None, product_name=None, zone_name=None, status="OPEN", view_mode="MARKETPLACE") -> Dict
async def get_auction_bids(self, auction_id, phone=None) -> Dict
async def get_auctions_bids(self, phone=None, status="OPEN") -> Dict
async def get_my_active_bids(self, phone) -> Dict
async def get_producer_auctions(self, phone, scope="MATCHABLE", product_name=None, zone_name=None, status="OPEN") -> Dict
async def select_winning_bid(self, bid_id, phone=None) -> Dict     # clôture, LOST les autres, crée Order, outbox
async def cancel_auction(self, auction_id, phone) -> Dict
async def update_bid_price(self, bid_id, phone, new_price) -> Dict
async def withdraw_bid(self, bid_id, phone) -> Dict
async def check_and_expire_auctions(self) -> Dict
async def get_price_recommendation(self, product_query, zone_query=None) -> Dict
```

**`category.py::PublicProductMixin`**:
```python
async def get_public_categories(self) -> List[Dict]
async def get_public_zones(self) -> List[Dict]
async def get_public_products(self, category_id=None, zone_id=None, search=None, limit=20, offset=0) -> Dict
async def get_market_snapshot(self, zone_query=None) -> Dict
async def get_related_products(self, product_id, limit=4) -> List[Dict]
async def search_by_proximity(self, lat, lng, radius_km=50) -> List[Dict]
async def get_delivery_estimate(self, buyer_lat, buyer_lng, product_id) -> Dict     # 100 FCFA/km
```

**`delivery.py::DeliveryMixin(BaseMixin)`**:
```python
async def create_delivery(self, order_id) -> Dict
async def claim_delivery(self, delivery_id, user_id) -> Dict          # UPDATE...RETURNING atomique
async def start_delivery_transit(self, delivery_id, user_id) -> Dict
async def confirm_delivery_with_otp(self, delivery_id, otp_code, user_id) -> Dict
async def get_delivery_status_tracking(self, order_id) -> Dict
```

**`moderation.py::ModerationMixin`**:
```python
MAX_CANCELLATIONS = 3
MAX_MODERATION_STRIKES = 3
async def get_account_status(self, phone) -> Dict
async def get_prohibited_terms(self) -> Dict            # cache 300s, union DB + defaults (~50)
async def record_moderation_strike(self, phone, matched_term="", excerpt="", kind="PROHIBITED_PRODUCT") -> Dict   # ban à >3
async def record_demand_signal(self, phone="", raw_query="", normalized_term="", zone_id=None) -> Dict
```

**`order_service.py::OrderService(BaseService)`** (standalone, `@transactional`) :
```python
@transactional(write=False) async def get_order(self, session, order_id) -> Optional[OrderModel]
async def list_buyer_orders(self, session, buyer_id, *, status=None) -> list[OrderModel]
@transactional(write=True) async def create_order(self, session, order: OrderModel) -> str
async def advance_order_status(self, session, order_id, to_status, *, actor_id=None, note=None) -> bool
async def advance_delivery_status(self, session, order_id, to_status, *, actor_id=None) -> bool
async def record_payment(self, session, payment: PaymentModel) -> str
@transactional(write=False) async def due_reminders(self, session, *, limit=100) -> list[dict]
```

### `workers/beat_schedule.py`
```python
BEAT_SCHEDULE: Dict[str, Dict[str, Any]] = {
    "auction-solicitation": {"task": "workers.auction_solicitation", "schedule": 120.0, "expires": 110},
    "outbox-dispatch": {"task": "workers.outbox_dispatch", "schedule": 30.0, "expires": 25},
    "proximity-matching": {"task": "workers.proximity_matching", "schedule": 900.0, "expires": 600},
}
```

### `workers/outbox/dispatcher.py`
```python
@dataclass
class DispatchReport:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

class OutboxDispatcher:
    def __init__(self, channels: Optional[Dict[str, Any]] = None) -> None
    async def run(self, *, batch_size: int = 50) -> DispatchReport
```
Design deux phases (claim puis envoi/commit par message). Canal actif : `WhatsAppChannel` (Twilio, réel) ; `EmailChannel`/`PushChannel` sont des stubs (`is_configured() -> False`).

---

## 3.B — `graphs/agents/market_coach/` (LangGraph, PRODUCER/BUYER)

### 3.B.0 — Pipeline du graphe (`core/graph_builder.py::build_graph`)

**22 nœuds nommés** (pas 14) :
```
role_guard, input_normalizer, security_moderation, input_interpreter,
cognitive_guard, cognitive_orchestrator, clarification_node,
semantic_disambiguation, goal_planner, memory_update, validator,
context_resolver, ui_engine, ensure_farm_node, confirmation_gate,
mcp_tool_executor, response_strategy, state_cleaner, final_response,
post_response_cleanup, onboarding_node, form_node
[+ BUYER only: cart_management, negotiation_gate, order_tracking_node]
```

**Arêtes** (`entry_point = "role_guard"`) :
```
role_guard → input_normalizer → security_moderation
security_moderation --[_route_after_security]--> {to_interpreter: input_interpreter, to_strategy: response_strategy}
input_interpreter --[FastPathPolicy.route]--> {to_cognitive: cognitive_guard, to_memory_fast: memory_update}
cognitive_guard → cognitive_orchestrator
cognitive_orchestrator --[_route_after_cognitive]--> {to_onboarding: onboarding_node, to_clarification: clarification_node}
clarification_node --[_route_after_clarification]--> {to_disambiguation: semantic_disambiguation, to_strategy: response_strategy}
semantic_disambiguation --[_route_after_disambiguation]--> {to_planner: goal_planner, to_strategy: response_strategy}
goal_planner --[_route_after_planner]--> {to_memory: memory_update, to_form: form_node, to_strategy: response_strategy}
form_node --[_route_after_form]--> {to_memory: memory_update, to_strategy: response_strategy}
memory_update → validator
validator --[AfterValidatorPolicy.decide]--> {
    to_resolver: context_resolver, to_confirmation: confirmation_gate, to_strategy: response_strategy,
    [BUYER only] to_cart: cart_management, to_negotiation: negotiation_gate, to_order_tracking: order_tracking_node
}
[BUYER only] cart_management → ui_engine ; negotiation_gate → ui_engine ; order_tracking_node → ui_engine
context_resolver --[_route_after_resolver]--> {to_confirmation: confirmation_gate, to_strategy: ui_engine, to_farm_guard: ensure_farm_node, to_form: form_node}
ui_engine → response_strategy
ensure_farm_node → confirmation_gate
confirmation_gate --[_route_after_confirmation]--> {to_executor: mcp_tool_executor, to_strategy: response_strategy}
mcp_tool_executor --[_route_after_executor]--> {to_strategy: response_strategy}
response_strategy → state_cleaner → final_response → post_response_cleanup → END
onboarding_node → response_strategy
```

**Fonctions de routage — `nodes/routing.py`**:
```python
def _route_after_security(state) -> str
def _route_after_resolver(state) -> str
def _route_after_confirmation(state) -> str
def _route_after_executor(state) -> str    # toujours "to_strategy"
```
Note : `nodes/routing.py` a aussi son propre `_route_after_planner` — **mort/legacy**, non câblé (celui de `graph_builder.py` est utilisé).

**Fonctions de routage — `core/graph_builder.py` (réellement câblées)**:
```python
def _route_after_planner(state: MarketAgentState) -> str
def _route_after_form(state: MarketAgentState) -> str
def _route_after_clarification(state: MarketAgentState) -> str
def _route_after_disambiguation(state: MarketAgentState) -> str
def _route_after_cognitive(state: MarketAgentState) -> str
def _make_route_after_interpreter(role: str)
def build_graph(role: str, mc_runtime: Optional[MarketRuntime] = None, checkpointer: Any = None,
                 llm_client: Any = None, mcp_session: Any = None)
```
`_FORM_GOALS: Dict[str,str] = {"SALES_PUBLISH_PRODUCT":"PRODUCT_CREATE","PROCUREMENT_CREATE_REQUEST":"AUCTION_CREATE"}`.

**Injecté via `core/policies.py`**:
```python
class FastPathPolicy:
    def should_skip_cognitive(self, state) -> bool
    def route(self, state) -> str
    @classmethod def for_buyer(cls) -> "FastPathPolicy"
    @classmethod def for_producer(cls) -> "FastPathPolicy"

class AfterValidatorPolicy:
    PRODUCER_RESOLVER_GOALS = frozenset({"MARKET_BROWSE_REQUESTS","MARKET_GET_MY_PROPOSALS","SALES_PLACE_BID"})
    def decide(self, state: Dict[str,Any]) -> str
    @classmethod def for_buyer(cls) -> "AfterValidatorPolicy"
    @classmethod def for_producer(cls) -> "AfterValidatorPolicy"

@dataclass(frozen=True)
class RouteRule:
    goals: FrozenSet[str]
    target: str
    guard: Optional[Callable[[Dict],bool]] = None

def get_after_validator_policy(role: str) -> AfterValidatorPolicy
def get_fast_path_policy(role: str) -> FastPathPolicy
```
`for_buyer()` (AfterValidatorPolicy) ordre des règles : `BUYER_CART_GOALS→to_cart (guard=_cart_guard)`, `BUYER_NEGOTIATION_GOALS→to_negotiation`, `BUYER_ORDER_TRACKING_GOALS|BUYER_AUCTION_TRACKING_GOALS→to_order_tracking`, `BUYER_PREORDER_GOALS→to_resolver`.

### 3.B.1 — `core/`

**`core/base.py`** — schéma de validation farm dérivé de `INTENT_CONFIG` :
```python
class FarmRuleConfig(BaseModel):
    write_requires_farm: FrozenSet[str] = frozenset()
    read_optional_farm: FrozenSet[str] = frozenset()
    @property def farm_critical_goals(self) -> FrozenSet[str]

class IntentConfigEntry(BaseModel):
    required: FrozenSet[str] = frozenset()

class MarketValidationConfig(BaseModel):
    auto_farm_notice: str
    farm: FarmRuleConfig
    intents: Dict[str, IntentConfigEntry] = {}
    @property def farm_id_required_goals(self) -> FrozenSet[str]

def get_node_logger(name: str) -> logging.Logger
def validate_config_drift() -> None    # raise RuntimeError si dérive registry/INTENT_CONFIG
```
Singletons : `MARKET_VALIDATION_CONFIG`, `WRITE_REQUIRES_FARM` (14), `READ_OPTIONAL_FARM` (5), `FARM_ID_REQUIRED_GOALS`, `FARM_CRITICAL_GOALS`. Appelle `registry.load_all_actions()` à l'import.

**`core/policies.py`** — voir §3.B.0.

**`core/tunnel_manager.py`** — moteur unique de décision tunnel :
```python
INTERRUPTION_CONFIDENCE_THRESHOLD: float = 0.60
CRITICAL_BREAKOUT_INTENTS: FrozenSet[str] = {BUYER_LIST_ORDERS, BUYER_CHECK_ORDER_STATUS, BUYER_CANCEL_ORDER, BUYER_VIEW_CART, MARKET_BROWSE_REQUESTS, MARKET_MY_REQUESTS}
SOFT_EXPECTED_INPUTS = {PRODUCT,PRICE,QUANTITY,UNIT,LOCATION,DATE,SELECTION}
HARD_EXPECTED_INPUTS = {CONFIRMATION,OTP,OTP_CODE}
ALWAYS_UNBREAKABLE_INPUTS = {OTP,OTP_CODE}

class TunnelDecision:
    __slots__ = ("stay_in_tunnel","allow_interrupt","reason")
    def __init__(self, stay_in_tunnel: bool, allow_interrupt: bool=False, reason: str="") -> None

class TunnelManager:
    def __init__(self, interruption_threshold: float=INTERRUPTION_CONFIDENCE_THRESHOLD, critical_breakout_intents: Optional[FrozenSet[str]]=None) -> None
    def evaluate(self, current_goal: Optional[str], expected_input: Optional[str], incoming_event: str, incoming_intent: str, confidence: float) -> TunnelDecision
    def is_cart_routeable(self, status: str, missing_fields: Optional[List[str]]=None) -> bool
    def is_negotiation_routeable(self, status: str) -> bool
    def build_drift_hint(self, current_goal, detected_intent, confidence: float, lang: str="fr") -> Optional[str]

tunnel_manager: TunnelManager = TunnelManager()
```
Ordre de priorité dans `evaluate` : pas de tunnel→False ; slot events (`CONFIRM,SELECTION,ANSWER,UPDATE,REJECT`)→stay ; `ALWAYS_UNBREAKABLE_INPUTS`→stay inconditionnel ; intent critique→break (vérifié AVANT hard-slot) ; hard slot→break seulement si `INTERRUPTION|NEW_TASK` et `confidence>=threshold` ; `INTERRUPTION`→break si seuil ; `NEW_TASK` en soft slot→break si seuil ; défaut→stay.

**`core/slots.py`** — registre canonique des slots :
```python
@dataclass(frozen=True)
class SlotDefinition:
    canonical: str
    aliases: FrozenSet[str]
    value_type: str
    blocking: bool = False
    label_fr: str = ""
    example_fr: str = ""
    auto_resolvable: bool = False
    default_value: Optional[Any] = None

@dataclass
class SlotStatus:
    name: str
    required: bool
    value: Any
    filled: bool
    valid: bool
    error: Optional[str] = None
    source: str = "unknown"

SLOT_REGISTRY: Tuple[SlotDefinition,...]   # 10 : product, quantity, unit(default="KG"), price, zone(auto_resolvable), selection_index, selected_value, movement_type, reason, otp_code(blocking)

def resolve_canonical(key) -> str
def compute_slot_status(payload, required_fields, *, goal=None) -> Dict[str,SlotStatus]
def build_remap_dict() -> Dict[str,str]
def build_canonical_field_aliases() -> Dict[str,str]
```

**`core/router.py`** — `DefaultDomainRouter` (legacy, partiellement mort — voir §5) :
```python
BUYER_CART_GOALS = frozenset({"BUYER_ADD_TO_CART","BUYER_VIEW_CART"})
BUYER_PREORDER_GOALS = frozenset({"BUYER_PREORDER_INIT","BUYER_PREORDER_CONFIRM","BUYER_CART_RESET"})
BUYER_NEGOTIATION_GOALS = frozenset({"BUYER_NEGOTIATE_PRICE"})
BUYER_ORDER_TRACKING_GOALS = frozenset({"BUYER_CHECK_ORDER_STATUS","BUYER_LIST_ORDERS","BUYER_CANCEL_ORDER"})
BUYER_AUCTION_TRACKING_GOALS = frozenset({"BUYER_LIST_AUCTIONS","BUYER_CHECK_AUCTION_STATUS"})

class DefaultDomainRouter:
    __slots__ = ("_role",)
    def __init__(self, role: str) -> None
    async def resolve(self, state, mc_runtime) -> DomainResult
    def route_after_validator(self, state) -> str    # DUPLIQUE la logique buyer de policies.py::for_buyer()
```

**`core/state.py`** — `MarketAgentState` (contrat unique d'état) :
```python
UserEvent = Literal["NEW_TASK","ANSWER","CONFIRM","REJECT","SELECTION","UPDATE","INTERRUPTION","RESUME","OUT_OF_SCOPE","UNKNOWN","ONBOARDING_INPUT"]

class MarketAgentState(BuyerContext, ProducerContext, TypedDict, total=False):
    # ~60 champs, 15 sections. Notables:
    status: Literal["START","INTERPRETING","PLANNING","VALIDATING","WAITING_INPUT","WAITING_CONFIRMATION","EXECUTING","COMPLETED","ERROR","BLOCKED"]
    response_strategy: Literal["ASK_MISSING_FIELD","CONFIRMATION","SELECTION_MENU","SUCCESS","ERROR","RECOVERY","CLARIFICATION","INTERRUPTION_HANDLER","ONBOARDING"]
    expected_input: Literal["PRODUCT","PRICE","QUANTITY","UNIT","CONFIRMATION","SELECTION","LOCATION","DATE","NONE"]
    # ... reducers par champ: replace_value (scalaire), replace_list (liste), merge_dict (dict shallow)
```
Reducers importés depuis **`ladini.agents.reducers`** (EXTERNE au sous-arbre `market_coach/`) — pas un `reducers.py` local.

**`core/state_profile.py`** — cycle de vie des champs :
```python
class FieldLifecycle(Enum): DURABLE, EPHEMERAL, DERIVED

@dataclass(frozen=True)
class FieldSpec:
    name: str
    lifecycle: FieldLifecycle
    reset_value: Any = None

def get_field_spec(name) -> FieldSpec
def is_ephemeral(name) -> bool
def build_reset_patch(field_names: FrozenSet[str], state: Dict[str,Any]) -> Dict[str,Any]
```
`_FIELDS: Tuple[FieldSpec,...]` (~55 entrées). Consommé par `nodes/cleaner.py` et `nodes/cleanup.py`.

### 3.B.2 — `interpreter/`

**`interpreter/intent.py`** — **`INTENT_CONFIG`** : ~55 clés, catalogue fermé de tous les intents métier.
```python
INTENT_CONFIG: Dict[str, dict]   # {tool_name, required: List[str], action_type: "READ"|"WRITE", requires_farm: bool, label, label_map, handled_by_flow?, lifecycle_mode}
INTENT_ROLE: Dict[str,str]       # ~65 entrées, "PRODUCER"|"BUYER"|"BOTH"
INTENT_DOMAIN: Dict[str,str]     # dérivé par préfixe: STOCK/SALES/PROCUREMENT/MARKET/CROP/FARM/FINANCE/PROFILE/SYSTEM
INTENT_DISAMBIGUATION: Dict[str, dict]   # 7 clés (2026-09-13 : STOCK_OR_SALE_RECORDING supprimée, option unique restante après suppression de STOCK_REMOVE_PARTIAL) : STOCK_OR_SALES_DECLARATION, SELLER_HUB, BUY_VS_BROWSE, MARKET_PRICE_LOOKUP, RESUME_TUNNEL, ORDER_TRACKING_INTENT, AUCTION_TRACKING_INTENT

def get_intent_config(intent: str) -> dict
def get_required_fields(intent: str) -> list
def get_tool_name(intent: str) -> str
def is_write_intent(intent: str) -> bool
def intent_requires_farm(intent: str) -> bool
```

**`interpreter/routing.py`**:
```python
PRODUCER_INTENTS/BUYER_INTENTS/COMMON_INTENTS: frozenset   # dérivés de INTENT_ROLE
def allowed_intents_for_role(role: str) -> frozenset
def make_input_interpreter(role: str="PRODUCER")
    # -> async def input_interpreter(state: MarketAgentState, mc_runtime: MarketRuntime) -> Dict[str,Any]
def make_route_after_validator(role: str="PRODUCER")
    # -> def route_after_validator(state: MarketAgentState) -> str
def _interpret_fast_path(state: Dict[str,Any], text: str) -> Optional[Dict[str,Any]]
```
`input_interpreter` flow : bypass clic interactif (0 token) → emit onboarding → fast-path déterministe → appel LLM (Groq `response_format=json_object`, `temperature=0.0`) → normalisation/garde-fous (filtre role-scope, `PROVIDE_INFO→ANSWER`, coercition `expected_input`, détection interruption) → remap entités + fallback quantité/unité + validation produit.
`route_after_validator` priorité : `waiting_for_confirmation`→`to_confirmation` ; `UNKNOWN|OUT_OF_SCOPE`→`to_strategy` ; missing_fields hors `_NAVIGATION_INTENTS`→`to_strategy` ; missing_fields dans nav intents→`to_resolver` ; SELECTION menu wait→`to_strategy` ; sinon→`to_resolver`.

**`interpreter/goal_planner.py`** — FSM pure de cycle de vie d'intent :
```python
INTENT_TO_GOAL_MAP: Dict[str,str] = {}   # peuplé via _init_intent_to_goal_map
_NAVIGATION_INTENTS: frozenset = {BUYER_VIEW_CART, BUYER_LIST_ORDERS, BUYER_CHECK_ORDER_STATUS, BUYER_CANCEL_ORDER, MARKET_BROWSE_REQUESTS, MARKET_MY_REQUESTS}

async def goal_planner(state: Dict[str,Any], mc_runtime: MarketRuntime) -> Dict[str,Any]
```
Cascade de règles (ordre) : RÈGLE 0bis (résolution disambiguation) → RÈGLE 1 (REJECT/cancel) → RÈGLE 1bis (verrou tunnel CONFIRM/SELECTION/ANSWER/UPDATE) → RÈGLE 1ter (persistance UNKNOWN) → RÈGLE 1quater (verrou via `tunnel_manager.evaluate`) → short-text shortcut → RÈGLE 2 (pollution pendant tunnel) → RÈGLE 3 (OUT_OF_SCOPE) → RÈGLE 4 (INTERRUPTION via `tunnel_manager`) → RÈGLE 4bis (RESUME via `goal_stack`) → RÈGLE 5 (instanciation NEW_TASK + purge) → fallback CLARIFICATION.

**`interpreter/entities.py`**:
```python
def _sanitize_product_candidate(value: Any) -> Optional[str]
def _remap_entities(raw_entities: Dict[str,Any]) -> Dict[str,Any]
def _fallback_quantity_unit_from_text(text: str) -> Optional[Dict[str,Any]]
```

**`interpreter/contracts.py`** — validation Pydantic post-LLM (défense en profondeur) :
```python
class AddToCartContract(BaseModel):
    product: str = Field(min_length=2)
    quantity: float = Field(gt=0)
    unit: Optional[str]
    phone: str = Field(min_length=8)
    @model_validator(mode="before") def _coerce_product(cls, values): ...

class NegotiationContract(BaseModel): product: str; price: float = Field(gt=0); phone: str
class ListOrdersContract(BaseModel): phone: str = Field(min_length=8)
class OrderStatusContract(ListOrdersContract): order_id: str = Field(min_length=6)

CONTRACTS: Dict[str, Type[BaseModel]]   # BUYER_ADD_TO_CART, BUYER_NEGOTIATE_PRICE, BUYER_LIST_ORDERS, BUYER_CHECK_ORDER_STATUS
def enforce_contract(intent: str, payload: Dict[str,Any]) -> Tuple[bool, Optional[str], Optional[str]]
```
Consommé par `nodes/contract_validation.py` — **non câblé dans `graph_builder.py`** (mort/dormant).

**`interpreter/strategy.py`** — `response_strategy` node (déterministe, aucun appel MCP) :
```python
async def response_strategy(state: Dict[str,Any], mc_runtime: Any) -> Dict[str,Any]
```

### 3.B.3 — `nodes/`

```python
# nodes/cognitive.py
async def cognitive_guard(state, mc_runtime) -> Dict[str,Any]
async def cognitive_orchestrator(state, mc_runtime) -> Dict[str,Any]

# nodes/memory.py — le nœud le plus complexe/large
async def memory_update(state: Dict[str,Any], mc_runtime: MarketRuntime) -> Dict[str,Any]
# pipeline: normalize_slot_keys → cleanup transitoire → réhydratation onboarding → tracking correction
# → détection changement rôle/intent → sanctuarisation goal ("FAILLE 1") → garde CONFIRM/REJECT
# → héritage entités stables → récupération vendor-context → injection zone → enrich_payload_from_text
# → résolution label menu libre → résolution index/valeur sélection → menu_snapshot_store.resolve
# → patch draft_payload → mirroring alias → merge patch de compaction d'état

# nodes/validation.py
_RESOLVER_PASSTHROUGH: Dict[str, Tuple[str, List[str]]]  # MARKET_BROWSE_REQUESTS, MARKET_MY_REQUESTS, SALES_PLACE_BID, MARKET_GET_MY_PROPOSALS, SALES_ACCEPT_CONTRACT, PROCUREMENT_ACCEPT_OFFER
async def validator(state: Dict[str,Any], mc_runtime: MarketRuntime) -> Dict[str,Any]

# nodes/executor.py
async def mcp_tool_executor(state: Dict[str,Any], mc_runtime: Any) -> Dict[str,Any]
# flow: ContextGuard → execution_authorized → résolution goal → registry.get_action → pré-vol farm
# → (WRITE) TaskHandler.handle() check → dispatch avec self-heal ValueError → is_tool_allowed
# → résolution schéma MCP (services.mcp.schema_resolver) → retry transitoire (max=2) → succès/erreur

# nodes/response_handlers.py — final_response, ~940 lignes
async def final_response(state: MarketAgentState, mc_runtime: Any) -> Dict[str,Any]
async def _generate_llm_question(mc_runtime, goal, field, label, payload, state=None) -> str   # timeout 10s

# nodes/clarification.py
async def clarification_node(state, mc_runtime) -> Dict[str,Any]

# nodes/security_moderation.py
async def security_moderation(state, mc_runtime) -> Dict[str,Any]
# compte de blocage, détection produit prohibé (strike, ban à 3), appel modération scam

# nodes/semantic_disambiguation.py
_DISAMBIGUATION_CONFIDENCE_THRESHOLD = 0.85
def _detect_disambiguation_candidates(text_lower: str, role_upper: str|None=None) -> Optional[Dict[str,Any]]
async def semantic_disambiguation(state, mc_runtime) -> Dict[str,Any]

# nodes/ui_engine.py
async def ui_engine(state, _mc_runtime=None, **_kwargs) -> Dict[str,Any]
# convertit pending_menu: MenuRequest -> ag_ui_component/available_mapping/expected_candidates
# persiste snapshot via menu_snapshot_store.save

# nodes/cleanup.py — post_response_cleanup, tourne APRÈS final_response
async def post_response_cleanup(state, mc_runtime) -> Dict[str,Any]

# nodes/cleaner.py — state_cleaner_node, tourne AVANT final_response
async def state_cleaner_node(state: MarketAgentState, *_, **__) -> Dict[str,Any]

# nodes/form_node.py
_FORM_TO_GOAL = {"PRODUCT_CREATE":"SALES_PUBLISH_PRODUCT","AUCTION_CREATE":"PROCUREMENT_CREATE_REQUEST"}
async def form_node(state: Dict[str,Any], mc_runtime: Any) -> Dict[str,Any]

# nodes/confirmation_gate.py
async def confirmation_gate(state, mc_runtime) -> Dict[str,Any]
# READ goals -> EXECUTING immédiat; sinon branche CONFIRM/REJECT; sinon build_confirmation_summary + FormConfirmation

# nodes/contract_validation.py — DORMANT, non câblé dans graph_builder.py
async def contract_validation(state, _mc_runtime=None, **_) -> Dict[str,Any]

# nodes/input_normalizer.py
async def input_normalizer(state, mc_runtime) -> Dict[str,Any]
# audio->STT -> harden text -> détection injection -> extraction phone -> chargement profil/onboarding

# nodes/role_guard.py
def make_role_guard(role: str)   # -> async def _role_guard(state, _) -> Dict[str,Any]
```

### 3.B.4 — `flows/producer/`

```python
# flows/producer/flow.py
GOALS_NEEDING_FARM_ID: frozenset          # dérivé dynamiquement de INTENT_CONFIG
async def producer_context_resolver(state: Dict[str,Any], mc_runtime: MarketRuntime) -> Dict[str,Any]

# flows/producer/auctions.py — machine à états complète découverte->bid->suivi
_BID_WM_KEYS: tuple   # 7 clés working_memory du tunnel de bid
async def browse_auctions(state, mc_runtime) -> Dict[str,Any]
async def ask_bid_price(state, mc_runtime, auction_id: str, *, reask: bool=False) -> Dict[str,Any]
async def recap_bid(state, mc_runtime, auction_id: str, price: float, *, reask: bool=False) -> Dict[str,Any]
async def submit_bid(state, mc_runtime, auction_id: str, price: float) -> Dict[str,Any]
async def track_my_bids(state, mc_runtime) -> Dict[str,Any]
async def ask_modify_price(state, mc_runtime, bid_id: str, *, reask: bool=False) -> Dict[str,Any]
async def recap_modify_price(state, mc_runtime, bid_id: str, new_price: float, *, reask: bool=False) -> Dict[str,Any]
async def submit_modify_price(state, mc_runtime, bid_id: str, new_price: float) -> Dict[str,Any]
async def producer_auction_resolver(state, mc_runtime) -> Dict[str,Any]   # orchestrateur (bid_phase dispatch)

# flows/producer/farm_logic.py
FARM_RULES = MARKET_VALIDATION_CONFIG.farm
async def ensure_farm_node(state: Dict[str,Any], mc_runtime: MarketRuntime) -> Dict[str,Any]

# flows/producer/state.py
class ProducerContext(TypedDict, total=False):
    user_farms_cache: Annotated[Optional[List[Dict]], replace_value]
    original_entity: Annotated[Optional[Dict], load_snapshot]
    current_entity: Annotated[Optional[Dict], load_snapshot]
```

### 3.B.5 — `flows/buyer/`

```python
# flows/buyer/flow.py
async def buyer_context_resolver(state, mc_runtime) -> Dict[str,Any]
def build_product_selection_menu(...) -> ...   # wrapper compat CartDomainService

# flows/buyer/cart.py
async def cart_management(state, mc_runtime) -> Dict[str,Any]

# flows/buyer/negotiation.py
async def negotiation_gate(state, mc_runtime) -> Dict[str,Any]
# phases: NEGOTIATION_MENU -> AWAIT_COUNTER_PRICE / VIEWING_OFFERS

# flows/buyer/preorder.py
def build_preflight_recap(cart: List[Dict], meta: Dict) -> str
async def update_preorder_phase(state, mc_runtime) -> Optional[Dict[str,Any]]
async def create_preorder(state, mc_runtime) -> Dict[str,Any]
# phases: CART -> PREORDER_DRAFTED -> CONFIRMED

# flows/buyer/procurement.py
def build_procurement_escalation(payload, working_memory, product_name, unit, message, existing_form_data=None) -> Dict[str,Any]
async def buyer_request_resolver(state, mc_runtime) -> Dict[str,Any]
async def resolve_received_bids(mc_runtime, phone, payload) -> Dict[str,Any]
async def resolve_buyer_bid_pick(mc_runtime, phone, payload) -> Dict[str,Any]
# NOTE: resolve_own_auctions supprimé (fusionné dans order_tracking.list_buyer_auctions, 2026-07-21)

# flows/buyer/order_tracking.py
MENU_SESSION_TTL_SECONDS = 1800
ORDER_TRACKING_GOALS = frozenset({"BUYER_CHECK_ORDER_STATUS","BUYER_LIST_ORDERS","BUYER_CANCEL_ORDER"})
AUCTION_TRACKING_GOALS = frozenset({"BUYER_LIST_AUCTIONS","BUYER_CHECK_AUCTION_STATUS","MARKET_MY_REQUESTS"})
def extract_order_ref(text: str) -> Optional[str]
async def list_orders(state, mc_runtime) -> Dict[str,Any]
async def check_order_status(state, mc_runtime) -> Dict[str,Any]
async def cancel_order(state, mc_runtime) -> Dict[str,Any]
async def list_buyer_auctions(state, mc_runtime) -> Dict[str,Any]      # sert BUYER_LIST_AUCTIONS ET MARKET_MY_REQUESTS
async def check_auction_status(state, mc_runtime) -> Dict[str,Any]
async def confirm_winner_selection(state, mc_runtime) -> Dict[str,Any]
async def finalize_winner(state, mc_runtime) -> Dict[str,Any]
async def proactive_order_check(state, mc_runtime) -> Optional[Dict[str,Any]]
async def order_tracking_resolver(state, mc_runtime) -> Dict[str,Any]

# flows/buyer/helpers.py — surface d'import partagée par tous les modules buyer
CART_GOALS, PREORDER_GOALS, NEGOTIATION_GOALS, ORDER_TRACKING_GOALS, AUCTION_TRACKING_GOALS: frozenset   # AUCTION_TRACKING_GOALS dupliqué depuis order_tracking.py, doit rester synchronisé (inclut MARKET_MY_REQUESTS)
def resolve_quantity(payload, stable_entities, *, allow_stable_fallback=True) -> ...
def draft_requires_completion(draft) -> bool
def clear_active_goal(state, *, clear_cart_snapshot=False) -> Dict
def read_only_intent(goal) -> bool

# flows/buyer/contexts.py — classes typées, PEU UTILISÉES (voir §5)
class VendorSelectionState: MAPPING_KIND="product_vendor"; from_state, apply_selection(index), to_patch()
class PreorderPhase: PHASES=("CART","PREORDER_DRAFTED","CONFIRMED")
class NegotiationContext: ...

# flows/buyer/state.py
class BuyerContext(TypedDict, total=False):
    active_cart: Annotated[List[Dict], replace_list]
    vendor_selection_context: Annotated[Optional[Dict], replace_value]
    # + cart_meta, negotiation_context, preorder_workflow, fallback_recommendations, last_order_summary, order_tracking_context
```

### 3.B.6 — `flows/common/`

```python
# menu_contracts.py — types purs, zéro dépendance flows/core
@dataclass(frozen=True, slots=True)
class MenuOption:
    index: str
    label: str
    value: Optional[str] = None

@dataclass(frozen=True, slots=True)
class MenuRequest:
    title: str
    options: List[MenuOption]
    kind: str = "generic"
    metadata: Dict[str, Any] = field(default_factory=dict)
    preformatted_text: Optional[str] = None

@dataclass(slots=True)
class DomainResult:
    state_patch: Dict[str, Any] = field(default_factory=dict)
    pending_menu: Optional[MenuRequest] = None

# menu_text.py
def render_numbered_menu(options, header=None) -> str
def render_selection_prompt(*, noun="choix", allow_cancel=True) -> str
def render_quick_actions(actions: Sequence[str]) -> str

# onboarding.py
async def onboarding_node(state: Dict[str,Any], mc_runtime: MarketRuntime) -> Dict[str,Any]
```

### 3.B.7 — `actions/` (plugin `register_action`)

```python
# actions/sales.py — 16 handlers @register_action, mode READ/WRITE
def prep_*(state: Mapping, payload: Mapping) -> Tuple[str, Dict[str,Any]]
# 1 par intent SALES_*/MARKET_*/DASHBOARD_PRODUCER/SEARCH_*/VALIDATE_PRICE

# actions/agro.py — 9 handlers pour AGRO_*, FARM_GET_MY_LIST, CROP_*, DECLARE_CROP_CYCLE, FARM_CREATE, FARM_UPDATE

# actions/tooling.py
class ToolResolutionError(RuntimeError): ...
class ToolId(str, Enum): ...   # ~40 constantes de nom d'outil MCP canonique
class ToolResolver:
    def configure_overrides(self, overrides) -> None
    def resolve_name(self, tool: ToolId|str|None) -> str
    def list_supported_ids(self) -> Iterable[ToolId]

# actions/tool_provider.py
@runtime_checkable
class ToolProvider(Protocol):
    async def execute(self, tool_name, args) -> Dict: ...
@dataclass
class MCPToolProvider:
    runtime: Any
    async def execute(self, tool_name, args) -> Dict   # -> runtime.call_db(tool_name, **args)

# actions/common.py
def require(payload, key) -> Any
def require_phone(state) -> str
def normalize_quantity_to_kg(qty, unit_raw) -> Tuple[float,str]
async def load_entity_snapshot(mc_runtime, goal, entity_id, payload=None, *, phone, entity_kind) -> Dict[str,Any]
```
Non lus en détail (même pattern `@register_action` + DTO/Command) : `actions/finance.py`, `finance_dto.py`, `procure.py`, `procure_dto.py`, `profile.py`, `profile_dto.py`, `stock.py`, `stock_dto.py`, `system.py`, `system_dto.py`, `sales_dto.py`, `farm_dto.py`.

### 3.B.8 — `domain/` (market_coach)

```python
# domain/model.py
@dataclass(frozen=True)
class DomainContext:
    user_id: ...; phone: ...; role: ...; language: ...; region: ...; organization: ...
    permissions: FrozenSet[str]; tenant: ...; timezone: ...
    @classmethod def from_state(cls, state, permissions=None) -> "DomainContext"

@dataclass(frozen=True)
class DomainEvent:
    name: str
    payload: Dict = field(default_factory=dict)

@dataclass
class DomainResult:
    tool_id: Optional[ToolId]
    tool_args: Dict[str,Any]
    events: List[DomainEvent] = []
    warnings: List[str] = []
    metadata: Dict[str,Any] = {}

# domain/sales.py
@dataclass(frozen=True) class SalesUpdateProductCommand: producer_id; product_id; price=None; quantity=None
@dataclass(frozen=True) class SalesPublishProductCommand: producer_id; product; quantity; unit; price; description=None; category_label=None
@dataclass(frozen=True) class SalesRecordDirectCommand: producer_id; product; quantity; unit; price
@dataclass(frozen=True) class MarketGetRequestsCommand: phone; status="OPEN"; view_mode="MARKETPLACE"; product_name=None; zone_name=None
@dataclass(frozen=True) class SalesListOrdersCommand: phone; status=None; limit=None

@dataclass
class SalesService:
    context: DomainContext
    def get_catalog(...) -> DomainResult
    def get_market_requests(...) -> DomainResult
    def publish_product(...) -> DomainResult
    def record_direct_sale(...) -> DomainResult
    def place_bid(...) -> DomainResult
    def accept_contract(...) -> DomainResult
    def update_product(...) -> DomainResult
    def list_orders(...) -> DomainResult
    # + get_request_detail, get_my_proposals, market_snapshot, market_snapshot_zonal, dashboard_producer, search_products, search_nearby, validate_price

# domain/agro.py
@dataclass(frozen=True) class FarmGetMyListCommand: phone
@dataclass(frozen=True) class FarmCreateCommand: phone; farm_name; zone; surface=None
@dataclass(frozen=True) class FarmUpdateCommand: phone; farm_id; farm_name=None; surface=None
@dataclass class AgronomyService: context: DomainContext   # 12 méthodes miroir de actions/agro.py
```
Non lus en détail (même pattern) : `domain/finance.py`, `domain/procurement.py`, `domain/profile.py`, `domain/stock.py`, `domain/system.py`.

### 3.B.9 — `services/domain/`

```python
# slot_enrichment.py
class SlotValidationError(RuntimeError): ...
class SlotExtractionPayload(BaseModel):
    quantity: Optional[float] = Field(ge=0, le=1_000_000)
    unit: Optional[str]
    product: Optional[str] = Field(max_length=80)
    @property def sanitized(self) -> Dict

_PRODUCTION_TYPE_SYNONYMS: Dict[str,str]   # → CROP/LIVESTOCK (12 entrées + fallback via is_livestock_product)
def extract_quantity_unit_from_text(text: str) -> Optional[Dict[str, Any]]
def extract_production_type_from_text(text: str) -> Optional[str]
def extract_surface_from_text(text: str) -> Optional[float]
def extract_future_datetime_from_text(text: str) -> Optional[str]
async def llm_extract_quantity_unit(mc_runtime, user_text: str) -> Optional[Dict]   # timeout 10s
async def enrich_payload_from_text(payload: Dict[str,Any], text: str, goal: Optional[str], mc_runtime: Any) -> Dict[str,Any]

# quantity_unit.py
UNIT_SYNONYMS: Dict[str,str]        # ~19 tokens -> 6 unités canoniques
VALID_UNITS = frozenset(UNIT_SYNONYMS.values())
LIVESTOCK_PRODUCT_KEYWORDS: frozenset   # ~65 noms d'animaux FR (singulier+pluriel)
def is_livestock_product(product: Any) -> bool
def default_unit_for_product(product: Any, fallback: str="KG") -> str
def parse_quantity_unit_from_text(text: str) -> ...
def parse_compound_quantity(text: str) -> ...   # somme "2 tonnes et 375 kg"

# cart_service.py
@dataclass
class CartDomainService:
    mc_runtime: MarketRuntime
    async def resolve_product_vendors(self, phone, product_name) -> Tuple[List[Dict],bool]
    def build_product_selection_menu(self, product_name, vendors, *, extra_context=None, post_hint=None) -> Tuple[Dict, MenuRequest]
    @staticmethod def recompute_cart_meta(cart) -> Dict
    def render_cart_menu(self, cart, meta, pending_draft=None) -> Dict
    async def add_to_cart_with_ref(self, phone, product_name, quantity, ref, cart, state) -> Dict[str,Any]

# product_validation.py
async def _validate_and_sanitize_product(product_value, mc_runtime) -> Optional[str]
```

### 3.B.10 — `services/mcp/`

```python
# gateway.py
class _BaseGateway:
    __slots__ = ("_rt",)
    async def _call(self, tool, **kwargs) -> Dict
# Gateways typés (sous-classes _BaseGateway):
ProfileGateway, FarmGateway, AuctionGateway, StockGateway, ProductGateway,
NegotiationGateway, PreorderGateway, OrderTrackingGateway, ModerationGateway, AgentActionGateway

# schema_resolver.py
async def list_mcp_tools(mc_runtime) -> List[Any]
async def get_tool_schema(mc_runtime, tool_name) -> Dict
def build_resolved_tool_args(tool_name, schema, state, payload, initial_args) -> Dict[str,Any]
class MissingRequiredMCPArgs(ValueError): ...

# error_translation.py
GENERIC_TECHNICAL_ERROR: str
def classify_error(raw_error) -> str
def translate_mcp_error(raw_error) -> str

# post_success.py
def post_success_suggestion(goal, payload) -> Optional[str]
```

### 3.B.11 — `services/ui/`

```python
# confirmation_summary.py
def build_confirmation_summary(goal: str, payload: Dict[str, Any]) -> str
```
**Duplication connue** : une 2ᵉ implémentation générique existe dans `utils.py` (root, §3.B.12) — celle-ci (`services/ui/`) est la version réellement utilisée par `nodes/confirmation_gate.py`.

### 3.B.12 — Modules racine

```python
# registry.py
class DuplicateIntentError(Exception): ...
class InvalidRegistrationError(Exception): ...

@dataclass
class ActionMetrics: calls=0; errors=0; total_duration_ms=0.0
    def record(self, duration_ms, error=False) -> None
    @property def average_duration_ms(self) -> float

@dataclass
class ActionRegistration:
    intent: str; is_write: bool; lifecycle_mode: str; handler: ActionCallable
    version: int=1; deprecated: bool=False; tool_id: Optional[str]=None
    description: Optional[str]=None; capability: Optional[str]=None
    permissions: tuple=(); timeout_seconds: Optional[float]=None; max_retries: int=0
    payload_model: Optional[Type]=None; response_model: Optional[Type]=None; metadata: Dict={}

REGISTRY: Dict[str, ActionRegistration] = {}

def register_action(intent_name, mode=None, *, is_write=None, version=1, deprecated=False, tool_id=None,
                     description=None, capability=None, permissions=None, timeout_seconds=None,
                     max_retries=0, payload_model=None, response_model=None, metadata=None)   # decorator factory
def load_all_actions(force_reload: bool=False) -> None
def get_action(intent: str) -> Optional[ActionRegistration]
def prepare_market_action(intent: str, state, payload) -> Tuple[str, Dict]
def validate_integrity() -> None   # raise si dérive registry/INTENT_CONFIG (exclut handled_by_flow)

# llm_router.py
def get_model_for_goal(goal: Optional[str]) -> str   # 3-tier fallback: ROUTING_MAP[goal] -> ROUTING_MAP["__default__"] -> settings.LLM_MODEL

# utils.py — module fourre-tout
@dataclass(frozen=True)
class ToolScope:
    name: str
    allowed_tools: Optional[Set[str]] = None
    allow_direct_db: bool = False

class ToolScopeManager:
    @classmethod def activate_for_node(cls, node_name: str) -> None
    @classmethod def can_call_tool(cls, tool_name: str) -> bool
    @classmethod def can_use_direct_db(cls) -> bool

class MarketRuntimeError(RuntimeError): ...

class MarketRuntime:
    def __init__(self, llm_client=None, transport_config: MCPTransportConfig|None=None, mcp_session=None, db_service=None)
    @property def llm(self) -> Any
    @property def model_answer(self) -> str
    def set_current_goal(self, goal) -> None
    def bind_user(self, phone) -> None
    async def __aenter__(self); async def __aexit__(self, *a)
    def ensure_db(self)   # deprecated
    async def call_db(self, tool_name: str, **kwargs) -> Dict[str, Any]   # POINT D'ENTRÉE UNIQUE MCP
    @classmethod def from_auto_path(cls, llm_client=None) -> "MarketRuntime"

def build_runtime(llm_client=None, transport_config=None) -> MarketRuntime
def build_runtime_from_session(llm_client=None, mcp_session=None) -> MarketRuntime
def normalize_slot_keys(data) -> Dict
def canonical_unit_label(value, default="KG") -> str
def ensure_dict(obj) -> Dict         # unwrap défensif réponse MCP (+ fallback ast.literal_eval)
def is_success_response(res) -> bool
def merge_payload(base, updates) -> Dict   # supporte sentinel {"__reset__": True}
def build_confirmation_summary(...) -> str   # DUPLIQUÉ, version legacy/probablement morte (voir services/ui/confirmation_summary.py)
```

---

# 4. FLUX DE DONNÉES CLÉS

## 4.1 Message WhatsApp entrant → réponse (flux principal)

```
Twilio (webhook POST /api/webhook/twilio)
  → api/routes/twilio_webhook.py::twilio_webhook()
      - dédup Redis (SET NX EX=600)
      - WorkspaceStore().get(phone) → résolution workspace_type/role
      - process_agent_task.delay(phone, user_query, workspace_type, role, force_role, interactive_id, trace_id)
      - background_tasks.add_task(send_wait_message)  # ack immédiat "Je réfléchis..."
  → [Celery worker process, event loop persistant]
  → api/tasks.py::process_agent_task(...)
      → Orchestrator().handle(phone, user_query, workspace_type, force_role, interactive_id)
          → WorkspaceResolver.resolve(workspace_id) → Workspace (postgres agri_workspaces)
          → Orchestrator._run_market(ws, ...)
              → live_runtime.call_db("get_user_by_phone", phone=phone)   # résolution rôle
              → GraphFactory.get_graph(role, mc_runtime, checkpointer)  # cache par (role, id(mc_runtime), id(checkpointer))
              → graph.ainvoke(inputs, config)   # === graphe LangGraph 22 nœuds, voir §3.B.0 ===
          → Orchestrator._sync_workspace(ws, final, langgraph_state)
          → Orchestrator._flush_workspace(ws, reason=...)   # persistance via WorkspaceCheckpointer.finalize_for_persistence
      → dict {final_response, ag_ui_component, ...}
  → send_whatsapp_message(twilio_client, from_, to, body, content_sid=..., content_vars=...)  # Content API si interactif, sinon texte brut chunké (limit 1500)
```

## 4.2 Flux interne du graphe LangGraph (par tour)

```
état entrant (Workspace.agent_state, chargé par WorkspaceCheckpointer.aget_tuple)
  → role_guard (verrouillage rôle + goal/tool allowed)
  → input_normalizer (hardening texte, injection detection, chargement profil/farms, résolution onboarding)
  → security_moderation (gate compte bloqué, produit prohibé, scam)
  → [route: to_strategy si bloqué | to_interpreter sinon]
  → input_interpreter (fast-path déterministe OU appel Groq LLM json_object) → detected_intent/interpreted_event/extracted_entities
  → [FastPathPolicy.route: to_memory_fast (skip cognitif) | to_cognitive]
  → cognitive_guard (intent_competition, carry-forward entités) → cognitive_orchestrator (phase FSM)
  → [to_onboarding | to_clarification]
  → clarification_node (pass-through sauf OUT_OF_SCOPE/abandon) → [to_strategy | to_disambiguation]
  → semantic_disambiguation (menu pédagogique si hint lexical + confiance basse) → [to_strategy | to_planner]
  → goal_planner (FSM pure: lock/unlock goal, purge transaction_state, résout disambiguation) → [to_memory | to_form | to_strategy]
  → (form_node si FORM_GOALS) → memory_update (mutation transaction_payload, enrichissement slots, résolution sélection menu)
  → validator (complétude INTENT_CONFIG.required, RESOLVER_PASSTHROUGH pour IDs différés)
  → AfterValidatorPolicy.decide → [to_resolver | to_confirmation | to_strategy | BUYER: to_cart/to_negotiation/to_order_tracking]
  → context_resolver (producer_context_resolver | buyer_context_resolver) — délègue aux flows/{role}/*
  → ensure_farm_node (auto-provision farm si WRITE, bloque si READ multi-farm)
  → confirmation_gate (READ auto-pass; WRITE demande confirmation explicite via build_confirmation_summary)
  → mcp_tool_executor (registry.get_action → self-heal → MarketRuntime.call_db → schema_resolver → retry transitoire)
  → response_strategy (routage déterministe de la stratégie de réponse)
  → state_cleaner_node (GC pré-réponse) → final_response (composition texte + ag_ui_component)
  → post_response_cleanup (GC post-réponse, reset champs éphémères)
  → état sortant persisté via WorkspaceCheckpointer.aput → Workspace.agent_state
```

## 4.3 Appel d'outil MCP (`MarketRuntime.call_db`)

```
mcp_tool_executor (nodes/executor.py)
  → registry.get_action(goal) → ActionRegistration.handler(state, payload) → (tool_name, tool_args)  [pattern actions/*.py -> domain/*.py -> ToolId]
  → services/mcp/schema_resolver.build_resolved_tool_args(tool_name, schema, state, payload, tool_args)
  → MarketRuntime.call_db(tool_name, **resolved_args)
      → ASCII-fold + None-strip
      → mcp_context_scope(FarmerContext(user_id, phone, session_id))
      → AgriMCPClient.call_tool(tool_name, arguments)  [transport stdio|http|grpc selon MCP_DB_TRANSPORT]
          → infrastructure/mcp/runtime.py::AgriDBMCPServer.call_tool
              → TOOL_SCOPE_MAP (scope, fail-closed) + MCPPermissionHostApp._preflight_scan (SQL-injection/fichiers sensibles)
              → ToolExecutionPolicy.execute (rate-limit + sanitisation + timeout + audit)
              → services/database/d.py::AgriDatabaseService (dispatch MRO)
              → Mixin.<method>(...) — session via db_session_ctx / @transactional
              → PostgreSQL (asyncpg, SQLAlchemy async)
  → ensure_dict(response) → is_success_response() → normalisation résultat
```

## 4.4 Traitement automatisé (Celery Beat crons)

```
Beat (BEAT_SCHEDULE, 3 entrées)
  ├─ "auction-solicitation" (120s) → workers.crons.auction_solicitation::run_auction_solicitation_cron
  │     → worker_session() → AuctionAutomationService(session).run(batch_size)
  │       → fetch_auctions_to_solicit → producers_for_auction (ciblage) → upsert_auction_solicitations → outbox enqueue
  ├─ "outbox-dispatch" (30s) → workers.crons.outbox_dispatch::run_outbox_dispatch_cron
  │     → OutboxDispatcher.run(batch_size) → claim_due (FOR UPDATE SKIP LOCKED) → WhatsAppChannel.send (Twilio) → mark_sent/mark_failed
  └─ "proximity-matching" (900s) → workers.crons.proximity_matching::run_proximity_matching_cron
        → ProximityMatchingService(session).run(...) → buyers_in_zone_for_category → upsert_offer_solicitations → outbox enqueue
```

---

# 5. DIAGNOSTIC & BACKLOG DE CORRECTION

> **✅ PHASE 4 EXÉCUTÉE (2026-07-21) — Performance & robustesse.**
> **(a) Timeouts LLM** : les 3 sites `asyncio.to_thread` sans `wait_for` sont couverts — `interpreter/routing.py::input_interpreter` (**15s** — le site critique : un appel Groq suspendu y gelait le tour entier jusqu'au timeout orchestrateur 45s ; `TimeoutError` retombe sur le fallback UNKNOWN existant), `utils.py::_llm_extract_onboarding_all` (10s), `nodes/clarification.py` (8s). Les autres sites (`slot_enrichment`, `rendering/ask.py`) avaient déjà leur `wait_for`. Audit : aucun appel LLM bloquant hors `to_thread` dans le graphe ; `services/memory/{profile_extractor,episodic_memory}` font des appels sync directs mais hors chemin de graphe (consommés lazy par mcp/context).
> **(b) Cache client termes interdits** : `nodes/security_moderation.py::_get_prohibited_terms_cached` (TTL 300s, aligné sur le cache serveur du ModerationMixin) — économise un aller-retour MCP (stdio inter-processus) PAR MESSAGE ; secours sur cache périmé si le fetch échoue. Le gate compte (`get_account_status`) reste volontairement NON caché (un ban doit s'appliquer au message suivant).
> **(c) §5.19 clos** : `LADINI_EAGER_IMPORTS=1` force l'import eager de `d.py` + 10 mixins dans `services/database/__init__.py` (fail-fast CI ; démontré : attrape le `rapidfuzz` manquant à l'import au lieu du premier accès en prod). À poser dans la CI et le smoke de démarrage.
> **(d) Audit N+1 resolvers** : aucun N+1 détecté dans `market_coach/` (profondeur 3 lignes) — seule boucle await trouvée : retry ×2 de `profile_loader` (légitime). Vérifié : cache 6 messages→1 fetch avec détection intacte, fallback périmé, graphes BUYER/PRODUCER compilés.

> **✅ PHASE 3 EXÉCUTÉE (2026-07-21) — UX conversationnelle : découpage du rendu.**
> `nodes/response_handlers.py` (~940 lignes, 9 stratégies dans une fonction, 8 renderers imbriqués) → **dispatcher de ~110 lignes** + package **`nodes/rendering/`** : `common.py` (RenderContext, `label_for_field`, `FIELD_BUSINESS_REASON`, `apply_corrections`, `unwrap_execution_result`, `fmt_num`/`fmt_date`, constructeurs AG-UI `status_component`/`list_menu_component`), `ask.py` (ONBOARDING + ASK_MISSING_FIELD + `generate_llm_question`), `confirm.py`, `menus.py`, `success.py` (renderers de sections promus au niveau module : farms/catalog/cycles/flat-list/buyer-catalog + gabarits transactionnels par goal), `feedback.py` (ERROR/RECOVERY/INTERRUPTION/fallback CLARIFICATION). **Règle historique préservée** : `status COMPLETED` sans stratégie dédiée court-circuite vers SUCCESS, y compris pour RECOVERY/INTERRUPTION (hors ensemble d'exclusion) — encodée dans `_select_handler`. `apply_corrections` reste appliqué UNIQUEMENT aux branches qui l'appliquaient (onboarding/ask/confirm/menus/success). Prompt de `generate_llm_question` compressé (~70 tokens vs ~180, temperature 0.3, max_tokens 90). Façade compat : `response_handlers._label_for_field` = alias (consommé par validation.py/cognitive.py). Vérifié : 13 scénarios couvrant les 9 stratégies + corrections + règle COMPLETED→SUCCESS, graphes BUYER/PRODUCER compilés. **Scope explicite** : `protocols/ag_ui/renderer.py` (réparé Phase 0) reste NON câblé — le pipeline WhatsApp actuel consomme `ag_ui_component` via Twilio Content API ; le branchement multi-canal (Web/SMS) est un chantier transport séparé. La réécriture du prompt d'`input_interpreter` (interpreter/routing.py) reste à faire — nécessite une passe d'éval dédiée.

> **✅ PHASE 2 EXÉCUTÉE (2026-07-21) — Filets de sécurité câblés.**
> **(a) Contrats Pydantic activés** (§5.11 clos) : `enforce_contract` est appelé par `nodes/validation.py::validator` après le contrôle de complétude — champ fautif purgé du payload + ré-inséré en tête de `missing_fields` (re-ask naturel). Règle de partage : les contrats ne valident que les **valeurs présentes** (`enforce_contract` ignore les erreurs Pydantic `missing` — la présence reste le travail exclusif d'`INTENT_CONFIG.required`, sinon `OrderStatusContract.order_id` casserait le flux « liste → sélectionne » d'order_tracking). Contrats étendus aux goals producteur `SALES_PUBLISH_PRODUCT`/`SALES_RECORD_DIRECT` (`PublishProductContract`). Bug latent corrigé : `AddToCartContract.unit: Optional[str]` sans défaut (= requis-nullable en Pydantic v2). `nodes/contract_validation.py` (nœud dormant) supprimé — supersédé par l'intégration au validator, pas de nouveau nœud de graphe.
> **(b) Unwrap d'enveloppe généralisé** : `utils.py::unwrap_tool_envelope` (public) appliqué au chokepoint `MarketRuntime.call_db` (étape 6, après `ensure_dict`) — détection STRICTE (`ok` booléen présent ET pas de `status` : les dicts domaine portent souvent leur propre clé `data` liste, qu'il ne faut jamais toucher). `cart_service._unwrap_tool_envelope` délègue désormais à la version canonique (le filet local, moins strict, pouvait corrompre un dict `{data: [...]}` sans `ok`). `is_success_response` : filet enveloppe (`ok` fait foi si pas de `status`) + `return False` explicite sur statut inconnu (l'ancien fallthrough retournait `None`). Au passage : `is_cart_goal` du validator aligné sur `core/goals.py`. Vérifié : 8 cas unwrap, 6 cas contrats, validator bout-en-bout (rejet→re-ask, valide→PROCESSING), graphes BUYER/PRODUCer compilés.

> **✅ PHASE 1 EXÉCUTÉE (2026-07-21) — Source de vérité du routage.**
> Nouveau module **`core/goals.py`** : les 7 ensembles de goals (5 tunnels buyer, `PRODUCER_RESOLVER_GOALS`, `NAVIGATION_BREAKOUT_GOALS`) sont **dérivés d'INTENT_CONFIG** via les nouveaux champs `tunnel`/`breakout` (assignés en boucle fail-fast dans `interpreter/intent.py::_TUNNEL_ASSIGNMENTS` — KeyError à l'import si un intent disparaît) + `_validate_goal_drift()` (tunnels vides, rôles incohérents, `handled_by_flow` sans tunnel). Les 3 copies (`core/router.py`, `flows/buyer/helpers.py`, `flows/buyer/order_tracking.py`) et les 2 ensembles de navigation (`tunnel_manager.CRITICAL_BREAKOUT_INTENTS`, `goal_planner._NAVIGATION_INTENTS` — même composition, unifiés en `NAVIGATION_BREAKOUT_GOALS`) sont devenus des **aliases d'import** (identité d'objet vérifiée). `BUYER_CART_RESET` reste une adjonction explicite (goal interne aux flows, hors catalogue interpréteur). Dérive résorbée : `MARKET_MY_REQUESTS` manquait dans la copie router → il route désormais directement `to_order_tracking` (équivalent à l'ancien chemin resolver→order_tracking_resolver, un hop de moins).
> **Fusion `DefaultDomainRouter` + `AfterValidatorPolicy`** → **`core/router.py::DomainRouter`** unique (`decide()` = ex-policy rules, `resolve()` = ex-router, factories `for_buyer`/`for_producer`, entrée `get_domain_router(role)`). `core/policies.py` ne garde que `FastPathPolicy`. `graph_builder` câble `domain_router.decide` + `domain_router.resolve`. ⚠️ Import eager de `tunnel_manager` retiré de `core/__init__.py` (créait un cycle intent→core/\_\_init\_\_→tunnel_manager→goals→intent). Vérifié : égalité stricte des 7 ensembles avec les valeurs historiques, build des graphes BUYER (27 nœuds) et PRODUCER (24 nœuds). NB : les 12 tests `test_market_registration_*` échouent sur `adapter.py` (« mcp_session is required ») — fixtures périmées antérieures à la Phase 1, chantier séparé.

> **✅ PHASE 0 EXÉCUTÉE (2026-07-21)** — Résolu : §5.1 (`protocols/core.py` recréé en version minimale : `CorrelationCtx`/`TraceCategory`/`TraceStep`/`TraceEnvelope`/`CachePolicy`/`ClientCapabilities`), §5.2 (imports `Base` redirigés vers `domain.orm_base`, shim mort retiré de `core/database.py`), §5.3 (`voice.py` supprimé, zéro appelant), §5.4 (`order.py::OrderMixin` supprimé), §5.5 (`main.py` racine supprimé + entrée `.gitignore`), §5.6 (`services/__init__.py` assaini, `AgriDatabaseService` lazy réel), §5.7/§5.8/§5.9 (doublons `utils.py` supprimés : `_detect_disambiguation_candidates`, `_build_proactive_hint`, `build_confirmation_summary`, `friendly_missing` ; `_compute_progress` unifié dans `utils.py` — bug de dénominateur `pct` corrigé — importé par `cognitive.py` et `validation.py`), §5.10 (`route_after_validator` + `_has_minimum_cart_payload` supprimés de `router.py` ; **NOTE : `DefaultDomainRouter.resolve()` est VIVANT** — c'est le `context_resolver` réel câblé par `graph_builder.py:237`), §5.11 (partiel : `nodes/routing.py::_route_after_planner` supprimé ; câblage de `contract_validation` reporté en Phase 2), §5.13 (`waste/` supprimé + export lazy `graph` retiré du `__init__` ; branche `onboarding_node` corrigée vers `flows.common.onboarding`). Tests zombies supprimés (`test_voice.py`, `test_imports.py`, `test_database.py` — visaient `db_handler`/`formation`/`sentinelle`/`a2a`, disparus) ; `test_market_coach_registration_flow.py`/`test_market_registration_fixes.py` réparés (`core.state` + `adapter`). Restent ouverts : §5.11 (câblage contracts), §5.12, §5.14–§5.19.

## 5.1 `protocols/ag_ui/renderer.py` — import cassé, module inutilisable
**Fichier** : `backend/src/ladini/protocols/ag_ui/renderer.py` (ligne d'import, en tête de fichier).
**Problème** : `from ladini.protocols.core import (ClientCapabilities, TraceCategory, TraceEnvelope)` — `ladini/protocols/core.py` **n'existe pas** dans l'arbre source (seul un `.pyc` orphelin subsiste dans `__pycache__`). Toute tentative d'import de ce module lève `ModuleNotFoundError`.
**Impact** : `WhatsAppRenderer`, `WebRenderer`, `SMSRenderer` sont inutilisables ; `protocols/ag_ui/__init__.py` qui re-exporte tout le module échouera aussi si `renderer.py` y est importé en dur.
**À faire** : soit restaurer `protocols/core.py` (retrouver `ClientCapabilities`/`TraceCategory`/`TraceEnvelope` dans l'historique git), soit retirer la dépendance et inline ces types localement dans `renderer.py`.

## 5.2 `infrastructure/mcp/context.py` + `services/memory/{user_profile,episodic_memory}.py` — imports cassés
**Fichiers** :
- `backend/src/ladini/infrastructure/mcp/context.py` (import lazy `ladini.protocols.core.CachePolicy`).
- `backend/src/ladini/services/memory/user_profile.py` (import `from ladini.services.database.model import Base`).
- `backend/src/ladini/services/memory/episodic_memory.py` (même import).
**Problème** : `ladini.services.database.model` n'existe pas (seul un `.pyc` orphelin). `UserFarmProfileModel(Base)` et `EpisodicMemoryModel(Base)` ne peuvent pas être définis.
**Impact** : tout import de `ladini.services.memory` échoue → `ContextOptimizer`/`ProfileExtractor` inutilisables, et `infrastructure/mcp/context.py::MCPContextServer` (qui les importe en lazy) casse dès qu'on appelle `build_context`/`enrich_state`.
**À faire** : restaurer `services/database/model.py` (probablement un alias vers `domain.orm_base.Base`) ou rediriger l'import vers `ladini.domain.orm_base.Base`.

## 5.3 `graphs/agents/common/voice.py` — import cassé
**Fichier** : `backend/src/ladini/graphs/agents/common/voice.py`, classe `VoiceAgent`.
**Problème** : `from ladini.services.voice_engine import VoiceEngine` — `services/voice_engine.py` n'existe pas dans l'arbre scanné.
**À faire** : soit implémenter `services/voice_engine.py`, soit supprimer `VoiceAgent` si la fonctionnalité vocale n'est plus utilisée (vérifier appelants avant suppression).

## 5.4 `services/database/order.py::OrderMixin` — mort, non câblé dans le MRO
**Fichier** : `backend/src/ladini/services/database/order.py`.
**Problème** : `OrderMixin` définit `get_order_details`/`run_order_status_hooks` mais n'apparaît PAS dans la liste MRO de `AgriDatabaseService` (`services/database/d.py`). Ces méthodes ne sont donc jamais accessibles via le dispatcher MCP.
**À faire** : soit ajouter `OrderMixin` au MRO de `d.py` si ces méthodes sont nécessaires, soit supprimer le fichier s'il est réellement mort (vérifier qu'`OrderService` dans `order_service.py` ne couvre pas déjà ce besoin — c'est probable vu le doublon de nommage).

## 5.5 `main.py` / `api/routes/__init__.py` — double point d'entrée FastAPI, l'un mort
**Fichiers** : `backend/src/ladini/main.py`, `backend/src/ladini/api/routes/__init__.py`.
**Problème** : `main.py` définit un `app = FastAPI(...)` alternatif qui monte `from .api.routes import router` — mais `api/routes/__init__.py` est **vide** (pas de `router` défini). `main.py` planterait à l'exécution (`ImportError: cannot import name 'router'`) ou monterait un routeur vide selon la résolution exacte. La vraie app de prod est `api/main.py` (montée via `api/server.py`, lancée par gunicorn/uvicorn).
**À faire** : supprimer `main.py` (dead code, source de confusion) OU le réparer pour pointer vers les vrais routers (`api.routes.market`, `api.routes.twilio_webhook`) s'il doit être conservé comme point d'entrée alternatif.

## 5.6 `services/__init__.py` — référence à un symbole jamais défini
**Fichier** : `backend/src/ladini/services/__init__.py`.
**Problème** : `__all__ = ["AgriDatabase", "AgriDatabaseService"]` mais `AgriDatabase` n'est ni défini ni importé nulle part dans le fichier ni dans le package scanné. `from ladini.services import AgriDatabase` lèvera `ImportError`.
**À faire** : retirer `"AgriDatabase"` de `__all__`, ou l'importer/aliaser correctement si un tel symbole doit exister (probablement une confusion avec `AgriDatabaseService`).

## 5.7 Duplication : deux scanners de désambiguïsation lexicale
**Fichiers** : `graphs/agents/market_coach/nodes/semantic_disambiguation.py::_detect_disambiguation_candidates` (role-aware, utilisée par le graphe réel) vs. `graphs/agents/market_coach/utils.py::_detect_disambiguation_candidates` (signature single-arg, sans filtre de rôle).
**Problème** : la version de `utils.py` n'est appelée par aucun code tracé — logique dupliquée avec un comportement légèrement différent (pas de filtrage par rôle), risque de dérive si l'une est modifiée sans l'autre.
**À faire** : supprimer la version dans `utils.py` si confirmé inutilisée (`grep` sur les appelants avant suppression), ou factoriser en un seul module partagé.

## 5.8 Duplication : `_compute_progress`/`_build_proactive_hint`
**Fichiers** : `graphs/agents/market_coach/nodes/cognitive.py` (définition locale) vs `graphs/agents/market_coach/utils.py` (autre définition, importée par `nodes/validation.py`).
**Problème** : deux implémentations quasi-identiques maintenues séparément.
**À faire** : unifier dans un seul module (probablement `utils.py` puisque `validation.py` en dépend déjà), faire pointer `cognitive.py` vers cette version unique.

## 5.9 Duplication : `build_confirmation_summary`
**Fichiers** : `graphs/agents/market_coach/services/ui/confirmation_summary.py` (riche, spécifique par goal, **utilisée** par `nodes/confirmation_gate.py`) vs. `graphs/agents/market_coach/utils.py::build_confirmation_summary` (générique, non utilisée par les nœuds tracés).
**À faire** : supprimer la version dans `utils.py` après confirmation qu'aucun appelant ne l'utilise (`grep -rn "from.*utils import.*build_confirmation_summary"`).

## 5.10 `core/router.py::DefaultDomainRouter.route_after_validator` — logique morte dupliquée
**Fichier** : `graphs/agents/market_coach/core/router.py`.
**Problème** : cette méthode réimplémente la logique de routage buyer déjà centralisée dans `core/policies.py::AfterValidatorPolicy.for_buyer()`. Le graphe câble réellement `policies.get_after_validator_policy(role)` (confirmé dans `core/graph_builder.py`), donc `route_after_validator` de `router.py` n'est atteint par aucun chemin d'exécution tracé.
**À faire** : supprimer la méthode (ou toute la classe si `DefaultDomainRouter.resolve()` n'est pas non plus appelée ailleurs — à vérifier par grep sur `DefaultDomainRouter(`) pour éviter la dérive entre deux sources de vérité sur le routage post-validator.

## 5.11 `nodes/contract_validation.py` et `nodes/routing.py::_route_after_planner` — nœuds dormants
**Fichiers** : `graphs/agents/market_coach/nodes/contract_validation.py`, `graphs/agents/market_coach/nodes/routing.py`.
**Problème** : ni le nœud `contract_validation` ni la fonction `_route_after_planner` locale à `nodes/routing.py` n'apparaissent dans la liste des nœuds/arêtes compilés de `core/graph_builder.py::build_graph`. `interpreter/contracts.py::enforce_contract` (4 contrats Pydantic buyer) n'est donc jamais exécuté en production malgré son apparente utilité (défense en profondeur post-LLM).
**À faire** : décider explicitement — soit câbler `contract_validation` dans le graphe (probablement après `input_interpreter`/`memory_update` pour les 4 goals buyer concernés), soit supprimer le module si la validation Pydantic est jugée redondante avec `nodes/validation.py`.

## 5.12 `flows/buyer/contexts.py` — classes typées non utilisées, migration inachevée
**Fichier** : `graphs/agents/market_coach/flows/buyer/contexts.py`.
**Problème** : `VendorSelectionState`, `PreorderPhase`, `NegotiationContext` sont des wrappers typés OOP sur les dicts `vendor_selection_context`/`preorder_workflow`/`negotiation_context`, mais `cart.py`, `preorder.py`, `negotiation.py` manipulent directement les dicts bruts sans jamais instancier ces classes.
**À faire** : soit terminer la migration (remplacer les accès dict bruts par ces wrappers dans `cart.py`/`preorder.py`/`negotiation.py`), soit supprimer `contexts.py` si l'effort n'est plus jugé prioritaire.

## 5.13 `waste/graph.py`, `waste/market.py` — code mort probable, à vérifier avant suppression
**Fichiers** : `graphs/agents/market_coach/waste/graph.py`, `graphs/agents/market_coach/waste/market.py`.
**Problème** : le nom du dossier (`waste/`) et son emplacement parallèle aux arbres actifs (`flows/`, `nodes/`) suggèrent un prototype périmé, dans la même veine que `flows/buyer/flow_backup.py` (confirmé mort, zéro importeur, session du 2026-07-21).
**À faire** : `grep -rn "waste\." backend/src/ladini/graphs/agents/market_coach/ --include="*.py"` pour confirmer zéro importeur, puis supprimer si confirmé.

## 5.14 `ladini.agents.reducers` — emplacement contre-intuitif des reducers LangGraph
**Fichiers concernés** : `graphs/agents/market_coach/core/state.py`, `flows/producer/state.py`, `flows/buyer/state.py`.
**Problème** : ces trois fichiers importent `merge_dict`, `replace_value`, `replace_list`, `load_snapshot`, `_KEEP` depuis `ladini.agents.reducers` — un module **hors** du sous-arbre `market_coach/`, alors que toute la logique de state (`MarketAgentState`, `BuyerContext`, `ProducerContext`) est locale à `market_coach/`. Ce n'est pas un bug fonctionnel mais une dépendance architecturale surprenante (un futur refactor qui déplacerait `market_coach/` risquerait de casser cet import sans qu'il soit évident où chercher).
**À faire** : documenter explicitement cette dépendance croisée dans le README de `market_coach/` (ou envisager de rapatrier `reducers.py` dans `market_coach/core/` si aucun autre agent ne les utilise — vérifier `grep -rn "agents.reducers" backend/src/ladini/` pour les autres consommateurs avant de déplacer).

## 5.15 Commentaires contradictoires sur `update_order_status`
**Fichier** : `backend/src/ladini/services/database/producer.py::ProducerMgmtMixin.update_order_status`.
**Problème** : le rapport d'extraction note des commentaires inline contradictoires quant à savoir si cette méthode (sans verrou explicite) ou une autre version avec `with_for_update` est la version "vivante". Risque de race condition sur mise à jour concurrente du statut de commande si la mauvaise version est celle réellement appelée par le dispatcher MRO.
**À faire** : auditer `services/database/d.py`'s MRO pour confirmer laquelle des deux implémentations est résolue en premier (ordre MRO Python : premier mixin dans la liste qui définit la méthode gagne), ajouter le verrou `with_for_update` sur celle qui est effectivement active si elle ne l'a pas.

## 5.16 `services/database/README.md` — dérive documentation/code
**Fichier** : `backend/src/ladini/services/database/README.md` (1138 lignes).
**Problème** : ce README documente en détail l'architecture (dispatcher MRO, `@transactional`, table map) mais son exactitude vs. le code actuel n'a été vérifiée que partiellement lors de cette extraction — il mentionne déjà lui-même au moins 2 situations de code mort/piège MRO en interne.
**À faire** : lors du prochain refactor de `services/database/`, relire ce README en parallèle du code et corriger les deux sections signalées comme obsolètes par le README lui-même.

## 5.17 `agents/task_handler.py` — FSM parallèle non intégrée
**Fichier** : `backend/src/ladini/agents/task_handler.py`.
**Problème** : `TaskHandler`/`AgentState`/`GoalState` forment une FSM Pydantic complète avec circuit-breaker, mais rien dans le graphe LangGraph MarketCoach ne semble l'utiliser directement pour piloter le flux principal — `nodes/executor.py::mcp_tool_executor` instancie un `TaskHandler` pour un "pre-check" WRITE seulement (usage partiel), pas pour tout le cycle de vie décrit par cette classe.
**À faire** : clarifier si `TaskHandler` est un vestige d'une architecture antérieure (pré-LangGraph) à supprimer, ou un mécanisme de sécurité à documenter/renforcer explicitement dans `executor.py`.

## 5.18 `nodes/routing.py::_route_after_security` vs `_SECURITY_BLOCKING`
**Fichier** : `graphs/agents/market_coach/nodes/routing.py`.
**Vérification recommandée** : confirmer que `_SECURITY_BLOCKING = frozenset({SCAM_DETECTED, ACCOUNT_BLOCKED, PROHIBITED_PRODUCT, PROFILE_UNAVAILABLE})` reste synchronisé avec les valeurs de `security_status` effectivement posées par `nodes/security_moderation.py` (`_blocked_patch`, `_check_account_gate`, `_check_prohibited`) — toute nouvelle valeur de statut ajoutée dans `security_moderation.py` sans mise à jour de ce frozenset créerait une fuite de sécurité silencieuse (message bloqué en apparence mais routé vers l'interprète).

## 5.19 Registre `services/database/__init__.py` — lazy import fragile
**Fichier** : `backend/src/ladini/services/database/__init__.py`.
**Problème** : `from ladini.core.database import get_db` + `def __getattr__(name)` pour import lazy de `AgriDatabaseService`. Ce pattern masque les erreurs d'import de `d.py` (et de ses 10 mixins) jusqu'au premier accès à l'attribut plutôt qu'à l'import du package — un mixin cassé (ex: `moderation.py` avec une faute de frappe) ne sera détecté qu'à l'exécution, pas au démarrage.
**À faire** : envisager un import eager derrière un flag de test/CI pour détecter les régressions de mixin au build plutôt qu'en prod.

---

## Annexe — Fichiers non lus en détail (signalés par les agents d'extraction, pattern déjà connu)

Ces fichiers suivent un pattern déjà entièrement documenté ailleurs dans ce document (`@register_action` + `DomainContext`/`DomainResult`, voir §3.B.7/3.B.8) — à lire intégralement si un refactor les touche directement :
`actions/{finance,finance_dto,procure,procure_dto,profile,profile_dto,stock,stock_dto,system,system_dto,sales_dto,farm_dto}.py`,
`domain/{finance,procurement,profile,stock,system}.py`,
`security.py` (market_coach root — `SecurityService`, consommé par `nodes/security_moderation.py`),
`adapter.py` (market_coach root — rôle non tracé),
`services/menu_snapshot.py`, `services/onboarding.py`, `services/profile_loader.py`.
