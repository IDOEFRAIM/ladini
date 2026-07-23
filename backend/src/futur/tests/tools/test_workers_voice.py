import sys
import os
import pytest

from pathlib import Path

# Ensure backend/src is on PATH for imports when running tests from repo root
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Add repository root so `import backend.src...` resolves to the local `backend` package
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Provide a minimal `celery.exceptions.SoftTimeLimitExceeded` if celery not installed
import types
if 'celery' not in sys.modules:
    celery_mod = types.ModuleType('celery')
    celery_excs = types.ModuleType('celery.exceptions')
    SoftTimeLimitExceeded = type('SoftTimeLimitExceeded', (Exception,), {})
    MaxRetriesExceededError = type('MaxRetriesExceededError', (Exception,), {})
    setattr(celery_excs, 'SoftTimeLimitExceeded', SoftTimeLimitExceeded)
    setattr(celery_excs, 'MaxRetriesExceededError', MaxRetriesExceededError)
    # Provide minimal API surface expected by agnostic imports
    setattr(celery_mod, 'Celery', lambda *a, **k: None)
    # Minimal Task base used by imports
    class _DummyTaskBase:
        pass
    setattr(celery_mod, 'Task', _DummyTaskBase)
    setattr(celery_mod, 'exceptions', celery_excs)
    sys.modules['celery'] = celery_mod
    sys.modules['celery.exceptions'] = celery_excs

# Provide a dummy `agriconnect.workers.celery_app` to avoid importing real Celery
celery_app_mod_name = 'agriconnect.workers.celery_app'
if celery_app_mod_name not in sys.modules:
    ca_mod = types.ModuleType(celery_app_mod_name)
    # dummy celery_app with a `.task` decorator that returns the function unchanged
    ca_mod.celery_app = types.SimpleNamespace(task=lambda *a, **k: (lambda f: f))
    sys.modules[celery_app_mod_name] = ca_mod

# Provide minimal kombu symbols used by celery_config
if 'kombu' not in sys.modules:
    kombu_mod = types.ModuleType('kombu')
    class Exchange:
        def __init__(self, *a, **k):
            pass

    class Queue:
        def __init__(self, *a, **k):
            pass

    kombu_mod.Exchange = Exchange
    kombu_mod.Queue = Queue
    sys.modules['kombu'] = kombu_mod

from agriconnect.workers.tasks import voice as voice_mod
from agriconnect.workers.task_base import (
    RateLimitHit,
    FatalTaskError,
    ExternalServiceDown,
)
from celery.exceptions import SoftTimeLimitExceeded


def test_validate_tts_text_empty():
    with pytest.raises(FatalTaskError):
        voice_mod._validate_tts_text("")


def test_validate_tts_text_too_long():
    long_text = "x" * (voice_mod.MAX_TTS_TEXT_LENGTH + 1)
    with pytest.raises(FatalTaskError):
        voice_mod._validate_tts_text(long_text)


class DummyCancellation:
    def __init__(self, reason, error_details):
        self.reason = reason
        self.error_details = error_details


class DummyResult:
    def __init__(self, reason=None, cancellation=None):
        self.reason = reason
        self.cancellation_details = cancellation


def test_format_tts_error_details_with_cancellation():
    cancel = DummyCancellation("ErrorReason", "Details")
    res = DummyResult(reason="Cancelled", cancellation=cancel)
    out = voice_mod._format_tts_error_details(res)
    assert "ErrorReason" in out and "Details" in out


def test_raise_tts_error_rate_limit():
    with pytest.raises(RateLimitHit):
        voice_mod._raise_tts_error_from_details("429 Too Many Requests", Path("/tmp/fake.wav"))


def test_raise_tts_error_auth():
    with pytest.raises(FatalTaskError):
        voice_mod._raise_tts_error_from_details("401 Unauthorized", Path("/tmp/fake.wav"))


def test_raise_tts_error_external():
    with pytest.raises(ExternalServiceDown):
        voice_mod._raise_tts_error_from_details("Some other error", Path("/tmp/fake.wav"))


def test_classify_and_handle_exception_soft_timeout(monkeypatch):
    class DummyTask:
        name = "test"

        def handle_timeout(self, payload=None):
            return {"timeout": True, "payload": payload}

        def classify_error(self, exc):
            return "not-fatal"

        def retry_with_backoff(self, exc, base_delay=5.0, max_delay=None):
            raise RuntimeError("retry")

    res = voice_mod._classify_and_handle_exception(SoftTimeLimitExceeded(), DummyTask(), "user1")
    assert isinstance(res, dict) and res.get("timeout") is True


def test_classify_and_handle_exception_fatal(monkeypatch):
    class DummyTask:
        name = "test"

        def classify_error(self, exc):
            return "fatal"

        def retry_with_backoff(self, exc, base_delay=5.0, max_delay=None):
            raise RuntimeError("retry")

    res = voice_mod._classify_and_handle_exception(Exception("boom"), DummyTask(), "user1")
    assert isinstance(res, dict) and "error" in res


def test_process_tts_result_success(tmp_path):
    import sys, types

    # Create fake azure.cognitiveservices.speech module with ResultReason
    speech_mod = types.ModuleType('azure.cognitiveservices.speech')
    class RR:
        pass
    RR.SynthesizingAudioCompleted = 'SynthCompleted'
    speech_mod.ResultReason = RR

    # Insert minimal azure module path
    sys.modules['azure'] = types.ModuleType('azure')
    sys.modules['azure.cognitiveservices'] = types.ModuleType('azure.cognitiveservices')
    sys.modules['azure.cognitiveservices.speech'] = speech_mod

    # create a small audio file to represent synthesized output
    audio_file = tmp_path / 'out.wav'
    audio_file.write_bytes(b'RIFFDATA')

    class DummyResult:
        def __init__(self, reason):
            self.reason = reason
            self.cancellation_details = None

    res = DummyResult(RR.SynthesizingAudioCompleted)
    out = voice_mod._process_tts_result(res, audio_file, 'audio-123', 42, 'user-7')
    assert out['status'] == 'success'
    assert out['audio_id'] == 'audio-123'
    assert out['user_id'] == 'user-7'
