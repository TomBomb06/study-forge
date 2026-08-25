import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .config import get_settings, looks_like_production, verify_production_config
from .db import Base, engine
from .routers import (auth, billing, gamify, math, shares, study_sets, tutor,
                      uploads, voice)

# MVP: create tables on startup. Move to Alembic migrations before production.
Base.metadata.create_all(bind=engine)


def _ensure_columns() -> None:
    """Add columns introduced after a user's DB was first created.

    create_all() won't alter existing tables (SQLite or Postgres), so add any
    missing columns by hand. Only columns that are actually missing are added,
    which keeps existing accounts and data intact when new features land.
    """
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    # NOTE: types below are written to work on the backend where they can
    # actually be missing. DATETIME columns predate the Postgres deploy, so on
    # Postgres they always already exist and their SQLite-flavored types are
    # never executed there.
    plan = {
        "study_sets": {
            "source_text": "TEXT",
            "test": "JSON",
            "matching": "JSON",
            "video": "JSON",
            "review_level": "INTEGER DEFAULT 0",
            "last_reviewed": "DATETIME",
            "next_review": "DATETIME",
            "share_token": "VARCHAR(32)",
            "imported_from": "VARCHAR(32)",
        },
        "users": {
            "plan": "VARCHAR(20) DEFAULT 'free'",
            "usage_period": "VARCHAR(7) DEFAULT ''",
            "videos_used": "INTEGER DEFAULT 0",
            "extra_video_credits": "INTEGER DEFAULT 0",
            "sets_used": "INTEGER DEFAULT 0",
            "tts_chars_used": "INTEGER DEFAULT 0",
            "tts_period": "VARCHAR(7) DEFAULT ''",
            "stripe_customer_id": "VARCHAR(64)",
            "stripe_subscription_id": "VARCHAR(64)",
            "token_version": "INTEGER DEFAULT 0",
            "tz_offset_min": "INTEGER",
            "display_name": "VARCHAR(40)",
            "game": "JSON",
            "voice": "VARCHAR(64)",
        },
    }
    with engine.begin() as conn:
        for table, additions in plan.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, coltype in additions.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))


_ensure_columns()

# Fail fast on an insecure production setup rather than serving users from it.
# In development this just prints what would be wrong on the live server.
_config_problems = verify_production_config()
if _config_problems:
    for _p in _config_problems:
        print(f"[config warning] {_p}")

_IS_PROD = looks_like_production(get_settings())

app = FastAPI(
    title="StudyForge API",
    version="0.1.0",
    description="Upload study material, get back a generated study kit.",
    # The interactive docs publish every route, including the dev-only
    # free-upgrade endpoints. Useful locally, a map for anyone else.
    docs_url=None if _IS_PROD else "/docs",
    redoc_url=None if _IS_PROD else "/redoc",
    openapi_url=None if _IS_PROD else "/openapi.json",
)

