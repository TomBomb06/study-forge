"""Unit economics: free study-set quota, paid = unlimited, transcription
gating, failure refunds, and the cheap-model-for-free split."""

import types
import uuid

from app import billing


def _u(plan="free"):
    return types.SimpleNamespace(
        id="u1", plan=plan, usage_period="", videos_used=0, sets_used=0,
    )


# ---------- billing meter ----------

FREE_CAP = billing.PLANS["free"]["monthly_sets"]


def test_free_plan_has_a_set_cap():
    u = _u("free")
    st = billing.sets_status(u)
    assert st["monthly_sets"] == FREE_CAP and st["remaining"] == FREE_CAP and st["can_create"]
    assert st["unlimited"] is False
    # Deliberate band: enough sets to prove the product works, few enough that a
    # real student hits the wall inside a week. Below 3 people bounce before
    # they're hooked; above 10 we're paying AI costs for users who never convert.
    assert 3 <= FREE_CAP <= 10


def test_consume_and_exhaust():
    u = _u("free")
    for _ in range(FREE_CAP):
        billing.consume_set(u)
    st = billing.sets_status(u)
    assert st["remaining"] == 0 and st["can_create"] is False


def test_refund_gives_a_credit_back():
    u = _u("free")
    billing.consume_set(u)
    billing.consume_set(u)
    billing.refund_set(u)
    assert billing.sets_status(u)["sets_used"] == 1


def test_pro_is_unlimited():
    u = _u("pro")
    st = billing.sets_status(u)
    assert st["unlimited"] is True and st["monthly_sets"] >= billing.UNLIMITED


def test_transcription_is_paid_only():
    assert billing.can_transcribe(_u("free")) is False
    assert billing.can_transcribe(_u("basic")) is True
    assert billing.can_transcribe(_u("pro")) is True


# ---------- model split ----------

def test_free_uses_cheap_model_paid_uses_best():
    from app.pipeline import jobs
    from app.config import get_settings
    s = get_settings()
    assert jobs._model_for(_u("free")) == s.claude_model_free
    assert jobs._model_for(_u("basic")) == s.claude_model
    assert jobs._model_for(_u("pro")) == s.claude_model


# ---------- API enforcement ----------

def _signup(client):
    email = f"econ-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201
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


def test_free_user_blocked_after_quota(client):
    email, headers = _signup(client)
    # Burn the free allowance directly, then a create attempt should 402.
    from app.db import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).one()
        billing.ensure_period(u)
        u.sets_used = FREE_CAP
        db.commit()
    finally:
        db.close()
    r = client.post("/uploads/text", headers=headers, json={"content": "The French Revolution"})
    assert r.status_code == 402
    assert "upgrade" in r.json()["detail"].lower()


def test_usage_exposes_sets_and_transcription(client):
    _, headers = _signup(client)
    u = client.get("/me/usage", headers=headers).json()
    assert u["sets"]["monthly_sets"] == FREE_CAP
    assert u["can_transcribe"] is False
