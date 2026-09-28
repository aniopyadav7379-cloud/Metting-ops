#!/usr/bin/env python3
"""
Setup admin user for Meeting-Ops in PostgreSQL
"""

import psycopg2
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
import os

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def setup_database():
    """Create users table and admin user in PostgreSQL"""
    
    # Get connection parameters from environment
    database_url = os.getenv("DATABASE_URL", "postgresql://meetingops:meetingops123@localhost:5432/meeting_sessions")
    
    # Parse the DATABASE_URL
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "")
    
    auth, host_db = database_url.split("@")
    user, password = auth.split(":")
    host_port, database = host_db.split("/")
    host, port = host_port.split(":") if ":" in host_port else (host_port, "5432")
    
    conn = psycopg2.connect(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        cursor_factory=RealDictCursor
    )
    
    cur = conn.cursor()
    
    # Create users table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            email VARCHAR(255),
            hashed_password TEXT NOT NULL,
            is_superuser BOOLEAN DEFAULT FALSE,
            full_name VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Check if admin user exists
    cur.execute("SELECT username FROM users WHERE username = %s", ("admin",))
    if not cur.fetchone():
        # Create admin user with password "admin123"
        hashed_password = pwd_context.hash("admin123")
        cur.execute("""
            INSERT INTO users (username, email, hashed_password, is_superuser, full_name)
            VALUES (%s, %s, %s, %s, %s)
        """, ("admin", "admin@meeting-ops.local", hashed_password, True, "Administrator"))
        print("✅ Admin user created: admin / admin123")
    else:
        # Update admin password to ensure it's correct
        hashed_password = pwd_context.hash("admin123")
        cur.execute("""
            UPDATE users 
            SET hashed_password = %s
            WHERE username = %s
        """, (hashed_password, "admin"))
        print("✅ Admin password reset to: admin123")
    
    # Also ensure user account exists
    cur.execute("SELECT username FROM users WHERE username = %s", ("user",))
    if not cur.fetchone():
        hashed_password = pwd_context.hash("user123")
        cur.execute("""
            INSERT INTO users (username, email, hashed_password, is_superuser, full_name)
            VALUES (%s, %s, %s, %s, %s)
        """, ("user", "user@meeting-ops.local", hashed_password, False, "Standard User"))
        print("✅ User account created: user / user123")
    
    conn.commit()
    
    # List all users
    cur.execute("SELECT username, email, is_superuser FROM users")
    users = cur.fetchall()
    print("\n📋 Current users in PostgreSQL database:")
    for user in users:
        role = "Admin" if user['is_superuser'] else "User"
        print(f"  - {user['username']} ({user['email']}) - {role}")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    print("🔧 Setting up Meeting-Ops PostgreSQL database and users...")
    setup_database()
    print("\n✅ PostgreSQL database setup complete!")
    print("\n🚀 You can now login with:")
    print("  Admin: admin / admin123")
    print("  User:  user / user123")