# Allow the web frontend (and a future Expo app in dev) to call the API.
_origins_setting = get_settings().allowed_origins.strip()
_origins = ["*"] if _origins_setting == "*" else [
    o.strip() for o in _origins_setting.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CSP = "; ".join([
    "default-src 'self'",
    # 'unsafe-inline' is required: the whole app is one inline <script>.
    "script-src 'self' 'unsafe-inline' https://connect.facebook.net "
    "https://www.googletagmanager.com https://pagead2.googlesyndication.com "
    "https://googleads.g.doubleclick.net https://tpc.googlesyndication.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    "media-src 'self' data: blob: https:",
    "connect-src 'self' https://www.google-analytics.com "
    "https://connect.facebook.net https://pagead2.googlesyndication.com",
    "frame-src https://googleads.g.doubleclick.net https://tpc.googlesyndication.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline hardening headers.

    nosniff stops a browser from treating an uploaded file as executable
    script; DENY on framing stops clickjacking (an attacker iframing
    forge.study invisibly over their own buttons); the referrer policy stops
    study-set IDs in URLs leaking to third parties via the Referer header.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), payment=()")
    # The session token lives in localStorage, so a CSP is the difference
    # between "an XSS somewhere" and "every account". Kept to exactly the
    # third parties actually loaded: Meta Pixel, GA, AdSense, Google Fonts.
    response.headers.setdefault("Content-Security-Policy", _CSP)
    if _IS_PROD:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(study_sets.router)
app.include_router(billing.router)
app.include_router(shares.router)
app.include_router(gamify.router)
app.include_router(tutor.router)
app.include_router(voice.router)
app.include_router(math.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# Serve the web app. Any non-API path falls through to index.html so the
# single-page app handles its own routing.
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

# The HTML must never be cached by the browser, or a fresh deploy won't show
# up until users hard-refresh. no-cache = "always revalidate with the server";
# the file is tiny so this costs nothing and guarantees everyone sees updates.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}


def _extract_pixel_id(raw: str) -> str:
    """Pull a usable Pixel ID out of whatever was pasted into the setting.

    Accepts the plain number ("1234567890123456") but also copes with someone
    pasting Meta's whole <script> snippet — we look for fbq('init','<id>') and
    otherwise fall back to the longest run of digits. Returns digits only, so
    nothing executable can ever reach the page.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    import re as _re
    m = _re.search(r"init['\"\s,]+(\d{6,})", raw)
    if m:
        return m.group(1)
    runs = _re.findall(r"\d{6,}", raw)
    return max(runs, key=len) if runs else ""


def _meta_pixel_snippet() -> str:
    """Meta Pixel base code — only emitted when META_PIXEL_ID is configured."""
    safe = _extract_pixel_id(get_settings().meta_pixel_id)
    if not safe:
        return ""
    # Consent-gated. The tracker is DEFINED here but nothing loads and no
    # cookie is set until SF_loadPixel() is called, which only happens after
    # the visitor accepts. Loading first and asking later is the thing that
    # actually gets sites fined under GDPR/ePrivacy.
    return (
        "<script>window.SF_PIXEL_ID='%s';"
        "window.SF_loadPixel=function(){"
        "if(window.__sfPixelLoaded)return;window.__sfPixelLoaded=true;"
        "!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?"
        "n.callMethod.apply(n,arguments):n.queue.push(arguments)};if(!f._fbq)f._fbq=n;"
        "n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;"
        "t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}"
        "(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');"
        "fbq('init',window.SF_PIXEL_ID);fbq('track','PageView');};"
        "try{if(localStorage.getItem('sf_consent')==='accepted')window.SF_loadPixel();}catch(e){}"
        "</script>"
    ) % (safe,)


def _ga_snippet() -> str:
    """Google Analytics 4 — consent-gated, same rule as the Pixel.

    Nothing loads and no cookie is set until SF_loadGA() is called by the
    consent banner. Only the measurement ID is emitted, sanitised to the
    G-XXXX shape so nothing executable can reach the page.
    """
    import re as _re
    raw = (get_settings().ga_measurement_id or "").strip()
    m = _re.search(r"G-[A-Z0-9]{4,20}", raw.upper())
    if not m:
        return ""
    gid = m.group(0)
    return (
        "<script>window.SF_GA_ID='%s';"
        "window.SF_loadGA=function(){"
        "if(window.__sfGaLoaded)return;window.__sfGaLoaded=true;"
        "var s=document.createElement('script');s.async=true;"
        "s.src='https://www.googletagmanager.com/gtag/js?id='+window.SF_GA_ID;"
        "document.head.appendChild(s);"
        "window.dataLayer=window.dataLayer||[];"
        "window.gtag=function(){dataLayer.push(arguments)};"
        "gtag('js',new Date());"
        "gtag('config',window.SF_GA_ID,{anonymize_ip:true});};"
        "try{if(localStorage.getItem('sf_consent')==='accepted')window.SF_loadGA();}catch(e){}"
        "</script>"
    ) % (gid,)


def _feature_flags_snippet() -> str:
    """Tell the page which optional features are actually switched on.

    The landing page advertises plan features before anyone logs in, so it can't
    ask /me/usage. Without this it promised "10 AI videos / month" while
    VIDEO_PROVIDER was unset and every generation returned a placeholder.
    """
    from .pipeline import video

    return (
        "<script>window.SF_FLAGS={videoLive:%s};</script>"
        % ("true" if video.is_live() else "false")
    )


def _html(name: str):
    """Serve an HTML page, injecting the Meta Pixel when one is configured."""
    path = os.path.join(_WEB_DIR, name)
    snippet = _meta_pixel_snippet() + _ga_snippet() + _feature_flags_snippet()
    if not snippet:
        return FileResponse(path, headers=_NO_CACHE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return FileResponse(path, headers=_NO_CACHE)
    if "<!--META_PIXEL-->" in html:
        html = html.replace("<!--META_PIXEL-->", snippet, 1)
    else:  # fall back to just before </head>
        html = html.replace("</head>", snippet + "</head>", 1)
    return HTMLResponse(content=html, headers=_NO_CACHE)


if os.path.isdir(_WEB_DIR):
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="web")

    @app.get("/", include_in_schema=False)
    def _root():
        return _html("index.html")

    @app.get("/privacy", include_in_schema=False)
    def _privacy():
        return _html("privacy.html")

    @app.get("/terms", include_in_schema=False)
    def _terms():
        return _html("terms.html")

    @app.get("/og.png", include_in_schema=False)
    def _og():
        return FileResponse(os.path.join(_WEB_DIR, "og.png"))

    @app.get("/robots.txt", include_in_schema=False)
    def _robots():
        return FileResponse(os.path.join(_WEB_DIR, "robots.txt"),
                            media_type="text/plain")

    @app.get("/sitemap.xml", include_in_schema=False)
    def _sitemap():
        return FileResponse(os.path.join(_WEB_DIR, "sitemap.xml"),
                            media_type="application/xml")

    @app.exception_handler(404)
    async def _not_found(request, exc):
        """Branded 404 for pages; JSON for the API.

        An API client that hits a bad path should get JSON, not a page of
        HTML it can't parse — so only browser-facing paths get the page.
        """
        path = request.url.path
        if path.startswith(("/auth", "/me", "/math", "/tutor", "/uploads",
                            "/study-sets", "/billing", "/shared", "/voice",
                            "/jobs", "/health", "/docs", "/openapi.json")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        try:
            with open(os.path.join(_WEB_DIR, "404.html"), "r", encoding="utf-8") as f:
                return HTMLResponse(f.read(), status_code=404, headers=_NO_CACHE)
        except OSError:
            return JSONResponse({"detail": "Not found"}, status_code=404)
