"""Usage, metered video generation, and plan/credit management.

The video endpoint is the paywall in action: it checks the user's allowance
BEFORE doing any (paid) work, deducts one on success, and returns a clear
"upgrade or buy more" payload when they're out.

`change_plan` and `add_credits` are DEV STAND-INS for what Stripe will do
later (a successful subscription/checkout webhook flips the plan or adds
packs). They let you exercise the whole flow now without real payments.
"""

import os
import re
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import billing, gamify, payments, srs
from ..auth import get_current_user
from ..config import get_settings
from ..db import get_db
from ..models import QuizAttempt, StudySet, User
from ..pipeline import video
from ..pipeline.jobs import run_video_job

router = APIRouter(tags=["billing"])

# A video stuck on "processing" for longer than this is assumed dead (the
# worker was killed mid-job) and may be retried. Generation itself is capped
# well below this by the provider timeout.
STALE_VIDEO_SECONDS = 15 * 60


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
def get_usage(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Per-user billing state: never let a proxy or CDN hand one user's plan
    # to another, or serve a stale "free" after an upgrade.
    response.headers["Cache-Control"] = "no-store"
    status = billing.video_status(user)
    sets = billing.sets_status(user)
    db.commit()  # persist any monthly reset ensure_period() applied
    settings = get_settings()
    plan = user.plan or "free"
    return {
        "email": user.email,
        "video": status,
        "sets": sets,
        "can_transcribe": billing.can_transcribe(user),
        # False while VIDEO_PROVIDER=stub: the UI must not sell or charge for
        # AI video until a real provider key is connected.
        "video_live": video.is_live(),
        "plans": billing.PLANS,
        "credit_packs": billing.CREDIT_PACKS,
        "billing_provider": settings.billing_provider,
        # Ads: free users see them — unless they're holding an ad-free pass
        # they bought with coins. Paying users never see ads.
        "show_ads": plan == "free" and not gamify.adfree_active(user),
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


@router.post("/billing/portal")
def billing_portal(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Open Stripe's billing portal so a subscriber can manage or cancel."""
    if get_settings().billing_provider != "stripe":
        raise HTTPException(status_code=400, detail="Billing isn't live yet.")
    try:
        return {"url": payments.create_portal_session(user)}
    except payments.PaymentsError as e:
        raise HTTPException(status_code=400, detail=str(e))


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

    # Already have a video? Don't charge again.
    if ss.video and ss.video.get("status") == "ready":
        return {"video": ss.video, "remaining": billing.video_status(user)}
    # Still making one? Same — unless the worker died mid-job. A process
    # restart (deploy, OOM) leaves the row on "processing" forever, and this
    # guard used to make that permanent: charged a credit, no video, no retry.
    if ss.video and ss.video.get("status") == "processing":
        started = ss.video.get("started_at") or 0
        if time.time() - started < STALE_VIDEO_SECONDS:
            return {"video": ss.video, "remaining": billing.video_status(user)}
        # Stale. Refund the old attempt before charging for a fresh one.
        billing.refund_video(user, ss.video.get("charged", ""))
        db.commit()

    # In demo mode there is no real video to make, so charging for one is
    # simply taking a paid allowance and returning a placeholder. Hand back the
    # preview and leave their balance alone.
    if not video.is_live():
        ss.video = video.generate_video_asset(ss)
        db.commit()
        return {"video": ss.video, "remaining": billing.video_status(user), "demo": True}

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
    ss.video = {
        "status": "processing",
        "started_at": time.time(),
        "charged": remaining.get("charged", ""),
    }
    db.commit()  # deduction + processing state are now durable

    background.add_task(run_video_job, ss.id)
    return {"video": ss.video, "remaining": remaining}


@router.get("/media/videos/{name}")
def serve_video(name: str):
    """Stream a generated video.

    The filename is a random 128-bit hex, which is the capability — the same
    unguessable-URL model every media host uses. It is validated strictly so a
    crafted name can never walk out of the videos directory.
    """
    if not re.fullmatch(r"[0-9a-f]{32}\.mp4", name or ""):
        raise HTTPException(status_code=404, detail="Not found.")
    path = os.path.join(video.video_dir(), name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(path, media_type="video/mp4",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/study-sets/{study_set_id}/video")
def get_video(
    study_set_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ss = _owned_set(study_set_id, user, db)
    return {"video": ss.video, "remaining": billing.video_status(user)}


# ---------- DEV stand-ins for Stripe (replace with payment webhooks) ----------
#
# These two routes hand out paid plans and video credits for free. They exist so
# the whole flow can be exercised without real money. That makes them a wide-open
# door in production: any signed-up user could POST /me/plan {"plan":"pro"} and
# grant themselves Pro forever, with no Stripe record and nothing to notice.
# _require_dev_billing() slams that door whenever real payments are switched on.

class PlanChange(BaseModel):
    plan: str


class CreditPurchase(BaseModel):
    pack: str


def _require_dev_billing() -> None:
    """Refuse the free-upgrade test routes once real payments are live."""
    if get_settings().billing_provider != "dev":
        # 404, not 403: don't advertise that the route exists at all.
        raise HTTPException(status_code=404, detail="Not found.")


@router.post("/me/plan")
def change_plan(
    body: PlanChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """DEV ONLY: simulate a successful plan upgrade (real: Stripe webhook)."""
    _require_dev_billing()
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
    _require_dev_billing()
    pack = billing.CREDIT_PACKS.get(body.pack)
    if pack is None:
        raise HTTPException(status_code=422, detail="Unknown credit pack.")
    user.extra_video_credits = (user.extra_video_credits or 0) + pack["videos"]
    db.commit()
    return billing.video_status(user)
