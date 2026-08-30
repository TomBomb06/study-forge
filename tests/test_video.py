"""Tests for the narrated cartoon-explainer video pipeline.

Nothing here touches the network or ffmpeg: the image provider, the
text-to-speech and the shell are all stubbed, so no API calls and no cost.
What is verified is the money-and-honesty logic — a placeholder must never be
sold as a real video — plus storyboard parsing and the file-serving guard.
"""

import json
import os
import types

import pytest

from app.pipeline import video
from app.pipeline.video import VideoGenerationError


def _study_set(summary=None):
    return types.SimpleNamespace(
        id="set123",
        title="Photosynthesis",
        summary=summary or (
            "Photosynthesis converts light energy into chemical energy. "
            "Chlorophyll inside the chloroplast absorbs photons. "
            "The Calvin cycle fixes carbon dioxide into a three carbon sugar. "
            "Stomata regulate gas exchange and water loss in the leaf. "
            "Light intensity and temperature are limiting factors."
        ),
    )


# ------------------------------------------------------------ is_live

def test_not_live_without_a_provider(monkeypatch):
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="stub", openai_api_key="k", higgsfield_credentials="c"))
    assert video.is_live() is False


def test_not_live_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="openai", openai_api_key="k", higgsfield_credentials=""))
    monkeypatch.setattr(video, "_ffmpeg", lambda: None)
    assert video.is_live() is False, "no ffmpeg means no video, whatever the key says"


def test_not_live_without_a_key(monkeypatch):
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="openai", openai_api_key="", higgsfield_credentials=""))
    monkeypatch.setattr(video, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    assert video.is_live() is False


def test_live_when_key_and_ffmpeg_present(monkeypatch):
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="openai", openai_api_key="k", higgsfield_credentials=""))
    monkeypatch.setattr(video, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    assert video.is_live() is True


# ------------------------------------------------------------ the stub

def test_stub_is_free_and_says_so(monkeypatch):
    monkeypatch.setattr(video, "is_live", lambda: False)
    asset = video.generate_video_asset(_study_set())
    assert asset["demo"] is True
    assert asset["url"] is None
    assert "isn't switched on" in asset["note"]
    assert asset["narration_script"]


# ------------------------------------------------------- storyboarding

def test_parses_a_clean_storyboard():
    raw = json.dumps([
        {"narration": "Plants turn sunlight into food.", "art": "a smiling leaf in the sun"},
        {"narration": "Chlorophyll absorbs the light.", "art": "green pigment glowing"},
    ])
    scenes = video._parse_scenes("Here you go:\n" + raw)
    assert len(scenes) == 2
    assert scenes[0]["narration"] == "Plants turn sunlight into food."
    assert scenes[0]["art"] == "a smiling leaf in the sun"


def test_drops_incomplete_scenes():
    raw = json.dumps([
        {"narration": "Good one.", "art": "a picture"},
        {"narration": "", "art": "no narration"},
        {"art": "missing narration entirely"},
        "not even an object",
    ])
    assert len(video._parse_scenes(raw)) == 1


def test_parse_survives_garbage():
    assert video._parse_scenes("the model apologised instead") == []
    assert video._parse_scenes("") == []


def test_fallback_still_produces_scenes():
    ss = _study_set()
    scenes = video._fallback_scenes(ss.title, ss.summary)
    assert scenes, "a missing Claude key must not mean a failed video"
    assert len(scenes) <= video.SCENES
    assert all(s["narration"] and s["art"] for s in scenes)


def test_storyboard_falls_back_when_claude_errors(monkeypatch):
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        anthropic_api_key="", generator_model="x"))
    scenes = video._storyboard(_study_set())
    assert scenes


# --------------------------------------------------- end-to-end assembly

class _FakeProvider(video._ImageProvider):
    def __init__(self):
        self.prompts = []

    def render(self, prompt, dest):
        self.prompts.append(prompt)
        with open(dest, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 512)


def test_generate_builds_a_video_and_returns_a_safe_url(monkeypatch, tmp_path):
    fake = _FakeProvider()
    monkeypatch.setattr(video, "is_live", lambda: True)
    monkeypatch.setattr(video, "_image_provider", lambda: fake)
    monkeypatch.setattr(video, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video, "_storyboard", lambda ss: [
        {"narration": "One.", "art": "a leaf"},
        {"narration": "Two.", "art": "the sun"},
    ])
    monkeypatch.setattr(video.tts, "is_enabled", lambda: False)
    monkeypatch.setattr(video, "video_dir", lambda: str(tmp_path))
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="openai", storage_dir=str(tmp_path)))

    written = {}

    def fake_run(cmd):
        out = cmd[-1]
        written[out] = True
        with open(out, "wb") as f:
            f.write(b"\0" * 4096)

    monkeypatch.setattr(video, "_run", fake_run)
    asset = video.generate_video_asset(_study_set())

    assert asset["status"] == "ready"
    assert asset["url"].startswith("/media/videos/")
    assert asset["url"].endswith(".mp4")
    assert "demo" not in asset, "a real video must never be labelled a demo"
    assert asset["scenes"] == ["One.", "Two."]
    # the art style is what keeps it a cartoon rather than photoreal
    assert all("cartoon" in p for p in fake.prompts)


def test_generate_refuses_to_return_an_empty_file(monkeypatch, tmp_path):
    monkeypatch.setattr(video, "is_live", lambda: True)
    monkeypatch.setattr(video, "_image_provider", lambda: _FakeProvider())
    monkeypatch.setattr(video, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video, "_storyboard", lambda ss: [{"narration": "One.", "art": "a leaf"}])
    monkeypatch.setattr(video.tts, "is_enabled", lambda: False)
    monkeypatch.setattr(video, "video_dir", lambda: str(tmp_path))
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="openai", storage_dir=str(tmp_path)))
    monkeypatch.setattr(video, "_run", lambda cmd: open(cmd[-1], "wb").close())
    with pytest.raises(VideoGenerationError):
        video.generate_video_asset(_study_set())


