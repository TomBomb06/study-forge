"""Tests for YouTube captions and audio/video transcription inputs.
Network and transcription APIs are mocked — no real calls, no cost."""

import io
import time

import pytest

from app import config
from app.pipeline import transcribe, youtube
from app.pipeline.extract import ExtractionError

LECTURE = (
    "Newton's first law states that an object in motion stays in motion unless a "
    "force acts on it. This is called inertia. The second law says force equals "
    "mass times acceleration. The third law states every action has an equal and "
    "opposite reaction. Momentum is conserved in a closed system."
)


# ---------- YouTube ----------

def test_youtube_id_extraction():
    for url in [
        "https://www.youtube.com/watch?v=abcd1234XYZ",
        "https://youtu.be/abcd1234XYZ",
        "https://youtube.com/shorts/abcd1234XYZ",
    ]:
        assert youtube._video_id(url) == "abcd1234XYZ"


def test_youtube_rejects_non_youtube_id():
    with pytest.raises(ExtractionError):
        youtube._video_id("https://example.com/no-id-here")


def test_is_youtube():
    assert youtube.is_youtube("https://youtu.be/x")
    assert not youtube.is_youtube("https://example.com")


def test_youtube_fetch_mocked(monkeypatch):
    class _FakeApi:
        @staticmethod
        def get_transcript(vid):
            return [{"text": "Newton's laws of motion."}, {"text": "Force equals mass times acceleration."}]

    import youtube_transcript_api
    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", _FakeApi)
    title, text = youtube.fetch_youtube_transcript("https://youtu.be/abcd1234XYZ")
    assert "Newton" in text and "acceleration" in text


# ---------- Transcription ----------

def test_transcription_off_by_default():
    config.get_settings.cache_clear()
    with pytest.raises(ExtractionError) as e:
        transcribe.transcribe("/tmp/whatever.mp3")
    assert "transcription isn't turned on" in str(e.value).lower()


def test_openai_transcription_mocked(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIBE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config.get_settings.cache_clear()
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"fake audio bytes")

    import types
    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(
            transcriptions=types.SimpleNamespace(
                create=lambda model, file: types.SimpleNamespace(text=LECTURE)
            )
        )
    )
    try:
        text = transcribe.transcribe(str(audio), client=fake_client)
        assert "Newton" in text
    finally:
        config.get_settings.cache_clear()


# ---------- endpoints ----------

def test_youtube_endpoint_mocked(client, auth_headers, monkeypatch):
    from app.pipeline import jobs
    monkeypatch.setattr(jobs, "fetch_youtube_transcript", lambda url: ("Lecture", LECTURE))
    r = client.post("/uploads/youtube", headers=auth_headers, json={"url": "https://youtu.be/abcd1234XYZ"})
    assert r.status_code == 202
    jid = r.json()["id"]
    for _ in range(50):
        j = client.get(f"/jobs/{jid}", headers=auth_headers).json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "completed", j.get("error")


def test_media_upload_transcription_off(client, auth_headers):
    # With transcription off (default), the job should fail with a clear note.
    files = {"file": ("lecture.mp3", io.BytesIO(b"fake audio data here padding padding"), "audio/mpeg")}
    r = client.post("/uploads/media", headers=auth_headers, files=files)
    assert r.status_code == 202
    jid = r.json()["id"]
    for _ in range(50):
        j = client.get(f"/jobs/{jid}", headers=auth_headers).json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "failed"
    assert "transcription" in (j["error"] or "").lower()


def test_media_upload_accepts_video_extension(client, auth_headers):
    files = {"file": ("class.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 padding bytes"), "video/mp4")}
    r = client.post("/uploads/media", headers=auth_headers, files=files)
    assert r.status_code == 202  # accepted by extension (transcription decides later)


def test_media_requires_auth(client):
    files = {"file": ("x.mp3", io.BytesIO(b"data"), "audio/mpeg")}
    assert client.post("/uploads/media", files=files).status_code == 401
