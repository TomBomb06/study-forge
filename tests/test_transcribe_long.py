"""Long-lecture transcription: the file must never be sent over the API's
25 MB ceiling, and a split recording must come back in the right order."""
import os

import pytest

from app.pipeline import transcribe as T
from app.pipeline.extract import ExtractionError


class _Resp:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """Records what it was asked to transcribe and rejects oversized files
    exactly the way the real API does."""

    def __init__(self, texts=None):
        self.sizes = []
        self.names = []
        self._texts = list(texts or [])
        self.audio = self

    @property
    def transcriptions(self):
        return self

    def create(self, model=None, file=None):
        data = file.read()
        self.sizes.append(len(data))
        self.names.append(os.path.basename(file.name))
        if len(data) > 25 * 1024 * 1024:
            raise AssertionError("sent a file over the API's 25 MB limit")
        return _Resp(self._texts.pop(0) if self._texts else "some transcript text")


def _write(path, size):
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    return str(path)


def test_small_file_is_sent_as_is(tmp_path, monkeypatch):
    p = _write(tmp_path / "short.mp3", 1024)
    c = FakeClient()
    monkeypatch.setattr(T, "_compress", lambda *a, **k: pytest.fail("should not compress"))
    assert T._transcribe_file(p, _settings(), c) == "some transcript text"
    assert c.sizes == [1024]


def test_big_file_is_compressed_before_upload(tmp_path, monkeypatch):
    big = _write(tmp_path / "lecture.m4a", T.MAX_UPLOAD_BYTES + 5000)
    small_bytes = 2 * 1024 * 1024

    def fake_compress(path, work):
        return _write(os.path.join(work, "audio.ogg"), small_bytes)

    monkeypatch.setattr(T, "_compress", fake_compress)
    monkeypatch.setattr(T, "_segment", lambda *a, **k: pytest.fail("should not need to split"))
    c = FakeClient()
    T._transcribe_file(big, _settings(), c)
    assert c.sizes == [small_bytes]


def test_very_long_file_is_split_and_rejoined_in_order(tmp_path, monkeypatch):
    big = _write(tmp_path / "double.m4a", T.MAX_UPLOAD_BYTES + 5000)

    def fake_compress(path, work):
        return _write(os.path.join(work, "audio.ogg"), T.MAX_UPLOAD_BYTES + 1)

    def fake_segment(path, work):
        # deliberately created out of order — the code must sort them
        c = _write(os.path.join(work, "seg0002.ogg"), 1000)
        a = _write(os.path.join(work, "seg0000.ogg"), 1000)
        b = _write(os.path.join(work, "seg0001.ogg"), 1000)
        return sorted([c, a, b])

    monkeypatch.setattr(T, "_compress", fake_compress)
    monkeypatch.setattr(T, "_segment", fake_segment)
    c = FakeClient(["first part", "second part", "third part"])
    out = T._transcribe_file(big, _settings(), c)
    assert out == "first part second part third part"
    assert c.names == ["seg0000.ogg", "seg0001.ogg", "seg0002.ogg"]


def test_split_needed_but_no_ffmpeg_gives_a_usable_message(tmp_path, monkeypatch):
    big = _write(tmp_path / "huge.m4a", T.MAX_UPLOAD_BYTES + 5000)
    monkeypatch.setattr(T, "_ffmpeg", lambda: None)
    c = FakeClient()
    with pytest.raises(ExtractionError) as e:
        T._transcribe_file(big, _settings(), c)
    msg = str(e.value)
    assert "too long" in msg.lower()
    assert str(tmp_path) not in msg          # never leak server paths
    assert c.sizes == []                      # and never send the oversized file


def test_ffmpeg_stderr_never_reaches_the_user(monkeypatch):
    class P:
        returncode = 1
        stdout = b""
        stderr = b"/data/storage/9f3a/secret-path.m4a: Invalid data found"

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(ExtractionError) as e:
        T._run(["ffmpeg"])
    assert "/data/storage" not in str(e.value)


def _settings():
    class S:
        whisper_model = "whisper-1"
    return S()
