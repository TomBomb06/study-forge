"""Premium AI tutor: in-depth 'Teach me' explanations and personalized
reviews of what a student got wrong. Paid plans only — free users get a
402 with an upgrade nudge, which the client turns into the upsell.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import StudySet, User
from ..pipeline import tutor

router = APIRouter(prefix="/tutor", tags=["tutor"])

PAID_PLANS = {"basic", "pro"}


class TutorItem(BaseModel):
    question: Optional[str] = None
    choices: Optional[List[str]] = None
    correct: Optional[str] = None
    your_answer: Optional[str] = None
    front: Optional[str] = None
    back: Optional[str] = None


class TutorRequest(BaseModel):
    set_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(min_length=1, max_length=20)
    items: Optional[List[TutorItem]] = None


def _is_paid(user: User) -> bool:
    return (user.plan or "free") in PAID_PLANS


@router.post("/explain")
def explain(
    body: TutorRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _is_paid(user):
        # 402 Payment Required — the client opens the upgrade screen.
        raise HTTPException(
            status_code=402,
            detail="In-depth tutoring is a Premium feature. Upgrade to unlock "
                   "step-by-step explanations and personalized reviews.",
        )

    ss = db.get(StudySet, body.set_id)
    if ss is None or ss.user_id != user.id:
        raise HTTPException(status_code=404, detail="Study set not found.")

    items = [i.model_dump(exclude_none=True) for i in (body.items or [])]
    try:
        text = tutor.explain(body.mode, {"items": items}, ss.summary or "")
    except tutor.TutorError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"mode": body.mode if body.mode in tutor.MODES else "question",
            "explanation": text}
