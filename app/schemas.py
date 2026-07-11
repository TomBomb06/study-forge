"""Pydantic schemas: API request/response models and the strict
study-set content schema every generator's output must validate against."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


# ---------- Auth ----------

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Non-file inputs ----------

class TextIngestRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    title: Optional[str] = Field(default=None, max_length=255)


class LinkIngestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


# ---------- Generated content (strict — validated before saving) ----------

class Flashcard(BaseModel):
    front: str = Field(min_length=1, max_length=500)
    back: str = Field(min_length=1, max_length=2000)


class QuizQuestion(BaseModel):
    # Lenient on purpose: real AI output varies slightly (3 or 5 choices, etc.).
    # We accept 2-6 choices and clamp the answer index rather than rejecting the
    # whole study set over a minor formatting difference.
    question: str = Field(min_length=1, max_length=1000)
    choices: list[str] = Field(min_length=2, max_length=6)
    answer_index: int = Field(default=0, ge=0)
    explanation: str = Field(default="", max_length=2000)

    @field_validator("choices")
    @classmethod
    def choices_nonempty(cls, v: list[str]) -> list[str]:
        cleaned = [c for c in v if c and c.strip()]
        if len(cleaned) < 2:
            raise ValueError("quiz question needs at least two choices")
        return cleaned

    @model_validator(mode="after")
    def _clamp_answer(self) -> "QuizQuestion":
        if self.answer_index >= len(self.choices) or self.answer_index < 0:
            self.answer_index = 0
        return self


class TestQuestion(BaseModel):
    kind: Literal["true_false", "fill_blank", "short_answer"]
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=1000)


class MatchPair(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=1000)


class StudySetContent(BaseModel):
    """The contract between the generation layer and the rest of the app.

    A single generation pass produces every study format; the user then
    chooses which one(s) to study from. Notes also drive the read-aloud
    and video (narrated-slideshow) modes on the client.
    """

    # Lenient minimums so a good-but-slightly-short AI response is accepted
    # rather than thrown away. The mock generator still produces rich output.
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1)  # the study-guide notes
    flashcards: list[Flashcard] = Field(min_length=1, max_length=100)
    quiz: list[QuizQuestion] = Field(min_length=1, max_length=50)
    test: list[TestQuestion] = Field(min_length=1, max_length=50)
    matching: list[MatchPair] = Field(min_length=1, max_length=30)


# ---------- API responses ----------

class JobResponse(BaseModel):
    id: str
    status: str
    error: Optional[str] = None
    study_set_id: Optional[str] = None
    source_filename: str
    created_at: datetime

    model_config = {"from_attributes": True}


class StudySetSummary(BaseModel):
    id: str
    title: str
    source_filename: str
    created_at: datetime
    review_level: int = 0
    mastery: str = "Learning"
    due: bool = True
    next_review: Optional[datetime] = None

    model_config = {"from_attributes": True}


class StudySetResponse(StudySetSummary):
    summary: str
    flashcards: list[Flashcard]
    quiz: list[QuizQuestion]
    test: list[TestQuestion]
    matching: list[MatchPair]
    video: Optional[dict] = None


class QuizScoreRequest(BaseModel):
    score: int = Field(ge=0)
    total: int = Field(gt=0)


class QuizAttemptResponse(BaseModel):
    id: str
    study_set_id: str
    score: int
    total: int
    created_at: datetime

    model_config = {"from_attributes": True}
