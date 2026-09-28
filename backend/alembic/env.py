from __future__ import annotations

from logging.config import fileConfig
import os

# Load backend/.env before DATABASE_URL is resolved below — see main.py's
# identical load_dotenv() call for why this is needed.
from dotenv import load_dotenv
load_dotenv()

from alembic import context
from sqlalchemy import engine_from_config, pool

from database.database import Base
from database.models import (  # noqa: F401
    RecordingSession,
    AudioFile,
    ChatHistory,
    SatelliteDevice,
    SessionCollaborator,
    ActionItem,
    BulkImportJob,
    BulkImportFile,
)
from database.models_rooms import (  # noqa: F401
    ConferenceRoom,
    RoomAudioSource,
    RoomPairingCode,
    RoomAcl,
)
from auth.models import Organization, User, UserOrganization, AuditLog  # noqa: F401
from models.vocabulary import CustomVocabulary, VocabularySet, SessionVocabulary  # noqa: F401
# Agent system tables are live (routers wired in main.py) but their model
# classes weren't reaching target_metadata, so autogenerate kept flagging
# them as "drop_table" drift. Importing here pulls them into Base.metadata
# without changing any runtime behaviour.
from models.agent_system import MeetingAgent, AgentTemplate, AgentSession  # noqa: F401
from models.unified_agent import UnifiedMeetingAgent  # noqa: F401

config = context.config

database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
