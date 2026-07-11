"""Spaced-repetition scheduling (a lightweight Leitner system).

Each study set has a review "level". Doing well on its quiz moves it up a
level (longer until the next review); doing poorly moves it down (resurfaces
it sooner). A set is "due" when its next_review date has passed — that's what
powers the "review these today" list, so weak topics come back automatically.
"""

from datetime import datetime, timedelta, timezone

# Days until next review, indexed by level. Level 0 = brand new / struggling.
INTERVALS_DAYS = [1, 2, 4, 9, 21, 45]
MAX_LEVEL = len(INTERVALS_DAYS) - 1

_MASTERY = ["Learning", "Learning", "Familiar", "Familiar", "Strong", "Mastered"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mastery_label(level: int) -> str:
    level = max(0, min(level, MAX_LEVEL))
    return _MASTERY[level]


def next_level(current_level: int, score: int, total: int) -> int:
    """New level after a quiz: >=80% up, >=50% hold, else down."""
    pct = (score / total) if total else 0
    if pct >= 0.8:
        return min(current_level + 1, MAX_LEVEL)
    if pct >= 0.5:
        return current_level
    return max(current_level - 1, 0)


def apply_review(study_set, score: int, total: int) -> None:
    """Advance a study set's schedule after a quiz. Caller commits."""
    level = next_level(study_set.review_level or 0, score, total)
    study_set.review_level = level
    study_set.last_reviewed = _now()
    study_set.next_review = _now() + timedelta(days=INTERVALS_DAYS[level])


def is_due(study_set, now: datetime = None) -> bool:
    """A set is due if it's never been reviewed or its review date has passed."""
    if study_set.next_review is None:
        return True
    now = now or _now()
    nr = study_set.next_review
    if nr.tzinfo is None:  # SQLite may return naive datetimes
        nr = nr.replace(tzinfo=timezone.utc)
    return nr <= now
