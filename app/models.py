import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Billing / video metering.
    plan: Mapped[str] = mapped_column(String(20), default="free")
    usage_period: Mapped[str] = mapped_column(String(7), default="")  # "YYYY-MM"
    videos_used: Mapped[int] = mapped_column(Integer, default=0)
    extra_video_credits: Mapped[int] = mapped_column(Integer, default=0)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    study_sets: Mapped[list["StudySet"]] = relationship(back_populates="user")


class Job(Base):
    """Tracks one processing run: upload -> extract -> generate."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | processing | completed | failed
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    study_set_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("study_sets.id"), nullable=True
    )
    source_filename: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class StudySet(Base):
    __tablename__ = "study_sets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source_filename: Mapped[str] = mapped_column(String(255))
    source_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    share_token: Mapped[Optional[str]] = mapped_column(
        String(32), unique=True, nullable=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text)  # study-guide notes
    flashcards: Mapped[list] = mapped_column(JSON)
    quiz: Mapped[list] = mapped_column(JSON)
    test: Mapped[list] = mapped_column(JSON, default=list)
    matching: Mapped[list] = mapped_column(JSON, default=list)
    video: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # premium
    # Spaced repetition.
    review_level: Mapped[int] = mapped_column(Integer, default=0)
    last_reviewed: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_review: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="study_sets")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    study_set_id: Mapped[str] = mapped_column(ForeignKey("study_sets.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
