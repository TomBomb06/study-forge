"""Stripe payment tests — webhook processing and checkout in dev + stripe
modes. Stripe is fully mocked; no network, no real charges."""

import uuid

from sqlalchemy import select

from app import config, payments
from app.db import SessionLocal
from app.models import User


def _new_user(client):
    email = f"pay-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    db = SessionLocal()
    uid = db.scalar(select(User).where(User.email == email)).id
    db.close()
    return uid, headers, email


def _user(uid):
    db = SessionLocal()
    u = db.get(User, uid)
    db.expunge(u)
    db.close()
    return u


# ---------- webhook event processing (pure logic) ----------

def test_webhook_plan_upgrade_completed(client):
    uid, _, _ = _new_user(client)
    event = {"type": "checkout.session.completed", "data": {"object": {
        "metadata": {"user_id": uid, "kind": "plan", "plan": "basic"},
        "customer": "cus_123"}}}
    db = SessionLocal()
    payments.process_event(event, db)
    db.close()
    u = _user(uid)
    assert u.plan == "basic"
    assert u.stripe_customer_id == "cus_123"


def test_webhook_pack_adds_credits(client):
    uid, _, _ = _new_user(client)
    event = {"type": "checkout.session.completed", "data": {"object": {
        "metadata": {"user_id": uid, "kind": "pack", "pack": "small", "videos": 5}}}}
    db = SessionLocal()
    payments.process_event(event, db)
    db.close()
    assert _user(uid).extra_video_credits == 5


def test_webhook_subscription_canceled_downgrades(client):
    uid, _, _ = _new_user(client)
    db = SessionLocal()
    u = db.get(User, uid)
    u.plan = "pro"
    u.stripe_customer_id = "cus_x"
    db.commit()
    db.close()
    event = {"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_x"}}}
    db = SessionLocal()
    payments.process_event(event, db)
    db.close()
    assert _user(uid).plan == "free"


def test_webhook_unknown_user_is_ignored(client):
    event = {"type": "checkout.session.completed", "data": {"object": {
        "metadata": {"user_id": "does-not-exist", "kind": "plan", "plan": "pro"}}}}
    db = SessionLocal()
    payments.process_event(event, db)  # must not raise
    db.close()


# ---------- checkout endpoint: dev mode (default) ----------

def test_checkout_plan_dev_applies_instantly(client):
    _, headers, _ = _new_user(client)
    r = client.post("/billing/checkout/plan", headers=headers, json={"plan": "basic"})
    assert r.status_code == 200
    assert r.json()["mode"] == "applied"
    assert r.json()["video"]["monthly_quota"] == 10


def test_checkout_pack_dev_applies_instantly(client):
    _, headers, _ = _new_user(client)
    r = client.post("/billing/checkout/pack", headers=headers, json={"pack": "medium"})
    assert r.json()["mode"] == "applied"
    assert r.json()["video"]["extra_credits"] == 15


# ---------- checkout endpoint: stripe mode (mocked) ----------

def test_checkout_plan_stripe_returns_redirect(client, monkeypatch):
    _, headers, _ = _new_user(client)
    monkeypatch.setenv("BILLING_PROVIDER", "stripe")
    config.get_settings.cache_clear()
    monkeypatch.setattr(payments, "create_plan_checkout", lambda user, plan: "https://checkout.stripe.test/abc")
    try:
        r = client.post("/billing/checkout/plan", headers=headers, json={"plan": "pro"})
        assert r.json()["mode"] == "redirect"
        assert r.json()["url"].startswith("https://checkout.stripe")
    finally:
        config.get_settings.cache_clear()


def test_webhook_endpoint_processes_event(client, monkeypatch):
    uid, _, _ = _new_user(client)
    event = {"type": "checkout.session.completed", "data": {"object": {
        "metadata": {"user_id": uid, "kind": "plan", "plan": "pro"}, "customer": "cus_9"}}}
    monkeypatch.setattr(payments, "verify_event", lambda payload, sig: event)
    r = client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "t"})
    assert r.status_code == 200
    assert _user(uid).plan == "pro"
