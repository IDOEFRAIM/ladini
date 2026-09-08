"""
Settings — Configuration centralisée AgriConnect (Pydantic Settings).

Toute la configuration passe par ici. Plus jamais de os.getenv() éparpillé.
Usage:
    from backend.core.settings import settings
    print(settings.DATABASE_URL)
"""

import logging
import os
from pathlib import Path
from typing import ClassVar

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger("agriconnect.settings")


class Settings(BaseSettings):
    """Configuration centralisée, lue depuis les variables d'env / .env."""

    # --- Paths ---
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    AUDIO_OUTPUT_DIR: str = "./audio_output"
    # Default to repository root `ca-certificate.crt` (developer-provided file)
    DB_CA_PATH: str = str(BASE_DIR.parent.parent / "ca-certificate.crt")
    # --- API ---
    APP_NAME: str = "AgriConnect"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ALLOWED_ORIGINS: list[str] = ["*"]

    # --- LLM (Provider-agnostic) ---
    # Valeurs possibles : "groq" (défaut), "bedrock", "azure" (scaffolding non
    # implémenté). Basculer de provider = changer LLM_PROVIDER + pointer
    # LLM_MODEL/LLM_MODEL_REASONING vers des IDs de modèle valides pour CE
    # provider (les noms Groq et Bedrock ne se recoupent pas) — jamais un
    # changement de code, tous les sites d'appel consomment le même adapter
    # normalisé.
    #
    # "bedrock" a DEUX chemins d'implémentation (voir core/get_llm.py) :
    #   1. Passerelle compatible OpenAI (OPENAI_API_KEY/OPENAI_BASE_URL
    #      ci-dessous, ex: "AWS Bedrock API keys") — PRÉFÉRÉ quand
    #      OPENAI_BASE_URL est défini : c'est le protocole Groq lui-même
    #      (Groq est déjà compatible OpenAI), donc réutilise l'adapter
    #      existant tel quel, aucune traduction de requête nécessaire.
    #   2. Accès natif via boto3/API Converse (identifiants IAM
    #      AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN/
    #      AWS_REGION ci-dessous) — utilisé en repli si OPENAI_BASE_URL
    #      n'est pas défini.
    LLM_PROVIDER: str = "groq"
    # Passerelle Bedrock compatible OpenAI (chemin 1 ci-dessus). Noms de
    # variables volontairement alignés sur ceux lus nativement par le SDK
    # `openai` (`OPENAI_API_KEY`/`OPENAI_BASE_URL`).
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""
    AGRICONNECT_APIKEY: str = ""
    GROQ_API_KEY: str = ""
    # Modèle rapide/économique — nœuds d'infrastructure (normalisation,
    # modération, nettoyage d'état) qui n'ont besoin d'aucun raisonnement.
    #
    # ATTENTION (2026-08-18, 2e incident du même type) : ces valeurs ont été
    # changées pour des noms de modèles Qwen ("qwen/qwen-2.5-72b-instruct",
    # "qwen/qwen-2.5-max") qui N'EXISTENT PAS sur Groq — ce sont des noms
    # DashScope/Alibaba Cloud, pas la nomenclature Groq. Résultat, confirmé
    # par log : "Error code: 404 - The model `qwen/qwen-2.5-max` does not
    # exist" à CHAQUE appel d'input_interpreter, forçant un repli
    # déterministe dégradé sur tous les tours — c'est la cause racine de
    # toute une série de symptômes en cascade (produit mal extrait,
    # confirmation générique "BUYER_REQUEST" qui fuite, etc.), pas des bugs
    # indépendants. Remis aux deux SEULES valeurs dont ce projet a une
    # preuve directe de fonctionnement sur Groq (200 OK observés en logs).
    # Si un modèle Qwen est vraiment souhaité, vérifier d'ABORD son nom
    # exact dans le catalogue Groq (console.groq.com/docs/models) — jamais
    # copier un nom depuis la documentation d'un autre fournisseur.
    LLM_MODEL: str = "qwen/qwen3.6-27b"
    # Modèle de raisonnement — tout goal métier complexe (interprétation
    # d'intent, planification de goal, génération de réponse). Utilisé par
    # `graphs/agents/market_coach/llm_router.py` via ROUTING_MAP ci-dessous.
    LLM_MODEL_REASONING: str = "openai/gpt-oss-120b"
    LLM_TEMPERATURE: float = 0.0

    # Table de routage goal -> modèle Groq, consommée par
    # `llm_router.get_model_for_goal()`. Clé "__default__" = modèle utilisé
    # pour tout goal non listé explicitement (raisonnement métier). Ne JAMAIS
    # coder de nom de modèle en dur ailleurs que LLM_MODEL/LLM_MODEL_REASONING
    # ci-dessus — cette table s'y réfère, elle ne duplique pas les valeurs.
    ROUTING_MAP: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _build_routing_map(self) -> "Settings":
        """Construit ROUTING_MAP depuis LLM_MODEL/LLM_MODEL_REASONING si elle
        n'a pas été fournie explicitement (ex: override JSON via env var).
        """
        if not self.ROUTING_MAP:
            self.ROUTING_MAP = {
                "INPUT_NORMALIZATION": self.LLM_MODEL,
                "SECURITY_MODERATION": self.LLM_MODEL,
                "STATE_CLEANER": self.LLM_MODEL,
                "__default__": self.LLM_MODEL_REASONING,
            }
        return self

    @property
    def llm_api_key(self) -> str:
        """Retourne la clé API LLM disponible (Groq ou générique)."""
        # Prefer explicit project key, then provider-specific key, then environment
        key = self.AGRICONNECT_APIKEY or self.GROQ_API_KEY
        if not key:
            # allow direct env override for interactive sessions
            key = os.getenv("GROQ_API_KEY") or os.getenv("AGRICONNECT_APIKEY") or ""
        return key

    # --- Azure OpenAI (utilisé si LLM_PROVIDER=azure) ---
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT_NAME: str = "gpt-4o"
    AZURE_OPENAI_API_VERSION: str = "2024-05-01-preview"

    # --- Database (PostgreSQL) ---
    DATABASE_URL: str = ""
    # Provider-specific override (DigitalOcean): prefer when set.
    DO_DATABASE_URL: str = ""
    # SSL policy for PostgreSQL connections:
    # - verify-full: TLS + certificate verification (recommended)
    # - require: TLS without certificate verification (dev fallback)
    # - disable: no TLS (local-only)
    DB_SSL_MODE: str = "require"
    DB_POOL_SIZE: int = 20
    DB_POOL_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: float = 60.0
    # --- Redis / Celery ---
    # Défaut local pour le dev (docker-compose / redis local) ; en prod,
    # définir REDIS_URL via variable d'environnement (ex: rediss://... pour
    # un Redis managé avec TLS).
    REDIS_URL: str = "redis://localhost:6379/0"
    VALKEY_ENDPOINT: str = ""
    VALKEY_AUTH_TOKEN: str = ""
    VALKEY_USE_TLS: bool = True
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""
    # Lean mode default: run tasks directly without broker/workers.
    USE_ASYNC_QUEUE: bool = False
    # Streams are optional in lean mode and enabled when async queue is enabled.
    USE_REDIS_STREAMS: bool = False
    # Gold storage backend: pgvector on Postgres by default (RDS-only).
    VECTOR_BACKEND: str = "pgvector"
    # Fallback strategy when RedisSearch (FT.*) is unavailable:
    # - "manual": force Redis HGET + cosine in Python (Tier 1)
    # - "pgvector": skip Redis fallback and use PgVector
    # - "auto": try manual Redis first, then PgVector
    RAG_REDIS_FALLBACK_MODE: str = "manual"

    # --- MCP Runtime Startup ---
    MCP_ALLOW_DEGRADED_START: bool = False
    MCP_DB_STARTUP_RETRIES: int = 2
    MCP_DB_STARTUP_RETRY_DELAY_SEC: float = 1.5
    MCP_DB_SERVER_HOST: str = "localhost"
    MCP_DB_SERVER_PORT: int = 8003
    MCP_DB_TRANSPORT: str = "http"
    MCP_DB_STDIO_ENTRYPOINT: str = str(
        (Path(__file__).resolve().parent.parent.parent)
        / "agriconnect"
        / "protocols"
        / "mcp"
        / "servers"
        / "db_server.py"
    )
    MCP_DB_STDIO_CWD: str = str(Path(__file__).resolve().parent.parent.parent.parent)
    MCP_DB_STDIO_PYTHON: str = ""
    MCP_DB_STDIO_ENV: dict[str, str] = Field(
        default_factory=lambda: {
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent.parent)
        }
    )
    # Si vide, dérivé de MCP_DB_SERVER_HOST/MCP_DB_SERVER_PORT (voir
    # infrastructure/mcp/client.py::MCPTransportConfig.from_settings) — évite
    # de configurer deux fois la même adresse (celle du daemon HTTP MCP et
    # celle que le client MarketRuntime doit contacter).
    MCP_DB_HTTP_URL: str = ""
    MCP_DB_HTTP_HEADERS: dict[str, str] = Field(default_factory=dict)
    # Secret partagé entre le daemon MCP HTTP (protocols/mcp/servers/http_server.py)
    # et son client (infrastructure/mcp/client.py::HttpMCPAdapter).
    #
    # ⚠️ SÉCURITÉ — le daemon expose `POST /mcp` (JSON-RPC, tools/call), qui
    # exécute N'IMPORTE QUEL outil DB (création de commande, déblocage de
    # fonds escrow, suppression de stock...) ET dérive l'identité de
    # l'appelant depuis le PAYLOAD (`_derive_context_identity`, runtime.py) :
    # sans authentification, quiconque atteint ce port agit comme n'importe
    # quel utilisateur.
    # Renseigner ce secret active la vérification `Authorization: Bearer ...`
    # côté daemon et son envoi automatique côté client. Laissé vide, le daemon
    # démarre quand même (aucune rupture de déploiement existant) mais journalise
    # un CRITICAL au démarrage : il ne doit alors JAMAIS écouter ailleurs que
    # sur la boucle locale.
    MCP_HTTP_AUTH_TOKEN: str = ""
    MCP_DB_GRPC_TARGET: str = ""
    MCP_DB_GRPC_TLS: bool = False
    MCP_DB_GRPC_METADATA: dict[str, str] = Field(default_factory=dict)
    MCP_DB_GRPC_LIST_TOOLS_METHOD: str = "/agriconnect.mcp.MCP/ListTools"
    MCP_DB_GRPC_CALL_TOOL_METHOD: str = "/agriconnect.mcp.MCP/CallTool"

    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    # --- Azure Speech (TTS/STT — indépendant du LLM provider) ---
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_KEY_2: str = ""
    AZURE_REGION: str = "westeurope"
    AZURE_SPEECH_ENDPOINT: str = "a"
    USE_AZURE_SPEECH: bool = False

    # --- Fournisseur de messagerie WhatsApp ---
    # "whatsapp_cloud" (API Meta directe, moins chère) ou "twilio" (repli —
    # tout le code Twilio reste en place pour un rollback instantané en cas de
    # souci avec l'intégration directe). Bascule un seul flag, aucune
    # réécriture nécessaire dans les deux sens.
    MESSAGING_PROVIDER: str = "whatsapp_cloud"

    # --- Twilio / WhatsApp (repli — voir MESSAGING_PROVIDER) ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_NUMBER: str = ""

    # --- Mode Sandbox (2026-08-27) ---
    # Bascule EXPLICITE vers les environnements de test — jamais un simple
    # "on espère que les bonnes clés sont dans .env". Voir `_apply_sandbox_mode`
    # (model_validator ci-dessous) : quand True, force PAYDUNYA_MODE="test"
    # (jamais de vrai paiement en sandbox) et avertit bruyamment si
    # TWILIO_WHATSAPP_NUMBER n'est PAS le numéro sandbox public Twilio
    # (whatsapp:+14155238886) — jamais de correction silencieuse d'un numéro
    # explicitement configuré, seulement un avertissement au démarrage.
    SANDBOX_MODE: bool = False
    # Numéro sandbox public Twilio (identique pour tous les comptes, valable
    # après avoir rejoint via le mot de passe sandbox dans la Console Twilio).
    TWILIO_SANDBOX_WHATSAPP_NUMBER: str = "whatsapp:+14155238886"
    # Remplace le SDK Groq réel par un client déterministe qui renvoie des
    # réponses JSON factices — permet de faire tourner l'agent/les tests sans
    # clé API réelle ni appel réseau. Voir core/get_llm.py::get_groq_sdk.
    # Indépendant de SANDBOX_MODE (utile aussi hors sandbox, ex: CI) mais
    # activé par défaut avec lui via `_apply_sandbox_mode` sauf override
    # explicite.
    MOCK_EXTERNAL_APIS: bool = False

    @model_validator(mode="after")
    def _apply_sandbox_mode(self) -> "Settings":
        if not self.SANDBOX_MODE:
            return self
        if self.PAYDUNYA_MODE != "test":
            logger.warning(
                "SANDBOX_MODE=true : PAYDUNYA_MODE forcé à 'test' (était %r) — "
                "aucun vrai paiement ne doit jamais partir en sandbox.",
                self.PAYDUNYA_MODE,
            )
            self.PAYDUNYA_MODE = "test"
        if (
            self.TWILIO_WHATSAPP_NUMBER
            and self.TWILIO_WHATSAPP_NUMBER != self.TWILIO_SANDBOX_WHATSAPP_NUMBER
        ):
            logger.warning(
                "SANDBOX_MODE=true mais TWILIO_WHATSAPP_NUMBER=%r n'est PAS le "
                "numéro sandbox public Twilio (%r) — vérifiez que ce numéro "
                "pointe bien vers un environnement de test, pas vers un "
                "sender WhatsApp de production.",
                self.TWILIO_WHATSAPP_NUMBER,
                self.TWILIO_SANDBOX_WHATSAPP_NUMBER,
            )
        logger.warning(
            "🧪 SANDBOX_MODE actif — provider=%s, twilio_number=%s, "
            "paydunya_mode=%s, mock_external_apis=%s",
            self.MESSAGING_PROVIDER,
            self.TWILIO_WHATSAPP_NUMBER or "(non configuré)",
            self.PAYDUNYA_MODE,
            self.MOCK_EXTERNAL_APIS,
        )
        return self

    # --- Twilio Messages Interactifs (boutons / listes) ---
    # Les boutons/listes WhatsApp passent par la Content API Twilio (ContentSid
    # pré-créé), et NE s'affichent PAS sur le sandbox — requièrent un Sender
    # WhatsApp de production approuvé. Tant que ce flag est False (ou qu'aucun
    # ContentSid n'est fourni), l'envoi retombe automatiquement sur du texte
    # simple (comportement actuel, universel). À activer une fois le sender prod
    # en place et les templates créés (voir Twilio Console → Content Template
    # Builder : type "Quick Reply" pour la confirmation OUI/NON).
    TWILIO_INTERACTIVE_ENABLED: bool = False
    # ContentSid du template Quick Reply de confirmation binaire (2 boutons :
    # payload "CONFIRM" et "REJECT"). Une variable {{1}} porte le corps du récap.
    TWILIO_CONFIRM_CONTENT_SID: str = ""
    # ContentSid du template twilio/list-picker (menus à choix multiples —
    # catalogue, précommandes, sélection de producteur...). Variables :
    # {{1}} corps, {{2}} libellé du bouton, {{3}} items sérialisés en JSON
    # (voir audit UX interactive 2026-08-27, api/tasks.py::_send_via_twilio).
    TWILIO_LIST_PICKER_CONTENT_SID: str = ""

    # --- WhatsApp Cloud API (Meta directe — provider par défaut) ---
    # Récupérés dans Meta for Developers → votre app → WhatsApp → API Setup.
    WHATSAPP_CLOUD_API_TOKEN: str = ""  # Access token permanent (System User)
    WHATSAPP_PHONE_NUMBER_ID: str = (
        ""  # ID du numéro expéditeur (pas le numéro lui-même)
    )
    WHATSAPP_BUSINESS_ACCOUNT_ID: str = (
        ""  # WABA ID (pour la gestion des templates, optionnel ici)
    )
    # Chaîne arbitraire que VOUS choisissez et déclarez dans Meta lors de la
    # configuration du webhook — sert uniquement à la vérification GET
    # initiale (hub.verify_token), jamais utilisée après.
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str = ""
    # Secret de l'app Meta — sert à vérifier la signature HMAC (header
    # X-Hub-Signature-256) de chaque webhook entrant. Sans lui, N'IMPORTE QUI
    # peut poster un faux message sur l'endpoint webhook.
    WHATSAPP_APP_SECRET: str = ""
    # Version de l'API Graph — à faire évoluer périodiquement (Meta déprécie
    # les anciennes versions après ~2 ans).
    WHATSAPP_GRAPH_API_VERSION: str = "v21.0"
    # Boutons interactifs natifs (max 3, sans template pré-approuvé — contrairement
    # à Twilio Content API). Activé par défaut : c'est justement l'un des
    # avantages de l'API directe. Désactiver retombe sur le texte brut
    # "Répondez oui/non" (comportement Twilio historique).
    WHATSAPP_NATIVE_INTERACTIVE_ENABLED: bool = True

    # --- Supabase Storage (photos produits envoyées par WhatsApp) ---
    # Même compte/projet Supabase que la plateforme web (bucket dédié côté
    # backend pour ne jamais mélanger avec d'éventuels buckets front-end).
    # Clé de SERVICE ROLE (pas la clé anon publique) — l'upload se fait
    # serveur à serveur depuis Celery, jamais exposée au client.
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_PRODUCT_BUCKET: str = "ladini"

    # --- Paydunya (Escrow paiement séquestre) ---
    # Réactivé le 2026-08-18 : le blocage KYC côté Paydunya est résolu.
    # Tant que True, une précommande N'EST PLUS confirmée directement — le
    # paiement devient OBLIGATOIRE : `_execute_confirm`
    # (flows/buyer/preorder.py) génère une facture Paydunya et réserve la
    # commande en `AWAITING_PAYMENT` ; le débit de stock + statut CONFIRMED
    # n'arrivent qu'à la réception de l'IPN, re-confirmée serveur-à-serveur
    # auprès de Paydunya (voir EscrowMixin.mark_escrow_paid). Recouper avec
    # False (paiement à la livraison) si Paydunya rebloque un jour — le code
    # escrow reste intact, ce flag est le seul point de bascule.
    ESCROW_PAYMENT_ENABLED: bool = True
    # URL publique de l'API (sans slash final) — sert à construire le
    # callback_url transmis à Paydunya (`/api/webhooks/paydunya-ipn`).
    PUBLIC_API_BASE_URL: str = ""
    PAYDUNYA_MASTER_KEY: str = ""
    PAYDUNYA_PRIVATE_KEY: str = ""
    PAYDUNYA_PUBLIC_KEY: str = ""
    PAYDUNYA_TOKEN: str = ""
    # "test" (sandbox Paydunya) ou "live". Ne détermine PAS la confiance
    # accordée à l'IPN : dans les deux modes, le montant/statut est toujours
    # re-confirmé serveur-à-serveur auprès de Paydunya avant toute écriture DB.
    PAYDUNYA_MODE: str = "test"
    # Fenêtre de réservation avant expiration automatique du paiement (heures).
    PAYDUNYA_PAYMENT_TTL_HOURS: int = 24

    # --- LangSmith / Observabilité ---
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "agriconnect"
    LANGCHAIN_ENDPOINT: str = "a"
    LANGSMITH_API_KEY: str = ""

    @property
    def langsmith_enabled(self) -> bool:
        """True si le tracing LangSmith est activé et configuré."""
        key = self.LANGCHAIN_API_KEY or self.LANGSMITH_API_KEY
        return bool(self.LANGCHAIN_TRACING_V2 and key)

    # --- OpenTelemetry (traces infra) ---
    OTEL_ENABLED: bool = False
    # Endpoint OTLP/gRPC du collector (ex: http://otel-collector:4317).
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # --- Prometheus (métriques /metrics) ---
    PROMETHEUS_ENABLED: bool = True

    # --- Langfuse (LLMOps self-hosted) ---
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    # Host du Langfuse auto-hébergé (ex: http://langfuse-web:3000 en réseau
    # Docker interne, ou https://langfuse.mondomaine.com en externe).
    # Incident 2026-08-27 : `.env` déclare `LANGFUSE_BASE_URL` (Langfuse Cloud),
    # que ce champ n'a JAMAIS lu — il retombait donc sur le défaut Docker
    # interne `langfuse-web`, injoignable hors docker-compose. Le thread
    # consommateur de la SDK Langfuse tourne en arrière-plan (hors de tout
    # try/except applicatif) : l'échec de connexion sur CHAQUE batch loggait
    # "Unexpected error occurred..." sans jamais impacter l'envoi du message
    # WhatsApp lui-même — silencieux mais total sur l'observabilité LLM.
    LANGFUSE_HOST: str = Field(
        default="http://langfuse-web:3000",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )

    # --- LLM Gateway (2026-09-02 — voir graphs/agents/market_coach/llm_gateway/) ---
    # Chaîne de repli par profil, PILOTÉE PAR .ENV (jamais de nom de modèle en
    # dur dans les nodes métier — voir Model Registry). Format : "provider:model"
    # (provider ∈ {groq, bedrock_gateway, bedrock_native}). Vide = candidat absent.
    # Défauts alignés sur les valeurs déjà en prod au moment de l'introduction
    # de cette Gateway (aucune valeur inventée) :
    #
    # Incident réel (2026-09-05) : Groq a décommissionné `llama-3.1-8b-instant`
    # et `llama-3.3-70b-versatile` le 2026-06-17 (tiers gratuit/développeur —
    # voir console.groq.com/docs/deprecations) — HTTP 404 "does not exist or
    # you do not have access to it", classé CONFIG par
    # `llm_gateway/error_classification.py`, disjoncteur ouvert en
    # permanence. Remplacés par les remplacements RECOMMANDÉS PAR GROQ,
    # confirmés existants sous ces IDs exacts (console.groq.com/docs/model/
    # openai/gpt-oss-120b, .../openai/gpt-oss-20b) : `openai/gpt-oss-120b`
    # (repli de `llama-3.3-70b-versatile`) et `openai/gpt-oss-20b` (repli de
    # `llama-3.1-8b-instant`). Choix délibéré de la variante 120b (pas
    # `qwen/qwen3.6-27b`, l'autre repli recommandé par Groq) pour le profil
    # REASONING : même famille de modèle que `bedrock_gateway:openai.gpt-oss-120b`
    # (FALLBACK_1) — une vraie redondance multi-provider sur le MÊME modèle,
    # pas seulement un repli "au cas où" vers un modèle différent.
    LLM_FAST_PRIMARY: str = "bedrock_gateway:qwen.qwen3-32b"
    LLM_FAST_FALLBACK_1: str = "groq:openai/gpt-oss-20b"
    LLM_FAST_FALLBACK_2: str = ""
    LLM_REASONING_PRIMARY: str = "bedrock_gateway:deepseek.v3.2"
    LLM_REASONING_FALLBACK_1: str = "bedrock_gateway:openai.gpt-oss-120b"
    LLM_REASONING_FALLBACK_2: str = "groq:openai/gpt-oss-120b"
    # --- Réconciliation PROCUREMENT (2026-09-03, phase 1 recovery) ---
    # Fenêtre au-delà de laquelle un `ProcurementDraft` `EXECUTING` est
    # considéré bloqué (crash probable) plutôt qu'en cours de traitement
    # légitime — voir `services/database/procurement_draft_store.py::
    # find_stale_executing` (qui réutilise `updated_at`, jamais un champ
    # dupliqué) et `services/reconciliation/procurement_reconciliation_service.py`.
    # Pas de valeur métier codée en dur ailleurs : ce SEUL seuil, ici.
    PROCUREMENT_EXECUTING_STALE_SECONDS: float = 900.0
    # Cadence du job de réconciliation périodique (Celery Beat).
    PROCUREMENT_RECONCILIATION_INTERVAL_SECONDS: float = 300.0

    # --- Réconciliation PREORDER (2026-09-03, clôture escrow/IPN) ---
    # Même principe que PROCUREMENT ci-dessus, appliqué aux 2 statuts
    # PREORDER pouvant rester bloqués : `EXECUTING` (appel
    # `confirm_preorder_draft` en vol) et `AWAITING_PAYMENT` (IPN jamais
    # reçu/traité). Un seul seuil pour les deux — même sémantique
    # "combien de temps avant de considérer une transition en vol comme
    # bloquée", pas de raison métier de les distinguer.
    PREORDER_EXECUTING_STALE_SECONDS: float = 900.0
    PREORDER_RECONCILIATION_INTERVAL_SECONDS: float = 300.0

    # --- Réconciliation SALES_PUBLISH_PRODUCT (2026-09-04, migration SALES) ---
    # Même principe que PROCUREMENT/PREORDER — un `SalesPublishDraft`
    # `EXECUTING` bloqué au-delà de ce seuil déclenche la réconciliation
    # périodique (`services/reconciliation/sales_publish_reconciliation_service.py`).
    SALES_EXECUTING_STALE_SECONDS: float = 900.0
    SALES_RECONCILIATION_INTERVAL_SECONDS: float = 300.0

    # Disjoncteur — défauts repris tels quels de l'ancien `_CircuitBreaker`
    # process-local de get_llm.py (3 échecs / 30s), désormais partagés via
    # Redis entre tous les workers (API + Celery + MCP).
    LLM_CIRCUIT_FAILURE_THRESHOLD: int = 3
    LLM_CIRCUIT_COOLDOWN_SECONDS: float = 30.0
    LLM_HALF_OPEN_PROBES: int = 1
    LLM_PROBE_LOCK_SECONDS: float = 10.0
    # Budget total (toutes tentatives/fallbacks confondus) par profil — repris
    # des `asyncio.wait_for` déjà observés aux sites d'appel existants
    # (8-15s), avec la marge nécessaire pour couvrir 1-2 fallbacks.
    LLM_FAST_BUDGET_SECONDS: float = 12.0
    LLM_REASONING_BUDGET_SECONDS: float = 20.0
    # Alerting admin (incident LLM Gateway) — webhook générique compatible
    # payload Slack Incoming Webhook. Vide = alertes en log structuré
    # uniquement (aucun canal réel configuré tant que l'URL n'est pas fournie).
    ADMIN_ALERT_WEBHOOK_URL: str = ""
    # Auth du nouvel endpoint GET /admin/llm/health (header `X-Admin-Token`) —
    # même esprit que MCP_HTTP_AUTH_TOKEN déjà en place ailleurs dans ce repo.
    # Vide = endpoint refusé par défaut (fail-closed), jamais public sans token.
    ADMIN_API_TOKEN: str = ""

    # --- RAG (adaptatif par profil) ---
    # Default RAG embedding model aligned with 768D pgvector schema.
    EMBEDDING_MODEL: str = "BAAI/bge-base-en-v1.5"
    # Enforce RAG embedding dimensionality throughout the codebase.
    RAG_EMBEDDING_DIM: int = 768
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    # Débutant : rapide, pas de HyDe, peu de résultats
    TOP_K_RETRIEVAL: int = 10
    TOP_K_RERANK: int = 5
    RAG_DEBUTANT_TOP_K: int = 5
    RAG_DEBUTANT_RERANK_K: int = 3
    RAG_DEBUTANT_USE_HYDE: bool = False
    # Intermédiaire : équilibré
    RAG_INTER_TOP_K: int = 10
    RAG_INTER_RERANK_K: int = 5
    RAG_INTER_USE_HYDE: bool = True
    # Expert : précision max, HyDe + rerank lourd
    RAG_EXPERT_TOP_K: int = 20
    RAG_EXPERT_RERANK_K: int = 8
    RAG_EXPERT_USE_HYDE: bool = True
    # --- PDF processor thresholds ---
    PDF_MAX_CHUNK_CHARS: int = 1500
    PDF_MIN_MERGE_CHARS: int = 50
    # Ingestion audit thresholds
    INGESTION_AUDIT_FAILURE_THRESHOLD: float = 0.1
    INGESTION_AUDIT_PREFIX: str = "ingestion_audit"

    # Pydantic Settings: prefer .env inside the package, but fall back to the
    # repository root `.env` (e.g. backend/.env) to support developer workflows.
    _pkg_env = Path(__file__).resolve().parent.parent / ".env"
    _root_env = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    env_path: ClassVar[Path] = _pkg_env if _pkg_env.exists() else _root_env
    model_config = {
        "env_file": str(env_path),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # --- Sentry (observabilité erreurs) ---
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = "development"

    # --- AWS S3 (optional) ---
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_SESSION_TOKEN: str = ""
    AWS_REGION: str = ""
    S3_BUCKET: str = ""
    S3_KEY_PREFIX: str = ""
    S3_REGION: str = ""
    # S3 ingestion hygiene: prefixes to exclude from raw ingestion (comma-separated or list)
    # Extended with common legacy snapshot/metadata prefixes discovered during audit
    INGESTION_S3_EXCLUDE_PREFIXES: list[str] = [
        "crawl_snapshots",
        "snapshots",
        "crawl",
        "data_platforms",
        "fao_publications",
        "fews_net",
    ]


# Singleton — importable partout
settings = Settings()


# Normalize DB URLs: remove surrounding quotes and whitespace so all code
# sees a canonical value. Support DO_DATABASE_URL as an explicit override.
def _normalize_db_url(url: str | None) -> str | None:
    if url is None:
        return None
    s = str(url).strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s or None


# Prefer explicit provider URL when present (loaded via pydantic from env).
_do_db = _normalize_db_url(
    settings.DO_DATABASE_URL
    or os.getenv("DO_DATABASE_URL")
    or os.getenv("AGRICONNECT_DO_DATABASE_URL")
)
_db = _normalize_db_url(settings.DATABASE_URL or os.getenv("DATABASE_URL"))
if _do_db:
    settings.DO_DATABASE_URL = _do_db
    settings.DATABASE_URL = _do_db
elif _db:
    settings.DATABASE_URL = _db
else:
    settings.DATABASE_URL = ""


# ── LangSmith : exporter les variables d'environnement ───────────────
# LangChain / LangGraph lisent ces variables automatiquement.
# On les exporte ici pour que tout .invoke() soit tracé sans code additionnel.
def _bootstrap_langsmith():
    if not settings.langsmith_enabled:
        return
    _key = settings.LANGCHAIN_API_KEY or settings.LANGSMITH_API_KEY
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", _key)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.LANGCHAIN_PROJECT)
    os.environ.setdefault("LANGCHAIN_ENDPOINT", settings.LANGCHAIN_ENDPOINT)


_bootstrap_langsmith()
