"""Background job runner: get text -> generate -> save study set.

The source of the text can be an uploaded file, pasted text, or a web link;
they all converge on the same generate-and-save path.

Runs via FastAPI BackgroundTasks with its own DB session. Good enough for
MVP single-process deployment; move to a real queue (RQ/Celery/Arq) before
scaling to multiple workers.
"""

import logging
from typing import Optional

from .. import billing
from ..db import SessionLocal
from ..models import Job, StudySet, User
from .extract import ExtractionError, MIN_USABLE_CHARS, extract_text
from .generate import GenerationError, generate_study_set
from .transcribe import transcribe
from .video import VideoGenerationError, generate_video_asset
from .web import fetch_url_text
from .youtube import fetch_youtube_transcript

logger = logging.getLogger("studyforge.jobs")


def run_video_job(study_set_id: str) -> None:
    """Generate a premium video in the background and record the result.

    The user's allowance was already deducted before this was scheduled, so
    on failure we refund one video.
    """
    db = SessionLocal()
    try:
        ss = db.get(StudySet, study_set_id)
        if ss is None:
            return
        try:
            asset = generate_video_asset(ss)
        except VideoGenerationError as e:
            ss.video = {"status": "failed", "error": str(e)}
            user = db.get(User, ss.user_id)
            if user is not None:
                billing.refund_video(user)
            db.commit()
            return
        except Exception:
            logger.exception("Unexpected video failure for study set %s", study_set_id)
            ss.video = {"status": "failed", "error": "Something went wrong making the video."}
            user = db.get(User, ss.user_id)
            if user is not None:
                billing.refund_video(user)
            db.commit()
            return
        ss.video = asset
        db.commit()
    finally:
        db.close()


def _resolve_text(
    *,
    file_path: Optional[str],
    ext: Optional[str],
    raw_text: Optional[str],
    url: Optional[str],
    youtube_url: Optional[str] = None,
    media_path: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Return (text, discovered_title). Raises ExtractionError on bad input."""
    if media_path is not None:
        return transcribe(media_path), None
    if youtube_url is not None:
        title, text = fetch_youtube_transcript(youtube_url)
        return text, title
    if file_path is not None and ext is not None:
        return extract_text(file_path, ext), None
    if url is not None:
        title, text = fetch_url_text(url)
        return text, title
    if raw_text is not None:
        text = raw_text.strip()
        if len(text) < MIN_USABLE_CHARS:
            raise ExtractionError(
                "Please paste a bit more text — there isn't enough here to build "
                "a study set from."
            )
        return text, None
    raise ExtractionError("No source material was provided.")


def run_multi_job(job_id: str, files: list) -> None:
    """Combine several uploaded files into one study set.

    `files` is a list of (stored_path, ext). Extract each, concatenate the
    readable text, then generate a single kit. Files that yield no text are
    skipped; if nothing usable remains, the job fails cleanly.
    """
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "processing"
        db.commit()

        chunks = []
        for path, ext in files:
            try:
                chunks.append(extract_text(path, ext))
            except ExtractionError:
                continue
        text = "\n\n".join(chunks).strip()
        if len(text) < MIN_USABLE_CHARS:
            job.status = "failed"
            job.error = "We couldn't read enough text from those files to build a study set."
            db.commit()
            return
        _finish(db, job, text)
    finally:
        db.close()


def _finish(db, job: "Job", text: str) -> None:
    """Shared tail: generate from text and save the study set (or fail)."""
    try:
        content = generate_study_set(text, job.source_filename)
    except (ExtractionError, GenerationError) as e:
        job.status = "failed"
        job.error = str(e)
        db.commit()
        return
    except Exception:
        logger.exception("Unexpected failure finishing job %s", job.id)
        job.status = "failed"
        job.error = "Something went wrong while processing this. Please try again."
        db.commit()
        return
    study_set = StudySet(
        user_id=job.user_id,
        title=content.title,
        source_filename=job.source_filename,
        source_text=text[:200_000],
        summary=content.summary,
        flashcards=[c.model_dump() for c in content.flashcards],
        quiz=[q.model_dump() for q in content.quiz],
        test=[t.model_dump() for t in content.test],
        matching=[m.model_dump() for m in content.matching],
    )
    db.add(study_set)
    db.flush()
    job.study_set_id = study_set.id
    job.status = "completed"
    db.commit()


def run_processing_job(
    job_id: str,
    *,
    file_path: Optional[str] = None,
    ext: Optional[str] = None,
    raw_text: Optional[str] = None,
    url: Optional[str] = None,
    youtube_url: Optional[str] = None,
    media_path: Optional[str] = None,
) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "processing"
        db.commit()

        try:
            text, discovered_title = _resolve_text(
                file_path=file_path, ext=ext, raw_text=raw_text, url=url,
                youtube_url=youtube_url, media_path=media_path,
            )
            content = generate_study_set(text, job.source_filename)
        except (ExtractionError, GenerationError) as e:
            job.status = "failed"
            job.error = str(e)
            db.commit()
            return
        except Exception:
            logger.exception("Unexpected failure processing job %s", job_id)
            job.status = "failed"
            job.error = "Something went wrong while processing this. Please try again."
            db.commit()
            return

        study_set = StudySet(
            user_id=job.user_id,
            title=content.title,
            source_filename=job.source_filename,
            source_text=text[:200_000],
            summary=content.summary,
            flashcards=[c.model_dump() for c in content.flashcards],
            quiz=[q.model_dump() for q in content.quiz],
            test=[t.model_dump() for t in content.test],
            matching=[m.model_dump() for m in content.matching],
        )
        db.add(study_set)
        db.flush()
        job.study_set_id = study_set.id
        job.status = "completed"
        db.commit()
    finally:
        db.close()
