from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import Job, User
from ..pipeline.jobs import run_multi_job, run_processing_job
from ..schemas import JobResponse, LinkIngestRequest, TextIngestRequest
from ..storage import UploadValidationError, save_upload

router = APIRouter(tags=["uploads"])


@router.post("/uploads", response_model=JobResponse, status_code=202)
def create_upload(
    file: UploadFile,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept a file, validate it, and start async processing.

    Returns a job immediately; the client polls GET /jobs/{id}.
    """
    try:
        stored_path, ext = save_upload(file, user.id)
    except UploadValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job = Job(user_id=user.id, source_filename=file.filename or "upload")
    db.add(job)
    db.commit()

    background.add_task(run_processing_job, job.id, file_path=stored_path, ext=ext)
    return job


@router.post("/uploads/multi", response_model=JobResponse, status_code=202)
def create_multi_upload(
    files: list[UploadFile],
    background: BackgroundTasks,
    title: str = Form(default=""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Combine several files into one study set."""
    if not files:
        raise HTTPException(status_code=422, detail="Add at least one file.")
    if len(files) > 10:
        raise HTTPException(status_code=422, detail="You can combine up to 10 files at once.")
    saved = []
    try:
        for f in files:
            saved.append(save_upload(f, user.id))
    except UploadValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    label = title.strip() or f"Combined ({len(files)} files)"
    job = Job(user_id=user.id, source_filename=label[:255])
    db.add(job)
    db.commit()

    background.add_task(run_multi_job, job.id, saved)
    return job


@router.post("/uploads/text", response_model=JobResponse, status_code=202)
def create_text_upload(
    body: TextIngestRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a study set from pasted or typed text."""
    label = (body.title or "").strip() or "Pasted notes"
    job = Job(user_id=user.id, source_filename=label[:255])
    db.add(job)
    db.commit()

    background.add_task(run_processing_job, job.id, raw_text=body.content)
    return job


@router.post("/uploads/link", response_model=JobResponse, status_code=202)
def create_link_upload(
    body: LinkIngestRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a study set from the readable text of a web link."""
    job = Job(user_id=user.id, source_filename=body.url.strip()[:255])
    db.add(job)
    db.commit()

    background.add_task(run_processing_job, job.id, url=body.url)
    return job


@router.post("/uploads/youtube", response_model=JobResponse, status_code=202)
def create_youtube_upload(
    body: LinkIngestRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a study set from a YouTube video's captions."""
    job = Job(user_id=user.id, source_filename=body.url.strip()[:255])
    db.add(job)
    db.commit()

    background.add_task(run_processing_job, job.id, youtube_url=body.url)
    return job


@router.post("/uploads/media", response_model=JobResponse, status_code=202)
def create_media_upload(
    file: UploadFile,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a study set from a lecture audio/video file (transcribed)."""
    try:
        stored_path, ext = save_upload(file, user.id)
    except UploadValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job = Job(user_id=user.id, source_filename=file.filename or "lecture")
    db.add(job)
    db.commit()

    background.add_task(run_processing_job, job.id, media_path=stored_path)
    return job


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job
