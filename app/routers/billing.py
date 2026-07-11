"""Usage, metered video generation, and plan/credit management.

The video endpoint is the paywall in action: it checks the user's allowance
BEFORE doing any (paid) work, deducts one on success, and returns a clear
"upgrade or buy more" payload when they're out.

`change_plan` and `add_credits` are DEV STAND-INS for what Stripe will do
later (a successful subscription/checkout webhook flips the plan or adds
packs). They let you exercise the whole flow now without real payments.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import billing, payments, srs
from ..auth import get_current_user
from ..config import get_settings
from ..db import get_db
from ..models import QuizAttempt, StudySet, User
from ..pipeline.jobs import run_video_job

router = APIRouter(tags=["billing"])


@router.get("/me/progress")
def get_progress(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Dashboard data: totals, score trend, and per-set mastery / review status."""
    sets = db.scalars(
        select(StudySet).where(StudySet.user_id == user.id)
    ).all()
    attempts = db.scalars(
        select(QuizAttempt)
        .where(QuizAttempt.user_id == user.id)
        .order_by(QuizAttempt.created_at.asc())
    ).all()

    titles = {s.id: s.title for s in sets}
    trend = [
        {
            "date": a.created_at.isoformat(),
            "pct": round(100 * a.score / a.total) if a.total else 0,
            "title": titles.get(a.study_set_id, "Study set"),
        }
        for a in attempts
    ]
    avg = round(sum(t["pct"] for t in trend) / len(trend)) if trend else None

    set_rows = [
        {
            "id": s.id,
            "title": s.title,
            "level": s.review_level or 0,
            "mastery": srs.mastery_label(s.review_level or 0),
            "due": srs.is_due(s),
            "next_review": s.next_review.isoformat() if s.next_review else None,
        }
        for s in sets
    ]
    # Due sets first, then least-mastered.
    set_rows.sort(key=lambda r: (not r["due"], r["level"]))

    return {
        "totals": {
            "sets": len(sets),
            "quizzes_taken": len(attempts),
            "avg_score": avg,
            "due_count": sum(1 for r in set_rows if r["due"]),
        },
        "trend": trend[-30:],
        "sets": set_rows,
    }


