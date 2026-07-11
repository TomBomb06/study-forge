from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

settings = get_settings()


def _normalize_db_url(url: str) -> str:
    """Cloud hosts often hand out 'postgres://...' — SQLAlchemy wants an
    explicit driver. Point it at psycopg (v3)."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


_db_url = _normalize_db_url(settings.database_url)

connect_args = {}
engine_kwargs = {}
if _db_url.startswith("sqlite"):
    # Needed because background jobs touch the DB from worker threads.
    connect_args = {"check_same_thread": False}
else:
    # Postgres: recycle connections and check liveness (hosts drop idle ones).
    engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 300}

engine = create_engine(_db_url, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
