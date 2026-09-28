import os
import sys
import tempfile
import types

# Create temp SQLite DB and set env BEFORE pytest collects anything else.
_db_fd, _db_path = tempfile.mkstemp(prefix="meeting_ops_test_", suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

# security-2: mark this a relaxed env so auth.config's SECRET_KEY boot-guard
# allows the insecure dev fallback instead of refusing to boot. Real deployments
# leave ENVIRONMENT unset (=> strict) and must supply a strong SECRET_KEY.
os.environ.setdefault("ENVIRONMENT", "test")

# Point RECORDINGS_DIR to a writable temp dir so bulk-import integration
# tests can create fake audio files without hitting the Docker-only path.
_rec_dir = tempfile.mkdtemp(prefix="meeting_ops_recordings_")
os.environ["RECORDINGS_DIR"] = _rec_dir

# Never touch real Garage object storage during tests — force media_storage
# onto its local backend so persist/purge stay in the temp recordings dir and
# tests have no network dependency. (Production leaves this unset.)
os.environ["MEDIA_STORAGE_DISABLED"] = "1"

# Tier-A reprocess queue: keep the legacy in-process direct-call path in tests
# so the suite's monkeypatches of recording._run_session_reprocess still fire and
# no test touches Redis/Arq. Production flips this on (default true) to use the
# bounded fair queue + daily soft cap.
os.environ["REPROCESS_ARQ_ENABLED"] = "false"


# Ensure backend root is on sys.path
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

try:
    import prometheus_client  # noqa: F401
except Exception:  # pragma: no cover - local test env fallback
    fake_prom = types.ModuleType("prometheus_client")

    def make_asgi_app():
        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            })
            await send({"type": "http.response.body", "body": b""})

        return app

    fake_prom.make_asgi_app = make_asgi_app
    sys.modules["prometheus_client"] = fake_prom


# ---------------------------------------------------------------------------
# Make Postgres-specific column types (JSONB/ARRAY/UUID/CITEXT) work on the
# SQLite test fixture. database.models imports these directly, and SQLite's
# type compiler raises CompileError when it sees them. We register SQLite
# compilation handlers + JSON bind/result processors so the columns
# round-trip values cleanly. Production Postgres is untouched.
# ---------------------------------------------------------------------------
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):
    return "VARCHAR(36)"


@compiles(CITEXT, "sqlite")
def _citext_sqlite(element, compiler, **kw):
    return "VARCHAR"


@compiles(ARRAY, "sqlite")
def _array_sqlite(element, compiler, **kw):
    return "JSON"


# JSON bind/result processors for the columns that store lists/dicts. Without
# these, SQLAlchemy raises "unsupported type" at flush time because JSONB and
# ARRAY don't know how to serialize Python collections on SQLite.
_json_instance = JSON()


def _patch_json_processors(type_cls):
    """Override bind_processor / result_processor so SQLite uses JSON's
    serialization. Idempotent — leaves Postgres path alone."""
    if getattr(type_cls, "_uc_test_patched", False):
        return
    original_bind = type_cls.bind_processor
    original_result = type_cls.result_processor

    def bind_processor(self, dialect):
        if dialect.name == "sqlite":
            return _json_instance.bind_processor(dialect)
        return original_bind(self, dialect)

    def result_processor(self, dialect, coltype):
        if dialect.name == "sqlite":
            return _json_instance.result_processor(dialect, coltype)
        return original_result(self, dialect, coltype)

    type_cls.bind_processor = bind_processor
    type_cls.result_processor = result_processor
    type_cls._uc_test_patched = True


_patch_json_processors(JSONB)
_patch_json_processors(ARRAY)


import pytest


@pytest.fixture
def canonical_db_schema():
    """Prepare canonical ORM tables for focused service tests without FastAPI."""
    from auth.models import Organization
    from database.database import SessionLocal, engine
    from database.models import Base
    from database.models_rooms import (  # noqa: F401 - registers FK targets
        ConferenceRoom,
        RoomAcl,
        RoomAudioSource,
        RoomPairingCode,
    )

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "summary-test-org").first()
        if not org:
            org = Organization(
                name="Summary Test Org",
                slug="summary-test-org",
                is_active=True,
            )
            db.add(org)
            db.commit()
            db.refresh(org)
        yield org.id
    finally:
        db.close()


def pytest_configure(config):
    """Ensure DATABASE_URL points to test SQLite before any imports."""
    os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"


@pytest.fixture(scope="session")
def app():
    """Create a test FastAPI app with an isolated SQLite database."""
    # Force reload database module to pick up SQLite URL
    import importlib

    # Reload database.models first (owns Base), then database.database (imports Base).
    # Any model modules that previously imported Base must be reloaded afterwards so
    # their tables are attached to the same metadata for this isolated SQLite DB.
    if "database.models" in sys.modules:
        importlib.reload(sys.modules["database.models"])
    if "database.database" in sys.modules:
        importlib.reload(sys.modules["database.database"])
    if "auth.models" in sys.modules:
        importlib.reload(sys.modules["auth.models"])
    # Conference Room models depend on database.models (for Base) AND on
    # auth.models being importable. Reload AFTER both so the FK targets
    # resolve to the freshly-reloaded tables on Base.metadata.
    if "database.models_rooms" in sys.modules:
        importlib.reload(sys.modules["database.models_rooms"])
    # Bulk-import code paths cache model references at import time
    # (workers.bulk_import_worker, services.bulk_import_queue). When other
    # tests import them BEFORE the app fixture runs (e.g. test_arq_worker.py),
    # those cached references point at the original Base, which the
    # database.models reload has now replaced. Reload them too so their
    # model lookups resolve through the freshly-bound Base.metadata.
    for mod_name in ("services.bulk_import_queue", "workers.bulk_import_worker"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
    from database.database import engine, SessionLocal
    from database.models import Base

    # Import the auth models needed by these API tests before create_all so
    # their tables register on the same metadata object as the core DB models.
    from auth.models import (  # noqa: F401
        AuditLog,
        Organization,
        PasswordResetToken,
        PersonalAccessToken,
        User,
        UserOrganization,
    )
    # Same for the Conference Room models — without this the room tables
    # never get attached to Base.metadata under SQLite, and the
    # recording_sessions FK pointing at room_audio_sources fails to
    # resolve at create_all time.
    from database.models_rooms import (  # noqa: F401
        ConferenceRoom,
        RoomAudioSource,
        RoomPairingCode,
        RoomAcl,
    )

    # Create all tables in the test SQLite DB
    Base.metadata.create_all(bind=engine)

    # Seed admin user for auth tests
    from auth.utils import get_password_hash

    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            admin_user = User(
                email="admin@meeting-ops.local",
                username="admin",
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                is_superuser=True,
            )
            db.add(admin_user)
            db.commit()

        default_org = db.query(Organization).filter(Organization.slug == "magic-unicorn").first()
        if not default_org:
            default_org = Organization(
                name="Magic Unicorn",
                slug="magic-unicorn",
                is_active=True,
            )
            db.add(default_org)
            db.commit()

        if not db.query(UserOrganization).filter(
            UserOrganization.user_id == admin_user.id,
            UserOrganization.organization_id == default_org.id,
        ).first():
            db.add(UserOrganization(
                user_id=admin_user.id,
                organization_id=default_org.id,
                role="admin",
            ))
            db.commit()
    finally:
        db.close()

    from main import app as fastapi_app
    yield fastapi_app

    # Cleanup temp DB
    if os.path.exists(_db_path):
        os.remove(_db_path)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client
