"""
Voice Tasks — TTS / STT via Azure Speech (production-grade).

PRINCIPES :
  - Hérite de AgriTask (logging structuré, backoff, classification)
  - azure.cognitiveservices.speech est importé LAZY (dans la tâche)
  - Vérification d'espace disque avant écriture
  - Nettoyage du fichier audio en cas d'échec (pas de fichiers corrompus)
  - Idempotency : un audio_id unique par appel
  - Time limits stricts (Azure Speech peut hang)
  - Fallback sur la clé secondaire Azure
"""

import logging
import shutil
import uuid
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from agriconnect.workers.celery_app import celery_app
from agriconnect.workers.celery_config import TIME_LIMITS
from agriconnect.workers.task_base import (
    AgriTask,
    ExternalServiceDown,
    FatalTaskError,
    RateLimitHit,
    error_result,
    success_result,
)
from agriconnect.core.settings import settings

logger = logging.getLogger("AgriConnect.tasks.voice")

AUDIO_DIR = Path(settings.AUDIO_OUTPUT_DIR).resolve()
_VOICE_LIMITS = TIME_LIMITS["voice"]

# Espace disque minimum requis pour écrire un fichier audio (50 MB)
MIN_DISK_SPACE_MB = 50
# Taille max du texte à synthétiser (Azure limit ≈ 10000 chars)
MAX_TTS_TEXT_LENGTH = 8000


def _check_disk_space(path: Path, min_mb: int = MIN_DISK_SPACE_MB) -> bool:
    """Vérifie qu'il y a assez d'espace disque."""
    try:
        usage = shutil.disk_usage(str(path.parent))
        free_mb = usage.free / (1024 * 1024)
        return free_mb >= min_mb
    except OSError:
        return True  # En cas de doute, on continue


def _get_azure_key() -> str:
    """Retourne la clé Azure disponible, ou lève FatalTaskError."""
    key = settings.AZURE_SPEECH_KEY or settings.AZURE_SPEECH_KEY_2
    if not key:
        raise FatalTaskError("Azure Speech non configuré (aucune clé disponible)")
    return key


def _cleanup_partial_file(path: Path):
    """Supprime un fichier audio partiel/corrompu."""
    try:
        if path.exists():
            path.unlink()
            logger.debug("Cleaned up partial audio file: %s", path)
    except OSError as e:
        logger.warning("Failed to cleanup partial file %s: %s", path, e)


def _validate_tts_text(text: str) -> None:
    """Validate TTS input text and raise FatalTaskError on invalid input."""
    if not text or not text.strip():
        raise FatalTaskError("Empty text — nothing to synthesize")
    if len(text) > MAX_TTS_TEXT_LENGTH:
        raise FatalTaskError(
            f"Text too long ({len(text)} chars, max {MAX_TTS_TEXT_LENGTH})"
        )


