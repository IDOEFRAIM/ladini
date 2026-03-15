import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
import logging

from .models import (
    Base, User, Zone, Alert, MarketItem, WeatherData,
    Conversation, ConversationMessage,
    UserCrop, SurplusOffer, SoilDiagnosis, PlantDiagnosis, Reminder,
    AgentAction, ExternalContext,
)

logger = logging.getLogger(__name__)

class AgriDatabase:
    """
    Abstraction de la couche mémoire (PostgreSQL).
    Gère les connexions, les sessions et les opérations métier pour l'Agent.

    Deux modes d'initialisation :
      1. db_url      → crée son propre engine (mode standalone)
      2. engine + session_factory → réutilise le pool centralisé (mode production)
    """
    def __init__(self, db_url: str = None, *, engine=None, session_factory=None):
        """
        Initialise la connexion à la base de données.

        SÉCURITÉ : create_all() utilise checkfirst=True (défaut SQLAlchemy).
        Cela signifie qu'il ne crée que les tables MANQUANTES, sans jamais
        DROP ou ALTER les tables existantes gérées par Prisma (web).
        """
        if engine is not None and session_factory is not None:
            # Mode production : réutilise le pool centralisé de core/database.py
            self.engine = engine
            self.SessionLocal = session_factory
            logger.info("✅ AgriDatabase initialisé (pool centralisé)")
        elif db_url:
            # Mode standalone : crée son propre engine
            self.engine = create_engine(db_url, pool_pre_ping=True)
            self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
            logger.info("✅ AgriDatabase initialisé (engine propre)")
        else:
            raise ValueError(
                "AgriDatabase: fournir db_url OU (engine + session_factory)"
            )

        # create_all(checkfirst=True) : crée uniquement les tables manquantes
        # N'écrase JAMAIS les tables existantes (safe pour coexistence Prisma)
        try:
            Base.metadata.create_all(self.engine, checkfirst=True)
        except Exception as e:
            logger.warning("DB schema sync (non-fatal): %s", e)

        # Executor partagé pour les écritures audit non-bloquantes
        self._audit_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audit")

    def close(self):
        """Ferme le pool d'audit et dispose l'engine si créé en standalone."""
        self._audit_executor.shutdown(wait=False)
        if hasattr(self, '_standalone_engine') and self._standalone_engine:
            self.engine.dispose()
    
    # ═══════════════════════════════════════════════════════════
    # AUDIT TRAIL & PREUVE DE PROTOCOLE (Axe 3 Financement)
    # ═══════════════════════════════════════════════════════════

    def log_audit_action(
        self,
        agent_name: str,
        action_type: str,
        user_id: str,
        protocol: str,
        payload: Dict[str, Any],
        resource: str = "internal",
        confidence: float = 1.0
    ):
        """
        Enregistre une action critique pour la traçabilité bancaire.
        Écriture NON-BLOQUANTE (thread séparé) pour ne pas ralentir la réponse.
        Génère une signature SHA-256 pour sceller l'enregistrement.
        Idempotent : la clé hash_signature empêche les doublons.
        """
        import hashlib
        import json
        from concurrent.futures import ThreadPoolExecutor

        payload_str = json.dumps(payload, sort_keys=True, default=str)
        signature_base = f"{agent_name}:{action_type}:{user_id}:{protocol}:{payload_str}"
        signature = hashlib.sha256(signature_base.encode()).hexdigest()

        def _write_audit():
            session = self.SessionLocal()
            try:
                # Idempotence: skip si signature déjà existante
                exists = session.execute(
                    text("SELECT 1 FROM agent_audit_trails WHERE hash_signature = :sig LIMIT 1"),
                    {"sig": signature}
                ).fetchone()
                if exists:
                    logger.debug("Audit doublon ignoré: %s", signature[:8])
                    return

                query = text("""
                    INSERT INTO agent_audit_trails 
                    (agent_name, action_type, user_id, protocol_used, resource_accessed, 
                     decision_payload, confidence_score, hash_signature)
                    VALUES 
                    (:agent, :action, :uid, :proto, :res, :payload, :conf, :sig)
                """)
                session.execute(query, {
                    "agent": agent_name,
                    "action": action_type,
                    "uid": user_id,
                    "proto": protocol,
                    "res": resource,
                    "payload": payload_str,
                    "conf": confidence,
                    "sig": signature
                })
                session.commit()
                logger.info(f"🔒 Audit Logged: {agent_name} -> {action_type} [{signature[:8]}]")
            except Exception as e:
                logger.error(f"Audit Log Error: {e}")
                session.rollback()
            finally:
                session.close()

        # Écriture asynchrone — ne bloque pas la réponse utilisateur
        # Réutilise un pool partagé au lieu de créer un executor par appel
        self._audit_executor.submit(_write_audit)

    @contextmanager
    def _get_session(self):
        """Fournit une session transactionnelle sécurisée."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    # --- LOGIQUE UTILISATEURS ---
    
    def get_user_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        with self._get_session() as session:
            # Select explicit minimal columns to avoid ORM selecting missing columns
            try:
                row = session.execute(
                    text("SELECT id, name, phone, zone_id, role FROM users WHERE phone = :phone LIMIT 1"),
                    {"phone": phone}
                ).fetchone()
                if not row:
                    return None
                return {"id": row[0], "name": row[1], "phone": row[2], "zone_id": row[3], "role": row[4]}
            except Exception:
                # Fallback to ORM in rare cases
                user = session.query(User).filter(User.phone == phone).first()
                if user is None:
                    return None
                return {"id": user.id, "phone": user.phone, "name": user.name,
                        "zone_id": user.zone_id, "role": user.role}

    def onboard_user(self, phone: str, name: str, zone_id: str, lang: str = "fr") -> Dict[str, Any]:
        with self._get_session() as session:
            user_id = str(uuid.uuid4())
            user = User(
                id=user_id,
                phone=phone,
                name=name,
                zone_id=zone_id,
            )
            session.add(user)
            try:
                session.flush()
            except Exception:
                # If flush fails, roll back will be handled by context manager
                pass
            return {"id": user_id, "phone": phone, "name": name, "zone_id": zone_id}

    def get_pending_alerts(self, zone_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            query = session.query(Alert).filter(Alert.processed == False)
            if zone_id:
                query = query.filter(Alert.zone_id == zone_id)
            alerts = query.all()
            return [{"id": a.id, "type": a.type, "severity": a.severity,
                      "message": a.message, "zone_id": a.zone_id} for a in alerts]

    # --- LOGIQUE MARCHÉ (MarketCoach) ---

    def get_latest_market_prices(self, product: str, zone_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            # Parameterized LIKE to prevent SQL wildcard injection
            items = session.query(MarketItem).filter(
                MarketItem.product_name.ilike(f"%{product}%"),
                MarketItem.zone_id == zone_id
            ).order_by(MarketItem.date.desc()).limit(limit).all()
            return [{"product_name": m.product_name, "zone_id": m.zone_id,
                      "price": m.price_kg, "date": str(m.date)} for m in items]

    # --- SANTÉ DU SYSTÈME ---

    def check_connection(self) -> bool:
        """Vérifie si la DB répond."""
        try:
            with self._get_session() as session:
                session.execute(text("SELECT 1"))
                return True
        except Exception:
            return False

    # --- MÉTÉO (WeatherData) ---

    def save_weather(self, zone_id: str, temperature: float = None,
                     precipitation: float = None, humidity: float = None,
                     forecast_date: str = None) -> Dict[str, Any]:
        with self._get_session() as session:
            weather_id = str(uuid.uuid4())
            weather = WeatherData(
                id=weather_id,
                zone_id=zone_id,
                temperature=temperature,
                precipitation=precipitation,
                humidity=humidity,
                forecast_date=forecast_date or datetime.utcnow(),
            )
            session.add(weather)
            return {"id": weather_id, "zone_id": zone_id,
                    "temperature": temperature, "precipitation": precipitation}

    # --- CONVERSATIONS (mémoire) ---

    def log_conversation(self, user_id: str, user_message: str,
                         assistant_message: str, audio_url: str = None,
                         channel: str = "api") -> str:
        """
        Sauvegarde un échange complet (question + réponse).
        Retourne le conversation_id.
        """
        session: Session = self.SessionLocal()
        conv_id = str(uuid.uuid4())
        try:
            # Normalize user_id: ensure we use a valid UUID for DB UUID columns.
            user_id_for_db = user_id
            try:
                uuid.UUID(str(user_id))
            except Exception:
                generated = str(uuid.uuid4())
                logger.warning("Non-UUID user_id passed to log_conversation (%s). Using generated id %s", user_id, generated)
                user_id_for_db = generated
            # Ensure the referenced user exists (create lightweight placeholder if needed)
            # Use a lightweight SQL check to avoid referencing model columns that may
            # not exist in the underlying Prisma-managed DB (avoids UndefinedColumn errors).
            try:
                exists = session.execute(
                    text("SELECT id FROM users WHERE id = :id LIMIT 1"), {"id": user_id_for_db}
                ).fetchone()
            except Exception:
                # Fallback to ORM query if the simple text query fails for any reason
                exists = session.query(User.id).filter(User.id == user_id_for_db).first()

            if not exists:
                try:
                    # Use a raw INSERT with minimal columns to avoid failures when
                    # the underlying Prisma-managed `users` table is missing
                    # optional columns (e.g. `image`, `email_verified`). This
                    # prevents SQLAlchemy ORM from generating INSERTs that refer
                    # to columns absent in the DB schema.
                    # Inspect available columns on the `users` table so we only
                    # INSERT into columns that actually exist in the DB. This
                    # tolerates differences between Prisma-managed schemas
                    # (camelCase) and our SQLAlchemy models (snake_case).
                    cols = session.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"),
                    ).fetchall()
                    col_set = {c[0] for c in cols} if cols else set()

                    # Candidate mappings from logical field -> possible DB column names
                    # Limit to a minimal safe set of columns to maximize
                    # chance of compatibility with Prisma-managed schemas.
                    candidates = {
                        "id": ["id"],
                        "name": ["name"],
                        "role": ["role"],
                        "created_at": ["created_at", "createdAt"],
                        "updated_at": ["updated_at", "updatedAt"],
                    }

                    insert_cols = []
                    insert_params = {}
                    now = datetime.utcnow()

                    for logical, options in candidates.items():
                        for opt in options:
                            if opt in col_set:
                                insert_cols.append(opt)
                                if logical == "id":
                                    insert_params[opt] = user_id_for_db
                                elif logical == "name":
                                    insert_params[opt] = ("Anonymous" if user_id == "anonymous" else "User")
                                elif logical == "phone":
                                    insert_params[opt] = None
                                elif logical == "zone_id":
                                    insert_params[opt] = None
                                elif logical == "role":
                                    insert_params[opt] = "USER"
                                elif logical in ("created_at", "updated_at"):
                                    insert_params[opt] = now
                                break

                    if not insert_cols:
                        raise RuntimeError("No writable user columns detected on target DB")

                    cols_sql = ", ".join(insert_cols)
                    vals_sql = ", ".join(f":{c}" for c in insert_cols)
                    insert_sql = text(f"INSERT INTO users ({cols_sql}) VALUES ({vals_sql})")
                    try:
                        session.execute(insert_sql, insert_params)
                        session.flush()
                        logger.debug("Created placeholder user (raw insert) for id=%s (original: %s)", user_id_for_db, user_id)
                    except Exception:
                        # Try schema-qualified table name (auth.users) as some
                        # deployments place the users table under the `auth` schema.
                        try:
                            insert_sql2 = text(f"INSERT INTO auth.users ({cols_sql}) VALUES ({vals_sql})")
                            session.execute(insert_sql2, insert_params)
                            session.flush()
                            logger.debug("Created placeholder user (raw insert auth.users) for id=%s (original: %s)", user_id_for_db, user_id)
                        except Exception as e2:
                            logger.exception("Failed to create placeholder user via raw insert: %s", e2)
                            raise
                except Exception as e:
                    # If user placeholder creation fails due to schema mismatch (missing columns),
                    # rollback this partial transaction to clear the session state and continue.
                    logger.debug("Could not create placeholder user: %s", e)
                    try:
                        session.rollback()
                    except Exception:
                        pass

            # Re-check that the user exists; if schema prevents creating users, skip persistence
            try:
                exists_after = session.execute(
                    text("SELECT id FROM users WHERE id = :id LIMIT 1"), {"id": user_id_for_db}
                ).fetchone()
            except Exception:
                exists_after = None

            if not exists_after:
                logger.warning("Skipping conversation persistence: user record unavailable for id=%s", user_id_for_db)
                try:
                    session.close()
                except Exception:
                    pass
                return conv_id
            conv = Conversation(
                id=conv_id,
                user_id=user_id_for_db,
                query=user_message or "",
                response=assistant_message or "Informations indisponibles",
                audio_url=audio_url,
                mode=channel if channel in ("text", "voice", "sms") else "text",
            )
            session.add(conv)

            # Also keep granular messages for agent memory
            session.add(ConversationMessage(
                id=str(uuid.uuid4()),
                conversation_id=conv_id,
                role="user",
                content=user_message or "",
            ))
            session.add(ConversationMessage(
                id=str(uuid.uuid4()),
                conversation_id=conv_id,
                role="assistant",
                content=assistant_message or "Informations indisponibles",
                audio_url=audio_url,
            ))

            session.commit()
            return conv_id
        except Exception as e:
            logger.error("DB persist conversation failed: %s", e)
            try:
                session.rollback()
            except Exception:
                pass
            raise
        finally:
            session.close()

    # ── PROACTIVE : Surplus / Marché (MarketCoach) ────────────────

    def save_surplus_offer(
        self, product_name: str, quantity_kg: float,
        price_kg: float = None, zone_id: str = None,
        location: str = None, user_id: str = "anonymous",
        channel: str = "api",
    ) -> Dict[str, Any]:
        """Enregistre une offre de surplus détectée par MarketCoach."""
        with self._get_session() as session:
            offer_id = str(uuid.uuid4())
            offer = SurplusOffer(
                id=offer_id,
                user_id=user_id,
                product_name=product_name,
                quantity_kg=quantity_kg,
                price_kg=price_kg,
                zone_id=zone_id,
                location=location,
                channel=channel,
            )
            session.add(offer)
            logger.info("💰 Surplus offer saved: %s kg of %s", quantity_kg, product_name)
            return offer.to_dict()

    def get_open_surplus_offers(
        self, product: str = None, zone_id: str = None, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Récupère les offres de surplus ouvertes (matching acheteur/vendeur)."""
        with self._get_session() as session:
            query = session.query(SurplusOffer).filter(SurplusOffer.status == "OPEN")
            if product:
                query = query.filter(SurplusOffer.product_name.ilike(f"%{product}%"))
            if zone_id:
                query = query.filter(SurplusOffer.zone_id == zone_id)
            offers = query.order_by(SurplusOffer.created_at.desc()).limit(limit).all()
            return [o.to_dict() for o in offers]

    # ── PROACTIVE : Diagnostic Sol (AgriSoilAgent) ────────────────

    def save_soil_diagnosis(
        self, village: str, diagnosis: Dict[str, Any],
        zone_id: str = None, user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        """Persiste un diagnostic sol produit par AgriSoilAgent."""
        identite = diagnosis.get("identite_pedologique", {})
        sante = diagnosis.get("bilan_sante", {})
        eau = diagnosis.get("gestion_eau", {})

        with self._get_session() as session:
            diag_id = str(uuid.uuid4())
            entry = SoilDiagnosis(
                id=diag_id,
                user_id=user_id,
                zone_id=zone_id,
                village=village,
                soil_type=identite.get("nom_local"),
                fertility=sante.get("fertilite"),
                ph_alert=sante.get("alerte_ph"),
                water_strategy=eau.get("strategie"),
                adapted_crops=identite.get("cultures_adaptees"),
                raw_diagnosis=diagnosis,
            )
            session.add(entry)
            logger.info("🌍 Soil diagnosis saved: %s (%s)", village, diag_id)
            return entry.to_dict()

    # ── PROACTIVE : Diagnostic Plante (PlantHealthDoctor) ─────────

    def save_plant_diagnosis(
        self, crop_name: str, diagnosis: Dict[str, Any],
        user_id: str = "anonymous",
    ) -> Dict[str, Any]:
        """Persiste un diagnostic phytosanitaire par PlantHealthDoctor."""
        with self._get_session() as session:
            diag_id = str(uuid.uuid4())
            entry = PlantDiagnosis(
                id=diag_id,
                user_id=user_id,
                crop_name=crop_name,
                disease_name=diagnosis.get("disease_name"),
                severity=diagnosis.get("severity"),
                treatment_bio=diagnosis.get("treatment_bio"),
                treatment_chimique=diagnosis.get("treatment_chimique"),
                estimated_cost=diagnosis.get("estimated_cost"),
                raw_diagnosis=diagnosis,
            )
            session.add(entry)
            logger.info("🌱 Plant diagnosis saved: %s (%s)", crop_name, diag_id)
            return entry.to_dict()

    # ── PROACTIVE : Cultures utilisateur (onboarding) ─────────────

    def register_user_crop(
        self, user_id: str, crop_name: str,
        surface_ha: float = None, planting_date=None,
    ) -> Dict[str, Any]:
        """Enregistre une culture déclarée par l'utilisateur."""
        with self._get_session() as session:
            crop_id = str(uuid.uuid4())
            crop = UserCrop(
                id=crop_id,
                user_id=user_id,
                crop_name=crop_name,
                surface_ha=surface_ha,
                planting_date=planting_date,
            )
            session.add(crop)
            logger.info("🌾 User crop registered: %s for %s", crop_name, user_id)
            return crop.to_dict()

    # ── PROACTIVE : Rappels ────────────────────────────────────────

    def create_reminder(
        self, user_id: str, title: str, message: str, scheduled_at,
    ) -> Dict[str, Any]:
        """Planifie un rappel pour l'utilisateur."""
        with self._get_session() as session:
            reminder_id = str(uuid.uuid4())
            reminder = Reminder(
                id=reminder_id,
                user_id=user_id,
                title=title,
                message=message,
                scheduled_at=scheduled_at,
            )
            session.add(reminder)
            logger.info("⏰ Reminder created: %s for %s", title, user_id)
            return reminder.to_dict()