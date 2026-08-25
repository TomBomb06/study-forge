"""Regression tests for the security review of 2026-08-15.

Every test here corresponds to a working exploit that was demonstrated against
the app. They exist so the holes cannot quietly reopen.
"""

import time
import uuid

import jwt
import pytest
from sqlalchemy import select

from app import config, mailer, ratelimit
from app.config import DEV_SECRET_KEY, InsecureConfigError, Settings, \
    looks_like_production, verify_production_config
from app.db import SessionLocal
from app.models import StudySet, User


def _signup(client, email=None, password="password123"):
    email = email or f"sec-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return email, {"Authorization": f"Bearer {r.json()['access_token']}"}


# --------------------------------------------------------------- config

def test_production_is_inferred_not_declared():
    """ENVIRONMENT unset or misspelled must not disable the safety checks.

    It defaults to "development", so a typo ("Prodction") or a forgotten
    variable meant the server booted signing JWTs with the dev key that is
    committed to a public repo — anyone could mint a token for any account.
    """
    typo = Settings(environment="Prodction", secret_key=DEV_SECRET_KEY,
                    database_url="postgresql://x/y", app_base_url="https://forge.study",
                    billing_provider="stripe")
    assert looks_like_production(typo) is True
    with pytest.raises(InsecureConfigError):
        verify_production_config(typo)

    unset = Settings(secret_key=DEV_SECRET_KEY, database_url="postgresql://x/y",
                     app_base_url="https://forge.study")
    assert looks_like_production(unset) is True

    local = Settings(secret_key=DEV_SECRET_KEY, database_url="sqlite:///./dev.db",
                     app_base_url="http://localhost:8000", billing_provider="dev")
    assert looks_like_production(local) is False
    verify_production_config(local)  # warns, does not raise


# ---------------------------------------------------------------- session

def test_password_reset_kills_existing_sessions(client, monkeypatch):
    """A stolen 7-day bearer token used to survive the password reset that the
    'your password changed' email tells the victim to perform."""
    sent = {}
    monkeypatch.setattr(mailer, "password_reset",
                        lambda to, link, ttl: sent.update(link=link))
    monkeypatch.setattr(mailer, "password_changed", lambda to: None)

    email, stolen = _signup(client)
    assert client.get("/me/usage", headers=stolen).status_code == 200

    client.post("/auth/forgot-password", json={"email": email})
    token = sent["link"].split("reset=")[1]
    r = client.post("/auth/reset-password",
                    json={"token": token, "password": "brand-new-password"})
    assert r.status_code == 200

    assert client.get("/me/usage", headers=stolen).status_code == 401, \
        "the pre-reset token still works — resetting the password did not evict the attacker"
    fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/me/usage", headers=fresh).status_code == 200


def test_forged_token_with_a_wrong_token_version_is_rejected(client):
    email, headers = _signup(client)
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == email))
    uid, secret = u.id, config.get_settings().secret_key
    u.token_version = 5
    db.commit()
    db.close()
    stale = jwt.encode({"sub": uid, "exp": int(time.time()) + 3600, "tv": 0},
                       secret, algorithm="HS256")
    assert client.get("/me/usage",
                      headers={"Authorization": f"Bearer {stale}"}).status_code == 401


# ------------------------------------------------------------ rate limits

def test_a_stranger_cannot_lock_a_victim_out_of_their_own_account(client):
    """~11 junk logins used to deny a named user their own account for 15
    minutes at a time, from any IP, forever. In a school app that is a
    bullying tool, not a defence."""
    ratelimit.reset_all()
    email, _ = _signup(client, password="the-real-password")
    attacker = {"X-Forwarded-For": "203.0.113.9, 10.1.1.1"}
    victim = {"X-Forwarded-For": "198.51.100.4, 10.2.2.2"}

    for _ in range(12):
        client.post("/auth/login", headers=attacker,
                    json={"email": email, "password": "wrong"})

    r = client.post("/auth/login", headers=victim,
                    json={"email": email, "password": "the-real-password"})
    assert r.status_code == 200, "the real owner was locked out by someone else's guessing"

    r = client.post("/auth/forgot-password", headers=victim, json={"email": email})
    assert r.status_code == 200, "the victim's recovery path was closed by someone else"


