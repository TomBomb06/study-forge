"""Premium AI video: a narrated cartoon explainer built from a study set.

What a user gets: six illustrated cartoon scenes, each narrated, cut together
into a ~60-second MP4 that actually teaches the topic.

Why it is built this way. The obvious approach — send the notes to a
text-to-video model — costs about 7.5 Higgsfield credits for FIVE SECONDS of
footage, and five seconds of dreamy b-roll teaches nobody anything. Generating
six still illustrations costs about 0.9 credits total, runs ~50x cheaper, and
produces a minute of material that maps onto the actual content. Narration
comes from the text-to-speech the app already pays for, and ffmpeg cuts it
together. That is the whole trick.

Providers for the artwork:
  - "stub"       (default): no art, no cost, honest preview. The paywall and
                 metering still work end to end.
  - "openai"     : gpt-image-1. Uses OPENAI_API_KEY, which the app already
                 relies on for transcription and read-aloud.
  - "higgsfield" : platform.higgsfield.ai. Needs a REAL platform API key
                 (a UUID pair) — a subscription to the higgsfield.ai app is
                 not the same thing and its key will be rejected with 401.

Whichever provider runs, it is only ever reached AFTER billing.consume_video
has already deducted the user's allowance.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Optional

import httpx

from ..config import get_settings
from . import tts

logger = logging.getLogger("studyforge.video")

SCENES = 6
WIDTH, HEIGHT = 1280, 720
MIN_SCENE_SECONDS = 3.0
_FFMPEG_TIMEOUT = 15 * 60

ART_STYLE = (
    "flat 2D cartoon illustration, bold clean outlines, bright friendly colours, "
    "simple shapes, educational explainer style, plain uncluttered background, "
    "no text, no words, no letters, no watermark"
)


class VideoGenerationError(Exception):
    """User-facing failure while producing a video."""


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def _narration_script(study_set) -> str:
    text = (study_set.summary or "").replace("#", " ").replace("*", " ")
    return text[:1500].strip()


# ---------------------------------------------------------------- stub

def _stub_asset(study_set) -> dict:
    return {
        "provider": "stub",
        "status": "ready",
        "url": None,
        "narration_script": _narration_script(study_set),
        "demo": True,
        "note": "Preview only — AI video isn't switched on yet. This didn't use any of your video allowance.",
    }


# ------------------------------------------------------------ storyboard

_STORYBOARD_PROMPT = """You are turning study notes into a short narrated cartoon explainer.

Write exactly {n} scenes that teach this topic in order, building understanding.

For each scene give:
  "narration" - one or two sentences a friendly teacher says aloud. 15-35 words.
                Plain spoken English. No markdown, no lists, no stage directions.
  "art"       - a literal description of ONE simple cartoon picture illustrating
                that narration. Describe only what is visibly in the frame:
                objects, characters, arrangement. No text or labels in the image.
                Under 30 words.

Return ONLY a JSON array of {n} objects with keys "narration" and "art". No prose.

TITLE: {title}

