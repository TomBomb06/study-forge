"""Natural read-aloud voice via OpenAI text-to-speech.

Pluggable like the other providers: with TTS_PROVIDER=none the app falls
back to the browser's built-in device voices (free). With TTS_PROVIDER=openai
and an OPENAI_API_KEY, read-aloud uses studio-quality AI voices.

One request handles up to ~4096 characters (the API limit), so the client
sends the notes in chunks and plays the returned MP3s back to back.
"""

from ..config import get_settings

# OpenAI's stock voices, with friendly labels for the picker.
VOICES = [
    {"id": "nova",    "name": "Nova",    "desc": "Warm, friendly (recommended)"},
    {"id": "shimmer", "name": "Shimmer", "desc": "Bright and clear"},
    {"id": "alloy",   "name": "Alloy",   "desc": "Neutral and balanced"},
    {"id": "echo",    "name": "Echo",    "desc": "Calm and steady"},
    {"id": "fable",   "name": "Fable",   "desc": "Expressive storyteller"},
    {"id": "onyx",    "name": "Onyx",    "desc": "Deep and confident"},
]
_VOICE_IDS = {v["id"] for v in VOICES}
DEFAULT_VOICE = "nova"

MAX_CHARS = 4000  # safely under OpenAI's 4096 limit


class TTSError(Exception):
    """User-facing text-to-speech problem."""


def is_enabled() -> bool:
    s = get_settings()
    return s.tts_provider == "openai" and bool(s.openai_api_key)


def valid_voice(voice: str) -> str:
    return voice if voice in _VOICE_IDS else DEFAULT_VOICE


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> bytes:
    """Return MP3 audio bytes for `text`. Raises TTSError if unavailable."""
    s = get_settings()
    if not is_enabled():
        raise TTSError("AI voice isn't turned on.")
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        raise TTSError("Nothing to read.")
    voice = valid_voice(voice)
    try:
        speed = max(0.5, min(2.0, float(speed)))
    except (TypeError, ValueError):
        speed = 1.0
    try:
        from openai import OpenAI
    except ImportError:
        raise TTSError("The 'openai' package isn't installed. Run: pip install openai")
    try:
        client = OpenAI(api_key=s.openai_api_key)
        resp = client.audio.speech.create(
            model=s.tts_model, voice=voice, input=text, speed=speed,
            response_format="mp3",
        )
        # SDK returns an object whose .content holds the raw bytes.
        data = getattr(resp, "content", None)
        if data is None and hasattr(resp, "read"):
            data = resp.read()
        if not data:
            raise TTSError("The voice service returned no audio.")
        return data
    except TTSError:
        raise
    except Exception as e:  # network/auth/rate-limit
        raise TTSError(f"The voice service returned an error: {e}") from e
