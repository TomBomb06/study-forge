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
PLANS: dict[str, dict] = {
    "free":  {"name": "Free",  "monthly_videos": 0,  "price_usd": 0},
    "basic": {"name": "Basic", "monthly_videos": 10, "price_usd": 9},
    "pro":   {"name": "Pro",   "monthly_videos": 40, "price_usd": 19},
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
    """Reset the monthly counter when a new month starts. Caller commits."""
    cur = _current_period()
    if getattr(user, "usage_period", None) != cur:
        user.usage_period = cur
        user.videos_used = 0


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
