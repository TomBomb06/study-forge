"""Public share + import: how a study set travels from one student to another.

GET /shared/{token} is public (no login) so a friend can preview a shared set
before deciding. POST /shared/{token}/import (login required) copies the set
into the current user's own library with a fresh review schedule.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ratelimit
from ..auth import get_current_user
from ..db import get_db
from ..models import StudySet, User

router = APIRouter(tags=["shares"])

# /shared/{token} is public and unauthenticated. The token is 48 bits so it is
# not realistically guessable, but an open, unthrottled lookup endpoint is
# still free load for anyone who wants to point a script at it.
SHARED_PREVIEW = ratelimit.Rule(max_attempts=120, window_s=300, block_s=300)
# Importing is cheap for the importer and copies up to 200 kB each time.
IMPORT_PER_USER = ratelimit.Rule(max_attempts=30, window_s=3600, block_s=1800)


def _by_token(token: str, db: Session) -> StudySet:
    ss = db.scalar(select(StudySet).where(StudySet.share_token == token))
    if ss is None:
        raise HTTPException(status_code=404, detail="This shared study set was not found.")
    return ss


@router.get("/shared/{token}")
def preview_shared(token: str, request: Request, db: Session = Depends(get_db)):
    """Public preview — enough to decide whether to import it."""
    ip = ratelimit.client_ip(request)
    ratelimit.check(f"shared:ip:{ip}", SHARED_PREVIEW)
    ratelimit.record_failure(f"shared:ip:{ip}", SHARED_PREVIEW)
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
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Copy a shared set into the current user's library."""
    ratelimit.check(f"import:user:{user.id}", IMPORT_PER_USER)
    ratelimit.record_failure(f"import:user:{user.id}", IMPORT_PER_USER)
    src = _by_token(token, db)
    if src.user_id == user.id:
        raise HTTPException(status_code=409, detail="This set is already in your library.")
    # Importing the SAME share more than once was allowed, so a user out of
    # study sets could loop this endpoint and add unlimited copies for free.
    already = db.scalar(
        select(StudySet).where(
            StudySet.user_id == user.id, StudySet.imported_from == src.id
        )
    )
    if already is not None:
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
        imported_from=src.id,
    )
    db.add(copy)
    db.commit()
    return {"id": copy.id, "title": copy.title}


@router.delete("/study-sets/{study_set_id}/share", status_code=200)
def revoke_share(
    study_set_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Turn a share link off.

    Share tokens used to be permanent and unrevocable: once a set was shared,
    the link worked forever and there was no way to take it back.
    """
    ss = db.get(StudySet, study_set_id)
    if ss is None or ss.user_id != user.id:
        raise HTTPException(status_code=404, detail="Study set not found.")
    ss.share_token = None
    db.commit()
    return {"shared": False}
