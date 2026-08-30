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


def _proxy_session():
    """A requests session pinned to the YouTube proxy, or None.

    Only YouTube traffic goes through it. Setting HTTP_PROXY globally would
    also drag OpenAI, Anthropic and Stripe through a third party's network,
    which is not a trade worth making to read a lecture's captions.
    """
    from ..config import get_settings

    url = (get_settings().youtube_proxy_url or "").strip()
    if not url:
        return None
    try:
        import requests

        sess = requests.Session()
        sess.proxies = {"http": url, "https": url}
        return sess
    except Exception:  # pragma: no cover - proxy is best-effort
        return None


def _fetch_snippets(video_id: str):
    """Fetch caption snippets, using the proxy when one is configured."""
    from youtube_transcript_api import YouTubeTranscriptApi

    sess = _proxy_session()
    if sess is not None:
        # 1.x takes an http_client; older releases don't and are direct-only.
        # Only the constructor is guarded — a TypeError out of fetch() would
        # otherwise silently retry unproxied and hit the same block.
        api = None
        try:
            api = YouTubeTranscriptApi(http_client=sess)
        except TypeError:
            api = None
        if api is not None:
            return api.fetch(video_id)
    # Library API changed across versions: classmethod get_transcript
    # (older) vs instance .fetch (newer). Support both.
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        return YouTubeTranscriptApi.get_transcript(video_id)
    return YouTubeTranscriptApi().fetch(video_id)


def fetch_youtube_transcript(url: str) -> tuple[str, str]:
    """Return (title, text). Title falls back to the video id."""
    video_id = _video_id(url)
    try:
        import youtube_transcript_api  # noqa: F401
    except ImportError:
        raise ExtractionError("YouTube support isn't installed on this server.")

    try:
        snippets = _fetch_snippets(video_id)
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
        # YouTube refuses transcript requests from datacenter IPs, which is
        # every request this server makes. It is our problem, not the user's,
        # and the old message ("the video has no captions") blamed them for it.
        if "blocked" in name or "toomany" in name or "ratelimit" in name:
            raise ExtractionError(
                "YouTube is blocking us from reading captions right now. This is "
                "on our side, not your video — paste the transcript or your notes "
                "in the text box instead and it'll work."
            )
        if "unavailable" in name or "video" in name:
            raise ExtractionError("That video is unavailable or private.")
        raise ExtractionError(
            "Couldn't fetch this video's captions. Paste the transcript or your "
            "notes in the text box instead and it'll work."
        )

    if len(text) < 40:
        raise ExtractionError("This video's transcript was too short to use.")
    return f"YouTube video {video_id}", text
