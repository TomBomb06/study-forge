"""Tests for the Claude generation path using a fake client — no real API
calls, no cost. These prove parsing, schema validation, and the
retry-once-then-fail contract without needing an API key."""

import json
import types

import pytest

from app.pipeline.generate import ClaudeGenerator, GenerationError
from app.schemas import StudySetContent

VALID_PAYLOAD = {
    "title": "Photosynthesis Basics",
    "summary": "Photosynthesis is how plants convert light into chemical energy. "
    "Key stages include the light-dependent reactions and the Calvin cycle. "
    "Chlorophyll absorbs light; oxygen is released as a byproduct.",
    "flashcards": [
        {"front": "What pigment absorbs light?", "back": "Chlorophyll."},
        {"front": "Byproduct of photosynthesis?", "back": "Oxygen."},
        {"front": "Where does the Calvin cycle occur?", "back": "In the stroma."},
        {"front": "What does the Calvin cycle fix?", "back": "Carbon dioxide."},
        {"front": "Energy source for photosynthesis?", "back": "Sunlight."},
    ],
    "quiz": [
        {
            "question": "Which pigment absorbs light energy?",
            "choices": ["Chlorophyll", "Keratin", "Hemoglobin", "Melanin"],
            "answer_index": 0,
            "explanation": "Chlorophyll is the light-absorbing pigment.",
        },
        {
            "question": "What is released as a byproduct?",
            "choices": ["Nitrogen", "Oxygen", "Methane", "Hydrogen"],
            "answer_index": 1,
            "explanation": "Oxygen is produced during the light reactions.",
        },
        {
            "question": "What does the Calvin cycle fix?",
            "choices": ["Oxygen", "Water", "Carbon dioxide", "Glucose"],
            "answer_index": 2,
            "explanation": "The Calvin cycle fixes CO2 into sugars.",
        },
    ],
    "test": [
        {"kind": "true_false", "question": "Chlorophyll absorbs light.", "answer": "True"},
        {"kind": "fill_blank", "question": "The _____ cycle fixes carbon dioxide.", "answer": "Calvin"},
        {"kind": "short_answer", "question": "What is photosynthesis?", "answer": "Converting light to chemical energy."},
    ],
    "matching": [
        {"term": "Chlorophyll", "definition": "Light-absorbing pigment"},
        {"term": "Oxygen", "definition": "Byproduct of photosynthesis"},
        {"term": "Calvin cycle", "definition": "Fixes carbon dioxide into sugar"},
        {"term": "Stroma", "definition": "Where the Calvin cycle occurs"},
    ],
}


def _fake_response(text: str):
    block = types.SimpleNamespace(text=text)
    return types.SimpleNamespace(content=[block])


class _FakeClient:
    """Mimics anthropic.Anthropic.messages.create, returning queued texts."""

    def __init__(self, *texts):
        self._texts = list(texts)
        self.calls = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        return _fake_response(self._texts.pop(0))


def test_claude_parses_clean_json():
    client = _FakeClient(json.dumps(VALID_PAYLOAD))
    gen = ClaudeGenerator(client=client, model="test-model")
    result = gen.generate("source text", "bio.pdf")
    assert isinstance(result, StudySetContent)
    assert result.title == "Photosynthesis Basics"
    assert client.calls == 1


def test_claude_strips_code_fences_and_prose():
    messy = "Here is your study kit:\n```json\n" + json.dumps(VALID_PAYLOAD) + "\n```\nHope this helps!"
    gen = ClaudeGenerator(client=_FakeClient(messy), model="test-model")
    result = gen.generate("source text", "bio.pdf")
    assert len(result.flashcards) == 5


def test_claude_retries_once_then_succeeds():
    client = _FakeClient("not json at all", json.dumps(VALID_PAYLOAD))
    gen = ClaudeGenerator(client=client, model="test-model")
    result = gen.generate("source text", "bio.pdf")
    assert result.title == "Photosynthesis Basics"
    assert client.calls == 2  # first attempt failed, second succeeded


def test_claude_fails_after_two_bad_responses():
    client = _FakeClient("garbage one", "garbage two")
    gen = ClaudeGenerator(client=client, model="test-model")
    with pytest.raises(GenerationError):
        gen.generate("source text", "bio.pdf")
    assert client.calls == 2  # exactly two attempts, no infinite retry


def test_claude_rejects_schema_violation():
    # Only 1 flashcard — violates the min-5 schema rule; must be rejected.
    bad = dict(VALID_PAYLOAD, flashcards=[{"front": "q", "back": "a"}])
    client = _FakeClient(json.dumps(bad), json.dumps(bad))
    gen = ClaudeGenerator(client=client, model="test-model")
    with pytest.raises(GenerationError):
        gen.generate("source text", "bio.pdf")


def test_claude_wraps_sdk_errors():
    class _ExplodingClient:
        def __init__(self):
            self.messages = types.SimpleNamespace(create=self._boom)

        def _boom(self, **kwargs):
            raise RuntimeError("rate limit exceeded")

    gen = ClaudeGenerator(client=_ExplodingClient(), model="test-model")
    with pytest.raises(GenerationError) as exc:
        gen.generate("source text", "bio.pdf")
    assert "AI service returned an error" in str(exc.value)
