"""Password reset. This flow can hand over an account, so every rule is asserted.

A weak reset is worse than no reset: it becomes the easiest way *in*.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app import mailer, ratelimit
from app.db import SessionLocal
from app.models import PasswordResetToken, User


def _email():
    return f"reset-{uuid.uuid4().hex[:10]}@example.com"


def _signup(client, email, password="password123"):
    r = client.post("/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r


def _capture_link(monkeypatch):
    """Intercept the outgoing email so tests can read the reset link."""
    box = {}

    def fake(to, link, minutes):
        box["to"] = to
        box["link"] = link
        box["minutes"] = minutes
        return True

    monkeypatch.setattr(mailer, "password_reset", fake)
    return box


def _token_from(link: str) -> str:
    return link.split("reset=", 1)[1]


# ------------------------------------------------------------ happy path

def test_full_reset_flow_lets_the_user_back_in(client, monkeypatch):
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email, "old-password-1")

    assert client.post("/auth/forgot-password",
                       json={"email": email}).status_code == 200
    assert box["to"] == email

    r = client.post("/auth/reset-password",
                    json={"token": _token_from(box["link"]), "password": "brand-new-pw-9"})
    assert r.status_code == 200
    assert r.json()["access_token"]          # signed straight in

    # new password works, old one doesn't
    ratelimit.reset_all()
    assert client.post("/auth/login",
                       json={"email": email, "password": "brand-new-pw-9"}).status_code == 200
    assert client.post("/auth/login",
                       json={"email": email, "password": "old-password-1"}).status_code == 401


def test_reset_notifies_the_owner_that_the_password_changed(client, monkeypatch):
    """If an attacker resets someone's password, this email is how the real
    owner finds out. It must always fire."""
    box = _capture_link(monkeypatch)
    sent = {}
    monkeypatch.setattr(mailer, "password_changed",
                        lambda to: sent.setdefault("to", to) or True)
    email = _email()
    _signup(client, email)
    client.post("/auth/forgot-password", json={"email": email})
    client.post("/auth/reset-password",
                json={"token": _token_from(box["link"]), "password": "another-pw-77"})
    assert sent.get("to") == email


# ------------------------------------------------------------ token rules

def test_token_is_single_use(client, monkeypatch):
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email)
    client.post("/auth/forgot-password", json={"email": email})
    tok = _token_from(box["link"])

    assert client.post("/auth/reset-password",
                       json={"token": tok, "password": "first-change-1"}).status_code == 200
    # replay must fail
    assert client.post("/auth/reset-password",
                       json={"token": tok, "password": "second-change-2"}).status_code == 400


def test_expired_token_is_rejected(client, monkeypatch):
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email)
    client.post("/auth/forgot-password", json={"email": email})
    tok = _token_from(box["link"])

    db = SessionLocal()
    try:
        import hashlib
        h = hashlib.sha256(tok.encode()).hexdigest()
        row = db.query(PasswordResetToken).filter_by(token_hash=h).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert client.post("/auth/reset-password",
                       json={"token": tok, "password": "too-late-now-1"}).status_code == 400


def test_requesting_a_new_link_kills_the_old_one(client, monkeypatch):
    """A stale link sitting in an inbox must not still work later."""
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email)

    client.post("/auth/forgot-password", json={"email": email})
    first = _token_from(box["link"])
    ratelimit.reset_all()
    client.post("/auth/forgot-password", json={"email": email})
    second = _token_from(box["link"])
    assert first != second

    assert client.post("/auth/reset-password",
                       json={"token": first, "password": "should-fail-11"}).status_code == 400
    assert client.post("/auth/reset-password",
                       json={"token": second, "password": "should-work-22"}).status_code == 200


def test_garbage_token_is_rejected(client):
    assert client.post("/auth/reset-password",
                       json={"token": "x" * 40, "password": "whatever-123"}).status_code == 400


def test_raw_token_is_never_stored_in_the_database(client, monkeypatch):
    """A database leak must not yield working reset links."""
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email)
    client.post("/auth/forgot-password", json={"email": email})
    tok = _token_from(box["link"])

    db = SessionLocal()
    try:
        stored = [r.token_hash for r in db.query(PasswordResetToken).all()]
    finally:
        db.close()
    assert tok not in stored, "raw reset token was stored in plaintext"


# ------------------------------------------------------------ privacy

def test_forgot_password_does_not_reveal_whether_an_account_exists(client, monkeypatch):
    """Otherwise this endpoint becomes a free 'is this person a user?' oracle."""
    _capture_link(monkeypatch)
    known = _email()
    _signup(client, known)
    ratelimit.reset_all()

    a = client.post("/auth/forgot-password", json={"email": known})
    ratelimit.reset_all()
    b = client.post("/auth/forgot-password", json={"email": _email()})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_forgot_password_is_rate_limited(client, monkeypatch):
    """Stops the endpoint being used to flood someone's inbox."""
    _capture_link(monkeypatch)
    email = _email()
    _signup(client, email)
    codes = [client.post("/auth/forgot-password", json={"email": email}).status_code
             for _ in range(ratelimit.FORGOT_PER_ACCOUNT.max_attempts + 2)]
    assert 429 in codes


