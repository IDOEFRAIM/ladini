"""
Tests unitaires — Modèles SQLAlchemy & DB Handler.
"""

import uuid
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime


class TestModels:
    """Vérifie que les modèles ORM sont correctement définis."""

    def test_user_model_tablename(self):
        from agriconnect.services.models import User
        assert User.__tablename__ == "users"

    def test_user_column_mappings(self):
        """Vérifie le mapping camelCase pour les colonnes Prisma."""
        from agriconnect.services.models import User
        # zone_id should map to "zoneId" in the DB
        col = User.__table__.columns["zoneId"]
        assert col is not None

    def test_conversation_column_mappings(self):
        from agriconnect.services.models import Conversation
        col_names = [c.name for c in Conversation.__table__.columns]
        assert "userId" in col_names
        assert "agentType" in col_names
        assert "audioUrl" in col_names
        assert "createdAt" in col_names

    def test_zone_model(self):
        from agriconnect.services.models import Zone
        assert Zone.__tablename__ == "zones"
        col_names = [c.name for c in Zone.__table__.columns]
        assert "climaticRegionId" in col_names

    def test_alert_model(self):
        from agriconnect.services.models import Alert
        assert Alert.__tablename__ == "alerts"

    def test_surplus_offer_model(self):
        from agriconnect.services.models import SurplusOffer
        assert SurplusOffer.__tablename__ == "surplus_offers"
        offer = SurplusOffer(
            id="test-123",
            product_name="Maïs",
            quantity_kg=500.0,
            status="OPEN",
        )
        d = offer.to_dict()
        assert d["product_name"] == "Maïs"
        assert d["quantity_kg"] == 500.0

    def test_agent_action_model(self):
        from agriconnect.services.models import AgentAction
        assert AgentAction.__tablename__ == "agent_actions"
        col_names = [c.name for c in AgentAction.__table__.columns]
        assert "agentName" in col_names
        assert "actionType" in col_names
        assert "orderId" in col_names

    def test_user_properties(self):
        """Vérifie les propriétés Python-only du User."""
        from agriconnect.services.models import User
        user = User(id="u1", name="Test", phone="+226700")
        assert user.language == "fr"
        assert user.is_onboarded is True
        assert user.voice_preference == "fr-FR-HenriNeural"

    def test_order_model_columns(self):
        from agriconnect.services.models import Order
        assert Order.__tablename__ == "orders"
        col_names = [c.name for c in Order.__table__.columns]
        assert "buyerId" in col_names
        assert "isAgentOrder" in col_names
        assert "totalAmount" in col_names

    def test_product_model_columns(self):
        from agriconnect.services.models import Product
        assert Product.__tablename__ == "products"
        col_names = [c.name for c in Product.__table__.columns]
        assert "shortCode" in col_names
        assert "localNames" in col_names
        assert "producerId" in col_names


class TestAgriDatabaseInit:
    """Teste l'initialisation de AgriDatabase."""

    def test_init_requires_url_or_engine(self):
        from agriconnect.services.db_handler import AgriDatabase
        with pytest.raises(ValueError, match="fournir db_url"):
            AgriDatabase()

    @patch("agriconnect.services.db_handler.create_engine")
    @patch("agriconnect.services.db_handler.Base")
    def test_init_with_url(self, mock_base, mock_engine):
        from agriconnect.services.db_handler import AgriDatabase
        mock_engine.return_value = MagicMock()
        mock_base.metadata.create_all = MagicMock()

        db = AgriDatabase(db_url="postgresql://test:test@localhost/test")
        assert db.engine is not None
        assert db.SessionLocal is not None
        db.close()

    def test_init_with_engine_and_factory(self):
        from agriconnect.services.db_handler import AgriDatabase

        mock_engine = MagicMock()
        mock_factory = MagicMock()

        with patch("agriconnect.services.db_handler.Base") as mock_base:
            mock_base.metadata.create_all = MagicMock()
            db = AgriDatabase(engine=mock_engine, session_factory=mock_factory)
            assert db.engine is mock_engine
            assert db.SessionLocal is mock_factory
            db.close()


class TestAgriDatabaseOperations:
    """Teste les opérations CRUD du db_handler (mockées)."""

    @pytest.fixture
    def mock_db(self):
        from agriconnect.services.db_handler import AgriDatabase
        mock_engine = MagicMock()
        mock_factory = MagicMock()
        with patch("agriconnect.services.db_handler.Base") as mock_base:
            mock_base.metadata.create_all = MagicMock()
            db = AgriDatabase(engine=mock_engine, session_factory=mock_factory)
        yield db
        db.close()

    def test_check_connection(self, mock_db):
        mock_session = MagicMock()
        mock_db.SessionLocal.return_value = mock_session
        mock_session.execute.return_value = True
        assert mock_db.check_connection() is True

    def test_create_alert(self, mock_db):
        mock_session = MagicMock()
        mock_db.SessionLocal.return_value = mock_session

        result = mock_db.create_alert(
            alert_type="WEATHER",
            severity="HAUT",
            message="Inondation prévue",
            zone_id="zone-bobo",
        )
        assert result["type"] == "WEATHER"
        assert result["severity"] == "HAUT"
        assert result["zone_id"] == "zone-bobo"
        assert "id" in result

    def test_log_conversation(self, mock_db):
        mock_session = MagicMock()
        mock_db.SessionLocal.return_value = mock_session

        conv_id = mock_db.log_conversation(
            user_id="user-1",
            user_message="Bonjour",
            assistant_message="Bonjour ! Comment puis-je vous aider ?",
        )
        assert conv_id is not None
        assert len(conv_id) == 36  # UUID format

    def test_save_surplus_offer(self, mock_db):
        mock_session = MagicMock()
        mock_db.SessionLocal.return_value = mock_session

        result = mock_db.save_surplus_offer(
            product_name="Mil",
            quantity_kg=200.0,
            zone_id="zone-kaya",
            user_id="farmer-1",
        )
        assert result["product_name"] == "Mil"
        assert result["quantity_kg"] == 200.0


class TestToolsDBHandler:
    """Teste le wrapper tools/db_handler.py."""

    def test_get_db_returns_none_without_config(self):
        with patch("agriconnect.tools.db_handler._core_db") as mock_core:
            mock_core._engine = None
            mock_core._SessionLocal = None
            with patch("agriconnect.tools.db_handler.settings") as mock_settings:
                mock_settings.DATABASE_URL = None
                # Reset singleton
                import agriconnect.tools.db_handler as mod
                mod._db_instance = None
                result = mod.get_db()
                assert result is None
