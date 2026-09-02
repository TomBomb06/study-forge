"""Math photo solver.

The free/paid split is the business model, so these tests guard both edges of
it. Free users must always get a complete, usable solution — a regression that
withholds the working turns this back into a hostage-taking solver. Paid users
must get the reasoning and the alternative methods — a regression that leaks
those removes the reason to upgrade. Both directions are failures.
"""

import io

import pytest

from app import billing
from app.pipeline import mathsolve


# ---------------------------------------------------------------- engine

def _only_steps(result):
    assert len(result["methods"]) == 1
    return result["methods"][0]["steps"]


def test_local_solver_handles_linear_equations():
    r = mathsolve._local_solve("2x + 5 = 17")
    assert r is not None
    assert r["answer"] == "x = 6"
    assert len(_only_steps(r)) >= 2


def test_local_solver_explains_why_not_just_what():
    """The `why` is what Premium sells, so the offline path has to produce real
    text for it — otherwise the paid tier is empty whenever AI is unavailable."""
    steps = _only_steps(mathsolve._local_solve("2x + 5 = 17"))
    assert all(s["do"] for s in steps)
    assert all(len(s["why"]) > 20 for s in steps)


def test_local_solver_names_its_method():
    m = mathsolve._local_solve("2x + 5 = 17")["methods"][0]
    assert m["name"] and m["tagline"]


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
    assert out["methods"] and out["topic"]


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


def test_clean_accepts_the_new_methods_shape():
    out = mathsolve._clean({
        "answer": "x = 3", "problem": "x + 1 = 4", "topic": "Linear equations",
        "methods": [
            {"name": "Balancing", "tagline": "The usual way",
             "steps": [{"do": "Subtract 1.", "why": "Keeps it balanced."}]},
            {"name": "Inspection", "tagline": "For small numbers",
             "steps": [{"do": "Ask what plus 1 makes 4.", "why": "Faster in your head."}]},
        ],
    })
    assert [m["name"] for m in out["methods"]] == ["Balancing", "Inspection"]
    assert out["methods"][0]["steps"][0]["why"] == "Keeps it balanced."


def test_clean_wraps_the_legacy_flat_steps_shape():
    """A model that answers in the old format must not lose its working."""
    out = mathsolve._clean({"answer": "x = 2", "steps": ["Halve both sides.", "x = 2."]})
    assert len(out["methods"]) == 1
    assert len(out["methods"][0]["steps"]) == 2
    assert out["methods"][0]["steps"][0]["do"] == "Halve both sides."


def test_clean_drops_methods_that_are_the_same_method_reworded():
    """Padding the list makes the paid tier feel like a con. Duplicates by name
    or by identical working are dropped."""
    out = mathsolve._clean({
        "answer": "x = 3",
        "methods": [
            {"name": "Factoring", "steps": [{"do": "Factor it.", "why": "a"}]},
            {"name": "factoring ", "steps": [{"do": "Something else.", "why": "b"}]},
            {"name": "Formula", "steps": [{"do": "Factor it.", "why": "c"}]},
        ],
    })
    assert len(out["methods"]) == 1


def test_clean_caps_the_method_count():
    out = mathsolve._clean({
        "answer": "x = 1",
        "methods": [{"name": f"Way {i}", "steps": [{"do": f"Do {i}.", "why": "w"}]}
                    for i in range(9)],
    })
    assert len(out["methods"]) <= mathsolve.MAX_METHODS


def test_clean_drops_a_method_with_no_steps():
    out = mathsolve._clean({
        "answer": "x = 1",
        "methods": [{"name": "Real", "steps": [{"do": "Do it.", "why": "w"}]},
                    {"name": "Empty", "steps": []}],
    })
    assert [m["name"] for m in out["methods"]] == ["Real"]


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
    assert len(mathsolve.teaser("x" * 200)) <= 90


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


