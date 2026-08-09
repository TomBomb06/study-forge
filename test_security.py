"""Security guarantees. These protect users, so they're asserted, not assumed.

If any of these fail, the app is exposing accounts or secrets — treat a
failure here as more serious than a broken feature.
"""

import uuid

import pytest

from app import ratelimit
from app.config import DEV_SECRET_KEY, InsecureConfigError, Settings, verify_production_config


def _email():
    return f"sec-{uuid.uuid4().hex[:10]}@example.com"


def _signup(client, email, password="password123"):
    return client.post("/auth/signup", json={"email": email, "password": password})


# ------------------------------------------------------------ brute force

def test_login_blocks_password_guessing(client):
    """The core protection: a bot cannot sit and guess passwords forever."""
    email = _email()
    assert _signup(client, email).status_code == 201

    codes = []
    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.max_attempts + 3):
        r = client.post("/auth/login", json={"email": email, "password": "wrong-guess"})
        codes.append(r.status_code)

    assert 429 in codes, "brute-force attempt was never blocked"
    blocked_at = codes.index(429)
    assert blocked_at <= ratelimit.LOGIN_PER_ACCOUNT.max_attempts, \
        "took too many attempts before blocking"


def test_blocked_login_rejects_even_the_correct_password(client):
    """Once locked, the right password must not open the door either —
    otherwise an attacker who guesses correctly mid-spray still wins."""
    email = _email()
    _signup(client, email, "password123")
    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.max_attempts + 1):
        client.post("/auth/login", json={"email": email, "password": "nope"})
    r = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 429


def test_block_response_tells_the_user_when_to_retry(client):
    email = _email()
    _signup(client, email)
    r = None
    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.max_attempts + 2):
        r = client.post("/auth/login", json={"email": email, "password": "x"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_a_real_user_who_fumbles_then_succeeds_is_not_penalised(client):
    """Honest users mistype. Two misses then a success must leave no residue."""
    email = _email()
    _signup(client, email, "password123")
    for _ in range(2):
        client.post("/auth/login", json={"email": email, "password": "typo"})
    assert client.post("/auth/login",
                       json={"email": email, "password": "password123"}).status_code == 200
    # counter cleared, so a fresh run of failures is needed to trigger a block
    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.max_attempts - 1):
        r = client.post("/auth/login", json={"email": email, "password": "typo"})
        assert r.status_code == 401, "blocked too early after a successful login"


def test_signup_flood_is_capped(client):
    """Each free account is real AI spend; mass signup must not be free."""
    codes = [_signup(client, _email()).status_code
             for _ in range(ratelimit.SIGNUP_PER_IP.max_attempts + 3)]
    assert 429 in codes


def test_login_does_not_reveal_whether_an_email_is_registered(client):
    """Different messages here hand an attacker a list of real users."""
    known = _email()
    _signup(client, known)
    ratelimit.reset_all()

    a = client.post("/auth/login", json={"email": known, "password": "wrong"})
    b = client.post("/auth/login", json={"email": _email(), "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_client_ip_prefers_first_forwarded_entry():
    """Later X-Forwarded-For entries are attacker-controlled; trusting them
    would let anyone rotate their apparent IP and skip the limit."""
    class FakeReq:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1, 172.16.0.1"}
        client = None
    assert ratelimit.client_ip(FakeReq()) == "203.0.113.9"


# ------------------------------------------------------------ config safety

def test_production_refuses_default_signing_key():
    s = Settings(environment="production", secret_key=DEV_SECRET_KEY,
                 allowed_origins="https://forge.study",
                 database_url="postgresql://x/y")
    with pytest.raises(InsecureConfigError):
        verify_production_config(s)


def test_production_refuses_wildcard_cors():
    s = Settings(environment="production", secret_key="x" * 40,
                 allowed_origins="*", database_url="postgresql://x/y")
    with pytest.raises(InsecureConfigError):
        verify_production_config(s)


def test_production_refuses_sqlite():
    """SQLite on Railway is wiped every deploy — every account would vanish."""
    s = Settings(environment="production", secret_key="x" * 40,
                 allowed_origins="https://forge.study",
                 database_url="sqlite:///./studyforge.db")
    with pytest.raises(InsecureConfigError):
        verify_production_config(s)


def test_good_production_config_passes():
    s = Settings(environment="production", secret_key="x" * 40,
                 allowed_origins="https://forge.study",
                 database_url="postgresql://user:pw@host/db")
    assert verify_production_config(s) == []


def test_development_warns_but_does_not_crash():
    s = Settings(environment="development", secret_key=DEV_SECRET_KEY)
    assert verify_production_config(s), "should still report problems"


# ------------------------------------------------------------ secret leakage

def test_no_secrets_are_served_to_the_browser(client):
    """The HTML must never contain an API key. This is the check that would
    have caught a copy-paste of a key into the frontend."""
    html = client.get("/").text
    for needle in ("sk-ant", "sk_live", "sk_test", "ANTHROPIC_API_KEY",
                   "anthropic_api_key", "STRIPE_SECRET"):
        assert needle not in html, f"{needle} appears in served HTML"


def test_security_headers_present(client):
    r = client.get("/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in r.headers
