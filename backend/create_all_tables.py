#!/usr/bin/env python3
"""Create all database tables"""

import sys
sys.path.insert(0, '.')

# Load backend/.env before resolving DATABASE_URL/etc — see main.py's
# identical load_dotenv() call for why this is needed (copying
# .env.example to .env previously did nothing on its own for the backend).
from dotenv import load_dotenv
load_dotenv()

from database.database import engine
from database.models import Base
from auth.models import User
# ConferenceRoom (referenced by RecordingSession.room_id's FK) lives in a
# separate module create_all_tables.py never imported, so its table was
# missing from SQLAlchemy's metadata graph and create_all() failed with
# NoReferencedTableError on any genuinely empty database (pre-existing bug,
# found while verifying migrations 060-064 against real Postgres).
import database.models_rooms  # noqa: F401
# MeetingAgent/AgentTemplate/AgentSession (backs api/agent_management*.py)
# use the SAME Base as database.models but live in a separate module
# create_all_tables.py never imported — the same class of gap as
# models_rooms above, and the reported root cause of "meeting_agents table
# missing -> agent_management router skipped" during local startup audits.
#
# NOTE: models/vocabulary.py, database/vocabulary_models.py,
# models/agent_config.py, and models/model_config.py are NOT imported here
# even though they also define SQLAlchemy models. They were investigated
# and left alone deliberately: they use inconsistent Base sources across
# files (database.models.declarative_base() vs database.database.Base vs a
# try/except backend.database-or-database import), and
# database/vocabulary_models.py defines a VocabularySet class that
# duplicates models/vocabulary.py's VocabularySet (the one actually used by
# api/vocabulary.py) — importing both would risk a
# "Table already defined" metadata conflict at import time. This is
# pre-existing technical debt outside the scope of the specific missing-
# table failure being fixed here; resolving it safely needs its own
# dedicated audit, not a speculative import added while chasing a
# different bug.
import models.agent_system  # noqa: F401
from sqlalchemy import inspect

# Check current tables
inspector = inspect(engine)
tables_before = inspector.get_table_names()
print(f"Tables before: {tables_before}")

# Create all tables
print("\nCreating all tables...")
Base.metadata.create_all(bind=engine)

# Verify creation
inspector = inspect(engine)
tables_after = inspector.get_table_names()
print(f"\nTables after: {tables_after}")

# Show new tables
new_tables = set(tables_after) - set(tables_before)
if new_tables:
    print(f"\n✅ Created {len(new_tables)} new tables:")
    for table in sorted(new_tables):
        print(f"  - {table}")
else:
    print("\n✅ All tables already exist")