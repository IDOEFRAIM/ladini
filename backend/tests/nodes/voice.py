import pytest
import tempfile
from pathlib import Path

from agriconnect.graphs.nodes.voice import VoiceAgent


def test_local_audio_url_creates_destination(tmp_path):
    agent = VoiceAgent()
    # create a temporary file to simulate generated audio
    src = tmp_path / "sample.mp3"
    src.write_text("dummy")
    url = agent._local_audio_url(str(src))
    assert url.startswith("file://")
    # destination file should exist
    dest_path = Path(url.replace("file://", ""))
    assert dest_path.exists()
