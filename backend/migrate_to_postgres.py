#!/usr/bin/env python3
"""
Migration script from SQLite to PostgreSQL for Meeting-Ops
"""

import os
import sys
import logging
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import psycopg2
from psycopg2 import sql

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Import existing models
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from database import Base, RecordingSession, TranscriptionSegment, Speaker, AudioFile, Settings, User


class DatabaseMigration:
    def __init__(self, sqlite_path: str, postgres_url: str):
        self.sqlite_path = sqlite_path
        self.postgres_url = postgres_url
        
        # Create engines
        self.sqlite_engine = create_engine(f'sqlite:///{sqlite_path}')
        self.postgres_engine = create_engine(postgres_url)
        
        # Create sessions
        SqliteSession = sessionmaker(bind=self.sqlite_engine)
        PostgresSession = sessionmaker(bind=self.postgres_engine)
        
        self.sqlite_session = SqliteSession()
        self.postgres_session = PostgresSession()
        
    def create_postgres_schema(self):
        """Create all tables in PostgreSQL"""
        logger.info("Creating PostgreSQL schema...")
        
        # Drop all tables if they exist (for clean migration)
        Base.metadata.drop_all(self.postgres_engine)
        
        # Create all tables
        Base.metadata.create_all(self.postgres_engine)
        
        # Add PostgreSQL-specific indexes for better performance
        with self.postgres_engine.connect() as conn:
            # Full-text search index on transcriptions
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_transcription_text_fts 
                ON transcription_segments 
                USING gin(to_tsvector('english', text))
            """))
            
            # Index for session queries
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_sessions_created_at 
                ON recording_sessions(created_at DESC)
            """))
            
            # Index for speaker identification
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_speakers_name 
                ON speakers(name)
            """))
            
            conn.commit()
            
        logger.info("PostgreSQL schema created successfully")
    
    def migrate_table(self, model_class, table_name: str):
        """Migrate a single table from SQLite to PostgreSQL"""
        logger.info(f"Migrating {table_name}...")
        
        try:
            # Get all records from SQLite
            records = self.sqlite_session.query(model_class).all()
            logger.info(f"Found {len(records)} records in {table_name}")
            
            if not records:
                return
            
            # Batch insert into PostgreSQL
            batch_size = 100
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                
                for record in batch:
                    # Create new instance with all attributes
                    new_record = model_class()
                    
                    # Copy all attributes
                    for attr in dir(record):
                        if not attr.startswith('_') and hasattr(new_record, attr):
                            try:
                                value = getattr(record, attr)
                                if not callable(value):
                                    setattr(new_record, attr, value)
                            except Exception as e:
                                logger.warning(f"Skipping attribute {attr}: {e}")
                    
                    self.postgres_session.add(new_record)
                
                # Commit batch
                self.postgres_session.commit()
                logger.info(f"Migrated {min(i + batch_size, len(records))}/{len(records)} records")
                
        except Exception as e:
            logger.error(f"Error migrating {table_name}: {e}")
            self.postgres_session.rollback()
            raise
    
    def migrate_all(self):
        """Migrate all tables in the correct order"""
        logger.info("Starting database migration from SQLite to PostgreSQL...")
        
        # Create schema
        self.create_postgres_schema()
        
        # Migrate tables in order (respecting foreign key constraints)
        tables_to_migrate = [
            (User, 'users'),
            (RecordingSession, 'recording_sessions'),
            (Speaker, 'speakers'),
            (TranscriptionSegment, 'transcription_segments'),
            (AudioFile, 'audio_files'),
            (Settings, 'settings'),
        ]
        
        for model_class, table_name in tables_to_migrate:
            self.migrate_table(model_class, table_name)
        
        # Update sequences for PostgreSQL
        self.update_sequences()
        
        logger.info("Migration completed successfully!")
        
    def update_sequences(self):
        """Update PostgreSQL sequences to match the migrated data"""
        logger.info("Updating PostgreSQL sequences...")
        
        with self.postgres_engine.connect() as conn:
            # Get all tables with serial columns
            tables = [
                'users',
                'recording_sessions', 
                'speakers',
                'transcription_segments',
                'audio_files',
                'settings'
            ]
            
            for table in tables:
                # Get the maximum ID
                result = conn.execute(text(f"SELECT MAX(id) FROM {table}"))
                max_id = result.scalar() or 0
                
                if max_id > 0:
                    # Update the sequence
                    sequence_name = f"{table}_id_seq"
                    conn.execute(text(f"SELECT setval('{sequence_name}', {max_id})"))
                    
            conn.commit()
            
        logger.info("Sequences updated successfully")
    
    def verify_migration(self):
        """Verify that all data was migrated correctly"""
        logger.info("Verifying migration...")
        
        tables = [
            ('users', User),
            ('recording_sessions', RecordingSession),
            ('speakers', Speaker),
            ('transcription_segments', TranscriptionSegment),
            ('audio_files', AudioFile),
            ('settings', Settings),
        ]
        
        all_good = True
        for table_name, model_class in tables:
            sqlite_count = self.sqlite_session.query(model_class).count()
            postgres_count = self.postgres_session.query(model_class).count()
            
            if sqlite_count == postgres_count:
                logger.info(f"✅ {table_name}: {postgres_count} records")
            else:
                logger.error(f"❌ {table_name}: SQLite={sqlite_count}, PostgreSQL={postgres_count}")
                all_good = False
        
        return all_good
    
    def close(self):
        """Close database connections"""
        self.sqlite_session.close()
        self.postgres_session.close()


def main():
    # Configuration
    sqlite_path = os.getenv('SQLITE_PATH', './meeting_sessions.db')
    postgres_url = os.getenv('DATABASE_URL', 'postgresql://meeting_ops:unicorn2025@localhost:5432/meeting_ops')
    
    if not os.path.exists(sqlite_path):
        logger.error(f"SQLite database not found at {sqlite_path}")
        sys.exit(1)
    
    # Create migration instance
    migration = DatabaseMigration(sqlite_path, postgres_url)
    
    try:
        # Run migration
        migration.migrate_all()
        
        # Verify
        if migration.verify_migration():
            logger.info("✅ Migration completed and verified successfully!")
            logger.info("You can now update your DATABASE_URL to use PostgreSQL")
        else:
            logger.error("❌ Migration verification failed!")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        migration.close()


if __name__ == "__main__":
    main()