def test_repeated_wrong_passwords_from_one_machine_are_still_blocked(client):
    """The lockout fix must not disable the brute-force defence."""
    ratelimit.reset_all()
    email, _ = _signup(client, password="the-real-password")
    same = {"X-Forwarded-For": "203.0.113.77, 10.1.1.1"}
    codes = [client.post("/auth/login", headers=same,
                         json={"email": email, "password": f"guess{i}"}).status_code
             for i in range(12)]
    assert 429 in codes, "an attacker can grind one account from one machine unchecked"


# ------------------------------------------------------------------ SSRF

def test_url_guard_is_applied_to_every_redirect_hop():
    """The guard checked only the URL the user typed, then httpx followed 30x
    anywhere — cloud metadata included — and the body became the study set."""
    import inspect
    from app.pipeline import web
    src = inspect.getsource(web.fetch_url_text)
    assert "follow_redirects=False" in src
    assert src.count("_guard_url") >= 2, "redirect targets are not re-checked"

    for bad in ("http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:8000/admin", "http://10.0.0.5/"):
        with pytest.raises(Exception):
            web._guard_url(bad)


# ------------------------------------------------------------ reset links

def test_reset_link_uses_a_fragment_so_analytics_never_sees_the_token(client, monkeypatch):
    """As '?reset=<token>' the live takeover credential was handed to Meta
    Pixel and GA4 (both transmit location.href) and written to browser history."""
    sent = {}
    monkeypatch.setattr(mailer, "password_reset",
                        lambda to, link, ttl: sent.update(link=link))
    email, _ = _signup(client)
    client.post("/auth/forgot-password", json={"email": email})
    assert "#reset=" in sent["link"], sent["link"]
    assert "?reset=" not in sent["link"], sent["link"]


# ---------------------------------------------------------------- shares

def _make_shared_set(client, headers):
    from tests.test_share_multi import _make_set  # reuse the existing helper
    ss_id = _make_set(client, headers)
    r = client.post(f"/study-sets/{ss_id}/share", headers=headers)
    return ss_id, r.json()["token"]


def test_the_same_share_cannot_be_imported_twice(client):
    """Import had no duplicate guard and no quota, so a user out of study sets
    could loop one share link and fill the database for free."""
    _, owner = _signup(client)
    _, importer = _signup(client)
    _, token = _make_shared_set(client, owner)

    assert client.post(f"/shared/{token}/import", headers=importer).status_code == 201
    for _ in range(5):
        r = client.post(f"/shared/{token}/import", headers=importer)
        assert r.status_code == 409, "the same shared set was imported again"


def test_a_share_link_can_be_revoked(client):
    """Share tokens were permanent and unrevocable."""
    _, owner = _signup(client)
    ss_id, token = _make_shared_set(client, owner)
    assert client.get(f"/shared/{token}").status_code == 200
    assert client.delete(f"/study-sets/{ss_id}/share", headers=owner).status_code == 200
    assert client.get(f"/shared/{token}").status_code == 404


# ------------------------------------------------------------------- XP

def test_rotating_the_timezone_cannot_refill_the_daily_xp_cap(client):
    """tz_offset came off the request body and the cap was keyed on the local
    date, so alternating -840/+840 re-rolled 'today' and refilled it — 4800 XP
    against a 1200 cap, which buys levels, and level 20 is a 20% coupon."""
    from app import gamify
    _, headers = _signup(client)
    last = None
    for i in range(60):
        last = client.post("/gamify/event", headers=headers, json={
            "type": "quiz", "data": {"score": 200, "total": 200},
            "tz_offset": -840 if i % 2 else 840,
        })
    xp = last.json()["state"]["daily_xp"]
    assert xp <= gamify.DAILY_XP_CAP, f"cap bypassed: {xp} XP against a {gamify.DAILY_XP_CAP} cap"
