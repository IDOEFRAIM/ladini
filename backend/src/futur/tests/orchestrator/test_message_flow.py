"""
Tests unitaires — MessageResponseFlow (orchestrateur).
"""

import pytest
from unittest.mock import MagicMock, patch


class TestMessageResponseFlowInit:
    """Teste l'initialisation de l'orchestrateur."""

    @patch("agriconnect.graphs.message_flow._core_db")
    @patch("agriconnect.graphs.message_flow.settings")
    @patch("agriconnect.graphs.message_flow.init_tracing", return_value=False)
    @patch("agriconnect.graphs.message_flow.get_groq_sdk")
    def test_init_without_db(self, mock_groq, mock_tracing, mock_settings, mock_core_db):
        """L'orchestrateur doit fonctionner même sans DB."""
        mock_core_db._engine = None
        mock_core_db._SessionLocal = None
        mock_settings.DATABASE_URL = None
        mock_settings.AZURE_SPEECH_KEY = None
        mock_settings.AZURE_SPEECH_KEY_2 = None
        mock_settings.AZURE_REGION = "westeurope"
        mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"
        mock_groq.return_value = MagicMock()

        from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
        flow = MessageResponseFlow(llm_client=MagicMock())
        assert flow.db is None
        assert flow.memory is None
        assert flow.graph is not None


class TestAnalyzeNeeds:
    """Teste le nœud ANALYZE."""

    @pytest.fixture
    def mock_flow(self):
           with patch("agriconnect.graphs.message_flow._core_db") as mock_core_db, \
               patch("agriconnect.graphs.message_flow.settings") as mock_settings, \
               patch("agriconnect.graphs.message_flow.init_tracing", return_value=False), \
               patch("agriconnect.graphs.message_flow.get_groq_sdk"):
            mock_core_db._engine = None
            mock_core_db._SessionLocal = None
            mock_settings.DATABASE_URL = None
            mock_settings.AZURE_SPEECH_KEY = None
            mock_settings.AZURE_SPEECH_KEY_2 = None
            mock_settings.AZURE_REGION = "westeurope"
            mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"

            mock_llm = MagicMock()
            from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
            flow = MessageResponseFlow(llm_client=mock_llm)
            yield flow

    def test_analyze_returns_needs(self, mock_flow):
        """analyze_needs doit retourner un dict avec 'needs'."""
        # Mock the LLM response
        mock_flow.llm.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"intent": "FORMATION", "needs_formation": true, "needs_sentinelle": false, "needs_market": false}'))]
        )

        state = {
            "requete_utilisateur": "Comment faire un compost ?",
            "zone_id": "Bobo",
            "crop": "Maïs",
        }
        result = mock_flow.analyze_needs(state)
        assert "needs" in result


class TestRouteFlow:
    """Teste le routage conditionnel."""

    @pytest.fixture
    def mock_flow(self):
           with patch("agriconnect.graphs.message_flow._core_db") as mock_core_db, \
               patch("agriconnect.graphs.message_flow.settings") as mock_settings, \
               patch("agriconnect.graphs.message_flow.init_tracing", return_value=False), \
               patch("agriconnect.graphs.message_flow.get_groq_sdk"):
            mock_core_db._engine = None
            mock_core_db._SessionLocal = None
            mock_settings.DATABASE_URL = None
            mock_settings.AZURE_SPEECH_KEY = None
            mock_settings.AZURE_SPEECH_KEY_2 = None
            mock_settings.AZURE_REGION = "westeurope"
            mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"

            from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
            flow = MessageResponseFlow(llm_client=MagicMock())
            yield flow

    def test_route_chat(self, mock_flow):
        state = {"needs": {"intent": "CHAT"}}
        assert mock_flow.route_flow(state) == "EXECUTE_CHAT"

    def test_route_reject(self, mock_flow):
        state = {"needs": {"intent": "REJECT"}}
        assert mock_flow.route_flow(state) == "REJECT"

    def test_route_solo_formation(self, mock_flow):
        state = {"needs": {"intent": "FORMATION", "needs_formation": True, "needs_sentinelle": False, "needs_market": False}}
        route = mock_flow.route_flow(state)
        assert route in ("SOLO_FORMATION", "PARALLEL_EXPERTS")

    def test_route_parallel(self, mock_flow):
        state = {"needs": {"intent": "CONSEIL", "needs_formation": True, "needs_sentinelle": True, "needs_market": False}}
        route = mock_flow.route_flow(state)
        assert route == "PARALLEL_EXPERTS"


