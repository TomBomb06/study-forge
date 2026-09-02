"""Math photo solver endpoints.

The business rule, in one place
------------------------------
Free users get a **complete, working solution**: the answer, the check, and
the main method fully worked out, step by step, unlimited. They also see the
*names* of every other way the problem can be solved.

Premium buys **understanding and breadth**:

  * "Why?" — the reason each step is allowed, or why you'd choose it there.
  * The alternative methods, worked out in full.

This is deliberately not the "free answer, pay to see the work" model. That
model treats the student's homework as the hostage, and the apps that run it
get punished for it in their own reviews. It also can't be shared: nobody
tells a friend about a solver that won't show its working. Giving away a
solution that actually stands on its own is what earns the recommendation,
and the wall then sits exactly where this app's whole pitch sits — on
comprehension, not on the answer key.

To change the split, edit the two constants below. Nothing else needs to move.

Solving does NOT consume the monthly study-set allowance: a math solve is
a much cheaper call than generating a whole study kit, and metering it
would kill the habit we're trying to build.

A light per-hour throttle keeps a scraper from turning "unlimited" into a
free math API. No real student doing homework will ever touch it.
"""

import time
from collections import deque
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import User
from ..pipeline import mathsolve
from ..storage import _extension  # shared extension parsing

router = APIRouter(prefix="/math", tags=["math"])

PAID_PLANS = {"basic", "pro"}

# ------------------------------------------------------------ the free/paid line
# How many solving methods a free user gets fully worked out. The rest are
# named and taglined but their steps are withheld. Set to a large number to
# give away every method; set to 0 to withhold the working entirely (the old
# behaviour — not recommended, see the module docstring).
FREE_METHODS = 1
# Whether a free user sees the per-step "why". This is the main thing Premium
# sells, so flipping it True gives the paid tier very little to stand on.
FREE_WHY = False

# Anti-abuse only — deliberately far above real homework use.
THROTTLE_MAX = 60
THROTTLE_WINDOW_S = 60 * 60

_hits: dict[str, deque] = {}
_hits_lock = Lock()


def _throttle(user_id: str) -> None:
    now = time.monotonic()
    with _hits_lock:
        q = _hits.setdefault(user_id, deque())
        while q and now - q[0] > THROTTLE_WINDOW_S:
            q.popleft()
        if len(q) >= THROTTLE_MAX:
            raise HTTPException(
                status_code=429,
                detail="That's a lot of problems in one hour! Take a short break "
                       "and try again shortly.",
            )
        q.append(now)
        # Keep the dict from growing without bound on a long-lived process.
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > THROTTLE_WINDOW_S]:
                _hits.pop(k, None)


def _is_paid(user: User) -> bool:
    return (user.plan or "free") in PAID_PLANS


WHY_UPGRADE = ("Premium explains why every step works — so you can do the next "
               "one without us.")
METHOD_UPGRADE = ("Premium works through every method, so you can use the one your "
                  "teacher taught.")


def _shape(result: dict, user: User) -> dict:
    """Full solution for everyone; the reasoning and the alternatives for paid."""
    paid = _is_paid(user)
    methods = result.get("methods") or []

    shaped = []
    for i, m in enumerate(methods):
        open_method = paid or i < FREE_METHODS
        entry = {
            "name": m["name"],
            "tagline": m.get("tagline", ""),
            "step_count": len(m["steps"]),
            "locked": not open_method,
        }
        if open_method:
            entry["steps"] = [
                {
                    "do": s["do"],
                    # An empty `why` on a step the model didn't explain must not
                    # read as "locked" — there is nothing behind that lock, and a
                    # paywall over nothing is the fastest way to lose trust.
                    "why": s["why"] if (paid or FREE_WHY) else "",
                    "why_locked": bool(s["why"]) and not (paid or FREE_WHY),
                }
                for s in m["steps"]
            ]
        else:
            entry["steps"] = []
            entry["teaser"] = mathsolve.teaser(m["steps"][0]["do"] if m["steps"] else "")
        shaped.append(entry)

    locked_methods = sum(1 for m in shaped if m["locked"])
    any_why_locked = any(
        s.get("why_locked") for m in shaped for s in (m.get("steps") or [])
    )

    return {
        "problem": result.get("problem", ""),
        "answer": result.get("answer", ""),
        "topic": result.get("topic", ""),
        # The check is part of a complete solution, so it is free. Withholding
        # "here's how to know you're right" was never worth what it earned.
        "check": result.get("check", ""),
        "methods": shaped,
        "method_count": len(shaped),
        "locked_methods": locked_methods,
        "why_locked": any_why_locked,
        "plan": user.plan or "free",
        "upgrade_why": WHY_UPGRADE if any_why_locked else "",
        "upgrade_methods": METHOD_UPGRADE if locked_methods else "",
    }


class SolveTextRequest(BaseModel):
    problem: str = Field(min_length=1, max_length=mathsolve.MAX_PROBLEM_CHARS)


@router.post("/solve")
async def solve_photo(
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Solve a math problem from a photo. Free = full first method; Premium = the why."""
    _throttle(user.id)

    media_type = mathsolve.media_type_for(_extension(file.filename or ""))
    if not media_type:
        raise HTTPException(
            status_code=422,
            detail="Please upload a photo (PNG, JPG or WEBP) of the problem.",
        )

    data = await file.read(mathsolve.MAX_IMAGE_BYTES + 1)
    if len(data) > mathsolve.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=422,
                            detail="That photo is too large — try a smaller shot.")
    try:
        result = mathsolve.solve_image(data, media_type)
    except mathsolve.MathError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _shape(result, user)


@router.post("/solve-text")
def solve_text(
    body: SolveTextRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Solve a typed-out math problem. Same free/paid split as the photo route."""
    _throttle(user.id)
    try:
        result = mathsolve.solve_text(body.problem)
    except mathsolve.MathError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _shape(result, user)
