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

import logging

from sqlalchemy import select

from . import billing, gamify
from .config import get_settings
from .models import ProcessedStripeEvent, User

log = logging.getLogger("studyforge.payments")


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
    kwargs = dict(
        mode="subscription",
        line_items=[{"price": price, "quantity": 1}],
        client_reference_id=user.id,
        customer=user.stripe_customer_id or None,
        success_url=f"{settings.app_base_url}/?checkout=success",
        cancel_url=f"{settings.app_base_url}/?checkout=cancel",
        metadata={"user_id": user.id, "kind": "plan", "plan": plan},
        # Stamp the plan on the subscription too. Events about the subscription
        # (renewal recovered, plan switched in Stripe's billing portal) carry
        # this metadata, so we can re-grant the right plan without guessing.
        subscription_data={"metadata": {"user_id": user.id, "plan": plan}},
    )
    # Loyalty + welcome-wheel discount: auto-apply the best coupon the user
    # holds (whichever is higher — level-earned or won on the welcome wheel).
    game = user.game if isinstance(user.game, dict) else {}
    level = game.get("level", 1)
    pct = max(gamify.discount_for(level), int(game.get("spin_discount", 0) or 0))
    coupon = ""
    if pct >= 20 and settings.stripe_coupon_20:
        coupon = settings.stripe_coupon_20
    elif pct >= 10 and settings.stripe_coupon_10:
        coupon = settings.stripe_coupon_10
    if coupon:
        kwargs["discounts"] = [{"coupon": coupon}]
    session = stripe.checkout.Session.create(**kwargs)
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


def create_portal_session(user: User) -> str:
    """Return a Stripe billing-portal URL so a subscriber can manage or cancel.

    Without this the only subscription UI in the app is a buy button, which a
    paying customer reads as "my subscription didn't register" — and clicking it
    starts a SECOND subscription on the same account.
    """
    if not user.stripe_customer_id:
        raise PaymentsError("No billing account on file yet.")
    settings = get_settings()
    stripe = _stripe()
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=settings.app_base_url or "/",
    )
    return session.url


def verify_event(payload: bytes, sig_header: str) -> dict:
    """Verify a webhook signature and return the parsed event as a plain dict.

    We use Stripe's construct_event only to VERIFY the signature (it raises if
    the payload was tampered with), then parse the raw bytes ourselves. The
    object construct_event returns is a StripeObject whose ``.get()`` behaves
    differently across library versions; parsing the raw JSON gives us an
    ordinary dict that process_event can read the same way in tests and prod.
    """
    import json

    settings = get_settings()
    stripe = _stripe()
    try:
        stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except Exception as e:
        raise PaymentsError(f"Invalid webhook signature: {e}")
    return json.loads(payload)


def plan_for_price(price_id: str) -> str:
    """Map a Stripe price id back to a plan name. '' if it isn't one of ours."""
    if not price_id:
        return ""
    settings = get_settings()
    if price_id == settings.stripe_price_basic:
        return "basic"
    if price_id == settings.stripe_price_pro:
        return "pro"
    return ""


def _subscription_plan(obj: dict) -> str:
    """Best-effort plan for a subscription object: metadata first, then price."""
    plan = (obj.get("metadata") or {}).get("plan")
    if plan in ("basic", "pro"):
        return plan
    try:
        items = (obj.get("items") or {}).get("data") or []
        return plan_for_price(((items[0] or {}).get("price") or {}).get("id", ""))
    except (IndexError, AttributeError, TypeError):
        return ""


def _already_processed(event: dict, db) -> bool:
    """Record this event id, or report that we've already applied it.

    Stripe retries deliveries. Applying `checkout.session.completed` twice is
    harmless for a plan but adds the credit pack twice, so every event goes
    through here first.
    """
    event_id = event.get("id")
    if not event_id:
        return False  # nothing to key on; better to apply than to drop a payment
    if db.get(ProcessedStripeEvent, event_id) is not None:
        log.info("stripe event %s already applied - skipping", event_id)
        return True
    db.add(ProcessedStripeEvent(id=event_id, kind=event.get("type", "")))
    try:
        db.commit()
    except Exception:  # concurrent delivery of the same event won the race
        db.rollback()
        return True
    return False


