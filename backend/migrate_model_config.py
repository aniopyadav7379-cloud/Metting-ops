#!/usr/bin/env python3
"""
Migration script to rename model_config column to model_settings in unified_meeting_agents table
"""
import logging
from sqlalchemy import text
from database.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate_model_config_column():
    """Rename model_config column to model_settings if it exists"""
    try:
        with engine.connect() as conn:
            # Check if model_config column exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'unified_meeting_agents' 
                AND column_name IN ('model_config', 'model_settings')
            """))
            
            columns = [row[0] for row in result]
            
            if 'model_config' in columns and 'model_settings' not in columns:
                # Rename the column
                logger.info("Renaming model_config to model_settings...")
                conn.execute(text("""
                    ALTER TABLE unified_meeting_agents 
                    RENAME COLUMN model_config TO model_settings
                """))
                conn.commit()
                logger.info("✅ Column renamed successfully")
            elif 'model_settings' in columns:
                logger.info("✅ model_settings column already exists")
            elif 'model_config' in columns and 'model_settings' in columns:
                # Both exist, copy data and drop old column
                logger.info("Both columns exist, migrating data...")
                conn.execute(text("""
                    UPDATE unified_meeting_agents 
                    SET model_settings = model_config 
                    WHERE model_settings IS NULL
                """))
                conn.execute(text("""
                    ALTER TABLE unified_meeting_agents 
                    DROP COLUMN model_config
                """))
                conn.commit()
                logger.info("✅ Data migrated and old column dropped")
            else:
                logger.info("Neither column exists, table might not be initialized yet")
                
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False
    
    return True

if __name__ == "__main__":
    if migrate_model_config_column():
        logger.info("✅ Migration completed successfully")
    else:
        logger.error("❌ Migration failed")