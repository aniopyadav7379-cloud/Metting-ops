#!/usr/bin/env python3
"""
Reset agent tables - drop and recreate
"""
import sys
sys.path.insert(0, '/srv/meeting-ops/backend')

from database.database import engine
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Drop tables
with engine.connect() as conn:
    conn.execute(text("DROP TABLE IF EXISTS agent_sessions CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS agent_templates CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS meeting_agents CASCADE"))
    conn.commit()
    logger.info("✅ Dropped existing agent tables")

# Now recreate
from create_agent_tables import main
main()