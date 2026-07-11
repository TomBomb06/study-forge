"""Fetch a YouTube video's transcript from its existing captions.

Free — no API key, no audio download. Works when the video has captions
(most lectures and educational videos do). If a video has none, we raise a
clear error telling the user to try a different video or paste the text.
"""

import re

from .extract import ExtractionError

_ID_PATTERNS = [
    r"(?:v=|/embed/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
    r"^([A-Za-z0-9_-]{11})$",
]


def _video_id(url: str) -> str:
    url = url.strip()
    for pat in _ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    raise ExtractionError("That doesn't look like a YouTube link.")


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or "", re.I))


def _snippet_text(snip) -> str:
    if isinstance(snip, dict):
        return snip.get("text", "")
    return getattr(snip, "text", "") or ""


def fetch_youtube_transcript(url: str) -> tuple[str, str]:
    """Return (title, text). Title falls back to the video id."""
    video_id = _video_id(url)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        raise ExtractionError("YouTube support isn't installed on this server.")

    try:
        # Library API changed across versions: classmethod get_transcript
        # (older) vs instance .fetch (newer). Support both.
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            snippets = YouTubeTranscriptApi.get_transcript(video_id)
        else:
            snippets = YouTubeTranscriptApi().fetch(video_id)
        text = " ".join(_snippet_text(s) for s in snippets).strip()
    except ExtractionError:
        raise
    except Exception as e:
        name = type(e).__name__.lower()
        if "disabled" in name or "notranscript" in name or "nofound" in name:
            raise ExtractionError(
                "This video doesn't have captions we can read. Try a video with "
                "captions, or paste the text directly."
            )
        if "unavailable" in name or "video" in name:
            raise ExtractionError("That video is unavailable or private.")
        raise ExtractionError(
            "Couldn't fetch this video's transcript. YouTube may be blocking "
            "requests, or the video has no captions — try pasting the text instead."
        )

    if len(text) < 40:
        raise ExtractionError("This video's transcript was too short to use.")
    return f"YouTube video {video_id}", text
