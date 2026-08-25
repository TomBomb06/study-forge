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


# ---------- regressions: the money bugs found 2026-08-14 ----------
#
# Each test below locks in a fix for a bug that either charged a customer twice
# or silently took away a plan they were paying for.

def _sub_event(etype, customer, status=None, sub_id="sub_live", plan=None):
    obj = {"customer": customer, "id": sub_id}
    if status:
        obj["status"] = status
    if plan:
        obj["metadata"] = {"plan": plan}
    return {"id": f"evt_{uuid.uuid4().hex[:12]}", "type": etype,
            "data": {"object": obj}}


def _ids():
    """Unique Stripe ids so tests can't collide on the same customer."""
    n = uuid.uuid4().hex[:10]
    return f"cus_{n}", f"sub_{n}"


def _paid(uid, customer="cus_live", sub="sub_live", plan="basic"):
    db = SessionLocal()
    u = db.get(User, uid)
    u.plan = plan
    u.stripe_customer_id = customer
    u.stripe_subscription_id = sub
    db.commit()
    db.close()


def _apply(event):
    db = SessionLocal()
    payments.process_event(event, db)
    db.close()


def test_past_due_does_not_downgrade_a_paying_customer(client):
    """Stripe marks a subscription past_due on the FIRST failed retry and then
    keeps trying for weeks. Revoking the plan there demoted real customers."""
    uid, _, _ = _new_user(client)
    cus, sub = _ids()
    _paid(uid, cus, sub)
    _apply(_sub_event("customer.subscription.updated", cus, "past_due", sub))
    assert _user(uid).plan == "basic"


def test_recovered_payment_restores_the_plan(client):
    uid, _, _ = _new_user(client)
    cus, sub = _ids()
    _paid(uid, cus, sub)
    _apply(_sub_event("customer.subscription.updated", cus, "past_due", sub))
    _apply(_sub_event("customer.subscription.updated", cus, "active", sub,
                      plan="basic"))
    assert _user(uid).plan == "basic"


def test_stale_subscription_event_cannot_downgrade(client):
    """A card that failed once leaves a dead subscription on the same customer.
    When Stripe later expires it, the live subscription must be untouched."""
    uid, _, _ = _new_user(client)
    cus, sub = _ids()
    _paid(uid, cus, sub)
    _apply(_sub_event("customer.subscription.deleted", cus,
                      sub_id=sub + "_abandoned"))
    assert _user(uid).plan == "basic"


def test_cancelling_the_live_subscription_still_downgrades(client):
    uid, _, _ = _new_user(client)
    cus, sub = _ids()
    _paid(uid, cus, sub)
    _apply(_sub_event("customer.subscription.deleted", cus, sub_id=sub))
    assert _user(uid).plan == "free"


def test_duplicate_event_delivery_does_not_double_credits(client):
    """Stripe redelivers events. The pack branch used to add credits each time."""
    uid, _, _ = _new_user(client)
    event = {"id": "evt_dupe_1", "type": "checkout.session.completed",
             "data": {"object": {"payment_status": "paid", "metadata": {
                 "user_id": uid, "kind": "pack", "pack": "large", "videos": 40}}}}
    _apply(event)
    _apply(event)
    _apply(event)
    assert _user(uid).extra_video_credits == 40


def test_plan_checkout_without_plan_metadata_never_sets_free(client):
    """meta.get("plan", "free") used to write a paying customer down to free."""
    uid, _, _ = _new_user(client)
    _paid(uid, plan="pro")
    _apply({"id": "evt_nometa", "type": "checkout.session.completed",
            "data": {"object": {"metadata": {"user_id": uid, "kind": "plan"},
                                "customer": "cus_live"}}})
    assert _user(uid).plan == "pro"


def test_plan_resolved_from_price_when_metadata_is_missing(client, monkeypatch):
    """Payment Links and dashboard-rebuilt sessions carry no metadata."""
    uid, _, _ = _new_user(client)
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_test")
    config.get_settings.cache_clear()
    try:
        _apply({"id": "evt_price", "type": "checkout.session.completed",
                "data": {"object": {
                    "mode": "subscription", "client_reference_id": uid,
                    "customer": "cus_pl", "subscription": "sub_pl",
                    "line_items": {"data": [{"price": {"id": "price_pro_test"}}]}}}})
        assert _user(uid).plan == "pro"
    finally:
        config.get_settings.cache_clear()


def test_unpaid_pack_session_grants_nothing(client):
    uid, _, _ = _new_user(client)
    _apply({"id": "evt_unpaid", "type": "checkout.session.completed",
            "data": {"object": {"payment_status": "unpaid", "metadata": {
                "user_id": uid, "kind": "pack", "videos": 40}}}})
    assert _user(uid).extra_video_credits == 0


def test_checkout_stores_the_subscription_id(client):
    uid, _, _ = _new_user(client)
    _apply({"id": "evt_sub", "type": "checkout.session.completed",
            "data": {"object": {
                "metadata": {"user_id": uid, "kind": "plan", "plan": "pro"},
                "customer": "cus_s", "subscription": "sub_s"}}})
    assert _user(uid).stripe_subscription_id == "sub_s"


# ---------- the free-Pro door ----------

def test_dev_plan_route_is_closed_in_stripe_mode(client, monkeypatch):
    """POST /me/plan {"plan":"pro"} granted any logged-in user Pro forever."""
    _, headers, _ = _new_user(client)
    monkeypatch.setenv("BILLING_PROVIDER", "stripe")
    config.get_settings.cache_clear()
    try:
        assert client.post("/me/plan", headers=headers,
                           json={"plan": "pro"}).status_code == 404
        assert client.post("/me/credits", headers=headers,
                           json={"pack": "large"}).status_code == 404
    finally:
        config.get_settings.cache_clear()
