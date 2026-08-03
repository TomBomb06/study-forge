"""Plan quotas and video-usage metering.

This is the money-control layer: every paid video generation must pass
through `consume_video`, which enforces the user's monthly allowance plus
any add-on packs they've bought. It is deliberately provider-agnostic — it
doesn't care whether the video comes from Higgsfield or anywhere else, and
it doesn't move money itself (Stripe does that, later). It only answers:
"is this user allowed one more video right now, and if so, deduct it."
"""

from datetime import date

# Plan catalog. Prices are placeholders for now — tune once you've tested
# willingness to pay. `monthly_videos` is the allowance included each month.
# Per-plan monthly allowances:
#   monthly_sets      — study sets a user can generate per month (the main AI cost).
#   monthly_tts_chars — AI read-aloud characters per month.
#   transcription     — may turn audio/video into study sets (the priciest feature).
# Free users get a generous taste; when they run out it's a 402 → upgrade.
UNLIMITED = 100000  # displayed as "Unlimited"; still a ceiling to stop abuse.

PLANS: dict[str, dict] = {
    "free":  {"name": "Free",  "monthly_videos": 0,  "price_usd": 0,
              "monthly_sets": 5,         "monthly_tts_chars": 6000,   "transcription": False},
    "basic": {"name": "Basic", "monthly_videos": 10, "price_usd": 9,
              "monthly_sets": 120,       "monthly_tts_chars": 150000, "transcription": True},
    "pro":   {"name": "Pro",   "monthly_videos": 40, "price_usd": 19,
              "monthly_sets": UNLIMITED, "monthly_tts_chars": 600000, "transcription": True},
}

DEFAULT_PLAN = "free"

# Add-on packs a user can buy when they run out (the "buy more" path).
CREDIT_PACKS: dict[str, dict] = {
    "small":  {"videos": 5,  "price_usd": 5},
    "medium": {"videos": 15, "price_usd": 12},
    "large":  {"videos": 40, "price_usd": 28},
}


def _current_period() -> str:
    return date.today().strftime("%Y-%m")


def ensure_period(user) -> None:
    """Reset the monthly counters when a new month starts. Caller commits."""
    cur = _current_period()
    if getattr(user, "usage_period", None) != cur:
        user.usage_period = cur
        user.videos_used = 0
        user.sets_used = 0


def plan_of(user) -> dict:
    return PLANS.get(user.plan or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def video_status(user) -> dict:
    """Everything the client needs to show the user's video balance."""
    ensure_period(user)
    plan = plan_of(user)
    quota = plan["monthly_videos"]
    used = user.videos_used or 0
    plan_remaining = max(0, quota - used)
    extra = user.extra_video_credits or 0
    return {
        "plan": user.plan or DEFAULT_PLAN,
        "plan_name": plan["name"],
        "monthly_quota": quota,
        "videos_used": used,
        "plan_remaining": plan_remaining,
        "extra_credits": extra,
        "total_remaining": plan_remaining + extra,
        "can_generate_video": (plan_remaining + extra) > 0,
    }


# ---------------- AI voice (read-aloud) metering ----------------

def ensure_tts_period(user) -> None:
    """Reset the monthly voice-character counter when a new month starts."""
    cur = _current_period()
    if getattr(user, "tts_period", None) != cur:
        user.tts_period = cur
        user.tts_chars_used = 0


def voice_status(user) -> dict:
    """The user's AI-voice character balance for this month."""
    ensure_tts_period(user)
    plan = plan_of(user)
    quota = plan.get("monthly_tts_chars", 0)
    used = user.tts_chars_used or 0
    remaining = max(0, quota - used)
    return {
        "plan": user.plan or DEFAULT_PLAN,
        "plan_name": plan["name"],
        "monthly_chars": quota,
        "chars_used": used,
        "remaining": remaining,
        "can_use": remaining > 0,
    }


def consume_tts_chars(user, n: int) -> dict:
    """Deduct `n` characters from the user's monthly voice allowance."""
    ensure_tts_period(user)
    user.tts_chars_used = (user.tts_chars_used or 0) + max(0, int(n))
    return voice_status(user)


# ---------------- study-set generation metering (the main AI cost) ----------------

def sets_status(user) -> dict:
    """The user's monthly study-set generation balance."""
    ensure_period(user)
    plan = plan_of(user)
    quota = plan.get("monthly_sets", 0)
    used = user.sets_used or 0
    remaining = max(0, quota - used)
    return {
        "plan": user.plan or DEFAULT_PLAN,
        "monthly_sets": quota,
        "sets_used": used,
        "remaining": remaining,
        "unlimited": quota >= UNLIMITED,
        "can_create": remaining > 0,
    }


def consume_set(user) -> dict:
    """Count one generated study set against the monthly allowance."""
    ensure_period(user)
    user.sets_used = (user.sets_used or 0) + 1
    return sets_status(user)


def refund_set(user) -> dict:
    """Give a set-credit back (e.g. when generation fails)."""
    ensure_period(user)
    if (user.sets_used or 0) > 0:
        user.sets_used -= 1
    return sets_status(user)


def can_transcribe(user) -> bool:
    """Audio/video transcription is a paid feature (it's the priciest to run)."""
    return bool(plan_of(user).get("transcription", False))


class QuotaExceeded(Exception):
    """Raised when a user has no video allowance left."""


def consume_video(user) -> dict:
    """Deduct one video from the user's balance, or raise QuotaExceeded.

    Spends the monthly plan allowance first, then any bought add-on packs.
    Must be called (and committed) BEFORE kicking off the paid generation,
    so we never pay a provider for a video the user wasn't entitled to.
    """
    status = video_status(user)
    if not status["can_generate_video"]:
        raise QuotaExceeded()
    if status["plan_remaining"] > 0:
        user.videos_used = (user.videos_used or 0) + 1
    else:
        user.extra_video_credits = (user.extra_video_credits or 0) - 1
    return video_status(user)


def refund_video(user) -> dict:
    """Give back one deducted video (e.g. when generation fails)."""
    ensure_period(user)
    if (user.videos_used or 0) > 0:
        user.videos_used -= 1
    else:
        user.extra_video_credits = (user.extra_video_credits or 0) + 1
    return video_status(user)
