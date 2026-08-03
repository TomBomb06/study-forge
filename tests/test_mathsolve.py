"""Math photo solver: the free/paid split is the whole business model here,
so these tests guard it hard — a regression that leaks `steps` to free users
would quietly remove the reason to upgrade.
"""

import io

import pytest

from app import billing
from app.pipeline import mathsolve


# ---------------------------------------------------------------- engine

def test_local_solver_handles_linear_equations():
    r = mathsolve._local_solve("2x + 5 = 17")
    assert r is not None
    assert r["answer"] == "x = 6"
    assert len(r["steps"]) >= 2


def test_local_solver_handles_arithmetic():
    r = mathsolve._local_solve("12 * (3 + 4)")
    assert r is not None and r["answer"] == "84"


def test_local_solver_handles_negative_coefficient():
    r = mathsolve._local_solve("-3x + 6 = 0")
    assert r is not None and r["answer"] == "x = 2"


def test_local_solver_declines_prose():
    assert mathsolve._local_solve("what is the capital of France") is None


def test_solve_text_falls_back_without_api_key():
    out = mathsolve.solve_text("2x + 5 = 17")
    assert out["answer"] == "x = 6"
    assert out["steps"] and out["topic"]


def test_never_invents_an_answer_when_ai_is_unavailable():
    """The single most important test in this file.

    With no AI key, anything the local solver can't handle must raise — not
    return a plausible-looking guess. A student would hand that in.
    """
    with pytest.raises(mathsolve.MathError):
        mathsolve.solve_text("find the area under y = sin(x) from 0 to pi")
    with pytest.raises(mathsolve.MathError):
        mathsolve.solve_image(b"\x89PNG fake bytes", "image/png")


def test_solve_text_rejects_empty():
    with pytest.raises(mathsolve.MathError):
        mathsolve.solve_text("   ")


def test_solve_image_rejects_empty_and_oversize():
    with pytest.raises(mathsolve.MathError):
        mathsolve.solve_image(b"", "image/png")
    with pytest.raises(mathsolve.MathError):
        mathsolve.solve_image(b"x" * (mathsolve.MAX_IMAGE_BYTES + 1), "image/png")


def test_coerce_tolerates_code_fences():
    d = mathsolve._coerce('```json\n{"answer":"x = 2","steps":["a"]}\n```')
    assert d["answer"] == "x = 2"


def test_clean_rejects_no_problem_marker():
    with pytest.raises(mathsolve.MathError):
        mathsolve._clean({"error": "no_problem"})


def test_clean_requires_an_answer():
    with pytest.raises(mathsolve.MathError):
        mathsolve._clean({"answer": "", "steps": ["a"]})


def test_media_type_mapping():
    assert mathsolve.media_type_for(".JPG") == "image/jpeg"
    assert mathsolve.media_type_for(".pdf") is None


def test_teaser_truncates_long_first_step():
    long_step = "x" * 200
    assert len(mathsolve.teaser([long_step])) <= 90


# ---------------------------------------------------------------- gating

def _png() -> bytes:
    # 1x1 PNG — enough to exercise the route; the engine is mocked without a key.
    return (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _signup(client):
    import uuid
    email = f"math-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return email, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _set_plan(email, plan):
    from app.db import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).one()
        u.plan = plan
        db.commit()
    finally:
        db.close()


def _remaining(email):
    from app.db import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).one()
        return billing.sets_status(u)["remaining"]
    finally:
        db.close()


def test_free_user_gets_answer_but_not_steps(client):
    _, headers = _signup(client)
    r = client.post("/math/solve-text", json={"problem": "2x + 5 = 17"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "x = 6"      # the hook is free
    assert body["locked"] is True
    assert body["steps"] == []            # the method is not
    assert body["teaser"] and body["upgrade"]
    assert body["step_count"] >= 2        # we still tell them how much they're missing


@pytest.mark.parametrize("plan", ["basic", "pro"])
def test_paid_user_gets_full_steps(client, plan):
    email, headers = _signup(client)
    _set_plan(email, plan)
    r = client.post("/math/solve-text", json={"problem": "2x + 5 = 17"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["locked"] is False
    assert len(body["steps"]) >= 2
    assert body["check"]
    assert "upgrade" not in body


def test_photo_route_fails_gracefully_without_ai(client):
    """No AI key in the test env: the route must return a clean, honest error
    rather than a fabricated answer."""
    _, headers = _signup(client)
    r = client.post("/math/solve",
                    files={"file": ("problem.png", io.BytesIO(_png()), "image/png")},
                    headers=headers)
    assert r.status_code == 422
    assert "available" in r.json()["detail"].lower()


def test_photo_route_rejects_non_image(client):
    _, headers = _signup(client)
    r = client.post("/math/solve",
                    files={"file": ("notes.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
                    headers=headers)
    assert r.status_code == 422


def test_solving_does_not_consume_study_set_quota(client):
    email, headers = _signup(client)
    before = _remaining(email)
    for _ in range(3):
        assert client.post("/math/solve-text", json={"problem": "1 + 1"},
                           headers=headers).status_code == 200
    assert _remaining(email) == before


def test_math_requires_auth(client):
    assert client.post("/math/solve-text", json={"problem": "1+1"}).status_code in (401, 403)


def test_throttle_blocks_runaway_use(client, monkeypatch):
    from app.routers import math as math_router
    _, headers = _signup(client)
    math_router._hits.clear()
    monkeypatch.setattr(math_router, "THROTTLE_MAX", 3)
    for _ in range(3):
        assert client.post("/math/solve-text", json={"problem": "1 + 1"},
                           headers=headers).status_code == 200
    assert client.post("/math/solve-text", json={"problem": "1 + 1"},
                       headers=headers).status_code == 429
    math_router._hits.clear()


def test_throttle_ceiling_is_far_above_real_homework_use():
    """Guard against someone 'tidying' this into a product cap."""
    from app.routers import math as math_router
    assert math_router.THROTTLE_MAX >= 50


# ---------------------------------------------------------------- pricing

def test_free_plan_is_five_sets():
    assert billing.PLANS["free"]["monthly_sets"] == 5
