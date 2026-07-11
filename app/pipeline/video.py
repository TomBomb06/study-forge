"""Premium AI-video generation (metered feature).

Pluggable provider, mirroring the text generator:
  - "stub" (default): metering + UI work end-to-end with NO real video and
    NO cost, so the paywall is fully demonstrable today.
  - "higgsfield": calls the real Higgsfield Cloud API. Requires a key and
    spends money per video.

Whichever provider runs, it is only ever reached AFTER billing.consume_video
has already deducted the user's allowance.

Higgsfield contract (from the official v2 SDK, platform.higgsfield.ai):
  - Auth header:  Authorization: Key KEY_ID:KEY_SECRET
  - Submit:       POST {base}{endpoint}  with body {"input": {...}}
                  -> {"request_id", "status_url", "status", ...}
  - Poll:         GET  {status_url}  (or {base}/requests/{id}/status)
                  status in queued|in_progress|completed|failed|nsfw
                  on completed -> {"video": {"url": "..."}}
"""

import time
from typing import Optional

import httpx

from ..config import get_settings


class VideoGenerationError(Exception):
    """User-facing failure while producing a video."""


def _narration_script(study_set) -> str:
    """The words the video would narrate — the study-guide notes, trimmed."""
    text = (study_set.summary or "").replace("#", " ").replace("*", " ")
    return text[:1500].strip()


# ---------------------------------------------------------------- stub

def _stub_asset(study_set) -> dict:
    return {
        "provider": "stub",
        "status": "ready",
        "url": None,
        "narration_script": _narration_script(study_set),
        "note": "Demo placeholder — real AI video renders here once a Higgsfield Cloud key is connected.",
    }


# ------------------------------------------------------------ higgsfield

class HiggsfieldVideoProvider:
    """Thin, correct client for the Higgsfield Cloud v2 API."""

    def __init__(self, client: Optional[httpx.Client] = None) -> None:
        s = get_settings()
        if not s.higgsfield_credentials:
            raise VideoGenerationError(
                "VIDEO_PROVIDER=higgsfield but HIGGSFIELD_CREDENTIALS is not set. "
                "Add your 'KEY_ID:KEY_SECRET' from cloud.higgsfield.ai to backend/.env."
            )
        self._base = s.higgsfield_base_url.rstrip("/")
        self._endpoint = s.higgsfield_video_endpoint
        self._model = s.higgsfield_video_model
        self._start_image = s.higgsfield_video_start_image
        self._headers = {
            "Authorization": f"Key {s.higgsfield_credentials}",
            "User-Agent": "higgsfield-server-js/2.0",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.Client(timeout=30.0)
        self._poll_interval = 3.0
        self._max_poll = 300.0  # 5 minutes

    def _input(self, study_set) -> dict:
        body: dict = {"model": self._model, "prompt": _narration_script(study_set)}
        if self._start_image:
            body["input_images"] = [{"type": "image_url", "image_url": self._start_image}]
        return body

    def generate(self, study_set) -> dict:
        url = self._base + self._endpoint
        try:
            resp = self._client.post(url, headers=self._headers, json={"input": self._input(study_set)})
            if resp.status_code in (401, 403):
                raise VideoGenerationError("Higgsfield rejected the credentials (check your key).")
            if resp.status_code == 402:
                raise VideoGenerationError("Higgsfield account is out of credits.")
            resp.raise_for_status()
            data = resp.json()
        except VideoGenerationError:
            raise
        except httpx.HTTPError as e:
            raise VideoGenerationError(f"Couldn't reach the video service: {e}")

        request_id = data.get("request_id")
        status_url = data.get("status_url") or (
            f"{self._base}/requests/{request_id}/status" if request_id else None
        )
        video_url = self._extract_video(data)
        if video_url:  # some responses complete immediately
            return {"provider": "higgsfield", "status": "ready", "url": video_url}
        if not status_url:
            raise VideoGenerationError("Video service returned no way to track the job.")
        return self._poll(status_url)

    def _poll(self, status_url: str) -> dict:
        deadline = time.time() + self._max_poll
        while time.time() < deadline:
            try:
                r = self._client.get(status_url, headers=self._headers)
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPError as e:
                raise VideoGenerationError(f"Lost contact with the video service: {e}")
            status = (data.get("status") or "").lower()
            if status == "completed":
                video_url = self._extract_video(data)
                if not video_url:
                    raise VideoGenerationError("Video finished but no file was returned.")
                return {"provider": "higgsfield", "status": "ready", "url": video_url}
            if status in ("failed", "nsfw", "canceled"):
                raise VideoGenerationError(f"Video generation {status}.")
            time.sleep(self._poll_interval)
        raise VideoGenerationError("Video generation timed out. Please try again.")

    @staticmethod
    def _extract_video(data: dict):
        video = data.get("video")
        if isinstance(video, dict):
            return video.get("url")
        results = data.get("results")
        if isinstance(results, dict):
            raw = results.get("raw")
            if isinstance(raw, dict):
                return raw.get("url")
        return None


# ---------------------------------------------------------------- entry

def generate_video_asset(study_set, client: Optional[httpx.Client] = None) -> dict:
    settings = get_settings()
    if settings.video_provider == "higgsfield":
        return HiggsfieldVideoProvider(client=client).generate(study_set)
    return _stub_asset(study_set)
