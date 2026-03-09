"""
Tests unitaires — VoiceEngine & VoiceAgent.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path


class TestVoiceEngine:
    """Teste le service VoiceEngine (Azure TTS/STT)."""

    def test_init_creates_storage_dir(self, tmp_path):
        from agriconnect.services.voice_engine import VoiceEngine

        storage = tmp_path / "audio_test"
        engine = VoiceEngine(api_key="fake-key", region="westeurope", storage_dir=str(storage))
        assert storage.exists()
        assert engine.api_key == "fake-key"
        assert engine.region == "westeurope"

    def test_init_with_fallback_key(self, tmp_path):
        from agriconnect.services.voice_engine import VoiceEngine

        engine = VoiceEngine(
            api_key="primary",
            fallback_key="secondary",
            storage_dir=str(tmp_path),
        )
        assert engine.fallback_key == "secondary"

    @patch("agriconnect.services.voice_engine.speechsdk")
    def test_generate_audio_returns_path(self, mock_sdk, tmp_path):
        from agriconnect.services.voice_engine import VoiceEngine

        # Mock the speech SDK
        mock_result = MagicMock()
        mock_result.reason = mock_sdk.ResultReason.SynthesizingAudioCompleted
        mock_synth = MagicMock()
        mock_synth.speak_text_async.return_value.get.return_value = mock_result
        mock_sdk.SpeechSynthesizer.return_value = mock_synth

        engine = VoiceEngine(api_key="fake", storage_dir=str(tmp_path))
        path = engine.generate_audio("Bonjour", voice="fr-FR-HenriNeural")
        assert path.endswith(".wav")

    def test_transcribe_audio_file_not_found(self, tmp_path):
        from agriconnect.services.voice_engine import VoiceEngine

        engine = VoiceEngine(api_key="fake", storage_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            engine.transcribe_audio("/nonexistent/file.wav")


class TestVoiceAgent:
    """Teste le VoiceAgent (pipeline complet)."""

    def test_init_without_azure_key(self):
        """Sans clé Azure, le VoiceAgent doit quand même s'initialiser."""
        with patch.dict("os.environ", {"USE_AZURE_SPEECH": "false"}, clear=False):
            with patch("agriconnect.graphs.nodes.voice.settings") as mock_settings:
                mock_settings.AZURE_SPEECH_KEY = None
                mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"
                from agriconnect.graphs.nodes.voice import VoiceAgent
                agent = VoiceAgent()
                assert agent.voice_engine is None
                assert agent.use_azure is False

    def test_lang_map(self):
        with patch("agriconnect.graphs.nodes.voice.settings") as mock_settings:
            mock_settings.AZURE_SPEECH_KEY = None
            mock_settings.AUDIO_OUTPUT_DIR = "./test_audio"
            from agriconnect.graphs.nodes.voice import VoiceAgent
            agent = VoiceAgent()
            assert agent._LANG_MAP["fr"] == "fr"
            assert agent._LANG_MAP["moore"] == "fr"
            assert agent._LANG_MAP["dioula"] == "fr"
            assert agent._LANG_MAP["en"] == "en"

    def test_local_audio_url(self, tmp_path):
        from agriconnect.graphs.nodes.voice import VoiceAgent

        # Create a temp file
        src = tmp_path / "test.wav"
        src.write_bytes(b"fake audio")

        with patch("agriconnect.graphs.nodes.voice.settings") as mock_settings:
            mock_settings.AUDIO_OUTPUT_DIR = str(tmp_path / "output")
            url = VoiceAgent._local_audio_url(str(src))
            assert url.startswith("file://")
            assert "test.wav" in url
