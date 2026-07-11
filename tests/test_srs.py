"""Spaced-repetition + progress tests."""

import time
import types
from datetime import datetime, timedelta, timezone

from app import srs


def _ss(level=0, next_review=None):
    return types.SimpleNamespace(
        review_level=level, next_review=next_review, last_reviewed=None
    )


def test_level_up_on_high_score():
    assert srs.next_level(0, 9, 10) == 1
    assert srs.next_level(4, 10, 10) == 5
    assert srs.next_level(5, 10, 10) == 5  # capped


def test_level_holds_on_medium_score():
    assert srs.next_level(2, 6, 10) == 2


def test_level_down_on_low_score():
    assert srs.next_level(3, 2, 10) == 2
    assert srs.next_level(0, 0, 10) == 0  # floored


def test_apply_review_sets_schedule():
    ss = _ss(level=0)
    srs.apply_review(ss, 10, 10)
    assert ss.review_level == 1
    assert ss.next_review > datetime.now(timezone.utc)
    assert ss.last_reviewed is not None


def test_is_due():
    assert srs.is_due(_ss(next_review=None)) is True
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert srs.is_due(_ss(next_review=past)) is True
    assert srs.is_due(_ss(next_review=future)) is False


def test_mastery_labels():
    assert srs.mastery_label(0) == "Learning"
    assert srs.mastery_label(5) == "Mastered"


# ---------- integration ----------

def _make_set(client, headers):
    text = (
        "Photosynthesis is the process by which green plants convert sunlight into "
        "chemical energy. Chlorophyll is the pigment that absorbs light energy. "
        "The Calvin cycle fixes carbon dioxide into glucose using ATP. "
        "Cellular respiration releases energy stored in glucose. Mitochondria host it. "
        "Oxygen is produced as a byproduct. Stomata regulate gas exchange in leaves."
    )
    r = client.post("/uploads/text", headers=headers, json={"content": text, "title": "Bio"})
    jid = r.json()["id"]
    for _ in range(50):
        j = client.get(f"/jobs/{jid}", headers=headers).json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "completed"
    return j["study_set_id"]


def test_new_set_is_due_and_learning(client, auth_headers):
    _make_set(client, auth_headers)
    sets = client.get("/study-sets", headers=auth_headers).json()
    assert sets[0]["due"] is True
    assert sets[0]["mastery"] == "Learning"


def test_quiz_advances_schedule_and_progress(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    # Ace the quiz -> level up -> not due, mastery improves.
    r = client.post(
        f"/study-sets/{ss_id}/quiz/attempts",
        headers=auth_headers,
        json={"score": 10, "total": 10},
    )
    assert r.status_code == 201
    sets = client.get("/study-sets", headers=auth_headers).json()
    assert sets[0]["due"] is False
    assert sets[0]["review_level"] == 1

    prog = client.get("/me/progress", headers=auth_headers).json()
    assert prog["totals"]["quizzes_taken"] == 1
    assert prog["totals"]["avg_score"] == 100
    assert len(prog["trend"]) == 1
    assert prog["sets"][0]["mastery"] in ("Familiar", "Learning")


def test_low_score_keeps_set_due_soon(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    client.post(
        f"/study-sets/{ss_id}/quiz/attempts",
        headers=auth_headers,
        json={"score": 1, "total": 10},
    )
    # Level stays 0; next review is 1 day out, so not due immediately.
    sets = client.get("/study-sets", headers=auth_headers).json()
    assert sets[0]["review_level"] == 0
