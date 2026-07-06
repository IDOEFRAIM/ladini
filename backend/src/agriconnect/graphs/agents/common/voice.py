"""
VoiceAgent — Interface vocale pour agriculteurs (WhatsApp + Web).

Pipeline :  audio_in → STT → orchestrateur → TTS → audio_out

Moteurs STT :
  - Whisper (local, gratuit, bon pour le français)
  - Azure Speech (premium, meilleur pour dialectes ouest-africains)

Moteur TTS :
  - Azure Speech via VoiceEngine (services/voice_engine.py)
  - gTTS fallback (gratuit, qualité moindre)
"""

import asyncio
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from agriconnect.services.voice_engine import VoiceEngine
from agriconnect.core.settings import settings

logger = logging.getLogger("VoiceAgent")


class VoiceAgent:
    """
    Agent vocal : audio → texte → traitement → audio.

    Responsabilités :
      1. STT (Whisper ou Azure)
      2. Routage vers MessageResponseFlow
      3. TTS (Azure ou gTTS)
      4. Persistance conversation
    """

    def __init__(self):
        # ── STT : Whisper (lazy load, CPU-intensif) ──
        self._whisper_model = None

        # ── Azure Speech (optionnel) ──
        self.use_azure = os.getenv("USE_AZURE_SPEECH", "false").lower() == "true"
        self.azure_key = os.getenv("AZURE_SPEECH_KEY") or getattr(settings, "AZURE_SPEECH_KEY", None)
        self.azure_region = os.getenv("AZURE_REGION", "westeurope")

        # ── TTS via VoiceEngine (Azure) ──
        storage_dir = getattr(settings, "AUDIO_OUTPUT_DIR", None) or "./audio_output"
        self.voice_engine: VoiceEngine | None = None
        if self.azure_key:
            try:
                self.voice_engine = VoiceEngine(
                    api_key=self.azure_key,
                    region=self.azure_region,
                    storage_dir=storage_dir,
                )
            except Exception as exc:
                logger.warning("VoiceEngine init failed (TTS disabled): %s", exc)

        # ── Langue → code gTTS ──
        self._LANG_MAP = {"fr": "fr", "moore": "fr", "dioula": "fr", "en": "en"}

    # ──────────────────────────────────────────────────────────────
    # PROPRIÉTÉ LAZY — Whisper
    # ──────────────────────────────────────────────────────────────

    @property
    def whisper_model(self):
        if self._whisper_model is None:
            try:
                import whisper
                self._whisper_model = whisper.load_model("base")
                logger.info("🎙️ Whisper model loaded (base)")
            except Exception as exc:
                logger.warning("Whisper not available: %s", exc)
        return self._whisper_model

    # ──────────────────────────────────────────────────────────────
    # PIPELINE PRINCIPAL
    # ──────────────────────────────────────────────────────────────

    async def process_voice_message(
        self,
        audio_file_path: str,
        user_phone: str,
        db_client,
    ) -> Dict[str, Any]:
        """
        Pipeline complet : audio → texte → orchestrateur → audio.

        Args:
            audio_file_path: chemin local vers le fichier audio téléchargé
            user_phone: numéro WhatsApp (sans préfixe whatsapp:)
            db_client: client Prisma pour accès DB
        """
        logger.info("🎤 Traitement message vocal de %s", user_phone)

        try:
            # 1. Speech-to-Text
            transcript, detected_lang = await self._stt(audio_file_path)
            logger.info("📝 Transcription (%s): %s…", detected_lang, transcript[:80])

            # 2. Récupérer profil utilisateur
            user = await db_client.user.find_unique(
                where={"phone": user_phone},
                include={"zone": True, "crops": True},
            )
            if not user:
                return await self._handle_new_user(user_phone, transcript, detected_lang)

            # 3. Routage vers l'Orchestrator unique
            from agriconnect.orchestrator import Orchestrator

            orchestrator = Orchestrator()
            result = await orchestrator.handle(phone=user_phone, user_query=transcript)

            response_text = result.get("final_response", "Je n'ai pas compris votre question.")
            agent_used = result.get("agent", "Unknown")

            # 4. TTS
            response_audio_url = await self._tts(response_text, getattr(user, "language", "fr"))

            # 5. Persistance
            await self._save_conversation(user, transcript, response_text, db_client)

            logger.info("✅ Traitement vocal terminé (Agent: %s)", agent_used)
            return {
                "transcript": transcript,
                "language": detected_lang,
                "response_text": response_text,
                "response_audio_url": response_audio_url,
                "agent_used": agent_used,
            }

        except Exception as exc:
            logger.error("❌ Erreur traitement vocal: %s", exc, exc_info=True)
            return {
                "error": str(exc),
                "response_text": "Désolé, je n'ai pas pu traiter votre message vocal.",
            }

    # ──────────────────────────────────────────────────────────────
    # STT
    # ──────────────────────────────────────────────────────────────

    async def _stt(self, audio_path: str) -> Tuple[str, str]:
        """Retourne (transcript, detected_language)."""
        if self.use_azure and self.azure_key:
            return await self._azure_stt(audio_path)
        return await self._whisper_stt(audio_path)

    async def _whisper_stt(self, audio_path: str) -> Tuple[str, str]:
        model = self.whisper_model
        if model is None:
            raise RuntimeError("Whisper model not available")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, model.transcribe, audio_path)
        return result.get("text", "").strip(), result.get("language", "fr")

    async def _azure_stt(self, audio_path: str) -> Tuple[str, str]:
        import azure.cognitiveservices.speech as speechsdk

        speech_config = speechsdk.SpeechConfig(subscription=self.azure_key, region=self.azure_region)
        auto_detect = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
            languages=["fr-FR", "fr-BF", "en-US"],
        )
        audio_config = speechsdk.audio.AudioConfig(filename=audio_path)
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            auto_detect_source_language_config=auto_detect,
            audio_config=audio_config,
        )
        result = recognizer.recognize_once()
        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            lang = result.properties.get(
                speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult,
                "fr",
            )
            return result.text, lang
        raise RuntimeError(f"Azure STT failed: {result.reason}")

    # ──────────────────────────────────────────────────────────────
    # TTS
    # ──────────────────────────────────────────────────────────────

    async def _tts(self, text: str, language: str = "fr") -> str:
        """Retourne l'URL (ou chemin) du fichier audio généré."""
        if self.voice_engine:
            path = self.voice_engine.generate_audio(text, voice="fr-FR-DeniseNeural")
            return self._local_audio_url(path)
        return await self._gtts_fallback(text, language)

    async def _gtts_fallback(self, text: str, language: str) -> str:
        from gtts import gTTS

        tts_lang = self._LANG_MAP.get(language, "fr")
        tts = gTTS(text=text, lang=tts_lang, slow=False)
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        tts.save(tmp_path)
        url = self._local_audio_url(tmp_path)
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
        return url

    @staticmethod
    def _local_audio_url(filepath: str) -> str:
        """Déplace le fichier vers AUDIO_OUTPUT_DIR et retourne une URL locale."""
        out_dir = getattr(settings, "AUDIO_OUTPUT_DIR", None) or "./audio_output"
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(out_dir) / Path(filepath).name
        try:
            Path(filepath).replace(dest)
        except OSError:
            import shutil
            shutil.copy2(filepath, dest)
        return f"file://{dest.resolve()}"

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    async def _handle_new_user(self, phone: str, first_message: str, language: str) -> Dict[str, Any]:
        welcome = (
            "Bienvenue sur AgriConnect ! "
            "Je suis votre assistant agricole vocal. "
            "Pour commencer, dites-moi dans quelle région vous cultivez."
        )
        audio_url = await self._tts(welcome, language)
        return {
            "transcript": first_message,
            "language": language,
            "response_text": welcome,
            "response_audio_url": audio_url,
            "agent_used": "ONBOARDING",
            "action_required": "ZONE_SELECTION",
        }

    @staticmethod
    async def _save_conversation(user, user_message: str, assistant_message: str, db_client):
        session_id = f"{user.id}_{datetime.now().strftime('%Y%m%d')}"
        conversation = await db_client.conversation.upsert(
            where={"userId_session_id": {"userId": user.id, "session_id": session_id}},
            create={"userId": user.id, "platform": "WHATSAPP", "session_id": session_id},
            update={},
        )
        await db_client.conversation_message.create_many(
            data=[
                {"conversationId": conversation.id, "role": "USER", "content": user_message, "content_type": "voice"},
                {"conversationId": conversation.id, "role": "ASSISTANT", "content": assistant_message, "content_type": "voice"},
            ]
        )