def _prepare_audio_path(audio_dir: Path) -> (str, Path):
    """Create audio id and path; ensure directory exists.

    Returns (audio_id, Path)
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_id = str(uuid.uuid4())
    return audio_id, audio_dir / f"{audio_id}.wav"


def _build_speech_config(key: str, voice_name: str = "fr-FR-DeniseNeural", timeout_ms: int = 30000):
    """Build and return an Azure SpeechConfig object."""
    import azure.cognitiveservices.speech as speechsdk

    speech_config = speechsdk.SpeechConfig(subscription=key, region=settings.AZURE_REGION)
    speech_config.speech_synthesis_voice_name = voice_name
    speech_config.set_property(
        speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs,
        str(timeout_ms),
    )
    return speechsdk, speech_config


def _synthesize_text(speechsdk, speech_config, audio_path: Path, text: str):
    """Synthesize text to the given audio_path and return the result object."""
    audio_config = speechsdk.audio.AudioOutputConfig(filename=str(audio_path))
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config,
        audio_config=audio_config,
    )
    return synthesizer.speak_text_async(text).get()


def _process_tts_result(result, audio_path: Path, audio_id: str, text_length: int, user_id: str):
    """Handle Azure TTS result: return success_result or raise appropriate errors."""
    try:
        import azure.cognitiveservices.speech as speechsdk
    except Exception:
        speechsdk = None

    if speechsdk and result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        # Ensure the audio file has been written and is non-empty
        file_size = _write_audio_file(audio_path)
        return _package_tts_success(audio_id, audio_path, file_size, text_length, user_id)
    # Analyse failure (delegated)
    error_details = _format_tts_error_details(result)
    _raise_tts_error_from_details(error_details, audio_path)


def _format_tts_error_details(result) -> str:
    """Extract a readable error string from a speech result object."""
    try:
        cancellation = getattr(result, "cancellation_details", None)
        if cancellation:
            return f"{getattr(result,'reason', '')}: {cancellation.reason} — {cancellation.error_details}"
    except Exception:
        pass
    return str(getattr(result, "reason", result))


def _write_audio_file(audio_path: Path) -> int:
    """Validate the produced audio file exists and return its size in bytes.

    Raises ExternalServiceDown if file missing or empty.
    """
    if not audio_path.exists():
        raise ExternalServiceDown(f"Audio file missing after synthesis: {audio_path}")
    size = audio_path.stat().st_size
    if size == 0:
        raise ExternalServiceDown(f"Audio file is empty after synthesis: {audio_path}")
    return size


def _package_tts_success(audio_id: str, audio_path: Path, file_size: int, text_length: int, user_id: str):
    """Return the standardized success_result payload for a completed TTS operation."""
    return success_result(
        data={
            "audio_id": audio_id,
            "audio_path": str(audio_path),
            "file_size_bytes": file_size,
            "text_length": text_length,
            "user_id": user_id,
        },
        task_name="agriconnect.workers.tasks.voice.generate_tts",
    )


def _raise_tts_error_from_details(error_details: str, audio_path: Path):
    """Raise the appropriate typed exception for the given error details."""
    low = error_details.lower()
    if "429" in error_details or "throttl" in low:
        _cleanup_partial_file(audio_path)
        raise RateLimitHit(f"Azure TTS rate limit: {error_details}")

    if "401" in error_details or "403" in error_details:
        _cleanup_partial_file(audio_path)
        raise FatalTaskError(f"Azure TTS auth error: {error_details}")

    _cleanup_partial_file(audio_path)
    raise ExternalServiceDown(f"TTS synthesis failed: {error_details}")


def _validate_audio_file(audio_file_path: str) -> Path:
    """Validate that the audio file exists and is non-empty, returning a Path."""
    audio_path = Path(audio_file_path)
    if not audio_path.exists():
        raise FatalTaskError(f"Audio file not found: {audio_file_path}")
    if audio_path.stat().st_size == 0:
        raise FatalTaskError(f"Audio file is empty: {audio_file_path}")
    return audio_path


@celery_app.task(
    base=AgriTask,
    name="backend.workers.tasks.voice.generate_tts",
    bind=True,
    max_retries=3,
    soft_time_limit=_VOICE_LIMITS["soft"],
    time_limit=_VOICE_LIMITS["hard"],
    acks_late=True,
    track_started=True,
)
def generate_tts(self, text: str, user_id: str = "unknown"):
    """
    Génère un fichier audio .wav via Azure TTS.

    Guards:
      - Texte vide ou trop long → FatalTaskError (pas de retry)
      - Espace disque insuffisant → FatalTaskError
      - Azure SDK timeout → SoftTimeLimitExceeded géré
      - Azure 429 → RateLimitHit → backoff long
      - Fichier corrompu → nettoyé en cas d'erreur

    Returns:
        dict: {"audio_id", "audio_path", "status", ...}
    """
    audio_path = None
    try:
        result, audio_path = _execute_tts_workflow(text, user_id)
        return result
    except Exception as exc:
        return _handle_generate_tts_exception(exc, audio_path, self, user_id)


def _execute_tts_workflow(text: str, user_id: str):
    """Core TTS workflow: validation, key retrieval, synthesize, and process result.

    Returns (success_result, audio_path) or raises typed exceptions.
    """
    # Validation
    _validate_tts_text(text)

    key = _get_azure_key()

    # Vérifier l'espace disque
    if not _check_disk_space(AUDIO_DIR):
        raise FatalTaskError(
            f"Insufficient disk space (need {MIN_DISK_SPACE_MB} MB free)"
        )

    audio_id, audio_path = _prepare_audio_path(AUDIO_DIR)

    # ── Import LAZY — le SDK Azure n'est chargé que quand nécessaire ──
    # Build speech config and synthesize
    speechsdk, speech_config = _build_speech_config(key)
    result = _synthesize_text(speechsdk, speech_config, audio_path, text)

    # Process result (may raise RateLimitHit / FatalTaskError / ExternalServiceDown)
    success = _process_tts_result(result, audio_path, audio_id, len(text), user_id)
    return success, audio_path


def _handle_generate_tts_exception(exc: Exception, audio_path: Path, task_self, user_id: str):
    """Centralized exception handling for `generate_tts` to reduce branching in the main task.

    Mirrors previous behavior: cleanup, classification, retry or return error/result.
    """
    _cleanup_on_error(audio_path)
    try:
        return _classify_and_handle_exception(exc, task_self, user_id)
    except Exception:
        raise


def _cleanup_on_error(audio_path: Path):
    """Perform cleanup of partial files; keep simple and testable."""
    if audio_path:
        _cleanup_partial_file(audio_path)


def _classify_and_handle_exception(exc: Exception, task_self, user_id: str):
    """Classify exception and perform retry/return behavior. Kept minimal to reduce branching."""
    if isinstance(exc, FatalTaskError):
        raise exc

    if isinstance(exc, SoftTimeLimitExceeded):
        return task_self.handle_timeout({"user_id": user_id})

    if isinstance(exc, (RateLimitHit, ExternalServiceDown)):
        task_self.retry_with_backoff(
            exc,
            base_delay=30.0 if isinstance(exc, RateLimitHit) else 5.0,
            max_delay=300.0 if isinstance(exc, RateLimitHit) else None,
        )

    classification = task_self.classify_error(exc)
    if classification == "fatal":
        return error_result(error=str(exc), task_name=task_self.name, retryable=False)

    task_self.retry_with_backoff(exc, base_delay=5.0)


@celery_app.task(
    base=AgriTask,
    name="backend.workers.tasks.voice.transcribe_audio",
    bind=True,
    max_retries=2,
    soft_time_limit=_VOICE_LIMITS["soft"],
    time_limit=_VOICE_LIMITS["hard"],
    acks_late=True,
    track_started=True,
)
def transcribe_audio(self, audio_file_path: str):
    """
    Transcrit un fichier audio en texte via Azure STT.

    Returns:
        dict: {"text", "status", ...}
    """
    try:
        # Validation
        audio_path = _validate_audio_file(audio_file_path)

        key = _get_azure_key()

        import azure.cognitiveservices.speech as speechsdk

        speech_config = speechsdk.SpeechConfig(
            subscription=key, region=settings.AZURE_REGION
        )
        speech_config.speech_recognition_language = "fr-FR"

        audio_config = speechsdk.audio.AudioConfig(filename=str(audio_path))
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config,
        )

        result = recognizer.recognize_once_async().get()

        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            return success_result(
                data={"text": result.text},
                task_name=self.name,
            )

        if result.reason == speechsdk.ResultReason.NoMatch:
            return success_result(
                data={"text": "", "note": "No speech recognized"},
                task_name=self.name,
            )

        raise ExternalServiceDown(f"STT failed: {result.reason}")

    except FatalTaskError:
        raise

    except SoftTimeLimitExceeded:
        return self.handle_timeout()

    except Exception as exc:
        classification = self.classify_error(exc)
        if classification == "fatal":
            return error_result(error=str(exc), task_name=self.name, retryable=False)
        self.retry_with_backoff(exc, base_delay=5.0)
