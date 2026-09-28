#!/usr/bin/env python3
"""Create audio_files table"""

import sys
sys.path.insert(0, '.')

from database.database import engine
from database.models import Base, AudioFile
from sqlalchemy import inspect

# Check current tables
inspector = inspect(engine)
tables_before = inspector.get_table_names()
print(f"Tables before: {tables_before}")

# Create audio_files table
print("\nCreating audio_files table...")
AudioFile.__table__.create(engine, checkfirst=True)

# Verify creation
inspector = inspect(engine)
tables_after = inspector.get_table_names()
print(f"\nTables after: {tables_after}")

if 'audio_files' in tables_after:
    print("\n✅ audio_files table created successfully!")
else:
    print("\n❌ Failed to create audio_files table")