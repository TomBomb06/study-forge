"""Public share + import: how a study set travels from one student to another.

GET /shared/{token} is public (no login) so a friend can preview a shared set
before deciding. POST /shared/{token}/import (login required) copies the set
into the current user's own library with a fresh review schedule.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import StudySet, User

router = APIRouter(tags=["shares"])


def _by_token(token: str, db: Session) -> StudySet:
    ss = db.scalar(select(StudySet).where(StudySet.share_token == token))
    if ss is None:
        raise HTTPException(status_code=404, detail="This shared study set was not found.")
    return ss


@router.get("/shared/{token}")
def preview_shared(token: str, db: Session = Depends(get_db)):
    """Public preview — enough to decide whether to import it."""
    ss = _by_token(token, db)
    return {
        "token": token,
        "title": ss.title,
        "counts": {
            "flashcards": len(ss.flashcards or []),
            "quiz": len(ss.quiz or []),
            "test": len(ss.test or []),
            "matching": len(ss.matching or []),
        },
    }


@router.post("/shared/{token}/import", status_code=201)
def import_shared(
    token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Copy a shared set into the current user's library."""
    src = _by_token(token, db)
    if src.user_id == user.id:
        raise HTTPException(status_code=409, detail="This set is already in your library.")
    copy = StudySet(
        user_id=user.id,
        title=src.title,
        source_filename=f"Imported: {src.title}"[:255],
        source_text=src.source_text,
        summary=src.summary,
        flashcards=src.flashcards,
        quiz=src.quiz,
        test=src.test or [],
        matching=src.matching or [],
    )
    db.add(copy)
    db.commit()
    return {"id": copy.id, "title": copy.title}
