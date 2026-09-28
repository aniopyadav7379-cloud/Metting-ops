#!/usr/bin/env python3
"""Create a test session for GUI testing"""

import sys
sys.path.insert(0, '.')

from database.database import engine, SessionLocal
from database.models import Base, Session
from datetime import datetime, timedelta
import uuid

# Create tables if needed
Base.metadata.create_all(bind=engine)

db = SessionLocal()

try:
    # Create a test session
    test_session = Session(
        session_id=str(uuid.uuid4()),
        name='Team Meeting - Q4 Planning',
        description='Quarterly planning meeting with product and engineering teams',
        start_time=datetime.utcnow() - timedelta(hours=2),
        end_time=datetime.utcnow() - timedelta(hours=1),
        duration_seconds=3600,
        status='completed',
        audio_file_path='/recordings/team-meeting-q4.wav',
        audio_format='wav',
        sample_rate=16000,
        channels=1,
        total_transcriptions=150,
        total_speakers=5,
        processing_completed=True,
        npu_accelerated=True,
        summary='Discussed Q4 product roadmap, engineering priorities, and resource allocation. Key decisions: 1) Focus on performance optimization, 2) Hire 2 additional engineers, 3) Launch beta program in November.',
        key_points=['Performance optimization is top priority', 'Need to hire 2 engineers', 'Beta launch in November'],
        action_items=['Create job postings by next week', 'Set up beta program infrastructure', 'Schedule follow-up meeting'],
        meeting_type='planning'
    )
    
    db.add(test_session)
    
    # Create another session that's currently recording
    active_session = Session(
        session_id=str(uuid.uuid4()),
        name='Daily Standup',
        description='Engineering team daily standup',
        start_time=datetime.utcnow() - timedelta(minutes=10),
        status='recording',
        audio_format='wav',
        sample_rate=16000,
        channels=1,
        total_transcriptions=20,
        total_speakers=8,
        processing_completed=False,
        npu_accelerated=True
    )
    
    db.add(active_session)
    
    # Create a session pending processing
    pending_session = Session(
        session_id=str(uuid.uuid4()),
        name='Customer Demo - Acme Corp',
        description='Product demo for potential enterprise customer',
        start_time=datetime.utcnow() - timedelta(days=1),
        end_time=datetime.utcnow() - timedelta(days=1, hours=-1),
        duration_seconds=3600,
        status='processing',
        audio_file_path='/recordings/customer-demo-acme.wav',
        audio_format='wav',
        sample_rate=16000,
        channels=1,
        processing_completed=False,
        npu_accelerated=True
    )
    
    db.add(pending_session)
    
    db.commit()
    print("Successfully created 3 test sessions!")
    
    # List all sessions
    sessions = db.query(Session).all()
    print(f"\nTotal sessions in database: {len(sessions)}")
    for s in sessions:
        print(f"- {s.name} ({s.status}) - ID: {s.session_id}")
        
except Exception as e:
    print(f"Error: {e}")
    db.rollback()
finally:
    db.close()