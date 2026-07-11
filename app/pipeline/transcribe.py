"""Audio/video transcription for lecture uploads.

Pluggable, mirroring the video generator:
  - "none" (default): transcription is off, so audio/video uploads fail with a
    clear message and there is NO cost or heavy dependency.
  - "openai": uses OpenAI's Whisper transcription API (needs an OpenAI key,
    ~$0.006/min). Scales fine for real customers.

A local-Whisper provider could be added later for offline use, but it's slow
on a laptop and doesn't scale server-side, so it's intentionally not wired.
"""

from typing import Optional

from ..config import get_settings
from .extract import ExtractionError


def transcribe(path: str, client=None) -> str:
    settings = get_settings()
    provider = settings.transcribe_provider
    if provider == "openai":
        return _openai_transcribe(path, settings, client)
    raise ExtractionError(
        "Audio/video transcription isn't turned on. To enable it, set "
        "TRANSCRIBE_PROVIDER=openai and an OPENAI_API_KEY in backend/.env. "
        "For now, upload a PDF or paste your notes instead."
    )


def _openai_transcribe(path: str, settings, client=None) -> str:
    if client is None:
        if not settings.openai_api_key:
            raise ExtractionError(
                "TRANSCRIBE_PROVIDER=openai but OPENAI_API_KEY is not set."
            )
        try:
            import openai
        except ImportError:
            raise ExtractionError(
                "The 'openai' package isn't installed. Run: pip install openai"
            )
        client = openai.OpenAI(api_key=settings.openai_api_key)

    try:
        with open(path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=settings.whisper_model, file=f
            )
        text = getattr(resp, "text", None) or (resp.get("text") if isinstance(resp, dict) else "")
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"Transcription failed: {e}")

    text = (text or "").strip()
    if len(text) < 40:
        raise ExtractionError("We couldn't hear enough speech in that file to study from.")
    return text