class TestSynthesizeResults:
    """Teste le fan-in (synthèse)."""

    @pytest.fixture
    def mock_flow(self):
           with patch("agriconnect.graphs.message_flow._core_db") as mock_core_db, \
               patch("agriconnect.graphs.message_flow.settings") as mock_settings, \
               patch("agriconnect.graphs.message_flow.init_tracing", return_value=False), \
               patch("agriconnect.graphs.message_flow.get_groq_sdk"):
            mock_core_db._engine = None
            mock_core_db._SessionLocal = None
            mock_settings.DATABASE_URL = None
            mock_settings.AZURE_SPEECH_KEY = None
            mock_settings.AZURE_SPEECH_KEY_2 = None
            mock_settings.AZURE_REGION = "westeurope"
            mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"

            from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
            flow = MessageResponseFlow(llm_client=MagicMock())
            yield flow

    def test_synthesize_empty(self, mock_flow):
        state = {"expert_responses": []}
        result = mock_flow.synthesize_results(state)
        assert "Aucun expert" in result["final_response"]

    def test_synthesize_single(self, mock_flow):
        state = {
            "expert_responses": [
                {"expert": "formation", "response": "Faites un compost.", "is_lead": True, "has_alerts": False},
            ]
        }
        result = mock_flow.synthesize_results(state)
        assert "compost" in result["final_response"]

    def test_synthesize_with_alerts(self, mock_flow):
        state = {
            "expert_responses": [
                {"expert": "formation", "response": "Semez maintenant.", "is_lead": True, "has_alerts": False},
                {"expert": "sentinelle", "response": "Inondation prévue demain !", "is_lead": False, "has_alerts": True},
            ]
        }
        result = mock_flow.synthesize_results(state)
        assert "Semez" in result["final_response"]
        assert "SENTINELLE" in result["final_response"]


class TestCleanForTTS:
    """Teste le nettoyage Markdown pour TTS."""

    @pytest.fixture
    def mock_flow(self):
           with patch("agriconnect.graphs.message_flow._core_db") as mock_core_db, \
               patch("agriconnect.graphs.message_flow.settings") as mock_settings, \
               patch("agriconnect.graphs.message_flow.init_tracing", return_value=False), \
               patch("agriconnect.graphs.message_flow.get_groq_sdk"):
            mock_core_db._engine = None
            mock_core_db._SessionLocal = None
            mock_settings.DATABASE_URL = None
            mock_settings.AZURE_SPEECH_KEY = None
            mock_settings.AZURE_SPEECH_KEY_2 = None
            mock_settings.AZURE_REGION = "westeurope"
            mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"

            from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
            flow = MessageResponseFlow(llm_client=MagicMock())
            yield flow

    def test_removes_markdown(self, mock_flow):
        text = "**Bonjour** _monde_ `code` # Titre"
        cleaned = mock_flow.clean_for_tts(text)
        assert "*" not in cleaned
        assert "_" not in cleaned
        assert "`" not in cleaned
        assert "#" not in cleaned

    def test_removes_html(self, mock_flow):
        text = "<b>Important</b> et <a href='url'>lien</a>"
        cleaned = mock_flow.clean_for_tts(text)
        assert "<" not in cleaned
        assert ">" not in cleaned

    def test_collapses_newlines(self, mock_flow):
        text = "Ligne 1\n\n\n\n\nLigne 2"
        cleaned = mock_flow.clean_for_tts(text)
        assert "\n\n\n" not in cleaned
