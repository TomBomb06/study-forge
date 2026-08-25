import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import mailer, ratelimit
from ..auth import create_access_token, hash_password, verify_password
from ..config import get_settings
from ..db import get_db
from ..models import PasswordResetToken, User
from ..schemas import (ForgotPasswordRequest, LoginRequest, ResetPasswordRequest,
                       SignupRequest, SimpleMessage, TokenResponse)

router = APIRouter(prefix="/auth", tags=["auth"])


# Verified against this when the email is unknown, so a login attempt costs the
# same time whether or not the account exists. Without it, "unknown email"
# returned in ~4ms and "real email, wrong password" in ~25ms — a 5.7x gap that
# turns the deliberately-identical error message into an account oracle.
_DUMMY_HASH = hash_password("not-a-real-password-placeholder")


@router.post("/signup", response_model=TokenResponse, status_code=201)
def signup(body: SignupRequest, request: Request, db: Session = Depends(get_db)):
    ip = ratelimit.client_ip(request)
    ip_key = f"signup:ip:{ip}"
    ratelimit.check(ip_key, ratelimit.SIGNUP_PER_IP)

    email = body.email.lower()
    exists = db.scalar(select(User).where(User.email == email))
    if exists:
        # Counted: repeated hits here are how you enumerate which emails are
        # registered, and it's also the shape of a mass-signup script.
        ratelimit.record_failure(ip_key, ratelimit.SIGNUP_PER_IP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    ratelimit.record_failure(ip_key, ratelimit.SIGNUP_PER_IP)  # counts toward the cap
    return TokenResponse(access_token=create_access_token(user.id, user.token_version))


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower()
    ip = ratelimit.client_ip(request)
    ip_key = f"login:ip:{ip}"
    # Keyed on the PAIR, not the email alone.
    #
    # A per-email lock checked before the password is verified means anyone who
    # knows a classmate's address can post ~11 junk logins every 15 minutes and
    # keep that person out of their own account indefinitely — the correct
    # password is refused too. In a school app that is a bullying tool, not a
    # defence. Per (account, IP) still stops one machine grinding one account,
    # and LOGIN_PER_IP still stops one machine spraying many.
    acct_key = f"login:acct:{email}|{ip}"

    ratelimit.check(ip_key, ratelimit.LOGIN_PER_IP)
    ratelimit.check(acct_key, ratelimit.LOGIN_PER_ACCOUNT)

    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, _DUMMY_HASH if user is None
                                           else user.password_hash):
        ratelimit.record_failure(ip_key, ratelimit.LOGIN_PER_IP)
        ratelimit.record_failure(acct_key, ratelimit.LOGIN_PER_ACCOUNT)
        # Deliberately identical message for "no such user" and "wrong
        # password" — saying which one would let an attacker harvest a list
        # of real email addresses without ever guessing a password.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    # Genuine login clears the counter, so someone who fumbles their password
    # twice and then gets it right never carries a penalty forward.
    ratelimit.record_success(acct_key)
    ratelimit.record_success(ip_key)
    return TokenResponse(access_token=create_access_token(user.id, user.token_version))


# ------------------------------------------------------------ password reset

def _hash_token(raw: str) -> str:
    """Store only the hash, so a database leak can't be turned into logins."""
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/forgot-password", response_model=SimpleMessage)
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Start a password reset.

    Always returns the same success message, whether or not the address is
    registered. Saying "no such account" here would turn this endpoint into a
    free tool for checking which of a leaked email list uses StudyForge.
    """
    ip = ratelimit.client_ip(request)
    email = body.email.lower()
    # Per (account, IP) for the same reason as login: keyed on the email alone,
    # three requests from a stranger closed the victim's own recovery path.
    ratelimit.check(f"forgot:ip:{ip}", ratelimit.FORGOT_PER_IP)
    ratelimit.check(f"forgot:acct:{email}|{ip}", ratelimit.FORGOT_PER_ACCOUNT)
    ratelimit.record_failure(f"forgot:ip:{ip}", ratelimit.FORGOT_PER_IP)
    ratelimit.record_failure(f"forgot:acct:{email}|{ip}", ratelimit.FORGOT_PER_ACCOUNT)

    settings = get_settings()
    generic = SimpleMessage(
        message="If an account exists for that email, we've sent a reset link. "
                "Check your inbox (and spam folder)."
    )

    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        return generic

    # Invalidate any outstanding tokens: requesting a new link should make
    # older links dead, so a stale email in an inbox can't be replayed later.
    now = datetime.now(timezone.utc)
    for old in db.scalars(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
    ):
        old.used_at = now

    raw = secrets.token_urlsafe(32)
    ttl = settings.password_reset_ttl_minutes
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=now + timedelta(minutes=ttl),
    ))
    db.commit()

    # Fragment, not query string. As "?reset=<token>" the live account-takeover
    # credential was handed to Meta Pixel and GA4 (both transmit location.href),
    # written into browser history, and into any access log that keeps query
    # strings. A fragment is never sent to a server or read by those tags.
    link = f"{settings.app_base_url.rstrip('/')}/#reset={raw}"
    # Sent in the background: a synchronous provider call (up to a 15s timeout)
    # made the response time itself reveal whether the account existed — 160ms
    # for a real address against 5ms for an unknown one, defeating the
    # deliberately identical message above.
    background.add_task(mailer.password_reset, user.email, link, ttl)
    return generic


@router.post("/reset-password", response_model=TokenResponse)
def reset_password(
    body: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)
):
    """Complete a reset and sign the user straight in."""
    ip = ratelimit.client_ip(request)
    ip_key = f"reset:ip:{ip}"
    ratelimit.check(ip_key, ratelimit.RESET_PER_IP)

    row = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_token(body.token)
        )
    )
    now = datetime.now(timezone.utc)

    def _expired(r) -> bool:
        exp = r.expires_at
        if exp.tzinfo is None:            # SQLite hands back naive datetimes
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= now

    if row is None or row.used_at is not None or _expired(row):
        # Counted so the token space can't be brute-forced.
        ratelimit.record_failure(ip_key, ratelimit.RESET_PER_IP)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset link is invalid or has expired. "
                   "Request a new one and try again.",
        )

    user = db.get(User, row.user_id)
    if user is None:
        ratelimit.record_failure(ip_key, ratelimit.RESET_PER_IP)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That reset link is no longer valid.")

    user.password_hash = hash_password(body.password)
    # Cut off every session minted before this moment. A reset is the remedy we
    # tell users to perform when they think someone else is in their account;
    # it has to actually evict them.
    user.token_version = int(user.token_version or 0) + 1
    row.used_at = now  # single use
    db.commit()

    # A successful reset clears any lockout — otherwise someone who was locked
    # out by an attacker's guessing couldn't get back in even after resetting.
    ratelimit.clear_prefix(f"login:acct:{user.email}|")

    # Tell the real owner this happened. If an attacker did it, this is the
    # message that surfaces it.
    mailer.password_changed(user.email)
    return TokenResponse(access_token=create_access_token(user.id, user.token_version))
