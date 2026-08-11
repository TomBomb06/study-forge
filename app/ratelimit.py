"""Brute-force and abuse protection.

Without this, a bot can sit on /auth/login and try passwords as fast as the
server will answer — tens of thousands per hour against every email it can
guess. Password hashing slows an *offline* attacker who has stolen the
database; it does nothing against someone guessing at the front door. This
module is the front door lock.

Two independent counters, because they stop different attacks:

  * **per IP**       — one machine spraying many accounts
  * **per account**  — a botnet spraying one account from many machines

Either tripping is enough to start refusing. That matters: a per-IP-only
limit is trivially defeated by a botnet, and a per-account-only limit lets
one host enumerate the whole user table.

Storage is in-process, which is the honest trade-off for a single-instance
app: it costs nothing, needs no Redis, and resets on deploy. If StudyForge
ever runs more than one replica, move `_HITS` to Redis or the limits become
per-replica (and effectively N times looser).
"""

import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class Rule:
    """`max_attempts` failures within `window_s` triggers `block_s` of refusal."""
    max_attempts: int
    window_s: int
    block_s: int


# Chosen so a real person who forgot their password is never blocked, while a
# script is stopped almost immediately.
#
# A human fumbling their password does 3-5 tries in a minute or two, then either
# succeeds or gives up. A bot does thousands. 8 failures/10min sits comfortably
# above the first and far below the second.
LOGIN_PER_ACCOUNT = Rule(max_attempts=8,  window_s=600,  block_s=900)
LOGIN_PER_IP      = Rule(max_attempts=20, window_s=600,  block_s=900)
# Signup is capped to stop mass fake-account creation burning AI credits —
# every free account is 5 study sets of real spend.
SIGNUP_PER_IP     = Rule(max_attempts=10, window_s=3600, block_s=3600)
# Password reset. Tight per account so the endpoint can't be used to spam
# someone's inbox, and tight per IP so it can't be used to enumerate emails.
FORGOT_PER_ACCOUNT = Rule(max_attempts=3,  window_s=900,  block_s=900)
FORGOT_PER_IP      = Rule(max_attempts=10, window_s=900,  block_s=900)
# Guessing a 32-byte token is hopeless, but cap the attempts anyway.
RESET_PER_IP       = Rule(max_attempts=10, window_s=900,  block_s=900)

_HITS: dict[str, deque] = {}
_BLOCKED: dict[str, float] = {}
_LOCK = threading.Lock()
_MAX_KEYS = 20000  # hard cap so a spray of unique keys can't exhaust memory


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Railway (like most PaaS) terminates TLS at a proxy, so `request.client`
    is the proxy. The real address is the FIRST entry of X-Forwarded-For;
    later entries are attacker-controllable and must never be trusted.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _prune_locked(now: float) -> None:
    """Drop expired blocks and stale counters. Caller must hold _LOCK."""
    for k in [k for k, until in _BLOCKED.items() if until <= now]:
        _BLOCKED.pop(k, None)
    if len(_HITS) > _MAX_KEYS:
        for k in [k for k, q in _HITS.items() if not q or now - q[-1] > 3600]:
            _HITS.pop(k, None)


def check(key: str, rule: Rule) -> None:
    """Raise 429 if `key` is currently blocked. Does NOT record an attempt."""
    now = time.monotonic()
    with _LOCK:
        _prune_locked(now)
        until = _BLOCKED.get(key)
        if until and until > now:
            retry = int(until - now) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Please wait a few minutes and try again.",
                headers={"Retry-After": str(retry)},
            )


def record_failure(key: str, rule: Rule) -> None:
    """Log a failed attempt and start blocking once the rule is exceeded."""
    now = time.monotonic()
    with _LOCK:
        q = _HITS.setdefault(key, deque(maxlen=rule.max_attempts * 4))
        while q and now - q[0] > rule.window_s:
            q.popleft()
        q.append(now)
        if len(q) >= rule.max_attempts:
            _BLOCKED[key] = now + rule.block_s
            q.clear()


def record_success(key: str) -> None:
    """Clear the counter after a genuine login, so honest users never accrue."""
    with _LOCK:
        _HITS.pop(key, None)
        _BLOCKED.pop(key, None)


def reset_all() -> None:
    """Test helper — wipes all state."""
    with _LOCK:
        _HITS.clear()
        _BLOCKED.clear()
