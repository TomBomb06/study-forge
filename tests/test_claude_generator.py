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


def test_claude_rejects_empty_content():
    # No usable flashcards at all -> even the lenient schema rejects it.
    bad = dict(VALID_PAYLOAD, flashcards=[])
    client = _FakeClient(json.dumps(bad), json.dumps(bad))
    gen = ClaudeGenerator(client=client, model="test-model")
    with pytest.raises(GenerationError):
        gen.generate("source text", "bio.pdf")


def test_claude_coerces_odd_but_usable_output():
    # 3 choices + a weird test kind: should be accepted and cleaned up, not
    # thrown away. (answer_index is in range here — an out-of-range index is a
    # different case and is covered by the answer-key tests below, because
    # guessing at it teaches the student a wrong fact.)
    odd = dict(
        VALID_PAYLOAD,
        quiz=[{"question": "Q?", "choices": ["a", "b", "c"], "answer_index": 2, "explanation": "x"}],
        test=[{"kind": "multiple_choice", "question": "What?", "answer": "Thing"}],
    )
    gen = ClaudeGenerator(client=_FakeClient(json.dumps(odd)), model="test-model")
    result = gen.generate("source text", "bio.pdf")
    assert result.quiz[0].answer_index == 2  # in-range index preserved exactly
    assert len(result.quiz[0].choices) == 3  # 3 choices accepted
    assert result.test[0].kind == "short_answer"  # unknown kind normalized


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


# ---------------------------------------------------------------- answer keys
#
# The worst bug a revision app can have is confidently marking the WRONG choice
# as correct — the student drills the wrong fact and has no way to know. These
# lock down every way the answer key used to drift.

def _one(quiz_item):
    from app.pipeline.generate import _coerce
    out = _coerce({"title": "t", "summary": "s", "flashcards": [],
                   "quiz": [quiz_item], "test": [], "matching": []})
    if not out["quiz"]:
        return None
    q = out["quiz"][0]
    return q["choices"][q["answer_index"]]


def test_blank_choice_does_not_shift_the_answer_key():
    """Filtering out a blank option used to renumber the list without moving
    answer_index — so the choice after the right one became 'correct'."""
    assert _one({"question": "Powerhouse of the cell?",
                 "choices": ["", "Mitochondria", "Ribosome", "Nucleus"],
                 "answer_index": 1}) == "Mitochondria"


def test_duplicate_choices_do_not_shift_the_answer_key():
    assert _one({"question": "Capital of England?",
                 "choices": ["Paris", "Paris", "London", "Rome"],
                 "answer_index": 2}) == "London"


def test_out_of_range_index_drops_the_question_instead_of_guessing():
    """It used to snap to choice 0 and present it as the answer."""
    assert _one({"question": "Q?", "choices": ["a", "b"], "answer_index": 9}) is None


def test_whole_quiz_using_1_based_indexing_is_repaired():
    """Models sometimes count from 1. Detectable as a pattern across the whole
    quiz, so it can be corrected rather than guessed at per question."""
    from app.pipeline.generate import _coerce
    out = _coerce({"title": "t", "summary": "s", "flashcards": [], "test": [], "matching": [],
                   "quiz": [{"question": "A?", "choices": ["w", "x", "y", "z"], "answer_index": 4},
                            {"question": "B?", "choices": ["p", "q", "r", "s"], "answer_index": 2}]})
    picked = [q["choices"][q["answer_index"]] for q in out["quiz"]]
    assert picked == ["z", "q"], picked


def test_schema_refuses_a_quiz_question_with_no_identifiable_answer():
    import pytest
    from pydantic import ValidationError
    from app.schemas import QuizQuestion
    with pytest.raises(ValidationError):
        QuizQuestion(question="Q?", choices=["a", "b"], answer_index=7)


def test_schema_keeps_the_answer_when_cleaning_blank_choices():
    from app.schemas import QuizQuestion
    q = QuizQuestion(question="Q?", choices=["", "right", "wrong"], answer_index=1)
    assert q.choices[q.answer_index] == "right"
