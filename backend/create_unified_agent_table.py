#!/usr/bin/env python3
"""
Create unified agent table and default agent
"""
import sys
import os

# Add the backend directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.database import engine, get_db_session
from models.unified_agent import UnifiedMeetingAgent

def create_table_and_default_agent():
    """Create the unified agent table and default agent"""
    print("Creating unified agent table...")
    
    # Create the table
    UnifiedMeetingAgent.metadata.create_all(bind=engine)
    print("✅ Table created successfully")
    
    # Create default agent
    db_session = get_db_session()
    try:
        existing_agent = UnifiedMeetingAgent.get_default_agent(db_session)
        if existing_agent:
            print(f"✅ Default agent already exists: {existing_agent.name}")
        else:
            default_agent = UnifiedMeetingAgent.create_default_agent(db_session)
            print(f"✅ Created default agent: {default_agent.name}")
            print(f"   Model: {default_agent.model_name}")
            print(f"   Provider: {default_agent.provider_type}")
    finally:
        db_session.close()

if __name__ == "__main__":
    create_table_and_default_agent()
    print("\n🎉 Unified agent system ready!")
    print("   - Start the backend: ./start-backend.sh")
    print("   - Access GUI: http://localhost:7778")
    print("   - Configure agent: Advanced Tools > Meeting Agent")