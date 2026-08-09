from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ratelimit
from ..auth import create_access_token, hash_password, verify_password
from ..db import get_db
from ..models import User
from ..schemas import LoginRequest, SignupRequest, TokenResponse

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
