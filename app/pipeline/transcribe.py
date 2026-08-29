"""Audio/video transcription for lecture uploads.

Pluggable, mirroring the video generator:
  - "none" (default): transcription is off, so audio/video uploads fail with a
    clear message and there is NO cost or heavy dependency.
  - "openai": uses OpenAI's Whisper transcription API (needs an OpenAI key,
    ~$0.006/min). Scales fine for real customers.

Long lectures: the transcription API rejects anything over 25 MB, which a
90-minute class recording blows straight past. So before uploading we
re-encode to mono 32 kbps Opus (speech stays perfectly legible and an hour
lands around 14 MB), and if it is STILL too big we split it into segments and
stitch the transcripts back together. Failing at the end of a lecture that
can't be recorded again is the one outcome worth this much effort to avoid.

A local-Whisper provider could be added later for offline use, but it's slow
on a laptop and doesn't scale server-side, so it's intentionally not wired.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from ..config import get_settings
from .extract import ExtractionError

logger = logging.getLogger("studyforge.transcribe")

# The API's hard ceiling is 25 MB. Stay under it with room for container
# overhead rather than discovering the difference on a user's only recording.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
SEGMENT_SECONDS = 20 * 60      # ~4.6 MB per segment at 32 kbps
_FFMPEG_TIMEOUT = 20 * 60


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _run(cmd: list) -> None:
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError("That recording took too long to process. Try a shorter one.")
    except OSError:
        logger.exception("ffmpeg could not be started")
        raise ExtractionError("We couldn't process that recording. Try uploading it again.")
    if proc.returncode != 0:
        # stderr can name on-disk paths, so it is logged, never returned.
        logger.error("ffmpeg failed (%s): %s", proc.returncode, proc.stderr[-2000:])
        raise ExtractionError(
            "We couldn't read the audio in that file. Make sure it actually "
            "contains a recording, then try again."
        )


def _compress(path: str, workdir: str) -> str:
    """Re-encode to mono 32 kbps Opus. Returns the original path if ffmpeg is
    unavailable — the caller still has the size check to fall back on."""
    ff = _ffmpeg()
    if not ff:
        return path
    out = os.path.join(workdir, "audio.ogg")
    _run([ff, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "libopus", "-b:a", "32k", "-loglevel", "error", out])
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        return path
    return out


def _segment(path: str, workdir: str) -> list:
    """Split an already-compressed file into SEGMENT_SECONDS pieces, in order."""
    ff = _ffmpeg()
    if not ff:
        raise ExtractionError(
            "That recording is too long for us to transcribe in one piece. "
            "Try splitting it, or record in shorter sessions."
        )
    pattern = os.path.join(workdir, "seg%04d.ogg")
    _run([ff, "-y", "-i", path, "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
          "-c", "copy", "-loglevel", "error", pattern])
    segs = sorted(
        os.path.join(workdir, f) for f in os.listdir(workdir)
        if f.startswith("seg") and f.endswith(".ogg")
    )
    if not segs:
        raise ExtractionError("We couldn't split that recording up to transcribe it.")
    return segs


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
        text = _transcribe_file(path, settings, client)
    except ExtractionError:
        raise
    except OSError:
        # An OSError here embeds the on-disk path (/app/storage/<user id>/…)
        # and it ends up in job.error, which is returned over the API.
        logger.exception("could not read the uploaded media file")
        raise ExtractionError(
            "We couldn't read that recording. Try uploading it again."
        )
    except Exception as e:
        logger.exception("transcription failed")
        raise ExtractionError(
            "We couldn't turn that recording into text. Try a clearer or "
            "shorter recording."
        )

    text = (text or "").strip()
    if len(text) < 40:
        raise ExtractionError("We couldn't hear enough speech in that file to study from.")
    return text


def _one(path: str, settings, client) -> str:
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(model=settings.whisper_model, file=f)
    return getattr(resp, "text", None) or (resp.get("text") if isinstance(resp, dict) else "") or ""


def _transcribe_file(path: str, settings, client) -> str:
    """Send the file to Whisper, compressing and splitting it if it is too big."""
    if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
        return _one(path, settings, client)

    with tempfile.TemporaryDirectory(prefix="sf-tx-") as work:
        small = _compress(path, work)
        if os.path.getsize(small) <= MAX_UPLOAD_BYTES:
            return _one(small, settings, client)
        parts = []
        for seg in _segment(small, work):
            # Segment boundaries can land mid-word; a space is the honest join.
            parts.append(_one(seg, settings, client).strip())
        return " ".join(p for p in parts if p)