def test_reset_clears_a_login_lockout(client, monkeypatch):
    """Someone locked out by an attacker's guessing must be able to recover."""
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email, "original-pw-1")

    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.max_attempts + 1):
        client.post("/auth/login", json={"email": email, "password": "guess"})
    assert client.post("/auth/login",
                       json={"email": email, "password": "original-pw-1"}).status_code == 429

    ratelimit.reset_all()  # clears only the per-IP forgot limit for the test
    client.post("/auth/forgot-password", json={"email": email})
    assert client.post("/auth/reset-password",
                       json={"token": _token_from(box["link"]),
                             "password": "recovered-pw-5"}).status_code == 200
    assert client.post("/auth/login",
                       json={"email": email, "password": "recovered-pw-5"}).status_code == 200


def test_short_password_rejected(client, monkeypatch):
    box = _capture_link(monkeypatch)
    email = _email()
    _signup(client, email)
    client.post("/auth/forgot-password", json={"email": email})
    r = client.post("/auth/reset-password",
                    json={"token": _token_from(box["link"]), "password": "short"})
    assert r.status_code == 422


# ------------------------------------------------------------ mail safety

def test_mailer_never_raises_when_the_provider_fails(monkeypatch):
    """A dead mail provider must not turn into a 500 that reveals the address
    exists — send() has to swallow and report False."""
    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setitem(mailer._SENDERS, "console", boom)
    assert mailer.send("a@b.com", "s", "t", "<p>h</p>") is False


def test_production_config_flags_console_email():
    """Launching with console email means nobody can ever reset a password.

    ENVIRONMENT here says "development" while everything else says production
    (Postgres + https). That combination is exactly the misconfiguration the
    inference in looks_like_production() exists to catch, so this must now be
    a refusal to boot rather than a printed warning.
    """
    import pytest
    from app.config import InsecureConfigError, Settings, verify_production_config
    s = Settings(environment="development", secret_key="x" * 40,
                 allowed_origins="https://forge.study",
                 database_url="postgresql://x/y",
                 email_provider="console", app_base_url="https://forge.study")
    with pytest.raises(InsecureConfigError) as exc:
        verify_production_config(s)
    assert "EMAIL_PROVIDER" in str(exc.value)

    # Same settings but genuinely local: warns, boots.
    local = Settings(environment="development", secret_key="x" * 40,
                     allowed_origins="https://forge.study",
                     database_url="sqlite:///./dev.db",
                     email_provider="console", app_base_url="http://localhost:8000",
                     billing_provider="dev")
    problems = verify_production_config(local)
    assert any("EMAIL_PROVIDER" in p for p in problems)
