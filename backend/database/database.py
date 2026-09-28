"""Database connection and session management"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from .models import Base

# Get database URL from environment or use PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://meetingops:meetingops123@localhost:5434/meeting_sessions")

# Validate pooled connections before checkout and retire long-lived connections
# before common Postgres/pgbouncer idle limits. These options are safe for the
# SQLite test/dev pool as well.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    pool_pre_ping=True,
    pool_recycle=1800,
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_database():
    """Ensure model modules are imported so their tables register on the shared metadata.

    Schema creation is owned by alembic now — the container entrypoint runs
    `alembic upgrade head` before uvicorn starts. We previously called
    Base.metadata.create_all() here, but that races ahead of new alembic
    revisions: when a freshly-defined SQLAlchemy model lands before its
    alembic migration runs, create_all happily creates the table WITHOUT
    the FK / index / server-default additions that the alembic revision
    would apply, leaving the live schema permanently out of sync with
    target_metadata. The Conference Room Phase 1 deploy hit this with
    room tables auto-created without the recording_sessions.room_id FK.

    Bootstrap (empty database) is fine: alembic upgrade head against an
    empty DB creates every table from revision 001 forward.
    """
    # Import models so their tables are registered on the shared metadata.
    # Kept even though we no longer call create_all so anything that
    # reads Base.metadata at runtime (tests, introspection) still sees
    # the full schema.
    from auth import models as _auth_models  # noqa: F401
    from models import vocabulary as _vocabulary_models  # noqa: F401

def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_db_session():
    """Get database session (direct, not for dependency injection)"""
    return SessionLocal()
