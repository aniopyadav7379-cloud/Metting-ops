#!/usr/bin/env python3
"""
Run database migrations for Meeting-Ops
"""

import os
import sys
from sqlalchemy import create_engine, text
import uuid
from datetime import datetime

# Database connection
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://meetingops:meetingops123@localhost:5432/meeting_sessions")

def run_vocabulary_migration():
    """Run the custom vocabulary migration"""
    print("📊 Running custom vocabulary migration...")
    
    try:
        engine = create_engine(DATABASE_URL)
        with engine.connect() as conn:
            trans = conn.begin()
            
            try:
                # Check if tables already exist
                result = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'custom_vocabulary'
                    );
                """))
                
                if result.fetchone()[0]:
                    print("✅ Vocabulary tables already exist")
                    return
                
                print("Creating custom vocabulary tables...")
                
                # Create custom_vocabulary table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS custom_vocabulary (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        term VARCHAR(100) NOT NULL,
                        expansion VARCHAR(500) NOT NULL,
                        category VARCHAR(50),
                        industry VARCHAR(50),
                        priority INTEGER DEFAULT 0,
                        context_hints TEXT[],
                        case_sensitive BOOLEAN DEFAULT FALSE,
                        regex_pattern VARCHAR(200),
                        is_active BOOLEAN DEFAULT TRUE,
                        usage_count INTEGER DEFAULT 0,
                        created_by UUID,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(term, category)
                    );
                """))
                
                # Create vocabulary_sets table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS vocabulary_sets (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        name VARCHAR(100) NOT NULL UNIQUE,
                        description TEXT,
                        is_active BOOLEAN DEFAULT TRUE,
                        is_default BOOLEAN DEFAULT FALSE,
                        industry VARCHAR(50),
                        category VARCHAR(50),
                        vocab_ids UUID[],
                        settings JSONB,
                        created_by UUID,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                
                # Create session_vocabulary table
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS session_vocabulary (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        session_id UUID NOT NULL,
                        vocabulary_set_id UUID NOT NULL REFERENCES vocabulary_sets(id) ON DELETE CASCADE,
                        applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        applied_to_live BOOLEAN DEFAULT FALSE,
                        applied_to_final BOOLEAN DEFAULT FALSE
                    );
                """))
                
                # Create indexes
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vocabulary_term ON custom_vocabulary(term);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vocabulary_category ON custom_vocabulary(category);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vocabulary_active ON custom_vocabulary(is_active);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vocabulary_sets_active ON vocabulary_sets(is_active);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_session_vocabulary_session ON session_vocabulary(session_id);"))
                
                print("✅ Tables created successfully")
                
                # Insert default vocabulary set for defense
                result = conn.execute(text("""
                    INSERT INTO vocabulary_sets (name, description, industry, category, is_active, is_default)
                    VALUES ('Defense Acronyms', 'Common defense and military acronyms', 'defense', 'military', true, true)
                    ON CONFLICT (name) DO NOTHING
                    RETURNING id;
                """))
                
                row = result.fetchone()
                if row:
                    set_id = row[0]
                    print(f"✅ Created default vocabulary set: {set_id}")
                    
                    # Insert defense acronyms
                    defense_terms = [
                        ('DOD', 'Department of Defense', 'military', 'defense', 100, ['military', 'pentagon', 'defense']),
                        ('DARPA', 'Defense Advanced Research Projects Agency', 'military', 'defense', 100, ['research', 'defense', 'agency']),
                        ('NATO', 'North Atlantic Treaty Organization', 'military', 'defense', 100, ['alliance', 'military', 'treaty']),
                        ('ROE', 'Rules of Engagement', 'military', 'defense', 90, ['combat', 'military', 'rules']),
                        ('IED', 'Improvised Explosive Device', 'military', 'defense', 90, ['explosive', 'bomb', 'device']),
                        ('UAV', 'Unmanned Aerial Vehicle', 'military', 'defense', 90, ['drone', 'aircraft', 'unmanned']),
                        ('C2', 'Command and Control', 'military', 'defense', 85, ['command', 'control', 'military']),
                        ('ISR', 'Intelligence, Surveillance, and Reconnaissance', 'military', 'defense', 85, ['intelligence', 'surveillance', 'recon']),
                        ('OPSEC', 'Operations Security', 'military', 'defense', 85, ['operations', 'security', 'military']),
                        ('SITREP', 'Situation Report', 'military', 'defense', 80, ['situation', 'report', 'status']),
                        ('EOD', 'Explosive Ordnance Disposal', 'military', 'defense', 80, ['explosive', 'disposal', 'bomb']),
                        ('QRF', 'Quick Reaction Force', 'military', 'defense', 80, ['quick', 'reaction', 'force']),
                        ('AOR', 'Area of Responsibility', 'military', 'defense', 75, ['area', 'responsibility', 'region']),
                        ('FOB', 'Forward Operating Base', 'military', 'defense', 75, ['forward', 'base', 'operating']),
                        ('MEDEVAC', 'Medical Evacuation', 'military', 'defense', 75, ['medical', 'evacuation', 'casualty']),
                    ]
                    
                    vocab_ids = []
                    for term, expansion, category, industry, priority, hints in defense_terms:
                        result = conn.execute(text("""
                            INSERT INTO custom_vocabulary 
                            (term, expansion, category, industry, priority, context_hints)
                            VALUES (:term, :expansion, :category, :industry, :priority, :hints)
                            ON CONFLICT (term, category) DO UPDATE
                            SET expansion = EXCLUDED.expansion,
                                priority = EXCLUDED.priority
                            RETURNING id;
                        """), {
                            'term': term,
                            'expansion': expansion,
                            'category': category,
                            'industry': industry,
                            'priority': priority,
                            'hints': hints
                        })
                        
                        vocab_id = result.fetchone()[0]
                        vocab_ids.append(str(vocab_id))
                    
                    # Update vocabulary set with term IDs
                    conn.execute(text("""
                        UPDATE vocabulary_sets 
                        SET vocab_ids = :vocab_ids
                        WHERE id = :set_id;
                    """), {
                        'vocab_ids': vocab_ids,
                        'set_id': set_id
                    })
                    
                    print(f"✅ Inserted {len(defense_terms)} defense acronyms")
                
                trans.commit()
                print("✅ Migration completed successfully!")
                
            except Exception as e:
                trans.rollback()
                raise e
                
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        raise

if __name__ == "__main__":
    run_vocabulary_migration()