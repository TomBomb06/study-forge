"""Topic mode: a short pasted 'topic' is researched into a full study set
instead of being rejected for being too short."""

from app.pipeline import generate, jobs


def test_short_paste_is_detected_as_topic():
    text, title, is_topic = jobs._resolve_text(
        file_path=None, ext=None, raw_text="The American Revolution",
        url=None, youtube_url=None, media_path=None,
    )
    assert is_topic is True
    assert text == "The American Revolution"


def test_long_paste_is_treated_as_material():
    long_notes = "Photosynthesis is a process. " * 20  # well over the topic cutoff
    text, title, is_topic = jobs._resolve_text(
        file_path=None, ext=None, raw_text=long_notes,
        url=None, youtube_url=None, media_path=None,
    )
    assert is_topic is False


def test_empty_paste_still_errors():
    import pytest
    from app.pipeline.extract import ExtractionError
    with pytest.raises(ExtractionError):
        jobs._resolve_text(file_path=None, ext=None, raw_text=" ",
                           url=None, youtube_url=None, media_path=None)


def test_topic_generation_returns_valid_set():
    # Mock generator (tests run GENERATOR=mock) must still yield a schema-valid
    # set from a bare topic — never crash on too-little-text.
    content = generate.generate_study_set("The American Revolution", "topic", topic=True)
    assert content.title
    assert content.summary
    assert len(content.flashcards) >= 1
    assert len(content.quiz) >= 1 and len(content.quiz[0].choices) >= 2
    assert len(content.test) >= 1
    assert len(content.matching) >= 1


def test_topic_end_to_end_via_text_upload(client, auth_headers):
    r = client.post("/uploads/text", headers=auth_headers,
                    json={"content": "Photosynthesis", "title": None})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    # Background task runs inline in TestClient; the job should complete.
    j = client.get(f"/jobs/{job_id}", headers=auth_headers).json()
    assert j["status"] == "completed", j
    ss = client.get(f"/study-sets/{j['study_set_id']}", headers=auth_headers).json()
    assert ss["quiz"] and ss["flashcards"] and ss["summary"]
