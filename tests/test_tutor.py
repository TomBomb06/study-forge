"""Premium tutor: mock-mode generation + plan gating on the endpoint."""

import uuid

from app.pipeline import tutor


# ---------- unit: tutor.explain (mock mode; GENERATOR=mock in tests) ----------

def test_review_mode_covers_missed_questions():
    items = [{"question": "What pigment absorbs light?",
              "correct": "Chlorophyll", "your_answer": "Mitochondria"}]
    out = tutor.explain("review", {"items": items}, "Chlorophyll absorbs light energy.")
    assert isinstance(out, str) and len(out) > 40
    assert "Chlorophyll" in out           # grounded in the correct answer
    assert "focus on next" in out.lower() # personalized-review structure


def test_each_mode_returns_markdown():
    for mode in ("question", "card", "notes"):
        payload = {"items": [{"question": "Q?", "correct": "A",
                              "front": "term", "back": "definition"}]}
        out = tutor.explain(mode, payload, "some notes")
        assert isinstance(out, str) and out.strip()


def test_unknown_mode_falls_back_safely():
    out = tutor.explain("nonsense", {"items": []}, "")
    assert isinstance(out, str) and out.strip()


# ---------- API: plan gating ----------

def _make_paid_user_with_set(email, plan="basic"):
    """Insert a study set owned by the freshly-signed-up user and set their plan."""
    from app.db import SessionLocal
    from app.models import StudySet, User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        user.plan = plan
        ss = StudySet(
            id=uuid.uuid4().hex, user_id=user.id, title="Bio",
            source_filename="notes.txt",
            summary="Chlorophyll absorbs light energy for photosynthesis.",
            flashcards=[], quiz=[], test=[], matching=[],
        )
        db.add(ss)
        db.commit()
        return ss.id
    finally:
        db.close()


def _signup(client):
    email = f"tutor-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return email, {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_free_user_is_blocked_with_402(client):
    email, headers = _signup(client)
    set_id = _make_paid_user_with_set(email, plan="free")
    r = client.post("/tutor/explain", headers=headers,
                    json={"set_id": set_id, "mode": "notes"})
    assert r.status_code == 402
    assert "Premium" in r.json()["detail"]


def test_paid_user_gets_explanation(client):
    email, headers = _signup(client)
    set_id = _make_paid_user_with_set(email, plan="basic")
    r = client.post("/tutor/explain", headers=headers, json={
        "set_id": set_id, "mode": "review",
        "items": [{"question": "What absorbs light?", "correct": "Chlorophyll",
                   "your_answer": "Stomata"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "review"
    assert body["explanation"].strip()


def test_paid_user_cannot_tutor_someone_elses_set(client):
    # User A (paid) owns a set; user B (paid) must not be able to use it.
    email_a, _ = _signup(client)
    set_id = _make_paid_user_with_set(email_a, plan="pro")
    email_b, headers_b = _signup(client)
    _make_paid_user_with_set(email_b, plan="pro")  # make B paid too
    r = client.post("/tutor/explain", headers=headers_b,
                    json={"set_id": set_id, "mode": "notes"})
    assert r.status_code == 404
