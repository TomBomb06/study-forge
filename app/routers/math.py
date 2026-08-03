"""Math photo solver endpoints.

The business rule, in one place:

  * Everyone — including free users — gets the **answer**, unlimited.
    That's the hook, and it's what gets shared and re-opened daily.
  * Only paid plans get the **steps** (how to actually solve it).

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


UPGRADE_COPY = ("Want to know *how* to solve it? Premium shows every step, "
                "so you can do the next one yourself.")


def _shape(result: dict, user: User) -> dict:
    """Answer for everyone; steps only for paid — locked teaser otherwise."""
    paid = _is_paid(user)
    steps = result.get("steps") or []
    out = {
        "problem": result.get("problem", ""),
        "answer": result.get("answer", ""),
        "topic": result.get("topic", ""),
        "locked": not paid,
        "step_count": len(steps),
    }
    if paid:
        out["steps"] = steps
        out["check"] = result.get("check", "")
    else:
        out["steps"] = []
        out["teaser"] = mathsolve.teaser(steps)
        out["upgrade"] = UPGRADE_COPY
    return out


class SolveTextRequest(BaseModel):
    problem: str = Field(min_length=1, max_length=mathsolve.MAX_PROBLEM_CHARS)


@router.post("/solve")
async def solve_photo(
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Solve a math problem from a photo. Free = answer, Premium = steps."""
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