def _solve(client, headers, problem="2x + 5 = 17"):
    r = client.post("/math/solve-text", json={"problem": problem}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_free_user_gets_a_complete_working_solution(client):
    """The load-bearing test for the whole model.

    A free user must be able to finish their homework and understand what they
    did. Answer, check, and the first method's working are all free. If this
    ever fails we are back to holding the working hostage.
    """
    _, headers = _signup(client)
    body = _solve(client, headers)
    assert body["answer"] == "x = 6"
    assert body["check"]
    first = body["methods"][0]
    assert first["locked"] is False
    assert len(first["steps"]) >= 2
    assert all(s["do"] for s in first["steps"])


def test_free_user_does_not_get_the_reasoning(client):
    """`why` is what Premium sells. It must be absent, and flagged as withheld
    so the UI can offer the upgrade in the right place."""
    _, headers = _signup(client)
    body = _solve(client, headers)
    steps = body["methods"][0]["steps"]
    assert all(s["why"] == "" for s in steps)
    assert any(s["why_locked"] for s in steps)
    assert body["why_locked"] is True
    assert body["upgrade_why"]


def test_free_user_sees_the_names_of_the_methods_they_cannot_open(client, monkeypatch):
    """Knowing another way exists is the pitch; it is not the product."""
    from app.routers import math as math_router
    monkeypatch.setattr(math_router, "FREE_METHODS", 1)
    _, headers = _signup(client)

    def fake_solve(problem):
        return {
            "problem": problem, "answer": "x = 3", "topic": "Quadratics",
            "check": "Substitute it back in.",
            "methods": [
                {"name": "Factoring", "tagline": "Fastest when it factors",
                 "steps": [{"do": "Factor it.", "why": "Zero product property."}]},
                {"name": "Quadratic formula", "tagline": "Always works",
                 "steps": [{"do": "Apply the formula.", "why": "Derived by completing the square."}]},
            ],
        }
    monkeypatch.setattr(math_router.mathsolve, "solve_text", fake_solve)

    body = _solve(client, headers)
    assert body["method_count"] == 2
    assert body["locked_methods"] == 1
    open_m, locked_m = body["methods"]
    assert open_m["locked"] is False and open_m["steps"]
    assert locked_m["locked"] is True
    assert locked_m["name"] == "Quadratic formula"   # named
    assert locked_m["tagline"]                       # and pitched
    assert locked_m["steps"] == []                   # but not worked
    assert locked_m["step_count"] == 1               # honest about the size
    assert body["upgrade_methods"]


@pytest.mark.parametrize("plan", ["basic", "pro"])
def test_paid_user_gets_the_reasoning(client, plan):
    email, headers = _signup(client)
    _set_plan(email, plan)
    body = _solve(client, headers)
    steps = body["methods"][0]["steps"]
    assert all(s["why"] for s in steps)
    assert not any(s["why_locked"] for s in steps)
    assert body["why_locked"] is False
    assert body["upgrade_why"] == ""


@pytest.mark.parametrize("plan", ["basic", "pro"])
def test_paid_user_gets_every_method_worked_out(client, plan, monkeypatch):
    from app.routers import math as math_router
    email, headers = _signup(client)
    _set_plan(email, plan)

    def fake_solve(problem):
        return {
            "problem": problem, "answer": "x = 3", "topic": "Quadratics", "check": "c",
            "methods": [
                {"name": "Factoring", "tagline": "t",
                 "steps": [{"do": "Factor it.", "why": "Zero product property."}]},
                {"name": "Quadratic formula", "tagline": "t",
                 "steps": [{"do": "Apply the formula.", "why": "It always works."}]},
            ],
        }
    monkeypatch.setattr(math_router.mathsolve, "solve_text", fake_solve)

    body = _solve(client, headers)
    assert body["locked_methods"] == 0
    assert all(m["steps"] and not m["locked"] for m in body["methods"])


def test_a_step_with_no_reasoning_is_not_sold_as_locked(client, monkeypatch):
    """A paywall over nothing is the fastest way to lose someone's trust. If the
    model gave no `why` for a step, that step must not show a lock."""
    from app.routers import math as math_router
    _, headers = _signup(client)

    def fake_solve(problem):
        return {"problem": problem, "answer": "x = 3", "topic": "t", "check": "c",
                "methods": [{"name": "M", "tagline": "t", "steps": [
                    {"do": "Do the thing.", "why": ""},
                ]}]}
    monkeypatch.setattr(math_router.mathsolve, "solve_text", fake_solve)

    body = _solve(client, headers)
    assert body["methods"][0]["steps"][0]["why_locked"] is False
    assert body["why_locked"] is False
    assert body["upgrade_why"] == ""


def test_the_split_is_configurable_from_one_place(client, monkeypatch):
    """Guard the escape hatch documented in the router: two constants, no more."""
    from app.routers import math as math_router
    monkeypatch.setattr(math_router, "FREE_WHY", True)
    _, headers = _signup(client)
    body = _solve(client, headers)
    assert all(s["why"] for s in body["methods"][0]["steps"])
    assert body["why_locked"] is False


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
