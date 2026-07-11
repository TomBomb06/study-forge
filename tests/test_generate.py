import pytest

from app.pipeline.generate import GenerationError, generate_study_set
from app.schemas import StudySetContent
from tests.conftest import SAMPLE_TEXT


def test_mock_generation_is_schema_valid():
    content = generate_study_set(SAMPLE_TEXT, "biology_notes.pdf")
    assert isinstance(content, StudySetContent)
    assert len(content.flashcards) >= 5
    assert len(content.quiz) >= 3
    for q in content.quiz:
        assert len(q.choices) == 4
        assert 0 <= q.answer_index <= 3
        # The correct choice must actually be one of the four options.
        assert q.choices[q.answer_index] in q.choices


def test_mock_generates_test_and_matching():
    content = generate_study_set(SAMPLE_TEXT, "biology_notes.pdf")
    assert len(content.test) >= 3
    assert len(content.matching) >= 4
    for t in content.test:
        assert t.kind in ("true_false", "fill_blank", "short_answer")
        assert t.question and t.answer
    for m in content.matching:
        assert m.term and m.definition


def test_generation_rejects_thin_input():
    with pytest.raises(GenerationError):
        generate_study_set("Too short.", "x.txt")


def test_title_derived_from_filename():
    content = generate_study_set(SAMPLE_TEXT, "cell_biology_chapter.pdf")
    assert "Cell Biology Chapter" in content.title