@router.get("/me/usage")
def get_usage(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status = billing.video_status(user)
    db.commit()  # persist any monthly reset ensure_period() applied
    settings = get_settings()
    plan = user.plan or "free"
    return {
        "email": user.email,
        "video": status,
        "plans": billing.PLANS,
        "credit_packs": billing.CREDIT_PACKS,
        "billing_provider": settings.billing_provider,
        # Ads: free users see them, paying users never do.
        "show_ads": plan == "free",
        "ads": {
            "provider": settings.ads_provider,
            "client_id": settings.adsense_client_id,
            "slots": {
                "home": settings.adsense_slot_home,
                "quiz": settings.adsense_slot_quiz,
                "break": settings.adsense_slot_break,
            },
            # How the free-tier ad breaks are paced (client reads these).
            # Shown every Nth study action, never more often than min_gap_seconds,
            # and always skippable after skip_after_seconds. Paced to stay light.
            "pacing": {
                "every_actions": 3,
                "min_gap_seconds": 150,
                "skip_after_seconds": 5,
            },
        },
    }


# ---------- Checkout (works in both dev and stripe modes) ----------

class PlanChoice(BaseModel):
    plan: str


class PackChoice(BaseModel):
    pack: str


@router.post("/billing/checkout/plan")
def checkout_plan(
    body: PlanChoice,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upgrade a plan. In 'stripe' mode returns a Checkout URL to redirect to;
    in 'dev' mode applies instantly (no real payment)."""
    if body.plan not in billing.PLANS:
        raise HTTPException(status_code=422, detail="Unknown plan.")
    if get_settings().billing_provider == "stripe":
        try:
            return {"mode": "redirect", "url": payments.create_plan_checkout(user, body.plan)}
        except payments.PaymentsError as e:
            raise HTTPException(status_code=400, detail=str(e))
    user.plan = body.plan
    db.commit()
    return {"mode": "applied", "video": billing.video_status(user)}


@router.post("/billing/checkout/pack")
def checkout_pack(
    body: PackChoice,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buy a video pack. Stripe mode → Checkout URL; dev mode → applied instantly."""
    pack = billing.CREDIT_PACKS.get(body.pack)
    if pack is None:
        raise HTTPException(status_code=422, detail="Unknown credit pack.")
    if get_settings().billing_provider == "stripe":
        try:
            return {"mode": "redirect", "url": payments.create_pack_checkout(user, body.pack)}
        except payments.PaymentsError as e:
            raise HTTPException(status_code=400, detail=str(e))
    user.extra_video_credits = (user.extra_video_credits or 0) + pack["videos"]
    db.commit()
    return {"mode": "applied", "video": billing.video_status(user)}


@router.post("/billing/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Receive Stripe events (payment succeeded, subscription canceled, …)."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = payments.verify_event(payload, sig)
    except payments.PaymentsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    payments.process_event(event, db)
    return {"received": True}


def _owned_set(study_set_id, user, db) -> StudySet:
    ss = db.get(StudySet, study_set_id)
    if ss is None or ss.user_id != user.id:
        raise HTTPException(status_code=404, detail="Study set not found.")
    return ss


@router.post("/study-sets/{study_set_id}/video", status_code=202)
def generate_video(
    study_set_id: str,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start a metered video generation. Real video takes a minute or two, so
    this returns immediately with status 'processing'; the client polls
    GET /study-sets/{id}/video. The allowance is charged up front and
    refunded automatically if generation fails."""
    ss = _owned_set(study_set_id, user, db)

    # Already have (or are making) a video? Don't charge again.
    if ss.video and ss.video.get("status") in ("ready", "processing"):
        return {"video": ss.video, "remaining": billing.video_status(user)}

    # Enforce the paywall BEFORE scheduling any paid work.
    try:
        remaining = billing.consume_video(user)
    except billing.QuotaExceeded:
        db.commit()
        raise HTTPException(
            status_code=402,  # Payment Required
            detail={
                "message": "You're out of video generations for this month.",
                "video": billing.video_status(user),
                "upgrade_to": [p for p in ("basic", "pro") if p != user.plan],
                "credit_packs": billing.CREDIT_PACKS,
            },
        )
    ss.video = {"status": "processing"}
    db.commit()  # deduction + processing state are now durable

    background.add_task(run_video_job, ss.id)
    return {"video": ss.video, "remaining": remaining}


@router.get("/study-sets/{study_set_id}/video")
def get_video(
    study_set_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ss = _owned_set(study_set_id, user, db)
    return {"video": ss.video, "remaining": billing.video_status(user)}


# ---------- DEV stand-ins for Stripe (replace with payment webhooks) ----------

class PlanChange(BaseModel):
    plan: str


class CreditPurchase(BaseModel):
    pack: str


@router.post("/me/plan")
def change_plan(
    body: PlanChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """DEV ONLY: simulate a successful plan upgrade (real: Stripe webhook)."""
    if body.plan not in billing.PLANS:
        raise HTTPException(status_code=422, detail="Unknown plan.")
    user.plan = body.plan
    db.commit()
    return billing.video_status(user)


@router.post("/me/credits")
def buy_credits(
    body: CreditPurchase,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """DEV ONLY: simulate buying an add-on video pack (real: Stripe checkout)."""
    pack = billing.CREDIT_PACKS.get(body.pack)
    if pack is None:
        raise HTTPException(status_code=422, detail="Unknown credit pack.")
    user.extra_video_credits = (user.extra_video_credits or 0) + pack["videos"]
    db.commit()
    return billing.video_status(user)
