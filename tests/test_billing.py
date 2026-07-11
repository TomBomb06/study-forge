"""Metering tests: quota enforcement, monthly reset, buy-more, upgrade,
and the metered video endpoint (uses the free 'stub' video provider)."""

import io
import time
import types

import pytest

from app import billing


# ---------- unit: quota logic ----------

def _fake_user(plan="free", used=0, extra=0, period=""):
    return types.SimpleNamespace(
        plan=plan, videos_used=used, extra_video_credits=extra, usage_period=period
    )


def test_free_plan_has_no_video_allowance():
    u = _fake_user("free")
    s = billing.video_status(u)
    assert s["total_remaining"] == 0
    assert s["can_generate_video"] is False


def test_basic_plan_allowance_and_consume():
    u = _fake_user("basic")
    assert billing.video_status(u)["total_remaining"] == 10
    for i in range(10):
        billing.consume_video(u)
    s = billing.video_status(u)
    assert s["videos_used"] == 10
    assert s["total_remaining"] == 0
    with pytest.raises(billing.QuotaExceeded):
        billing.consume_video(u)


def test_extra_credits_spent_after_plan():
    u = _fake_user("basic", used=10, extra=3, period=billing._current_period())
    # plan exhausted; extra pack should cover it
    billing.consume_video(u)
    assert u.extra_video_credits == 2
    assert u.videos_used == 10  # plan untouched once exhausted


def test_monthly_reset():
    u = _fake_user("basic", used=10, period="2000-01")  # stale period
    s = billing.video_status(u)  # triggers reset
    assert s["videos_used"] == 0
    assert s["total_remaining"] == 10


# ---------- integration: endpoints ----------

def _make_set(client, headers):
    text = (
        "Photosynthesis is the process by which green plants convert sunlight into "
        "chemical energy. Chlorophyll is the pigment that absorbs light energy. "
        "The light-dependent reactions occur in the thylakoid membranes. "
        "The Calvin cycle fixes carbon dioxide into glucose using ATP. "
        "Cellular respiration releases energy stored in glucose molecules. "
        "Mitochondria are the organelles where respiration primarily takes place. "
        "Oxygen is produced as a byproduct of photosynthesis and released into air. "
        "Stomata are pores in leaves that regulate gas exchange with the atmosphere."
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


def _video_status(client, headers, ss_id):
    for _ in range(20):
        v = client.get(f"/study-sets/{ss_id}/video", headers=headers).json()["video"]
        if v and v.get("status") in ("ready", "failed"):
            return v
        time.sleep(0.05)
    return v


def test_usage_endpoint_defaults_free(client, auth_headers):
    u = client.get("/me/usage", headers=auth_headers).json()
    assert u["video"]["plan"] == "free"
    assert u["video"]["total_remaining"] == 0
    assert "basic" in u["plans"] and "pro" in u["plans"]


def test_free_user_blocked_with_upgrade_and_packs(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    r = client.post(f"/study-sets/{ss_id}/video", headers=auth_headers)
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert "basic" in detail["upgrade_to"]
    assert detail["credit_packs"]


def test_upgrade_then_generate_async(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    client.post("/me/plan", headers=auth_headers, json={"plan": "basic"})
    # Starts async: 202 + processing, charges up front.
    r = client.post(f"/study-sets/{ss_id}/video", headers=auth_headers)
    assert r.status_code == 202, r.text
    assert r.json()["remaining"]["videos_used"] == 1
    # Stub provider completes quickly; poll to ready.
    v = _video_status(client, auth_headers, ss_id)
    assert v["status"] == "ready"


def test_buy_more_lets_free_user_generate(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    # Free user buys an add-on pack, then can generate.
    client.post("/me/credits", headers=auth_headers, json={"pack": "small"})
    r = client.post(f"/study-sets/{ss_id}/video", headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["remaining"]["extra_credits"] == 4  # 5 bought, 1 used


def test_regenerate_does_not_double_charge(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    client.post("/me/plan", headers=auth_headers, json={"plan": "basic"})
    client.post(f"/study-sets/{ss_id}/video", headers=auth_headers)
    _video_status(client, auth_headers, ss_id)  # let it finish
    # A second POST on the same (now-ready) set must NOT charge again.
    r = client.post(f"/study-sets/{ss_id}/video", headers=auth_headers)
    assert r.status_code == 202
    assert r.json()["remaining"]["videos_used"] == 1  # still just one used


def test_video_requires_auth(client):
    assert client.post("/study-sets/whatever/video").status_code == 401


def test_ads_shown_for_free_not_paid(client, auth_headers):
    u = client.get("/me/usage", headers=auth_headers).json()
    assert u["show_ads"] is True
    assert u["ads"]["provider"] == "none"  # placeholder by default
    # Upgrading turns ads off.
    client.post("/billing/checkout/plan", headers=auth_headers, json={"plan": "basic"})
    u2 = client.get("/me/usage", headers=auth_headers).json()
    assert u2["show_ads"] is False
