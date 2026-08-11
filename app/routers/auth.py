import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    return TokenResponse(access_token=create_access_token(user.id))


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower()
    ip = ratelimit.client_ip(request)
    ip_key = f"login:ip:{ip}"
    acct_key = f"login:acct:{email}"

    # Check BOTH before touching the database. One IP spraying many accounts
    # and many IPs spraying one account are different attacks; either alone
    # is enough to start refusing.
    ratelimit.check(ip_key, ratelimit.LOGIN_PER_IP)
    ratelimit.check(acct_key, ratelimit.LOGIN_PER_ACCOUNT)

    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
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
    return TokenResponse(access_token=create_access_token(user.id))


# ------------------------------------------------------------ password reset

def _hash_token(raw: str) -> str:
    """Store only the hash, so a database leak can't be turned into logins."""
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/forgot-password", response_model=SimpleMessage)
def forgot_password(
    body: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)
):
    """Start a password reset.

    Always returns the same success message, whether or not the address is
    registered. Saying "no such account" here would turn this endpoint into a
    free tool for checking which of a leaked email list uses StudyForge.
    """
    ip = ratelimit.client_ip(request)
    email = body.email.lower()
    ratelimit.check(f"forgot:ip:{ip}", ratelimit.FORGOT_PER_IP)
    ratelimit.check(f"forgot:acct:{email}", ratelimit.FORGOT_PER_ACCOUNT)
    ratelimit.record_failure(f"forgot:ip:{ip}", ratelimit.FORGOT_PER_IP)
    ratelimit.record_failure(f"forgot:acct:{email}", ratelimit.FORGOT_PER_ACCOUNT)

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

    link = f"{settings.app_base_url.rstrip('/')}/?reset={raw}"
    mailer.password_reset(user.email, link, ttl)
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
    row.used_at = now  # single use
    db.commit()

    # A successful reset clears any lockout — otherwise someone who was locked
    # out by an attacker's guessing couldn't get back in even after resetting.
    ratelimit.record_success(f"login:acct:{user.email}")

    # Tell the real owner this happened. If an attacker did it, this is the
    # message that surfaces it.
    mailer.password_changed(user.email)
    return TokenResponse(access_token=create_access_token(user.id))
