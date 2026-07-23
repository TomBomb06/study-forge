import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .config import get_settings
from .db import Base, engine
from .routers import auth, billing, gamify, shares, study_sets, tutor, uploads

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
        },
        "users": {
            "plan": "VARCHAR(20) DEFAULT 'free'",
            "usage_period": "VARCHAR(7) DEFAULT ''",
            "videos_used": "INTEGER DEFAULT 0",
            "extra_video_credits": "INTEGER DEFAULT 0",
            "stripe_customer_id": "VARCHAR(64)",
            "display_name": "VARCHAR(40)",
            "game": "JSON",
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

app = FastAPI(
    title="StudyForge API",
    version="0.1.0",
    description="Upload study material, get back a generated study kit.",
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

app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(study_sets.router)
app.include_router(billing.router)
app.include_router(shares.router)
app.include_router(gamify.router)
app.include_router(tutor.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# Serve the web app. Any non-API path falls through to index.html so the
# single-page app handles its own routing.
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="web")

    @app.get("/", include_in_schema=False)
    def _root():
        return FileResponse(os.path.join(_WEB_DIR, "index.html"))

    @app.get("/privacy", include_in_schema=False)
    def _privacy():
        return FileResponse(os.path.join(_WEB_DIR, "privacy.html"))

    @app.get("/terms", include_in_schema=False)
    def _terms():
        return FileResponse(os.path.join(_WEB_DIR, "terms.html"))

    @app.get("/og.png", include_in_schema=False)
    def _og():
        return FileResponse(os.path.join(_WEB_DIR, "og.png"))
