"""Server-side visitor counting.

The problem this solves
----------------------
The Meta Pixel and Google Analytics both sit behind the cookie banner: neither
fires until a visitor clicks "Accept". Most visitors never touch the banner, so
most visits are invisible to both. On Sep 2 2026 the Pixel had recorded two
page views in two days while TikTok and Instagram were reportedly sending
traffic — that gap is what this module closes.

The server sees every request whether or not anyone consents to anything. So we
count here instead, and we count in a way that needs no consent: one row per
day per page per traffic source, holding nothing but a number. No cookies, no
IP addresses, no user agents, no identifiers, no way to reconstruct a person or
a session. That is a deliberate trade — it means we cannot report unique
visitors, only views, and reporting views honestly beats reporting uniques by
fingerprinting people who declined to be tracked.

Nothing in here may ever break a page load. Every entry point swallows its own
exceptions: a failed count is worth strictly less than a served page.
"""

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .models import PageHit, StudySet, User

# Pages worth counting. Anything else — API calls, assets, probes — is noise.
COUNTED_PATHS = {"/", "/privacy", "/terms", "/app", "/app/"}

# Crawlers announce themselves. They are not visitors and counting them would
# make a quiet week look like a good one.
_BOT = re.compile(
    r"bot|crawler|spider|crawling|slurp|bingpreview|facebookexternalhit|"
    r"headlesschrome|python-requests|curl/|wget|monitor|uptime|pingdom|"
    r"lighthouse|gtmetrix|semrush|ahrefs|mj12|dotbot|petalbot|bytespider",
    re.I,
)

# Referrer host → the name a human would use for that source. Checked as a
# suffix match, so "l.instagram.com" and "www.instagram.com" both land on
# "instagram".
_SOURCES = [
    ("tiktok.com", "tiktok"),
    ("instagram.com", "instagram"),
    ("facebook.com", "facebook"),
    ("fb.me", "facebook"),
    ("messenger.com", "facebook"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("google.", "google"),
    ("bing.com", "bing"),
    ("duckduckgo.com", "duckduckgo"),
    ("reddit.com", "reddit"),
    ("x.com", "x"),
    ("twitter.com", "x"),
    ("t.co", "x"),
    ("snapchat.com", "snapchat"),
    ("discord.com", "discord"),
    ("linkedin.com", "linkedin"),
    ("pinterest.com", "pinterest"),
]

MAX_SOURCE_LEN = 40


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clean(value: str) -> str:
    """Reduce arbitrary query input to a short, safe label."""
    v = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
    return v[:MAX_SOURCE_LEN]


def classify(referer: str, query_params=None) -> str:
    """Where did this visit come from?

    An explicit ?utm_source= wins, because it is the only thing that survives
    an in-app browser. TikTok and Instagram open bio links in their own webview
    and frequently send no Referer header at all — without a tagged link those
    visits are indistinguishable from someone typing the URL. This is why the
    bio link should read forge.study/?utm_source=tiktok.
    """
    params = query_params or {}
    utm = _clean(params.get("utm_source") or "")
    if utm:
        return utm
    # Meta stamps every ad click with fbclid, so an ad click is identifiable
    # even when the referrer is stripped.
    if params.get("fbclid"):
        return "meta-ads"
    if params.get("gclid"):
        return "google-ads"
    if params.get("ttclid"):
        return "tiktok-ads"

    host = ""
    try:
        host = (urlparse(referer or "").hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return "direct"
    if host.endswith("forge.study"):
        return "internal"
    for needle, name in _SOURCES:
        if needle in host:
            return name
    return _clean(host.removeprefix("www.")) or "other"


def should_count(path: str, user_agent: str) -> bool:
    return path in COUNTED_PATHS and not _BOT.search(user_agent or "")


def record(db, path: str, referer: str, user_agent: str, query_params=None) -> None:
    """Add one to today's count. Never raises."""
    try:
        if not should_count(path, user_agent):
            return
        day, source = _today(), classify(referer, query_params)
        row = (
            db.query(PageHit)
            .filter(PageHit.day == day, PageHit.path == path, PageHit.source == source)
            .one_or_none()
        )
        if row is None:
            db.add(PageHit(day=day, path=path, source=source, views=1))
            try:
                db.commit()
            except IntegrityError:
                # Two requests raced to create the same row. The other one won;
                # fall through and increment it instead of losing this view.
                db.rollback()
                row = (
                    db.query(PageHit)
                    .filter(PageHit.day == day, PageHit.path == path,
                            PageHit.source == source)
                    .one_or_none()
                )
                if row is None:
                    return
                row.views = (row.views or 0) + 1
                db.commit()
        else:
            row.views = (row.views or 0) + 1
            db.commit()
    except Exception:
        # Analytics must never cost a page load. Give up silently.
        try:
            db.rollback()
        except Exception:
            pass


# ------------------------------------------------------------------ reporting

def summary(db, days: int = 14) -> dict:
    """Everything the dashboard shows, in one query pass."""
    days = max(1, min(days, 90))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    start_s = start.strftime("%Y-%m-%d")
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)

    rows = db.query(PageHit).filter(PageHit.day >= start_s).all()

    by_day: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_page: dict[str, int] = {}
    for r in rows:
        v = r.views or 0
        by_day[r.day] = by_day.get(r.day, 0) + v
        by_source[r.source] = by_source.get(r.source, 0) + v
        by_page[r.path] = by_page.get(r.path, 0) + v

    # Days with no traffic are the most informative days on the chart, so they
    # have to appear as zeroes rather than being missing.
    daily = [
        {"day": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
         "views": by_day.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
        for i in range(days)
    ]

    def _since(dt):
        return db.query(func.count(User.id)).filter(User.created_at >= dt).scalar() or 0

    midnight = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    total_views = sum(by_day.values())
    signups_window = _since(start_dt)

    return {
        "window_days": days,
        "since": start_s,
        "views_total": total_views,
        "daily": daily,
        "sources": sorted(({"source": k, "views": v} for k, v in by_source.items()),
                          key=lambda d: -d["views"]),
        "pages": sorted(({"path": k, "views": v} for k, v in by_page.items()),
                        key=lambda d: -d["views"]),
        "users_total": db.query(func.count(User.id)).scalar() or 0,
        "users_today": _since(midnight),
        "users_window": signups_window,
        "sets_total": db.query(func.count(StudySet.id)).scalar() or 0,
        "sets_window": db.query(func.count(StudySet.id))
                         .filter(StudySet.created_at >= start_dt).scalar() or 0,
        # The number that decides whether the problem is traffic or the page.
        "signup_rate": (round(100.0 * signups_window / total_views, 1)
                        if total_views else None),
    }