def process_event(event: dict, db) -> None:
    """Apply a (verified) Stripe event to the user's account. Pure logic —
    unit-testable with a plain dict, no Stripe library needed."""
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})

    if _already_processed(event, db):
        return

    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        user_id = meta.get("user_id") or obj.get("client_reference_id")
        user = db.get(User, user_id) if user_id else None
        if user is None:
            log.error("checkout completed for unknown user_id=%r session=%r",
                      user_id, obj.get("id"))
            return
        if obj.get("customer"):
            user.stripe_customer_id = obj["customer"]

        kind = meta.get("kind")
        # A session created outside our own checkout call (a Payment Link, or
        # one rebuilt in the Stripe dashboard) has no metadata. Fall back to the
        # session mode so a real payment is never silently ignored.
        if kind is None:
            kind = "plan" if obj.get("mode") == "subscription" else None

        if kind == "plan":
            plan = meta.get("plan")
            if plan not in ("basic", "pro"):
                plan = plan_for_price(_price_id_of(obj))
            if plan not in ("basic", "pro"):
                # Never default a paying customer to "free" here — that was the
                # old behaviour and it silently un-upgraded people.
                log.error("paid plan checkout with unresolvable plan: session=%r "
                          "user=%s - left plan unchanged", obj.get("id"), user.id)
                return
            user.plan = plan
            if obj.get("subscription"):
                user.stripe_subscription_id = obj["subscription"]
            log.info("upgraded user=%s to plan=%s", user.id, plan)
        elif kind == "pack":
            # Asynchronous payment methods complete the session before the money
            # arrives; only grant credits once Stripe says it is actually paid.
            if obj.get("payment_status") not in (None, "paid", "no_payment_required"):
                log.info("pack checkout not paid yet (%s) - skipping credits",
                         obj.get("payment_status"))
                return
            user.extra_video_credits = (
                (user.extra_video_credits or 0) + int(meta.get("videos", 0))
            )
        db.commit()

    elif etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        _apply_subscription_state(etype, obj, db)


def _price_id_of(session: dict) -> str:
    """Pull a price id off a checkout session if Stripe expanded the line items."""
    try:
        items = (session.get("line_items") or {}).get("data") or []
        return ((items[0] or {}).get("price") or {}).get("id", "")
    except (IndexError, AttributeError, TypeError):
        return ""


def _apply_subscription_state(etype: str, obj: dict, db) -> None:
    """React to a subscription's lifecycle, for the RIGHT subscription only.

    Two bugs used to live here:

    1. We matched on the customer alone. A customer accumulates subscriptions —
       a card that failed once, an abandoned attempt, an old plan. When any of
       those died, Stripe sent us an event and we downgraded an account that was
       paying perfectly well on a different subscription.
    2. We treated `past_due` as cancellation. Stripe sets `past_due` on the
       FIRST failed renewal attempt and then retries for weeks; most recover.
       We revoked the plan immediately and had no path to give it back, so one
       momentary card decline demoted a paying customer permanently.

    Now: only the subscription they're actually on can change their plan,
    `past_due` leaves them entitled while Stripe retries, and a recovered or
    portal-switched subscription re-grants the plan.
    """
    customer_id = obj.get("customer")
    sub_id = obj.get("id")
    # Prefer the subscription id: it identifies exactly one account. The customer
    # id is the fallback for accounts that subscribed before we started storing
    # the subscription, and it is only unique if every account has its own
    # Stripe customer (which is how create_*_checkout builds them).
    user = None
    if sub_id:
        user = db.scalar(
            select(User).where(User.stripe_subscription_id == sub_id)
        )
    if user is None and customer_id:
        user = db.scalar(select(User).where(User.stripe_customer_id == customer_id))
    if user is None:
        return
    # If we know which subscription they pay on, ignore events about any other.
    if user.stripe_subscription_id and sub_id and sub_id != user.stripe_subscription_id:
        log.info("ignoring %s for stale subscription %s (user %s pays on %s)",
                 etype, sub_id, user.id, user.stripe_subscription_id)
        return

    status = obj.get("status")
    if etype == "customer.subscription.deleted" or status in (
        "canceled", "unpaid", "incomplete_expired"
    ):
        if user.plan != "free":
            log.info("downgrading user=%s (event=%s status=%s)", user.id, etype, status)
        user.plan = "free"
        user.stripe_subscription_id = None
        db.commit()
    elif status in ("active", "trialing"):
        # Covers: a retried payment succeeding, and a plan switched inside
        # Stripe's billing portal (which never sends a checkout session).
        plan = _subscription_plan(obj)
        if plan in ("basic", "pro"):
            if user.plan != plan:
                log.info("granting user=%s plan=%s from %s", user.id, plan, etype)
            user.plan = plan
            user.stripe_subscription_id = sub_id or user.stripe_subscription_id
            db.commit()
    # `past_due` and `incomplete` deliberately do nothing: Stripe is still
    # trying to collect, and the customer keeps what they paid for meanwhile.
