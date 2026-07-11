from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import srs
from ..auth import get_current_user
from ..db import get_db
from ..models import QuizAttempt, StudySet, User
from ..schemas import (
    QuizAttemptResponse,
    QuizScoreRequest,
    StudySetResponse,
    StudySetSummary,
)

router = APIRouter(prefix="/study-sets", tags=["study-sets"])


def _summary(ss: StudySet) -> StudySetSummary:
    return StudySetSummary(
        id=ss.id,
        title=ss.title,
        source_filename=ss.source_filename,
        created_at=ss.created_at,
        review_level=ss.review_level or 0,
        mastery=srs.mastery_label(ss.review_level or 0),
        due=srs.is_due(ss),
        next_review=ss.next_review,
    )


def _owned_study_set(study_set_id: str, user: User, db: Session) -> StudySet:
    ss = db.get(StudySet, study_set_id)
    if ss is None or ss.user_id != user.id:
        raise HTTPException(status_code=404, detail="Study set not found.")
    return ss


@router.get("", response_model=list[StudySetSummary])
def list_study_sets(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    sets = db.scalars(
        select(StudySet)
        .where(StudySet.user_id == user.id)
        .order_by(StudySet.created_at.desc())
    ).all()
    return [_summary(s) for s in sets]


@router.get("/{study_set_id}", response_model=StudySetResponse)
def get_study_set(
    study_set_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _owned_study_set(study_set_id, user, db)


@router.post("/{study_set_id}/share")
def share_study_set(
    study_set_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create (or return) a public share token for a study set."""
    import uuid

    ss = _owned_study_set(study_set_id, user, db)
    if not ss.share_token:
        ss.share_token = uuid.uuid4().hex[:12]
        db.commit()
    return {"token": ss.share_token, "share_path": f"/?s={ss.share_token}"}


@router.post(
    "/{study_set_id}/quiz/attempts",
    response_model=QuizAttemptResponse,
    status_code=201,
)
def record_quiz_attempt(
    study_set_id: str,
    body: QuizScoreRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ss = _owned_study_set(study_set_id, user, db)
    if body.score > body.total:
        raise HTTPException(status_code=422, detail="Score cannot exceed total.")
    attempt = QuizAttempt(
        user_id=user.id, study_set_id=ss.id, score=body.score, total=body.total
    )
    db.add(attempt)
    # Advance the spaced-repetition schedule based on this score.
    srs.apply_review(ss, body.score, body.total)
    db.commit()
    return attempt


@router.get(
    "/{study_set_id}/quiz/attempts", response_model=list[QuizAttemptResponse]
)
def list_quiz_attempts(
    study_set_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ss = _owned_study_set(study_set_id, user, db)
    return db.scalars(
        select(QuizAttempt)
        .where(QuizAttempt.study_set_id == ss.id)
        .order_by(QuizAttempt.created_at.desc())
    ).all()
