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

from .config import get_settings


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
# NOTE: a classroom, dorm or mobile carrier is ONE IP. Ten was low enough that
# a student showing the app to friends would get the 11th of them blocked for an
# hour — which is the growth loop, not an attack.
SIGNUP_PER_IP     = Rule(max_attempts=40, window_s=3600, block_s=1800)
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

    Railway (like most PaaS) terminates TLS at a proxy, so `request.client` is
    the proxy. A proxy APPENDS to any X-Forwarded-For the caller sent, so the
    header reads `<whatever the client typed>, <what proxy 1 saw>, ...` and only
    the entries our own infrastructure added can be trusted. Counting back
    TRUSTED_PROXY_HOPS from the end gives the address the outermost proxy saw.

    Reading the FIRST entry (the old behaviour) meant the value was whatever the
    caller put in the header: every per-IP limit was bypassable by rotating it,
    and because the per-account login rule is checked before the password is
    verified, anyone could lock a known customer out of their own account.

    TRUSTED_PROXY_HOPS is 1 for a single proxy (Railway alone). Put another one
    in front — Cloudflare, a load balancer — and it must become 2, or every
    request will look like it came from that proxy and share one rate-limit
    bucket.
    """
    hops = max(1, get_settings().trusted_proxy_hops)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-min(hops, len(parts))][:64]
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


def clear_prefix(prefix: str) -> None:
    """Clear every counter whose key starts with `prefix`.

    Login counters are keyed per (account, IP), so clearing an account's
    lockout after a successful password reset has to sweep all of them —
    otherwise the machine the attacker used stays blocked forever and,
    more importantly, the owner's own earlier failures don't clear.
    """
    with _LOCK:
        for k in [k for k in _HITS if k.startswith(prefix)]:
            _HITS.pop(k, None)
        for k in [k for k in _BLOCKED if k.startswith(prefix)]:
            _BLOCKED.pop(k, None)


def reset_all() -> None:
    """Test helper — wipes all state."""
    with _LOCK:
        _HITS.clear()
        _BLOCKED.clear()
