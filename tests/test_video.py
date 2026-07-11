"""Tests for the real Higgsfield Cloud video provider — with the network
fully mocked, so no API calls and no cost. Verifies auth-header shape,
submit + poll, video-URL extraction, and error handling."""

import types

import pytest

from app import config
from app.pipeline import video
from app.pipeline.video import HiggsfieldVideoProvider, VideoGenerationError


def _study_set():
    return types.SimpleNamespace(
        summary="Photosynthesis converts light to energy. Chlorophyll absorbs light. "
        "The Calvin cycle fixes carbon dioxide into glucose."
    )


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Records the POST, then serves queued GET responses for polling."""

    def __init__(self, post_resp, get_resps):
        self.post_resp = post_resp
        self.get_resps = list(get_resps)
        self.posted = None

    def post(self, url, headers=None, json=None):
        self.posted = {"url": url, "headers": headers, "json": json}
        return self.post_resp

    def get(self, url, headers=None):
        return self.get_resps.pop(0)


@pytest.fixture
def higgsfield_env(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER", "higgsfield")
    monkeypatch.setenv("HIGGSFIELD_CREDENTIALS", "KEYID:KEYSECRET")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_submit_then_poll_completes(higgsfield_env):
    submit = _Resp(200, {"request_id": "abc", "status_url": "https://x/requests/abc/status"})
    polls = [
        _Resp(200, {"status": "in_progress"}),
        _Resp(200, {"status": "completed", "video": {"url": "https://cdn/video.mp4"}}),
    ]
    fake = _FakeClient(submit, polls)
    # avoid real sleeps between polls
    prov = HiggsfieldVideoProvider(client=fake)
    prov._poll_interval = 0
    result = prov.generate(_study_set())
    assert result["status"] == "ready"
    assert result["url"] == "https://cdn/video.mp4"
    # Correct auth header shape per the v2 SDK.
    assert fake.posted["headers"]["Authorization"] == "Key KEYID:KEYSECRET"
    assert "input" in fake.posted["json"]


def test_immediate_video_in_submit_response(higgsfield_env):
    submit = _Resp(200, {"request_id": "abc", "video": {"url": "https://cdn/quick.mp4"}})
    prov = HiggsfieldVideoProvider(client=_FakeClient(submit, []))
    result = prov.generate(_study_set())
    assert result["url"] == "https://cdn/quick.mp4"


def test_failed_status_raises(higgsfield_env):
    submit = _Resp(200, {"request_id": "abc", "status_url": "https://x/s"})
    fake = _FakeClient(submit, [_Resp(200, {"status": "failed"})])
    prov = HiggsfieldVideoProvider(client=fake)
    prov._poll_interval = 0
    with pytest.raises(VideoGenerationError):
        prov.generate(_study_set())


def test_bad_credentials_raise(higgsfield_env):
    submit = _Resp(401, {})
    with pytest.raises(VideoGenerationError):
        HiggsfieldVideoProvider(client=_FakeClient(submit, [])).generate(_study_set())


def test_missing_credentials_raise(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER", "higgsfield")
    monkeypatch.setenv("HIGGSFIELD_CREDENTIALS", "")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(VideoGenerationError):
            HiggsfieldVideoProvider()
    finally:
        config.get_settings.cache_clear()


def test_stub_provider_is_default():
    # Default env (stub) returns a ready placeholder with no URL, no network.
    asset = video.generate_video_asset(_study_set())
    assert asset["provider"] == "stub"
    assert asset["status"] == "ready"
    assert asset["url"] is None
