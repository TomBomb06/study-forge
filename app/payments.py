"""Stripe payments: Checkout for subscriptions + credit packs, and the
webhook that turns a successful payment into plan/credit changes.

Design mirrors the other providers — nothing here charges anyone until
BILLING_PROVIDER=stripe and real keys are set. The event-processing logic
(process_event) is split from signature verification so it can be unit
tested without the Stripe library doing crypto.

Money flow: the customer pays Stripe directly — you never touch card data. On success
Stripe calls our webhook; we look the user up by the id we stamped on the
Checkout session and update their plan or add video credits.
"""

from sqlalchemy import select

from . import billing
from .config import get_settings
from .models import User


class PaymentsError(Exception):
    """User-facing payment/config problem."""


def _stripe():
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise PaymentsError("Stripe isn't configured (STRIPE_SECRET_KEY missing).")
    try:
        import stripe
    except ImportError:
        raise PaymentsError("The 'stripe' package isn't installed. Run: pip install stripe")
    stripe.api_key = settings.stripe_secret_key
    return stripe


def _plan_price(settings, plan: str) -> str:
    return {"basic": settings.stripe_price_basic, "pro": settings.stripe_price_pro}.get(plan, "")


def _pack_price(settings, pack: str) -> str:
    return {
        "small": settings.stripe_price_pack_small,
        "medium": settings.stripe_price_pack_medium,
        "large": settings.stripe_price_pack_large,
    }.get(pack, "")


def create_plan_checkout(user: User, plan: str) -> str:
    """Return a Stripe Checkout URL for a subscription upgrade."""
    settings = get_settings()
    if plan not in ("basic", "pro"):
        raise PaymentsError("Unknown plan.")
    price = _plan_price(settings, plan)
    if not price:
        raise PaymentsError(f"No Stripe price configured for the {plan} plan.")
    stripe = _stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price, "quantity": 1}],
        client_reference_id=user.id,
        customer=user.stripe_customer_id or None,
        success_url=f"{settings.app_base_url}/?checkout=success",
        cancel_url=f"{settings.app_base_url}/?checkout=cancel",
        metadata={"user_id": user.id, "kind": "plan", "plan": plan},
        subscription_data={"metadata": {"user_id": user.id, "plan": plan}},
    )
    return session.url


def create_pack_checkout(user: User, pack: str) -> str:
    """Return a Stripe Checkout URL for a one-time credit pack."""
    settings = get_settings()
    info = billing.CREDIT_PACKS.get(pack)
    if info is None:
        raise PaymentsError("Unknown credit pack.")
    price = _pack_price(settings, pack)
    if not price:
        raise PaymentsError(f"No Stripe price configured for the {pack} pack.")
    stripe = _stripe()
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price, "quantity": 1}],
        client_reference_id=user.id,
        customer=user.stripe_customer_id or None,
        success_url=f"{settings.app_base_url}/?checkout=success",
        cancel_url=f"{settings.app_base_url}/?checkout=cancel",
        metadata={"user_id": user.id, "kind": "pack", "pack": pack, "videos": info["videos"]},
    )
    return session.url


def verify_event(payload: bytes, sig_header: str) -> dict:
    """Verify a webhook signature and return the parsed event."""
    settings = get_settings()
    stripe = _stripe()
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except Exception as e:
        raise PaymentsError(f"Invalid webhook signature: {e}")


def process_event(event: dict, db) -> None:
    """Apply a (verified) Stripe event to the user's account. Pure logic —
    unit-testable with a plain dict, no Stripe library needed."""
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        user_id = meta.get("user_id") or obj.get("client_reference_id")
        user = db.get(User, user_id) if user_id else None
        if user is None:
            return
        if obj.get("customer"):
            user.stripe_customer_id = obj["customer"]
        if meta.get("kind") == "plan":
            user.plan = meta.get("plan", "free")
        elif meta.get("kind") == "pack":
            user.extra_video_credits = (user.extra_video_credits or 0) + int(meta.get("videos", 0))
        db.commit()

    elif etype in ("customer.subscription.deleted",):
        _downgrade_by_customer(obj.get("customer"), db)

    elif etype == "customer.subscription.updated":
        status = obj.get("status")
        if status in ("canceled", "unpaid", "incomplete_expired", "past_due"):
            _downgrade_by_customer(obj.get("customer"), db)


def _downgrade_by_customer(customer_id, db) -> None:
    if not customer_id:
        return
    user = db.scalar(select(User).where(User.stripe_customer_id == customer_id))
    if user is not None:
        user.plan = "free"
        db.commit()