def test_missing_ffmpeg_is_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.setattr(video, "_ffmpeg", lambda: None)
    with pytest.raises(VideoGenerationError) as e:
        video._build([{"narration": "x", "art": "y"}], str(tmp_path), str(tmp_path / "o.mp4"))
    assert "ffmpeg" in str(e.value)


def test_ffmpeg_stderr_never_reaches_the_user(monkeypatch):
    class P:
        returncode = 1
        stdout = b""
        stderr = b"/data/storage/9f3a/secret.png: No such file"

    monkeypatch.setattr(video.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(VideoGenerationError) as e:
        video._run(["ffmpeg"])
    assert "/data/storage" not in str(e.value)


# ------------------------------------------------------ media URL guard

@pytest.mark.parametrize("name", [
    "../../etc/passwd", "..%2f..%2fetc", "abc.mp4", "", "x" * 32 + ".mp4",
    "0123456789abcdef0123456789abcdef.mov",
    "0123456789abcdef0123456789abcde.mp4",   # 31 chars
])
def test_only_a_32_hex_name_is_accepted(name):
    import re
    assert not re.fullmatch(r"[0-9a-f]{32}\.mp4", name)


def test_a_real_generated_name_is_accepted():
    import re
    import uuid
    assert re.fullmatch(r"[0-9a-f]{32}\.mp4", f"{uuid.uuid4().hex}.mp4")


# ------------------------------------------- a refused image is not fatal

class _RefusingProvider(video._ImageProvider):
    """Image models refuse prompts on war, medicine and crime. Study sets
    about all three are perfectly legitimate."""

    def __init__(self, refuse_containing):
        self.refuse = refuse_containing
        self.prompts = []

    def render(self, prompt, dest):
        self.prompts.append(prompt)
        if self.refuse in prompt:
            raise video.VideoGenerationError("Couldn't draw a scene.")
        with open(dest, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 512)


def _wire_build(monkeypatch, tmp_path, provider, scenes):
    monkeypatch.setattr(video, "is_live", lambda: True)
    monkeypatch.setattr(video, "_image_provider", lambda: provider)
    monkeypatch.setattr(video, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video, "_storyboard", lambda ss: scenes)
    monkeypatch.setattr(video.tts, "is_enabled", lambda: False)
    monkeypatch.setattr(video, "video_dir", lambda: str(tmp_path))
    monkeypatch.setattr(video, "get_settings", lambda: types.SimpleNamespace(
        video_provider="openai", storage_dir=str(tmp_path)))

    def fake_run(cmd):
        with open(cmd[-1], "wb") as f:
            f.write(b"\0" * 4096)

    monkeypatch.setattr(video, "_run", fake_run)


def test_a_refused_scene_retries_with_a_neutral_prompt(monkeypatch, tmp_path):
    provider = _RefusingProvider("bombing raid")
    _wire_build(monkeypatch, tmp_path, provider, [
        {"narration": "One.", "art": "a bombing raid over a city"},
        {"narration": "Two.", "art": "a map of Europe"},
    ])

    asset = video.generate_video_asset(_study_set())

    assert asset["status"] == "ready"
    assert asset["scenes"] == ["One.", "Two."]
    # the refused prompt was tried, then a neutral one for the same scene
    assert "bombing raid" in provider.prompts[0]
    assert "bombing raid" not in provider.prompts[1]
    assert "cartoon" in provider.prompts[1]


def test_a_scene_refused_twice_still_yields_a_video(monkeypatch, tmp_path):
    # refuses everything -- both the real prompt and the neutral one
    provider = _RefusingProvider("cartoon")
    _wire_build(monkeypatch, tmp_path, provider, [
        {"narration": "One.", "art": "anything at all"},
    ])

    asset = video.generate_video_asset(_study_set())

    assert asset["status"] == "ready"
    assert asset["url"].endswith(".mp4")


def test_fallback_scene_art_is_never_raw_notes():
    notes = ("The bombing of civilian centres killed many thousands of people. "
             "Casualty figures remain disputed by historians to this day. "
             "The campaign continued for several more months without result.")
    scenes = video._fallback_scenes("Strategic Bombing", notes)
    assert scenes
    for scene in scenes:
        assert scene["narration"], "the notes must still be narrated"
        assert "bombing" not in scene["art"].lower(), \
            "notes text in an art prompt gets the image refused"


def test_provider_error_text_never_reaches_the_user(monkeypatch):
    """A 400 from the image model carries policy prose and a request id.
    Neither belongs in front of a student."""
    import sys, types as _t

    class _Boom:
        class images:
            @staticmethod
            def generate(**kwargs):
                raise RuntimeError(
                    "400 moderation_blocked request_id req_0518e37193674dcc8bc3"
                )

    fake = _t.ModuleType("openai")
    fake.OpenAI = lambda api_key=None: _Boom
    monkeypatch.setitem(sys.modules, "openai", fake)

    provider = video.OpenAIImages.__new__(video.OpenAIImages)
    provider._key = "k"
    provider._model = "gpt-image-1"

    with pytest.raises(video.VideoGenerationError) as excinfo:
        provider.render("a leaf", "/tmp/does-not-matter.png")

    message = str(excinfo.value)
    assert "req_" not in message
    assert "moderation" not in message