NOTES:
{notes}"""


def _storyboard(study_set) -> list:
    """Ask Claude for the scene list. Falls back to slicing the notes."""
    settings = get_settings()
    notes = _narration_script(study_set)
    title = getattr(study_set, "title", "") or "this topic"
    if settings.anthropic_api_key:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.claude_model,
                max_tokens=2000,
                messages=[{"role": "user", "content": _STORYBOARD_PROMPT.format(
                    n=SCENES, title=title, notes=notes[:4000])}],
            )
            raw = "".join(getattr(b, "text", "") for b in resp.content)
            scenes = _parse_scenes(raw)
            if scenes:
                return scenes[:SCENES]
        except Exception:
            logger.exception("storyboard generation failed; falling back")
    return _fallback_scenes(title, notes)


def _parse_scenes(raw: str) -> list:
    match = re.search(r"\[.*\]", raw or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        narration = str(item.get("narration") or "").strip()
        art = str(item.get("art") or "").strip()
        if narration and art:
            out.append({"narration": narration[:400], "art": art[:300]})
    return out


def _fallback_scenes(title: str, notes: str) -> list:
    """No Claude? Still ship something coherent rather than failing."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", notes) if len(s.strip()) > 25]
    if not sentences:
        sentences = [f"An introduction to {title}."]
    per = max(1, len(sentences) // SCENES)
    scenes = []
    for i in range(0, len(sentences), per):
        chunk = " ".join(sentences[i:i + per])[:400]
        # Raw notes make a terrible drawing prompt and routinely trip the
        # image model's safety filter. Keep the art generic; the narration
        # carries the teaching.
        scenes.append({"narration": chunk, "art": _safe_art(len(scenes))})
        if len(scenes) == SCENES:
            break
    return scenes


# --------------------------------------------------------------- artwork

class _ImageProvider:
    def render(self, prompt: str, dest: str) -> None:
        raise NotImplementedError


class OpenAIImages(_ImageProvider):
    def __init__(self) -> None:
        s = get_settings()
        if not s.openai_api_key:
            raise VideoGenerationError(
                "VIDEO_PROVIDER=openai but OPENAI_API_KEY is not set."
            )
        self._key = s.openai_api_key
        self._model = s.video_image_model_openai

    def render(self, prompt: str, dest: str) -> None:
        import base64

        try:
            from openai import OpenAI
        except ImportError:
            raise VideoGenerationError("The 'openai' package isn't installed.")
        client = OpenAI(api_key=self._key)
        try:
            resp = client.images.generate(
                model=self._model, prompt=prompt,
                size="1536x1024", quality="low", n=1,
            )
            item = resp.data[0]
            raw = getattr(item, "b64_json", None)
            if raw:
                data = base64.b64decode(raw)
            else:
                url = getattr(item, "url", None)
                if not url:
                    raise VideoGenerationError("The image service returned nothing.")
                data = httpx.get(url, timeout=60.0).content
        except VideoGenerationError:
            raise
        except Exception as e:
            logger.exception("openai image generation failed")
            raise VideoGenerationError("Couldn't draw a scene.")
        with open(dest, "wb") as f:
            f.write(data)


class HiggsfieldImages(_ImageProvider):
    """platform.higgsfield.ai. Submit -> poll -> download."""

    def __init__(self, client: Optional[httpx.Client] = None) -> None:
        s = get_settings()
        if not s.higgsfield_credentials:
            raise VideoGenerationError(
                "VIDEO_PROVIDER=higgsfield but HIGGSFIELD_CREDENTIALS is not set."
            )
        self._base = s.higgsfield_base_url.rstrip("/")
        self._endpoint = s.higgsfield_image_endpoint
        self._model = s.higgsfield_image_model
        self._headers = {
            "Authorization": f"Key {s.higgsfield_credentials}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.Client(timeout=60.0)

    def render(self, prompt: str, dest: str) -> None:
        import time as _time

        body = {"input": {"model": self._model, "prompt": prompt,
                          "aspect_ratio": "16:9"}}
        try:
            r = self._client.post(self._base + self._endpoint, headers=self._headers, json=body)
        except httpx.HTTPError as e:
            raise VideoGenerationError(f"Couldn't reach the image service: {e}")
        if r.status_code in (401, 403):
            raise VideoGenerationError(
                "Higgsfield rejected the API key. A higgsfield.ai subscription is not "
                "an API key — generate one at platform.higgsfield.ai and set "
                "HIGGSFIELD_CREDENTIALS to it."
            )
        if r.status_code == 402:
            raise VideoGenerationError("The Higgsfield account is out of credits.")
        try:
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise VideoGenerationError(f"The image service returned an error: {e}")

        url = _extract_media(data)
        if not url:
            status_url = data.get("status_url") or (
                f"{self._base}/requests/{data.get('request_id')}/status"
                if data.get("request_id") else None)
            if not status_url:
                raise VideoGenerationError("The image service returned no way to track the job.")
            deadline = _time.time() + 240
            while _time.time() < deadline:
                _time.sleep(3.0)
                try:
                    p = self._client.get(status_url, headers=self._headers)
                    p.raise_for_status()
                    pdata = p.json()
                except (httpx.HTTPError, ValueError) as e:
                    raise VideoGenerationError(f"Lost contact with the image service: {e}")
                status = (pdata.get("status") or "").lower()
                if status in ("completed", "succeeded"):
                    url = _extract_media(pdata)
                    break
                if status in ("failed", "nsfw", "canceled"):
                    raise VideoGenerationError(f"Image generation {status}.")
            if not url:
                raise VideoGenerationError("Image generation timed out.")
        try:
            img = httpx.get(url, timeout=120.0)
            img.raise_for_status()
        except httpx.HTTPError as e:
            raise VideoGenerationError(f"Couldn't download a scene: {e}")
        with open(dest, "wb") as f:
            f.write(img.content)


def _extract_media(data: dict):
    for key in ("image", "video", "output"):
        node = data.get(key)
        if isinstance(node, dict) and node.get("url"):
            return node["url"]
        if isinstance(node, list) and node and isinstance(node[0], dict) and node[0].get("url"):
            return node[0]["url"]
    results = data.get("results")
    if isinstance(results, dict):
        raw = results.get("raw") or results.get("min")
        if isinstance(raw, dict):
            return raw.get("url")
    return None


def _image_provider() -> _ImageProvider:
    provider = get_settings().video_provider
    if provider == "higgsfield":
        return HiggsfieldImages()
    return OpenAIImages()


# ----------------------------------------------------------------- render

def _run(cmd: list) -> None:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=_FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise VideoGenerationError("The video took too long to put together.")
    except OSError:
        logger.exception("ffmpeg could not be started")
        raise VideoGenerationError("We couldn't put the video together.")
    if proc.returncode != 0:
        logger.error("ffmpeg failed (%s): %s", proc.returncode, proc.stderr[-2000:])
        raise VideoGenerationError("We couldn't put the video together.")


def _audio_seconds(path: str) -> float:
    probe = _ffprobe()
    if not probe:
        return 8.0
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return max(MIN_SCENE_SECONDS, float(out.stdout.strip() or 8.0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 8.0


def _safe_art(index: int) -> str:
    """A neutral visual no image model will refuse.

    Study sets are legitimately about war, medicine, crime and disease, and
    image models refuse prompts on all of those. When the real prompt is
    rejected we still owe the user a video.
    """
    motifs = [
        "an open notebook and a pencil on a desk",
        "a friendly cartoon student reading at a desk",
        "a stack of books beside a mug",
        "a simple lightbulb above an open book",
        "a chalkboard with blank space",
        "a backpack, a notebook and a pair of glasses",
    ]
    return f"{motifs[index % len(motifs)]}, calm and encouraging"


def _colour_card(ff: str, dest: str) -> None:
    """Last resort frame: a plain brand-coloured card."""
    _run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
          "-i", f"color=c=0x0B1D3A:s={WIDTH}x{HEIGHT}",
          "-frames:v", "1", dest])


def _render_scene(provider, scene: dict, index: int, dest: str, ff: str) -> None:
    """Draw one scene, degrading rather than failing.

    Real prompt -> neutral prompt -> plain card. A single refused image is
    not a reason to throw away five other scenes and the user's credit.
    """
    for prompt in (f'{scene["art"]}. {ART_STYLE}',
                   f'{_safe_art(index)}. {ART_STYLE}'):
        try:
            provider.render(prompt, dest)
            return
        except VideoGenerationError:
            logger.exception("scene %s refused; falling back to a safer prompt", index)
    _colour_card(ff, dest)


def _build(scenes: list, work: str, out_path: str) -> None:
    ff = _ffmpeg()
    if not ff:
        raise VideoGenerationError(
            "Video assembly needs ffmpeg, which isn't installed on this server."
        )
    provider = _image_provider()
    clips = []
    for i, scene in enumerate(scenes):
        img = os.path.join(work, f"scene{i}.png")
        # The house style is applied in _render_scene, once, so the two
        # providers can never drift into drawing different-looking videos.
        _render_scene(provider, scene, i, img, ff)

        audio = None
        seconds = 8.0
        if tts.is_enabled():
            try:
                mp3 = tts.synthesize(scene["narration"], speed=1.0)
                audio = os.path.join(work, f"scene{i}.mp3")
                with open(audio, "wb") as f:
                    f.write(mp3)
                seconds = _audio_seconds(audio) + 0.6  # let the sentence land
            except tts.TTSError:
                logger.exception("narration failed for scene %s", i)
                audio = None

        clip = os.path.join(work, f"clip{i}.mp4")
        # Slow push-in so a still frame doesn't read as a dead slide.
        vf = (f"scale={WIDTH*2}:-2,"
              f"zoompan=z='min(zoom+0.0006,1.10)':d={int(seconds*25)}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps=25,"
              f"format=yuv420p")
        cmd = [ff, "-y", "-loglevel", "error", "-loop", "1", "-i", img]
        if audio:
            cmd += ["-i", audio]
        cmd += ["-t", f"{seconds:.2f}", "-vf", vf, "-c:v", "libx264",
                "-preset", "medium", "-crf", "23", "-r", "25"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100", "-shortest"]
        else:
            cmd += ["-an"]
        cmd += [clip]
        _run(cmd)
        clips.append(clip)

    listing = os.path.join(work, "clips.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    _run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
          "-i", listing, "-c:v", "libx264", "-preset", "medium", "-crf", "23",
          "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path])


# ---------------------------------------------------------------- entry

def is_live() -> bool:
    """True when a real provider is configured AND can actually render.

    Everything user-facing keys off this: a placeholder must never be sold as,
    charged for, or described as an AI video.
    """
    s = get_settings()
    if s.video_provider == "openai":
        return bool(s.openai_api_key) and bool(_ffmpeg())
    if s.video_provider == "higgsfield":
        return bool(s.higgsfield_credentials) and bool(_ffmpeg())
    return False


def video_dir() -> str:
    path = os.path.join(get_settings().storage_dir, "videos")
    os.makedirs(path, exist_ok=True)
    return path


def generate_video_asset(study_set, client: Optional[httpx.Client] = None) -> dict:
    settings = get_settings()
    if not is_live():
        return _stub_asset(study_set)

    scenes = _storyboard(study_set)
    if not scenes:
        raise VideoGenerationError("There wasn't enough in these notes to make a video from.")

    name = f"{uuid.uuid4().hex}.mp4"
    out_path = os.path.join(video_dir(), name)
    with tempfile.TemporaryDirectory(prefix="sf-vid-") as work:
        _build(scenes, work, out_path)

    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise VideoGenerationError("The video came out empty. Please try again.")

    return {
        "provider": settings.video_provider,
        "status": "ready",
        "url": f"/media/videos/{name}",
        "scenes": [s["narration"] for s in scenes],
        "narration_script": " ".join(s["narration"] for s in scenes),
    }
