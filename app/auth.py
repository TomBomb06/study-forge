from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, token_version: int = 0) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    # "tv" pins the token to a generation of the account. Resetting the
    # password bumps User.token_version, which invalidates every token minted
    # before it — otherwise a stolen 7-day bearer token survived the password
    # reset the "your password changed" email tells the user to do.
    payload = {"sub": user_id, "exp": expires, "tv": int(token_version or 0)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials, settings.secret_key, algorithms=["HS256"]
        )
    except jwt.PyJWTError:
        raise unauthorized
    user = db.get(User, payload.get("sub", ""))
    if user is None:
        raise unauthorized
    # Tokens minted before the last password reset are dead. Absent "tv" means
    # a token issued before this check existed; those are treated as version 0,
    # so they stop working the first time the account's password is reset.
    if int(payload.get("tv", 0) or 0) != int(user.token_version or 0):
        raise unauthorized
    return user
